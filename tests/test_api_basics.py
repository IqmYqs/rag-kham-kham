from fastapi.testclient import TestClient

from app.core.security import KEY_PREFIX, generate_api_key, hash_api_key
from tests.conftest import BOOTSTRAP_KEY


def test_health_without_key_returns_envelope(client: TestClient) -> None:
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"]["status"] == "ok"
    assert body["data"]["checks"] == {
        "database": "not_configured",
        "storage": "not_configured",
        "llm": "not_configured",
    }
    assert body["meta"]["request_id"] == res.headers["X-Request-ID"]


def test_request_id_is_echoed(client: TestClient) -> None:
    res = client.get("/api/v1/health", headers={"X-Request-ID": "req_abc"})
    assert res.headers["X-Request-ID"] == "req_abc"
    assert res.json()["meta"]["request_id"] == "req_abc"


def test_missing_key_is_401(client: TestClient) -> None:
    res = client.get("/api/v1/auth/me")
    assert res.status_code == 401
    body = res.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_wrong_key_is_401(client: TestClient) -> None:
    res = client.get("/api/v1/auth/me", headers={"X-API-Key": "rkk_wrong"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_bootstrap_key_is_admin(client: TestClient) -> None:
    res = client.get("/api/v1/auth/me", headers={"X-API-Key": BOOTSTRAP_KEY})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["role"] == "admin"
    assert data["owner_name"] == "bootstrap-admin"


def test_unknown_route_uses_envelope(client: TestClient) -> None:
    res = client.get("/api/v1/does-not-exist")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_cors_allows_any_origin(client: TestClient) -> None:
    res = client.options(
        "/api/v1/health",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
    )
    assert res.headers["access-control-allow-origin"] == "*"


def test_generated_keys_are_unique_and_hash_is_stable() -> None:
    a, b = generate_api_key(), generate_api_key()
    assert a != b and a.startswith(KEY_PREFIX)
    assert hash_api_key(a) == hash_api_key(a) != hash_api_key(b)
