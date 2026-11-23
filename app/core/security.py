"""API key authentication and role checks (US-A01)."""

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.models import ApiKey
from app.db.session import get_sessionmaker

Role = Literal["admin", "user"]
KEY_PREFIX = "rkk_"

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Principal:
    role: Role
    owner_name: str
    key_id: uuid.UUID | None = None  # None for the bootstrap admin key
    team_id: uuid.UUID | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def _lookup_db_key(raw_key: str) -> Principal | None:
    maker = get_sessionmaker()
    if maker is None:
        return None
    async with maker() as session:
        row = await session.scalar(
            select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key), ApiKey.is_active)
        )
    if row is None:
        return None
    return Principal(role=row.role, owner_name=row.owner_name, key_id=row.id, team_id=row.team_id)  # type: ignore[arg-type]


async def get_principal(raw_key: Annotated[str | None, Security(api_key_header)]) -> Principal:
    if not raw_key:
        raise AppError(ErrorCode.UNAUTHORIZED, "ต้องส่ง X-API-Key", status_code=401)

    bootstrap = get_settings().admin_bootstrap_key
    if bootstrap and hmac.compare_digest(raw_key.encode(), bootstrap.encode()):
        return Principal(role="admin", owner_name="bootstrap-admin")

    principal = await _lookup_db_key(raw_key)
    if principal is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "API key ไม่ถูกต้อง", status_code=401)
    return principal


def require_role(*roles: Role) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: `Depends(require_role("admin"))`."""

    async def _check(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if principal.role not in roles:
            raise AppError(ErrorCode.FORBIDDEN, "สิทธิ์ไม่พอสำหรับคำสั่งนี้", status_code=403)
        return principal

    return _check


CurrentUser = Annotated[Principal, Depends(require_role("admin", "user"))]
CurrentAdmin = Annotated[Principal, Depends(require_role("admin"))]
