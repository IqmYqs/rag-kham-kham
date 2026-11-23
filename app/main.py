"""FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1 import api_router
from app.core.config import get_app_config, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import RequestIdMiddleware, setup_logging


def create_app() -> FastAPI:
    settings = get_settings()
    cfg = get_app_config()
    setup_logging(settings.log_level)

    app = FastAPI(
        title="rag-kham-kham",
        description="RAG API สำหรับค้นหาและแต่งมุกขำๆ ภาษาไทย",
        version=__version__,
    )

    allow_all = cfg.cors.allow_origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors.allow_origins,
        # Browsers reject credentials with a wildcard origin; auth is via X-API-Key anyway.
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_middleware(RequestIdMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
