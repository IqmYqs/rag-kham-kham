"""OpenAI-compatible LLM client with provider fallback (DESIGN.md section 8).

- Each role (chat / internal / vision) has an ordered provider list in config.yaml.
- Fall through to the next provider on 429, 5xx, timeout or connection error; a 4xx other
  than 429 is our bug and is raised immediately.
- A model that returns 429 is skipped for `COOLDOWN_SECONDS` (in-memory; a cold start
  forgets it, which is fine). Keyed by provider+model because Gemini quotas are per model.
- Streaming can fall back only before the first token. A failure mid-stream propagates.
- Embeddings have no fallback: vectors from different models are not comparable. Background
  jobs pass `max_wait_seconds` to wait out per-minute quotas (Gemini free tier counts every
  input text as one request, 100/min).
"""

import asyncio
import logging
import math
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

import openai
from openai import AsyncOpenAI

from app.core.config import AppConfig, ModelRef, Settings, get_app_config, get_settings
from app.core.errors import AppError, ErrorCode
from app.services.llm.pricing import estimate_cost

logger = logging.getLogger(__name__)

Role = Literal["chat", "internal", "vision"]
Message = dict[str, Any]
ClientFactory = Callable[[str, str, str], Any]  # (provider, base_url, api_key) -> AsyncOpenAI

COOLDOWN_SECONDS = 60.0
TIMEOUT_SECONDS = 60.0
EMBED_BATCH = 100


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal(0)


@dataclass
class ChatStream:
    """Async iterator of text deltas. Usage fields are filled once iteration finishes."""

    provider: str
    model: str
    _stream: Any
    _pricing: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal(0)
    _parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self._parts)

    async def __aiter__(self) -> AsyncIterator[str]:
        async for chunk in self._stream:
            if chunk.usage:
                self.input_tokens = chunk.usage.prompt_tokens or 0
                self.output_tokens = chunk.usage.completion_tokens or 0
            for choice in chunk.choices or []:
                delta = choice.delta.content if choice.delta else None
                if delta:
                    self._parts.append(delta)
                    yield delta
        self.cost = estimate_cost(self._pricing, self.model, self.input_tokens, self.output_tokens)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def _retry_after(exc: Exception, default: float = 30.0) -> float:
    """Seconds to wait, from the Retry-After header or Gemini's "retry in 41.9s" message."""
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    if header:
        try:
            return float(header) + 1
        except ValueError:
            pass
    match = re.search(r"retry in ([\d.]+)s", str(exc))
    return float(match.group(1)) + 1 if match else default


def _default_factory(provider: str, base_url: str, api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


class LLMClient:
    def __init__(
        self,
        config: AppConfig,
        settings: Settings,
        client_factory: ClientFactory = _default_factory,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._settings = settings
        self._factory = client_factory
        self._clock = clock
        self._sleep = sleep
        self._clients: dict[str, Any] = {}
        self._cooldown_until: dict[tuple[str, str], float] = {}

    # ---- provider plumbing -------------------------------------------------------------

    def _api_key(self, provider: str) -> str | None:
        cfg = self._config.providers.get(provider)
        if cfg is None:
            return None
        if cfg.api_key_env is None:  # e.g. local Ollama
            return "not-needed"
        env = cfg.api_key_env
        return getattr(self._settings, env.lower(), None) or os.environ.get(env) or None

    def _client(self, provider: str) -> Any:
        if provider not in self._clients:
            cfg = self._config.providers[provider]
            key = self._api_key(provider)
            if key is None:
                raise AppError(ErrorCode.LLM_UNAVAILABLE, f"ไม่มี API key ของ {provider}", 503)
            self._clients[provider] = self._factory(provider, cfg.base_url, key)
        return self._clients[provider]

    def _candidates(self, role: Role) -> list[ModelRef]:
        now = self._clock()
        refs = getattr(self._config.llm, role)
        usable = [
            r
            for r in refs
            if self._api_key(r.provider) is not None
            and self._cooldown_until.get((r.provider, r.model), 0) <= now
        ]
        if not usable:
            raise AppError(
                ErrorCode.LLM_UNAVAILABLE,
                "ไม่มีโมเดลที่ใช้ได้ในขณะนี้ กรุณาลองใหม่ภายหลัง",
                status_code=503,
                details={"role": role},
                headers={"Retry-After": str(int(COOLDOWN_SECONDS))},
            )
        return usable

    def _on_error(self, ref: ModelRef, exc: Exception) -> None:
        if isinstance(exc, openai.APIStatusError) and exc.status_code == 429:
            self._cooldown_until[(ref.provider, ref.model)] = self._clock() + COOLDOWN_SECONDS
        status = getattr(exc, "status_code", None)
        logger.warning(
            "LLM %s/%s failed: %s %s", ref.provider, ref.model, type(exc).__name__, status
        )

    def _all_failed(self, role: str, errors: list[str]) -> AppError:
        return AppError(
            ErrorCode.LLM_UNAVAILABLE,
            "ผู้ให้บริการ AI ไม่ตอบสนอง กรุณาลองใหม่ภายหลัง",
            status_code=503,
            details={"role": role, "tried": errors},
            headers={"Retry-After": str(int(COOLDOWN_SECONDS))},
        )

    def _chat_kwargs(self, max_tokens: int | None) -> dict[str, Any]:
        return {
            "max_tokens": max_tokens or self._config.llm.max_output_tokens,
            "timeout": self._config.llm.timeout_seconds,
        }

    # ---- public API --------------------------------------------------------------------

    async def chat(
        self,
        role: Role,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        errors: list[str] = []
        for ref in self._candidates(role):
            try:
                kwargs = self._chat_kwargs(max_tokens)
                if temperature is not None:
                    kwargs["temperature"] = temperature
                resp = await self._client(ref.provider).chat.completions.create(
                    model=ref.model, messages=messages, **kwargs
                )
            except Exception as exc:
                if not _is_retryable(exc):
                    raise
                self._on_error(ref, exc)
                errors.append(f"{ref.provider}:{type(exc).__name__}")
                continue
            usage = resp.usage
            inp = (usage.prompt_tokens or 0) if usage else 0
            out = (usage.completion_tokens or 0) if usage else 0
            return ChatResult(
                text=resp.choices[0].message.content or "",
                provider=ref.provider,
                model=ref.model,
                input_tokens=inp,
                output_tokens=out,
                cost=estimate_cost(self._config.llm.pricing, ref.model, inp, out),
            )
        raise self._all_failed(role, errors)

    async def stream_chat(
        self,
        role: Role,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatStream:
        """Open a stream, falling back between providers until one accepts the request."""
        errors: list[str] = []
        for ref in self._candidates(role):
            try:
                kwargs = self._chat_kwargs(max_tokens)
                if temperature is not None:
                    kwargs["temperature"] = temperature
                stream = await self._client(ref.provider).chat.completions.create(
                    model=ref.model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    **kwargs,
                )
            except Exception as exc:
                if not _is_retryable(exc):
                    raise
                self._on_error(ref, exc)
                errors.append(f"{ref.provider}:{type(exc).__name__}")
                continue
            return ChatStream(ref.provider, ref.model, stream, self._config.llm.pricing)
        raise self._all_failed(role, errors)

    @property
    def embedding_model(self) -> str:
        ref = self._config.llm.embedding
        if ref is None:
            raise AppError(ErrorCode.LLM_UNAVAILABLE, "ยังไม่ได้ตั้งค่าโมเดล embedding", 503)
        return ref.model

    async def embed(self, texts: list[str], *, max_wait_seconds: float = 0) -> list[list[float]]:
        """Embed texts in batches; vectors are L2-normalised (reduced-dimension Gemini
        output is not), so cosine == dot product downstream.

        On 429, wait as the provider suggests and retry, for up to `max_wait_seconds` in total
        (0 = fail fast, for user-facing requests)."""
        ref = self._config.llm.embedding
        if ref is None:
            raise AppError(ErrorCode.LLM_UNAVAILABLE, "ยังไม่ได้ตั้งค่าโมเดล embedding", 503)
        client = self._client(ref.provider)
        vectors: list[list[float]] = []
        waited = 0.0
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            while True:
                try:
                    resp = await client.embeddings.create(
                        model=ref.model, input=batch, dimensions=ref.dimensions
                    )
                    break
                except Exception as exc:
                    if not _is_retryable(exc):
                        raise
                    delay = _retry_after(exc)
                    if waited + delay > max_wait_seconds:
                        self._on_error(ref, exc)
                        tried = [f"{ref.provider}:{type(exc).__name__}"]
                        raise self._all_failed("embedding", tried) from exc
                    logger.info("Embedding throttled, retrying in %.0fs", delay)
                    await self._sleep(delay)
                    waited += delay
            data = list(resp.data)
            if len(data) != len(batch):
                raise AppError(ErrorCode.INTERNAL_ERROR, "จำนวน embedding ไม่ตรงกับข้อความ", 500)
            if all(d.index is not None for d in data):  # Gemini leaves index empty
                data.sort(key=lambda d: d.index)
            for item in data:
                vec = list(item.embedding)
                if len(vec) != ref.dimensions:
                    raise AppError(
                        ErrorCode.INTERNAL_ERROR,
                        f"embedding มี {len(vec)} มิติ แต่ตั้งไว้ {ref.dimensions}",
                        status_code=500,
                    )
                vectors.append(l2_normalize(vec))
        return vectors


@lru_cache
def get_llm() -> LLMClient:
    return LLMClient(get_app_config(), get_settings())
