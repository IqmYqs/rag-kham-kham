"""POST /ask and GET /random (US-D01–D03, D08, D09). Users and admins."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.responses import Envelope, ok
from app.core.security import CurrentUser
from app.db.session import get_db
from app.schemas.ask import AskIn, AskOut, Citation, Confidence, RandomOut, Usage
from app.services.answer.pipeline import ask as run_ask
from app.services.answer.pipeline import record
from app.services.llm.client import LLMClient, get_llm
from app.services.retrieval import Hit, random_chunk

router = APIRouter(tags=["ask"])

DB = Annotated[AsyncSession, Depends(get_db)]
LLMDep = Annotated[LLMClient, Depends(get_llm)]


def _citation(h: Hit) -> Citation:
    return Citation(
        file_id=h.file_id,
        file_name=h.file_name,
        chunk_id=h.chunk_id,
        location=h.location,
        content=h.content,
    )


@router.post("/ask", response_model=Envelope[AskOut], summary="ถามหามุก")
async def ask(principal: CurrentUser, session: DB, llm: LLMDep, body: AskIn) -> Envelope[AskOut]:
    """Ask anything: a single word (`รองเท้า`), a request (`ขอมุกเกี่ยวกับมะม่วง`), small
    talk (`วันนี้เหนื่อยจัง`) or a creation request (`แต่งมุกแนว -โต้`). Answers come from
    the library when possible; AI-written answers have `is_ai_generated = true`."""
    result = await run_ask(session, principal, llm, body.message.strip(), mode=body.mode)
    await record(session, principal, body.message, body.mode, result)
    return ok(
        AskOut(
            answer_id=result.answer_id,
            answer=result.answer,
            intent=result.intent,
            source_stage=result.source_stage,
            is_ai_generated=result.is_ai_generated,
            notice=result.notice,
            direct=result.direct,
            citations=[_citation(h) for h in result.citations],
            confidence=Confidence(retrieval=result.retrieval_score, llm=result.llm_confidence),
            stages_tried=result.stages_tried,
            usage=Usage(
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                estimated_cost=result.cost,
            ),
            latency_ms=result.latency_ms,
        )
    )


@router.get("/random", response_model=Envelope[RandomOut], summary="สุ่มมุก")
async def random_joke(
    principal: CurrentUser,
    session: DB,
    category: Annotated[str | None, Query(max_length=100)] = None,
) -> Envelope[RandomOut]:
    """A random joke from the library in your scope. Uses no tokens."""
    hit = await random_chunk(session, principal, category)
    if hit is None:
        raise AppError(ErrorCode.NOT_FOUND, "ยังไม่มีมุกในคลัง", 404)
    return ok(RandomOut(content=hit.content, citation=_citation(hit)))
