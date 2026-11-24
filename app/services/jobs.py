"""Background conversion using the `files` table as the queue (DESIGN.md 5.2, US-B05).

queued --claim--> converting --ok--> pending_review (reprocess also starts from queued)
                             --permanent error--> failed
                             --transient error / timeout--> queued (attempts < max) | failed

Nothing guarantees a background task finishes on serverless, so `recover()` (called from
GET /files, GET /files/{id} and the daily cron) puts stuck jobs back and returns jobs that
need (re)starting. Claiming is a conditional UPDATE, so a job never runs twice at once.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_app_config
from app.db.models import File, MdRevision
from app.services.conversion.converter import ConversionError, convert
from app.services.conversion.llm_format import reformat_with_llm
from app.services.llm.client import get_llm
from app.services.revisions import make_diff
from app.services.storage import ObjectNotFound, Storage
from app.services.uploads import md_path

logger = logging.getLogger(__name__)

# A queued job untouched for this long is assumed lost (its background task died).
QUEUED_GRACE_SECONDS = 60


@dataclass
class Claimed:
    id: uuid.UUID
    name: str
    raw_path: str
    mime_type: str
    attempts: int
    reprocess_method: str | None
    old_md_path: str | None


async def claim(session: AsyncSession, file_id: uuid.UUID) -> Claimed | None:
    row = (
        await session.execute(
            text(
                """
                UPDATE files
                   SET status = 'converting', locked_at = now(), attempts = attempts + 1,
                       error_code = NULL, error_message = NULL, updated_at = now()
                 WHERE id = :id AND status = 'queued'
             RETURNING id, name, raw_path, mime_type, attempts, reprocess_method, md_path
                """
            ),
            {"id": file_id},
        )
    ).first()
    await session.commit()
    return Claimed(*row) if row else None


async def _set_status(
    session: AsyncSession, file_id: uuid.UUID, status: str, **values: object
) -> bool:
    """Leave `converting` only if we still own the job (it may have been recovered/deleted)."""
    result = await session.execute(
        update(File)
        .where(File.id == file_id, File.status == "converting")
        .values(status=status, locked_at=None, updated_at=text("now()"), **values)
    )
    return result.rowcount == 1  # type: ignore[attr-defined]


async def _read_old_md(storage: Storage, path: str | None) -> str | None:
    if not path:
        return None
    try:
        return (await storage.download(path)).decode("utf-8", errors="replace")
    except ObjectNotFound:
        return None


async def process_file(
    file_id: uuid.UUID, *, storage: Storage, maker: async_sessionmaker[AsyncSession]
) -> str | None:
    """Run one conversion job. Returns the resulting status, or None if not claimed."""
    cfg = get_app_config().conversion
    async with maker() as session:
        job = await claim(session, file_id)
    if job is None:
        return None

    try:
        async with asyncio.timeout(cfg.job_timeout_minutes * 60):
            data = await storage.download(job.raw_path)
            result = convert(data, job.name, job.mime_type)
            if job.reprocess_method == "llm":
                result = await reformat_with_llm(result, get_llm())
            old_md = await _read_old_md(storage, job.old_md_path)
            path = md_path(job.id)
            await storage.upload(path, result.markdown.encode(), "text/markdown; charset=utf-8")
            if result.pii_removed:
                logger.info("Removed personal data from %s: %s", job.id, result.pii_removed)
            async with maker() as session:
                if await _set_status(
                    session,
                    job.id,
                    "pending_review",
                    md_path=path,
                    convert_method=result.method,
                    reprocess_method=None,
                ):
                    session.add(
                        MdRevision(
                            file_id=job.id,
                            reason="reprocess" if job.reprocess_method else "convert",
                            diff=make_diff(old_md, result.markdown),
                        )
                    )
                await session.commit()
        return "pending_review"
    except ConversionError as exc:
        async with maker() as session:
            await _set_status(
                session, job.id, "failed", error_code=exc.code, error_message=exc.message
            )
            await session.commit()
        return "failed"
    except Exception as exc:  # transient: storage hiccup, timeout, provider outage...
        logger.exception("Conversion of %s failed (attempt %s)", job.id, job.attempts)
        give_up = job.attempts >= cfg.max_attempts
        async with maker() as session:
            if give_up:
                code = "TIMEOUT" if isinstance(exc, TimeoutError) else "CONVERSION_FAILED"
                await _set_status(
                    session,
                    job.id,
                    "failed",
                    error_code=code,
                    error_message=f"แปลงไฟล์ไม่สำเร็จหลังลอง {job.attempts} ครั้ง",
                )
            else:
                await _set_status(session, job.id, "queued")
            await session.commit()
        return "failed" if give_up else "queued"


async def recover(session: AsyncSession, *, limit: int = 3) -> list[uuid.UUID]:
    """Requeue/fail stuck jobs and return queued jobs that nobody is running."""
    cfg = get_app_config().conversion
    params = {"mins": cfg.job_timeout_minutes, "max": cfg.max_attempts}
    stuck = "status = 'converting' AND locked_at < now() - make_interval(mins => :mins)"
    await session.execute(
        text(
            f"""
            UPDATE files SET status = 'queued', locked_at = NULL, updated_at = now()
             WHERE {stuck} AND attempts < :max
            """
        ),
        params,
    )
    await session.execute(
        text(
            f"""
            UPDATE files
               SET status = 'failed', locked_at = NULL, updated_at = now(),
                   error_code = 'TIMEOUT',
                   error_message = 'แปลงไฟล์ไม่เสร็จภายในเวลาที่กำหนด (ลองครบ ' || attempts || ' ครั้ง)'
             WHERE {stuck} AND attempts >= :max
            """
        ),
        params,
    )
    rows = await session.execute(
        text(
            """
            SELECT id FROM files
             WHERE status = 'queued' AND updated_at < now() - make_interval(secs => :grace)
             ORDER BY updated_at
             LIMIT :limit
            """
        ),
        {"grace": QUEUED_GRACE_SECONDS, "limit": limit},
    )
    ids = [r[0] for r in rows]
    await session.commit()
    return ids


async def run_jobs(
    file_ids: list[uuid.UUID], *, storage: Storage, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Background-task entrypoint: process files one at a time."""
    for file_id in file_ids:
        try:
            await process_file(file_id, storage=storage, maker=maker)
        except Exception:
            logger.exception("Job runner crashed on %s", file_id)
