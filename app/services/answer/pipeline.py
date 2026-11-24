"""POST /ask: library first, AI last (US-D01, D03, D05, D08; DESIGN.md 6.1).

    route (rules, 0 tokens)
      -> hybrid search
      -> direct   exact lookup or cosine >= direct_return_cosine: the original joke, 0 tokens
      -> md       LLM answers from the top chunks; first line CONF must be >= llm_confidence
      -> ai       LLM answers/creates on its own, labelled is_ai_generated
                  (mode=library_only answers "not found" instead)

Phase 2 adds: answer cache, the source-file stage, streaming, conversation history, quota.
If every LLM provider is down, the nearest library joke is returned rather than an error.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_app_config
from app.core.errors import AppError, ErrorCode
from app.core.security import Principal
from app.db.models import Answer
from app.services.answer.intent import Intent, route
from app.services.llm.client import ChatResult, LLMClient
from app.services.llm.prompts import ai_messages, library_messages, parse_library_reply
from app.services.retrieval import Hit, hybrid_search, pattern_examples

logger = logging.getLogger(__name__)

Mode = Literal["auto", "ai_only", "library_only"]
Stage = Literal["md", "ai", "none"]
AI_NOTICE = "AI แต่งเอง ไม่ได้มาจากคลัง"
NOT_FOUND = "ไม่พบมุกที่ตรงกับคำถามในคลังเลย 😅 ลองถามคำอื่นดูนะ"


@dataclass
class AskResult:
    answer: str
    intent: Intent
    source_stage: Stage
    is_ai_generated: bool
    citations: list[Hit]
    stages_tried: list[str]
    retrieval_score: float | None = None
    llm_confidence: float | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal(0)
    direct: bool = False
    notice: str | None = None
    answer_id: uuid.UUID | None = None
    latency_ms: int = 0
    chunk_ids: list[uuid.UUID] = field(default_factory=list)

    def add_usage(self, result: ChatResult) -> None:
        self.model = result.model
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.cost += result.cost


def display(content: str) -> str:
    """Table rows are stored as "คำ: รองเท้า | มุก: กีโต้"; show them as "รองเท้า → กีโต้"."""
    cells = [c.strip() for c in content.split(" | ")]
    if len(cells) > 1 and all(re.match(r"^[^:]{1,30}: .+", c) for c in cells):
        return " → ".join(c.split(": ", 1)[1] for c in cells)
    return content


def _direct_text(hits: list[Hit]) -> str:
    if len(hits) == 1:
        return display(hits[0].content)
    return "\n".join(f"- {display(h.content)}" for h in hits)


async def ask(
    session: AsyncSession,
    principal: Principal,
    llm: LLMClient,
    message: str,
    *,
    mode: Mode = "auto",
) -> AskResult:
    started = time.perf_counter()
    cfg = get_app_config()
    th = cfg.thresholds
    routed = route(message)
    result = AskResult("", routed.intent, "none", False, [], [])

    # ---- retrieval (also feeds examples to the AI stage) --------------------------------
    query_vector = None
    try:
        query_vector = (await llm.embed([message]))[0]
    except AppError:
        logger.warning("Query embedding unavailable; full-text search only")
    found = await hybrid_search(
        session,
        principal,
        message,
        query_vector,
        top_k=cfg.retrieval.top_k,
        candidates=cfg.retrieval.candidates,
        rrf_k=cfg.retrieval.rrf_k,
    )
    hits = found.hits
    result.retrieval_score = round(found.best_cosine, 4) if found.vector_used else None
    relevant = bool(hits) and (
        found.best_cosine >= th.retrieval_min_cosine
        or bool(found.exact_hits)
        or not found.vector_used
    )

    if mode != "ai_only" and routed.intent != "generate":
        # ---- direct: the original joke, no LLM ------------------------------------------
        result.stages_tried.append("direct")
        direct_hits: list[Hit] = []
        if routed.intent == "lookup" and found.exact_hits:
            direct_hits = found.exact_hits[:3]
        elif (
            routed.intent in ("lookup", "request") and found.best_cosine >= th.direct_return_cosine
        ):
            direct_hits = [max(hits, key=lambda h: h.cosine or 0)]
        if direct_hits:
            result.answer, result.citations = _direct_text(direct_hits), direct_hits
            result.source_stage, result.direct = "md", True
            return _done(result, started)

        # ---- md: LLM answers from the library -------------------------------------------
        if relevant:
            result.stages_tried.append("md")
            try:
                reply = await llm.chat("chat", library_messages(message, hits))
            except AppError as exc:
                if exc.code != ErrorCode.LLM_UNAVAILABLE:
                    raise
                return _library_fallback(result, hits, started)
            result.add_usage(reply)
            conf, used, body = parse_library_reply(reply.text, len(hits))
            result.llm_confidence = conf
            if conf is not None and conf >= th.llm_confidence and body:
                result.answer, result.source_stage = body, "md"
                result.citations = [hits[i] for i in used] or hits[:1]
                return _done(result, started)

        if mode == "library_only":
            result.answer = NOT_FOUND
            return _done(result, started)

    # ---- ai: answer / create without the library ----------------------------------------
    result.stages_tried.append("ai")
    examples = hits
    if routed.pattern:
        examples = await pattern_examples(session, principal, routed.pattern) or hits
    try:
        reply = await llm.chat(
            "chat", ai_messages(message, examples, pattern=routed.pattern), temperature=0.9
        )
    except AppError as exc:
        if exc.code != ErrorCode.LLM_UNAVAILABLE or not hits:
            raise
        return _library_fallback(result, hits, started)
    result.add_usage(reply)
    result.answer, result.source_stage = reply.text.strip(), "ai"
    result.is_ai_generated, result.notice = True, AI_NOTICE
    return _done(result, started)


def _library_fallback(result: AskResult, hits: list[Hit], started: float) -> AskResult:
    best = max(hits, key=lambda h: (h.exact, h.cosine or 0, h.rrf))
    result.answer, result.citations, result.source_stage = display(best.content), [best], "md"
    result.direct = True
    result.notice = "ระบบ AI ไม่พร้อมใช้งานชั่วคราว จึงส่งมุกที่ใกล้เคียงที่สุดจากคลังให้แทน"
    return _done(result, started)


def _done(result: AskResult, started: float) -> AskResult:
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    result.chunk_ids = [h.chunk_id for h in result.citations]
    return result


async def record(
    session: AsyncSession, principal: Principal, message: str, mode: str, result: AskResult
) -> None:
    """Store the answer and count usage (feeds history, stats and phase-2 quotas).
    The env bootstrap admin has no api_keys row, so its questions are not stored."""
    if principal.key_id is None:
        return
    row = Answer(
        api_key_id=principal.key_id,
        question=message,
        intent=result.intent,
        mode=mode,
        answer=result.answer,
        source_stage=None if result.source_stage == "none" else result.source_stage,
        is_ai_generated=result.is_ai_generated,
        citations=[
            {"file_id": str(h.file_id), "chunk_id": str(h.chunk_id), "location": h.location}
            for h in result.citations
        ],
        chunk_ids=result.chunk_ids,
        retrieval_score=result.retrieval_score,
        llm_confidence=result.llm_confidence,
        stages_tried=result.stages_tried,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost=result.cost,
        latency_ms=result.latency_ms,
    )
    session.add(row)
    await session.flush()
    result.answer_id = row.id
    day = datetime.now(ZoneInfo(get_app_config().quota.timezone)).date()
    await session.execute(
        text(
            """
            INSERT INTO usage_daily (api_key_id, day, requests, tokens)
            VALUES (:key, :day, 1, :tokens)
            ON CONFLICT (api_key_id, day)
            DO UPDATE SET requests = usage_daily.requests + 1,
                          tokens = usage_daily.tokens + EXCLUDED.tokens
            """
        ),
        {"key": principal.key_id, "day": day, "tokens": result.input_tokens + result.output_tokens},
    )
    await session.commit()
