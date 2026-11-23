"""Standard response envelope (DESIGN.md section 3 / US-H01)."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import get_request_id


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class Meta(BaseModel):
    request_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_config = {"extra": "allow"}


class Envelope[T](BaseModel):
    success: bool
    data: T | None = None
    error: ErrorBody | None = None
    meta: Meta = Field(default_factory=Meta)


def _meta(extra: dict[str, Any] | None) -> Meta:
    return Meta(request_id=get_request_id(), **(extra or {}))


def ok[T](data: T, meta: dict[str, Any] | None = None) -> Envelope[T]:
    return Envelope[T](success=True, data=data, meta=_meta(meta))


def fail(code: str, message: str, details: dict[str, Any] | None = None) -> Envelope[None]:
    return Envelope[None](
        success=False,
        error=ErrorBody(code=code, message=message, details=details),
        meta=_meta(None),
    )
