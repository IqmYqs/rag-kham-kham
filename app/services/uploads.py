"""Upload validation shared by multipart and signed-URL flows (US-B01, US-B03, US-B04)."""

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorCode
from app.db.models import File, Team

MB = 1024 * 1024

# US-B01 AC2. Decided by extension: browsers and curl send unreliable Content-Types.
EXTENSION_MIME = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass
class Rejection(Exception):
    """A single file was refused; other files in the same request carry on."""

    code: ErrorCode
    message: str
    status_code: int
    details: dict[str, Any] | None = field(default=None)


def clean_name(raw: str | None) -> str:
    """Drop any client-side directory part (`C:\\x\\a.txt`, `../a.txt`) and whitespace."""
    name = PurePath((raw or "").replace("\\", "/")).name.strip()
    if not name or len(name) > 255:
        raise Rejection(ErrorCode.VALIDATION_ERROR, "ชื่อไฟล์ไม่ถูกต้อง", 400)
    return name


def detect_mime(name: str) -> str:
    ext = PurePath(name).suffix.lower()
    mime = EXTENSION_MIME.get(ext)
    if mime is None:
        raise Rejection(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            f"ไม่รองรับไฟล์ชนิด '{ext or '(ไม่มีนามสกุล)'}'",
            415,
            {"supported": sorted(EXTENSION_MIME)},
        )
    return mime


def check_size(size: int, max_bytes: int, *, hint: str | None = None) -> None:
    if size <= 0:
        raise Rejection(ErrorCode.VALIDATION_ERROR, "ไฟล์ว่างเปล่า", 400)
    if size > max_bytes:
        message = f"ไฟล์ใหญ่เกิน {max_bytes // MB} MB"
        raise Rejection(
            ErrorCode.FILE_TOO_LARGE,
            f"{message} — {hint}" if hint else message,
            413,
            {"size_bytes": size, "max_bytes": max_bytes},
        )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_path(file_id: uuid.UUID, name: str) -> str:
    # Storage keys reject many non-ASCII characters, so the Thai name stays in the DB only.
    return f"raw/{file_id}{PurePath(name).suffix.lower()}"


def md_path(file_id: uuid.UUID) -> str:
    return f"md/{file_id}.md"


async def validate_scope(session: AsyncSession, scope: str, team_id: uuid.UUID | None) -> None:
    if scope == "global":
        if team_id is not None:
            raise Rejection(ErrorCode.VALIDATION_ERROR, "scope=global ต้องไม่ระบุ team_id", 400)
        return
    if team_id is None or await session.get(Team, team_id) is None:
        raise Rejection(ErrorCode.VALIDATION_ERROR, "scope=team ต้องระบุ team_id ที่มีอยู่จริง", 400)


async def find_duplicate(
    session: AsyncSession,
    *,
    name: str | None,
    sha256: str | None,
    scope: str,
    team_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> File | None:
    """Same name or same content in the same library (US-B03). Rejected files don't count."""
    stmt = select(File).where(
        File.scope == scope,
        File.team_id.is_(None) if team_id is None else File.team_id == team_id,
        File.status != "rejected",
        or_(
            File.name == name if name else false(),
            File.sha256 == sha256 if sha256 else false(),
        ),
    )
    if exclude_id is not None:
        stmt = stmt.where(File.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


def duplicate_rejection(existing: File, *, sha256: str | None) -> Rejection:
    same_content = sha256 is not None and existing.sha256 == sha256
    return Rejection(
        ErrorCode.FILE_DUPLICATE,
        "มีไฟล์เนื้อหาเดียวกันอยู่แล้ว" if same_content else "มีไฟล์ชื่อนี้อยู่แล้ว",
        409,
        {"file_id": str(existing.id), "name": existing.name, "status": existing.status},
    )


async def ensure_not_duplicate(
    session: AsyncSession,
    *,
    name: str | None,
    sha256: str | None,
    scope: str,
    team_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> list[File]:
    """Raise on a real duplicate. Returns abandoned `awaiting_upload` rows with the same
    name (an earlier signed-URL attempt that never completed) for the caller to delete."""
    stale: list[File] = []
    while True:
        existing = await find_duplicate(
            session, name=name, sha256=sha256, scope=scope, team_id=team_id, exclude_id=exclude_id
        )
        if existing is None:
            return stale
        if existing.status == "awaiting_upload" and existing.name == name and existing not in stale:
            stale.append(existing)
            await session.delete(existing)
            await session.flush()
            continue
        raise duplicate_rejection(existing, sha256=sha256)
