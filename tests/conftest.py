import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.db import session

BOOTSTRAP_KEY = "rkk_test_bootstrap"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with no database and a known bootstrap admin key."""
    monkeypatch.setenv("ADMIN_BOOTSTRAP_KEY", BOOTSTRAP_KEY)
    monkeypatch.setenv("DATABASE_URL", "")
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        monkeypatch.setenv(name, "")
    for cached in (
        config.get_settings,
        config.get_app_config,
        session.get_engine,
        session.get_sessionmaker,
    ):
        cached.cache_clear()

    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def storage():
    """In-memory Storage for the integration tests."""
    from tests.integration import FakeStorage

    return FakeStorage()


@pytest.fixture
def fake_llm():
    """Offline LLMClient stand-in for the integration tests."""
    from tests.integration import FakeLLM

    return FakeLLM()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, storage, fake_llm):
    """App wired to the real database (.env), in-memory Storage and a fake LLM."""
    from tests.integration import make_api

    yield from make_api(monkeypatch, storage, fake_llm)
