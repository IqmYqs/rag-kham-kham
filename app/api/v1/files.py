"""File upload and status endpoints (US-B01, B03–B06). Admin only.

Two ways to upload (DESIGN.md 5.1):
- `POST /files` multipart, files <= 4 MB (Vercel caps request bodies at 4.5 MB)
- `POST /files/uploads` -> PUT each file to its signed URL -> `POST /files/complete`
"""

import contextlib
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, UploadFile
from fastapi import File as FormFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_app_config
from app.core.errors import AppError, ErrorCode
from app.core.responses import Envelope, ErrorBody, ok
from app.core.security import CurrentAdmin
from app.db.models import File
from app.db.session import get_db, get_sessionmaker
from app.schemas.files import (
    CompleteRequest,
    DeleteResult,
    FileOut,
    FilePage,
    FileStatus,
    Scope,
    UploadResult,
    UploadResults,
    UploadsRequest,
)
from app.services import uploads as up
from app.services.jobs import recover, run_jobs
from app.services.storage import ObjectNotFound, Storage, StorageError, get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["files"])

DB = Annotated[AsyncSession, Depends(get_db)]
StorageDep = Annotated[Storage, Depends(get_storage)]
UPLOAD_RESPONSES = {202: {"model": Envelope[UploadResults]}}


# ---- helpers ---------------------------------------------------------------------------


def _failed(name: str, rej: up.Rejection) -> tuple[UploadResult, up.Rejection]:
    error = ErrorBody(code=rej.code, message=rej.message, details=rej.details)
    return UploadResult(name=name, error=error), rej


def _respond(results: list[tuple[UploadResult, up.Rejection | None]]) -> JSONResponse:
    """202 if at least one file was accepted (failures are listed per file);
    otherwise the first file's error status, with every file's result in details."""
    files = [r for r, _ in results]
    if any(rej is None for _, rej in results):
        body = ok(UploadResults(files=files)).model_dump(mode="json")
        return JSONResponse(status_code=202, content=body)
    first = next(rej for _, rej in results if rej is not None)
    raise AppError(
        first.code,
        first.message,
        first.status_code,
        details={"files": [f.model_dump(mode="json") for f in files]},
    )


def _schedule(background: BackgroundTasks, storage: Storage, ids: list[uuid.UUID]) -> None:
    maker = get_sessionmaker()
    if ids and maker is not None:
        background.add_task(run_jobs, ids, storage=storage, maker=maker)


async def _delete_objects(storage: Storage, paths: list[str | None]) -> None:
    try:
        await storage.delete([p for p in paths if p])
    except StorageError:
        logger.warning("Could not delete storage objects %s", paths)


async def _commit_new(session: AsyncSession, row: File) -> None:
    """Commit a new file row; a lost race against the unique indexes becomes a 409."""
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await up.find_duplicate(
            session, name=row.name, sha256=row.sha256, scope=row.scope, team_id=row.team_id
        )
        if existing is None:
            raise
        raise up.duplicate_rejection(existing, sha256=row.sha256) from None


def _new_file(
    *,
    file_id: uuid.UUID,
    name: str,
    mime: str,
    status: str,
    scope: str,
    team_id: uuid.UUID | None,
    admin: CurrentAdmin,
    category: str | None,
    tags: list[str],
    description: str | None,
    author: str | None,
    size: int,
    sha256: str | None,
) -> File:
    return File(
        id=file_id,
        name=name,
        source_type="upload",
        mime_type=mime,
        size_bytes=size,
        sha256=sha256,
        raw_path=up.raw_path(file_id, name),
        scope=scope,
        team_id=team_id,
        status=status,
        category=category,
        tags=tags,
        tags_source="user" if (category or tags) else None,
        description=description,
        author=author,
        uploaded_by=admin.key_id,
    )


async def _get_file(session: AsyncSession, file_id: uuid.UUID) -> File:
    row = await session.get(File, file_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "ไม่พบไฟล์", 404, {"file_id": str(file_id)})
    return row


async def _scope_or_400(session: AsyncSession, scope: str, team_id: uuid.UUID | None) -> None:
    try:
        await up.validate_scope(session, scope, team_id)
    except up.Rejection as rej:
        raise AppError(rej.code, rej.message, rej.status_code) from None


# ---- upload ----------------------------------------------------------------------------


@router.post("", status_code=202, responses=UPLOAD_RESPONSES, summary="อัปโหลดไฟล์ (≤ 4 MB)")
async def upload_files(
    admin: CurrentAdmin,
    session: DB,
    storage: StorageDep,
    background: BackgroundTasks,
    files: Annotated[list[UploadFile], FormFile(description="เลือกได้หลายไฟล์")],
    scope: Annotated[Scope, Form()] = "global",
    team_id: Annotated[uuid.UUID | None, Form()] = None,
    category: Annotated[str | None, Form(max_length=100)] = None,
    tags: Annotated[str | None, Form(description="คั่นด้วยจุลภาค เช่น ออฟฟิศ,วันเกิด")] = None,
    description: Annotated[str | None, Form(max_length=1000)] = None,
    author: Annotated[str | None, Form(max_length=200)] = None,
) -> JSONResponse:
    """Multipart upload for small files. Metadata applies to every file in the request.
    For files over 4 MB use `POST /files/uploads`."""
    cfg = get_app_config().upload
    limit = min(cfg.multipart_max_mb, cfg.max_file_mb) * up.MB
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    await _scope_or_400(session, scope, team_id)

    results: list[tuple[UploadResult, up.Rejection | None]] = []
    accepted: list[uuid.UUID] = []
    for upload in files:
        display = upload.filename or "(no name)"
        try:
            name = up.clean_name(upload.filename)
            mime = up.detect_mime(name)
            data = await upload.read(limit + 1)
            up.check_size(len(data), limit, hint="ใช้ POST /api/v1/files/uploads (signed URL) แทน")
            digest = up.sha256_hex(data)
            stale = await up.ensure_not_duplicate(
                session, name=name, sha256=digest, scope=scope, team_id=team_id
            )
            file_id = uuid.uuid4()
            row = _new_file(
                file_id=file_id, name=name, mime=mime, status="queued", scope=scope,
                team_id=team_id, admin=admin, category=category, tags=tag_list,
                description=description, author=author, size=len(data), sha256=digest,
            )  # fmt: skip
            await storage.upload(row.raw_path or "", data, mime)
            session.add(row)
            try:
                await _commit_new(session, row)
            except up.Rejection:
                await _delete_objects(storage, [row.raw_path])
                raise
            await _delete_objects(storage, [s.raw_path for s in stale])
            results.append((UploadResult(name=name, file_id=file_id, status="queued"), None))
            accepted.append(file_id)
        except up.Rejection as rej:
            await session.rollback()
            results.append(_failed(display, rej))

    _schedule(background, storage, accepted)
    return _respond(results)


@router.post(
    "/uploads", status_code=202, responses=UPLOAD_RESPONSES, summary="ขอ signed URL (≤ 20 MB)"
)
async def request_uploads(
    admin: CurrentAdmin, session: DB, storage: StorageDep, body: UploadsRequest
) -> JSONResponse:
    """Step 1 of the large-file flow. Checks type, size and duplicates *before* the upload
    (US-B01 AC7), then returns a signed URL per file. Next: `PUT <upload_url>` with the
    file as the body, then `POST /files/complete`."""
    cfg = get_app_config()
    max_bytes = cfg.upload.max_file_mb * up.MB
    await _scope_or_400(session, body.scope, body.team_id)

    results: list[tuple[UploadResult, up.Rejection | None]] = []
    for item in body.files:
        try:
            name = up.clean_name(item.name)
            mime = up.detect_mime(name)
            up.check_size(item.size_bytes, max_bytes)
            sha = item.sha256.lower() if item.sha256 else None
            stale = await up.ensure_not_duplicate(
                session, name=name, sha256=sha, scope=body.scope, team_id=body.team_id
            )
            file_id = uuid.uuid4()
            row = _new_file(
                file_id=file_id, name=name, mime=mime, status="awaiting_upload",
                scope=body.scope, team_id=body.team_id, admin=admin, category=item.category,
                tags=item.tags, description=item.description, author=item.author,
                size=item.size_bytes, sha256=sha,
            )  # fmt: skip
            session.add(row)
            await _commit_new(session, row)
            await _delete_objects(storage, [s.raw_path for s in stale])
            try:
                url = await storage.create_signed_upload_url(row.raw_path or "")
            except StorageError:
                await session.delete(row)
                await session.commit()
                raise
            results.append(
                (
                    UploadResult(
                        name=name,
                        file_id=file_id,
                        status="awaiting_upload",
                        upload_url=url,
                        expires_in=cfg.storage.signed_upload_expires_seconds,
                    ),
                    None,
                )
            )
        except up.Rejection as rej:
            await session.rollback()
            results.append(_failed(item.name, rej))
    return _respond(results)


@router.post("/complete", status_code=202, responses=UPLOAD_RESPONSES, summary="แจ้งว่าอัปโหลดเสร็จ")
async def complete_uploads(
    _: CurrentAdmin,
    session: DB,
    storage: StorageDep,
    background: BackgroundTasks,
    body: CompleteRequest,
) -> JSONResponse:
    """Step 3 of the large-file flow: verifies each uploaded object (size, SHA-256,
    duplicates) and queues it for conversion. Calling it again is harmless."""
    max_bytes = get_app_config().upload.max_file_mb * up.MB
    results: list[tuple[UploadResult, up.Rejection | None]] = []
    accepted: list[uuid.UUID] = []

    for file_id in dict.fromkeys(body.file_ids):
        row = await session.get(File, file_id)
        display = row.name if row else str(file_id)  # row expires on rollback
        try:
            if row is None:
                raise up.Rejection(ErrorCode.NOT_FOUND, "ไม่พบไฟล์", 404, {"file_id": str(file_id)})
            if row.status != "awaiting_upload":  # already completed: idempotent
                results.append(
                    (UploadResult(name=row.name, file_id=row.id, status=row.status), None)
                )  # type: ignore[arg-type]
                continue
            try:
                data = await storage.download(row.raw_path or "")
            except ObjectNotFound:
                raise up.Rejection(
                    ErrorCode.INVALID_STATE,
                    "ยังไม่พบไฟล์ใน Storage ให้ PUT ไฟล์ไปที่ upload_url ก่อน",
                    409,
                    {"file_id": str(file_id)},
                ) from None
            digest = up.sha256_hex(data)
            try:
                up.check_size(len(data), max_bytes)
                if row.sha256 and row.sha256 != digest:
                    raise up.Rejection(
                        ErrorCode.VALIDATION_ERROR,
                        "SHA-256 ของไฟล์ที่อัปโหลดไม่ตรงกับที่แจ้งไว้",
                        400,
                        {"expected": row.sha256, "actual": digest},
                    )
                existing = await up.find_duplicate(
                    session, name=None, sha256=digest, scope=row.scope, team_id=row.team_id,
                    exclude_id=row.id,
                )  # fmt: skip
                if existing is not None:
                    raise up.duplicate_rejection(existing, sha256=digest)
            except up.Rejection:
                # The upload is unusable: drop it so the name can be used again.
                await _delete_objects(storage, [row.raw_path])
                await session.delete(row)
                await session.commit()
                raise
            row.sha256, row.size_bytes, row.status = digest, len(data), "queued"
            await session.commit()
            results.append((UploadResult(name=row.name, file_id=row.id, status="queued"), None))
            accepted.append(row.id)
        except up.Rejection as rej:
            await session.rollback()
            results.append(_failed(display, rej))

    _schedule(background, storage, accepted)
    return _respond(results)


# ---- read / delete ---------------------------------------------------------------------


@router.get("", response_model=Envelope[FilePage], summary="รายการไฟล์")
async def list_files(
    _: CurrentAdmin,
    session: DB,
    storage: StorageDep,
    background: BackgroundTasks,
    status: FileStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Envelope[FilePage]:
    _schedule(background, storage, await recover(session))
    stmt = select(File)
    if status:
        stmt = stmt.where(File.status == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(File.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars()
    items = [FileOut.model_validate(r) for r in rows]
    return ok(FilePage(items=items, total=total, page=page, page_size=page_size))


@router.get("/{file_id}", response_model=Envelope[FileOut], summary="สถานะไฟล์")
async def get_file(
    _: CurrentAdmin,
    session: DB,
    storage: StorageDep,
    background: BackgroundTasks,
    file_id: uuid.UUID,
) -> Envelope[FileOut]:
    _schedule(background, storage, await recover(session))
    return ok(FileOut.model_validate(await _get_file(session, file_id)))


@router.delete("/{file_id}", response_model=Envelope[DeleteResult], summary="ลบไฟล์")
async def delete_file(
    _: CurrentAdmin, session: DB, storage: StorageDep, file_id: uuid.UUID
) -> Envelope[DeleteResult]:
    """Deletes the original, the .md, chunks/embeddings (cascade), revisions and any cached
    answers that cited this file (US-B06)."""
    row = await _get_file(session, file_id)
    if row.status == "converting":
        raise AppError(
            ErrorCode.INVALID_STATE, "ไฟล์กำลังแปลงอยู่ ลองใหม่ภายหลัง", 409, {"status": row.status}
        )
    with contextlib.suppress(ObjectNotFound):
        await storage.delete([p for p in (row.raw_path, row.md_path) if p])
    await session.execute(
        text("DELETE FROM answer_cache WHERE :id = ANY(file_ids)"), {"id": file_id}
    )
    await session.delete(row)
    await session.commit()
    return ok(DeleteResult(id=file_id))
