"""LLMClient fallback / cooldown / embedding behaviour, with a fake OpenAI client (no network)."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx  # the HTTP library bundled with openai>=3
import openai
import pytest

from app.core.config import AppConfig, Settings
from app.core.errors import AppError
from app.services.llm.client import LLMClient, _retry_after, l2_normalize
from app.services.llm.pricing import estimate_cost

CONFIG = AppConfig.model_validate(
    {
        "llm": {
            "chat": [
                {"provider": "gemini", "model": "flash"},
                {"provider": "gemini", "model": "flash-lite"},
                {"provider": "groq", "model": "llama"},
            ],
            "embedding": {"provider": "gemini", "model": "emb", "dimensions": 3},
            "pricing": {"llama": {"input_per_m": 1.0, "output_per_m": 2.0}},
        },
        "providers": {
            "gemini": {"base_url": "http://gemini", "api_key_env": "GEMINI_API_KEY"},
            "groq": {"base_url": "http://groq", "api_key_env": "GROQ_API_KEY"},
        },
    }
)


def status_error(code: int, message: str = "err", headers: dict | None = None) -> Exception:
    response = httpx.Response(
        code, headers=headers or {}, request=httpx.Request("POST", "http://x")
    )
    cls = openai.RateLimitError if code == 429 else openai.APIStatusError
    if code >= 500:
        cls = openai.InternalServerError
    return cls(message, response=response, body=None)


def completion(text: str, inp: int = 10, out: int = 5) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=inp, completion_tokens=out),
    )


class FakeProvider:
    """Plays back scripted outcomes per model: an Exception is raised, anything else returned."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.embeddings = SimpleNamespace(create=self._embed)

    async def _next(self, model: str) -> Any:
        self.calls.append(model)
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _chat(self, *, model: str, **_: Any) -> Any:
        return await self._next(model)

    async def _embed(self, *, model: str, input: list[str], **_: Any) -> Any:
        return await self._next(model)


def make_client(
    script: dict[str, list[Any]], groq_key: str | None = "g"
) -> tuple[LLMClient, FakeProvider, list[float]]:
    fake = FakeProvider(script)
    slept: list[float] = []
    now = [1000.0]

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    client = LLMClient(
        CONFIG,
        Settings(_env_file=None, gemini_api_key="k", groq_api_key=groq_key),
        client_factory=lambda provider, base_url, key: fake,
        clock=lambda: now[0],
        sleep=sleep,
    )
    client._now = now  # type: ignore[attr-defined]  # let tests move the clock
    return client, fake, slept


async def test_chat_uses_first_provider_and_counts_tokens() -> None:
    client, fake, _ = make_client({"flash": [completion("ฮา", 12, 3)]})
    res = await client.chat("chat", [{"role": "user", "content": "x"}])
    assert (res.text, res.model, res.input_tokens, res.output_tokens) == ("ฮา", "flash", 12, 3)
    assert res.cost == 0
    assert fake.calls == ["flash"]


async def test_chat_falls_back_on_503_and_429_then_prices_paid_model() -> None:
    client, fake, _ = make_client(
        {
            "flash": [status_error(503)],
            "flash-lite": [status_error(429)],
            "llama": [completion("ok", 1000, 500)],
        }
    )
    res = await client.chat("chat", [])
    assert res.model == "llama"
    assert fake.calls == ["flash", "flash-lite", "llama"]
    assert res.cost == Decimal("0.002000")


async def test_chat_does_not_fall_back_on_400() -> None:
    client, fake, _ = make_client({"flash": [status_error(400)]})
    with pytest.raises(openai.APIStatusError):
        await client.chat("chat", [])
    assert fake.calls == ["flash"]


async def test_chat_timeout_falls_back() -> None:
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "http://x"))
    client, fake, _ = make_client({"flash": [timeout], "flash-lite": [completion("ok")]})
    assert (await client.chat("chat", [])).model == "flash-lite"


async def test_429_cools_down_only_that_model_for_60s() -> None:
    client, fake, _ = make_client(
        {
            "flash": [status_error(429), completion("back")],
            "flash-lite": [completion("a"), completion("b")],
        }
    )
    await client.chat("chat", [])
    await client.chat("chat", [])  # flash still cooling down -> straight to flash-lite
    assert fake.calls == ["flash", "flash-lite", "flash-lite"]
    client._now[0] += 61  # type: ignore[attr-defined]
    assert (await client.chat("chat", [])).text == "back"


async def test_all_providers_failing_raises_503_with_retry_after() -> None:
    client, _, _ = make_client(
        {"flash": [status_error(500)], "flash-lite": [status_error(502)]}, groq_key=None
    )
    with pytest.raises(AppError) as info:
        await client.chat("chat", [])
    assert info.value.status_code == 503
    assert info.value.headers == {"Retry-After": "60"}
    assert info.value.details["tried"] == ["gemini:InternalServerError"] * 2


def embedding_response(vectors: list[list[float]], with_index: bool = False) -> Any:
    return SimpleNamespace(
        data=[
            SimpleNamespace(embedding=v, index=i if with_index else None)
            for i, v in enumerate(vectors)
        ]
    )


async def test_embed_normalises_and_keeps_order() -> None:
    client, _, _ = make_client({"emb": [embedding_response([[3, 4, 0], [0, 0, 2]])]})
    assert await client.embed(["a", "b"]) == [[0.6, 0.8, 0.0], [0.0, 0.0, 1.0]]


async def test_embed_fails_fast_on_429_by_default() -> None:
    client, _, slept = make_client({"emb": [status_error(429, "retry in 5s")]})
    with pytest.raises(AppError):
        await client.embed(["a"])
    assert slept == []


async def test_embed_waits_and_retries_when_allowed() -> None:
    client, fake, slept = make_client(
        {"emb": [status_error(429, "Please retry in 41.9s."), embedding_response([[1, 0, 0]])]}
    )
    assert await client.embed(["a"], max_wait_seconds=120) == [[1.0, 0.0, 0.0]]
    assert slept == [pytest.approx(42.9)]
    assert fake.calls == ["emb", "emb"]


async def test_embed_rejects_wrong_dimension() -> None:
    client, _, _ = make_client({"emb": [embedding_response([[1, 0]])]})
    with pytest.raises(AppError):
        await client.embed(["a"])


def test_retry_after_prefers_header() -> None:
    assert _retry_after(status_error(429, "retry in 5s", {"retry-after": "10"})) == 11
    assert _retry_after(status_error(429, "nothing")) == 30


def test_helpers() -> None:
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]
    assert estimate_cost({}, "x", 100, 100) == 0
