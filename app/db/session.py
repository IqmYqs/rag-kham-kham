"""Async DB engine for Supabase's transaction pooler (DESIGN.md 4.3)."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode


def make_engine(url: str) -> AsyncEngine:
    # NullPool: serverless instances come and go; the Supabase pooler does the pooling.
    # prepare_threshold=None: the transaction pooler does not support prepared statements.
    return create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"prepare_threshold": None},
    )


@lru_cache
def get_engine() -> AsyncEngine | None:
    url = get_settings().database_url
    return make_engine(url) if url else None


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession] | None:
    engine = get_engine()
    return async_sessionmaker(engine, expire_on_commit=False) if engine else None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Fails with 500 when no database is configured."""
    maker = get_sessionmaker()
    if maker is None:
        raise AppError(ErrorCode.INTERNAL_ERROR, "ยังไม่ได้ตั้งค่า DATABASE_URL", status_code=500)
    async with maker() as session:
        yield session
