"""Daily maintenance called by Vercel Cron (Hobby plan: once a day).

Vercel sends `GET` with `Authorization: Bearer $CRON_SECRET`.
"""

import hmac
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.responses import Envelope, ok
from app.db.session import get_db, get_sessionmaker
from app.services.indexing import finish_indexing, pending_index_work
from app.services.jobs import process_file, recover
from app.services.llm.client import LLMClient, get_llm
from app.services.storage import Storage, StorageError, get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)

# Stay well inside Vercel Hobby's 300 s function limit.
TIME_BUDGET_SECONDS = 200
ABANDONED_UPLOAD_HOURS = 24


class CronResult(BaseModel):
    processed: dict[str, str | None]
    unembedded_chunks: dict[str, int]
    abandoned_uploads_removed: int


def require_cron_secret(authorization: Annotated[str | None, Header()] = None) -> None:
    secret = get_settings().cron_secret
    expected = f"Bearer {secret}" if secret else None
    if not expected or not authorization or not hmac.compare_digest(authorization, expected):
        raise AppError(ErrorCode.UNAUTHORIZED, "ไม่มีสิทธิ์เรียก cron", 401)


@router.get(
    "/cron", response_model=Envelope[CronResult], dependencies=[Depends(require_cron_secret)]
)
async def cron(
    session: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[Storage, Depends(get_storage)],
    llm: Annotated[LLMClient, Depends(get_llm)],
) -> Envelope[CronResult]:
    started = time.monotonic()
    maker = get_sessionmaker()
    assert maker is not None  # get_db already required a database

    # 1. Recover stuck jobs and run queued ones inline (no background task to lose here).
    processed: dict[str, str | None] = {}
    while time.monotonic() - started < TIME_BUDGET_SECONDS:
        ids = [i for i in await recover(session, limit=5) if str(i) not in processed]
        if not ids:
            break
        for file_id in ids:
            if time.monotonic() - started >= TIME_BUDGET_SECONDS:
                break
            processed[str(file_id)] = await process_file(file_id, storage=storage, maker=maker)

    # 2. Finish embeddings and tags for approved files.
    unembedded: dict[str, int] = {}
    left = TIME_BUDGET_SECONDS - (time.monotonic() - started)
    pending = await pending_index_work(session, limit=20, include_untagged=True)
    if pending and left > 10:
        unembedded = await finish_indexing(
            pending, maker=maker, storage=storage, llm=llm, time_budget=left
        )

    # 3. Remove signed-URL uploads that were never completed.
    rows = await session.execute(
        text(
            """
            DELETE FROM files
             WHERE status = 'awaiting_upload'
               AND created_at < now() - make_interval(hours => :hours)
         RETURNING raw_path
            """
        ),
        {"hours": ABANDONED_UPLOAD_HOURS},
    )
    paths = [r[0] for r in rows if r[0]]
    await session.commit()
    if paths:
        try:
            await storage.delete(paths)
        except StorageError:
            logger.warning("Could not delete abandoned uploads %s", paths)

    # (Phase 2: delete answers/conversations older than retention_days.)
    return ok(
        CronResult(
            processed=processed,
            unembedded_chunks=unembedded,
            abandoned_uploads_removed=len(paths),
        )
    )
