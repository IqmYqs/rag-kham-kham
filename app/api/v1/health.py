"""GET /api/v1/health — no auth (US-H01 AC5)."""

import logging
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_app_config, get_settings
from app.core.responses import Envelope, ok
from app.db.session import get_engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])

CheckStatus = Literal["ok", "error", "not_configured", "configured"]


class HealthData(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    checks: dict[str, CheckStatus]


async def _check_database() -> CheckStatus:
    engine = get_engine()
    if engine is None:
        return "not_configured"
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        logger.exception("Database health check failed")
        return "error"


def _check_storage() -> CheckStatus:
    s = get_settings()
    return "configured" if s.supabase_url and s.supabase_service_key else "not_configured"


def _check_llm() -> CheckStatus:
    # Only checks that keys exist for the configured chat providers; does not call the
    # provider, so health checks never spend free-tier quota.
    settings, cfg = get_settings(), get_app_config()
    for ref in cfg.llm.chat:
        provider = cfg.providers.get(ref.provider)
        if provider is None:
            continue
        if provider.api_key_env is None:  # e.g. local Ollama
            return "configured"
        if getattr(settings, provider.api_key_env.lower(), None):
            return "configured"
    return "not_configured"


@router.get("/health", response_model=Envelope[HealthData])
async def health() -> JSONResponse:
    from app import __version__

    checks: dict[str, CheckStatus] = {
        "database": await _check_database(),
        "storage": _check_storage(),
        "llm": _check_llm(),
    }
    degraded = "error" in checks.values()
    data = HealthData(status="degraded" if degraded else "ok", version=__version__, checks=checks)
    return JSONResponse(
        status_code=503 if degraded else 200, content=ok(data).model_dump(mode="json")
    )
