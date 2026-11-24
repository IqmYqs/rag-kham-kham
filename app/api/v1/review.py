"""Review converted Markdown before it reaches the library (US-C04, US-D04). Admin only.

pending_review --approve--> approved (chunks + full-text now; embeddings/tags in background)
pending_review --reject--> rejected
pending_review | approved | failed --reprocess--> queued
PUT markdown on an approved file sends it back to pending_review (re-approve to index again).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.responses import Envelope, ok
from app.core.security import CurrentAdmin
from app.db.models import File, MdRevision
from app.db.session import get_db, get_sessionmaker
from app.schemas.files import FileOut, MarkdownIn, MarkdownOut, ReprocessIn, RevisionOut
from app.services.conversion.pii import redact
from app.services.indexing import build_chunks, finish_indexing
from app.services.jobs import run_jobs
from app.services.llm.client import LLMClient, get_llm
from app.services.revisions import make_diff
from app.services.storage import ObjectNotFound, Storage, get_storage
from app.services.uploads import md_path

router = APIRouter(prefix="/files", tags=["review"])

DB = Annotated[AsyncSession, Depends(get_db)]
StorageDep = Annotated[Storage, Depends(get_storage)]
HAS_MARKDOWN = ("pending_review", "approved")


async def _transition(
    session: AsyncSession, file_id: uuid.UUID, allowed: tuple[str, ...], **values: object
) -> File:
    """Atomic state change: only succeeds from an allowed status (no lost races)."""
    result = await session.execute(
        update(File)
        .where(File.id == file_id, File.status.in_(allowed))
        .values(updated_at=text("now()"), **values)
        .returning(File)
        .execution_options(synchronize_session=False)
    )
    row = result.scalar_one_or_none()
    if row is None:
        current = await session.get(File, file_id)
        if current is None:
            raise AppError(ErrorCode.NOT_FOUND, "ไม่พบไฟล์", 404, {"file_id": str(file_id)})
        raise AppError(
            ErrorCode.INVALID_STATE,
            f"ทำรายการนี้ไม่ได้เมื่อไฟล์อยู่ในสถานะ {current.status}",
            409,
            {"status": current.status, "allowed": list(allowed)},
        )
    return row


async def _unindex(session: AsyncSession, file_id: uuid.UUID) -> None:
    """The .md is changing: drop its chunks and any cached answers that cited it."""
    await session.execute(text("DELETE FROM chunks WHERE file_id = :id"), {"id": file_id})
    await session.execute(
        text("DELETE FROM answer_cache WHERE :id = ANY(file_ids)"), {"id": file_id}
    )


async def _load_markdown(storage: Storage, row: File) -> str:
    if row.status not in HAS_MARKDOWN or not row.md_path:
        raise AppError(
            ErrorCode.INVALID_STATE,
            f"ไฟล์ยังไม่มี .md (สถานะ {row.status})",
            409,
            {"status": row.status, "allowed": list(HAS_MARKDOWN)},
        )
    try:
        return (await storage.download(row.md_path)).decode("utf-8")
    except ObjectNotFound:
        raise AppError(
            ErrorCode.INTERNAL_ERROR, "ไม่พบไฟล์ .md ใน Storage ให้ใช้ reprocess", 500
        ) from None


async def _get(session: AsyncSession, file_id: uuid.UUID) -> File:
    row = await session.get(File, file_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "ไม่พบไฟล์", 404, {"file_id": str(file_id)})
    return row


@router.get("/{file_id}/markdown", response_model=Envelope[MarkdownOut], summary="ดู .md")
async def get_markdown(
    _: CurrentAdmin, session: DB, storage: StorageDep, file_id: uuid.UUID
) -> Envelope[MarkdownOut]:
    row = await _get(session, file_id)
    markdown = await _load_markdown(storage, row)
    return ok(
        MarkdownOut(
            file_id=row.id, status=row.status, convert_method=row.convert_method, markdown=markdown
        )  # type: ignore[arg-type]
    )


@router.put("/{file_id}/markdown", response_model=Envelope[MarkdownOut], summary="แก้ไข .md")
async def put_markdown(
    admin: CurrentAdmin, session: DB, storage: StorageDep, file_id: uuid.UUID, body: MarkdownIn
) -> Envelope[MarkdownOut]:
    """Replace the .md. Personal data is removed from the edit as well. An approved file
    returns to `pending_review` and leaves search until it is approved again."""
    row = await _get(session, file_id)
    old = await _load_markdown(storage, row)
    markdown, removed = redact(body.markdown.replace("\r\n", "\n"))
    if not markdown.endswith("\n"):
        markdown += "\n"
    path = row.md_path or md_path(row.id)
    await storage.upload(path, markdown.encode(), "text/markdown; charset=utf-8")

    updated = await _transition(
        session, file_id, HAS_MARKDOWN, status="pending_review", approved_at=None, md_path=path
    )
    await _unindex(session, file_id)
    session.add(
        MdRevision(
            file_id=file_id,
            reason="manual_edit",
            diff=make_diff(old, markdown),
            created_by=admin.key_id,
        )
    )
    await session.commit()
    return ok(
        MarkdownOut(
            file_id=updated.id,
            status=updated.status,  # type: ignore[arg-type]
            convert_method=updated.convert_method,
            markdown=markdown,
            pii_removed=removed,
        )
    )


@router.post("/{file_id}/approve", response_model=Envelope[FileOut], summary="อนุมัติ .md")
async def approve(
    _: CurrentAdmin,
    session: DB,
    storage: StorageDep,
    background: BackgroundTasks,
    llm: Annotated[LLMClient, Depends(get_llm)],
    file_id: uuid.UUID,
) -> Envelope[FileOut]:
    """`pending_review` → `approved` (US-C05). Splits the .md into chunks and indexes them
    for full-text search right away; embeddings, summary, category and tags are added in the
    background (see `index` in GET /files/{id} for progress)."""
    row = await _transition(
        session, file_id, ("pending_review",), status="approved", approved_at=text("now()")
    )
    markdown = await _load_markdown(storage, row)
    count = await build_chunks(session, row.id, markdown, llm.embedding_model)
    if count == 0:
        await session.rollback()
        raise AppError(ErrorCode.INVALID_STATE, "ไม่พบเนื้อหาที่นำไปทำดัชนีได้ ให้แก้ .md ก่อน", 409)
    # The library changed, so cached answers may no longer be the best ones (DESIGN 5.5).
    await session.execute(text("DELETE FROM answer_cache"))
    await session.commit()

    maker = get_sessionmaker()
    if maker is not None:
        background.add_task(finish_indexing, [row.id], maker=maker, storage=storage, llm=llm)
    out = FileOut.model_validate(row)
    out.index = {"chunks": count, "embedded": 0}
    return ok(out)


@router.post("/{file_id}/reject", response_model=Envelope[FileOut], summary="ปฏิเสธ .md")
async def reject(_: CurrentAdmin, session: DB, file_id: uuid.UUID) -> Envelope[FileOut]:
    """`pending_review` → `rejected`. The same name/content can then be uploaded again."""
    row = await _transition(session, file_id, ("pending_review",), status="rejected")
    await session.commit()
    return ok(FileOut.model_validate(row))


@router.post(
    "/{file_id}/reprocess", status_code=202, response_model=Envelope[FileOut], summary="แปลงใหม่"
)
async def reprocess(
    _: CurrentAdmin,
    session: DB,
    storage: StorageDep,
    background: BackgroundTasks,
    file_id: uuid.UUID,
    body: ReprocessIn | None = None,
) -> Envelope[FileOut]:
    """Convert this file again (`default`) or let the AI restructure it (`llm`).
    One file at a time only — there is no whole-library reprocess (US-C04 AC5)."""
    method = (body or ReprocessIn()).method
    row = await _transition(
        session,
        file_id,
        ("pending_review", "approved", "failed"),
        status="queued",
        attempts=0,
        error_code=None,
        error_message=None,
        approved_at=None,
        reprocess_method=method,
    )
    await _unindex(session, file_id)
    await session.commit()
    maker = get_sessionmaker()
    if maker is not None:
        background.add_task(run_jobs, [file_id], storage=storage, maker=maker)
    return ok(FileOut.model_validate(row))


@router.get(
    "/{file_id}/revisions", response_model=Envelope[list[RevisionOut]], summary="ประวัติการแก้ .md"
)
async def revisions(
    _: CurrentAdmin, session: DB, file_id: uuid.UUID
) -> Envelope[list[RevisionOut]]:
    await _get(session, file_id)
    rows = await session.execute(
        select(MdRevision)
        .where(MdRevision.file_id == file_id)
        .order_by(MdRevision.created_at.desc())
    )
    return ok([RevisionOut.model_validate(r) for r in rows.scalars()])
