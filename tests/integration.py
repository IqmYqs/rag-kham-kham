"""Shared helpers for the opt-in integration tests (real database from .env, in-memory
Storage). The `storage` and `api` fixtures live in conftest.py.

    RKK_INTEGRATION=1 pytest tests/test_files_integration.py tests/test_review_integration.py

Every file the tests create is named `__test__...` and removed afterwards.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core import config
from app.core.aio import loop_factory
from app.db import session as db_session
from app.services.llm.client import get_llm
from app.services.storage import ObjectNotFound, get_storage

pytestmark = pytest.mark.skipif(
    os.environ.get("RKK_INTEGRATION") != "1", reason="set RKK_INTEGRATION=1 to run"
)

KEY = "rkk_integration_admin"
H = {"X-API-Key": KEY}


class FakeLLM:
    """Deterministic stand-in for LLMClient: hash-based unit vectors, canned tag JSON."""

    embedding_model = "fake-embedding"

    def __init__(self) -> None:
        self.embedded: list[str] = []
        self.chats: list[str] = []

    async def embed(self, texts: list[str], *, max_wait_seconds: float = 0) -> list[list[float]]:
        import hashlib
        import math

        self.embedded += texts
        out = []
        for t in texts:
            seed = hashlib.sha256(t.encode()).digest()
            vec = [(seed[i % 32] - 128) / 128 for i in range(768)]
            norm = math.sqrt(sum(x * x for x in vec))
            out.append([x / norm for x in vec])
        return out

    async def chat(self, role, messages, **kwargs):
        from types import SimpleNamespace

        self.chats.append(role)
        reply = '{"summary": "มุกทดสอบ", "category": "มุกคำพ้องเสียง", "tags": ["ทดสอบ", "ฮา"]}'
        return SimpleNamespace(text=reply, model="fake-lite")


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        self.objects[path] = data

    async def download(self, path: str) -> bytes:
        if path not in self.objects:
            raise ObjectNotFound("missing", 404)
        return self.objects[path]

    async def delete(self, paths: list[str]) -> None:
        for p in paths:
            self.objects.pop(p, None)

    async def create_signed_upload_url(self, path: str) -> str:
        return f"fake://{path}"


def sql(statement: str, **params: object) -> list:
    async def go() -> list:
        engine = db_session.make_engine(config.get_settings().database_url or "")
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result) if result.returns_rows else []

    return asyncio.run(go(), loop_factory=loop_factory())


def make_api(
    monkeypatch: pytest.MonkeyPatch, storage: FakeStorage, llm: FakeLLM
) -> Iterator[TestClient]:
    monkeypatch.setenv("ADMIN_BOOTSTRAP_KEY", KEY)
    for cached in (
        config.get_settings,
        config.get_app_config,
        db_session.get_engine,
        db_session.get_sessionmaker,
    ):
        cached.cache_clear()
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_llm] = lambda: llm
    with TestClient(app, backend_options={"loop_factory": loop_factory()}) as client:
        yield client
    sql("DELETE FROM files WHERE name LIKE '\\_\\_test\\_\\_%'")


def name(ext: str = "txt") -> str:
    return f"__test__{uuid.uuid4().hex[:8]}.{ext}"


def upload(api: TestClient, *files: tuple[str, bytes], **form: str):
    return api.post(
        "/api/v1/files",
        headers=H,
        files=[("files", (n, data, "application/octet-stream")) for n, data in files],
        data=form,
    )
