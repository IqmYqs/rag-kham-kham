import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.responses import ErrorBody

FileStatus = Literal[
    "awaiting_upload", "queued", "converting", "pending_review", "approved", "rejected", "failed"
]
Scope = Literal["global", "team"]


class FileMeta(BaseModel):
    """Optional metadata (US-B04). Leave category/tags empty to let AI fill them in."""

    category: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=20)
    description: str | None = Field(default=None, max_length=1000)
    author: str | None = Field(default=None, max_length=200)


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    source_type: str
    mime_type: str
    size_bytes: int | None
    sha256: str | None
    scope: Scope
    team_id: uuid.UUID | None
    status: FileStatus
    error_code: str | None
    error_message: str | None
    attempts: int
    convert_method: str | None
    category: str | None
    tags: list[str]
    description: str | None
    author: str | None
    summary: str | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None


class FilePage(BaseModel):
    items: list[FileOut]
    total: int
    page: int
    page_size: int


class UploadResult(BaseModel):
    """Per-file outcome: either `file_id` + `status`, or `error`."""

    name: str
    file_id: uuid.UUID | None = None
    status: FileStatus | None = None
    upload_url: str | None = Field(
        default=None, description="PUT the file body here (signed-URL flow only)"
    )
    expires_in: int | None = None
    error: ErrorBody | None = None


class UploadResults(BaseModel):
    files: list[UploadResult]


class UploadRequestItem(FileMeta):
    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class UploadsRequest(BaseModel):
    files: list[UploadRequestItem] = Field(min_length=1, max_length=20)
    scope: Scope = "global"
    team_id: uuid.UUID | None = None


class CompleteRequest(BaseModel):
    file_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class DeleteResult(BaseModel):
    id: uuid.UUID
    deleted: bool = True
