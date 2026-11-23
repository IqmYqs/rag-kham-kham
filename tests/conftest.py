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
