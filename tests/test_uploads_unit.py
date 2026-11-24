"""Upload helpers, converter placeholder and endpoint guards (no database)."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ErrorCode
from app.services import uploads as up
from app.services.conversion.converter import ConversionError, convert
from tests.conftest import BOOTSTRAP_KEY

ADMIN = {"X-API-Key": BOOTSTRAP_KEY}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("มุก.txt", "มุก.txt"),
        ("C:\\Users\\me\\มุก.txt", "มุก.txt"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("  a.md  ", "a.md"),
    ],
)
def test_clean_name(raw: str, expected: str) -> None:
    assert up.clean_name(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "x" * 300])
def test_clean_name_rejects(raw: str | None) -> None:
    with pytest.raises(up.Rejection):
        up.clean_name(raw)


def test_detect_mime_by_extension_case_insensitive() -> None:
    assert up.detect_mime("JOKES.XLSX").endswith("spreadsheetml.sheet")
    assert up.detect_mime("a.jpeg") == "image/jpeg"
    with pytest.raises(up.Rejection) as info:
        up.detect_mime("virus.exe")
    assert info.value.status_code == 415
    assert info.value.code == ErrorCode.UNSUPPORTED_FILE_TYPE


def test_check_size() -> None:
    up.check_size(4 * up.MB, 4 * up.MB)
    with pytest.raises(up.Rejection) as info:
        up.check_size(4 * up.MB + 1, 4 * up.MB, hint="ใช้ signed URL")
    assert info.value.status_code == 413
    assert "signed URL" in info.value.message
    with pytest.raises(up.Rejection) as info:
        up.check_size(0, 10)
    assert info.value.status_code == 400


def test_storage_paths_are_ascii() -> None:
    fid = uuid.UUID(int=1)
    assert up.raw_path(fid, "มุกฮา.TXT") == f"raw/{fid}.txt"
    assert up.md_path(fid) == f"md/{fid}.md"


def test_convert_text_utf8_and_cp874() -> None:
    assert convert("\ufeffมุก\r\nฮา".encode(), "a.txt", "text/plain").markdown == "มุก\nฮา\n"
    assert convert("มุก".encode("cp874"), "a.txt", "text/plain").markdown == "มุก\n"


def test_convert_errors_are_permanent() -> None:
    with pytest.raises(ConversionError) as info:
        convert(b"  \n", "a.txt", "text/plain")
    assert info.value.code == "EMPTY_CONTENT"
    with pytest.raises(ConversionError):
        convert(b"%PDF", "a.pdf", "application/pdf")


def test_file_endpoints_require_admin(client: TestClient) -> None:
    assert client.get("/api/v1/files").status_code == 401
    # Without a database the admin request gets as far as the DB dependency.
    resp = client.get("/api/v1/files", headers=ADMIN)
    assert resp.status_code == 500
    assert "DATABASE_URL" in resp.json()["error"]["message"]


def test_uploads_request_is_validated(client: TestClient) -> None:
    from app.db.session import get_db

    async def no_db():
        yield None

    from app.services.storage import get_storage

    client.app.dependency_overrides[get_db] = no_db  # type: ignore[attr-defined]
    client.app.dependency_overrides[get_storage] = lambda: None  # type: ignore[attr-defined]
    resp = client.post("/api/v1/files/uploads", headers=ADMIN, json={"files": []})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_cron_requires_secret(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get("/api/v1/internal/cron").status_code == 401  # no secret configured
    from app.core import config

    monkeypatch.setenv("CRON_SECRET", "s3cret")
    config.get_settings.cache_clear()
    bad = client.get("/api/v1/internal/cron", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    good = client.get("/api/v1/internal/cron", headers={"Authorization": "Bearer s3cret"})
    assert good.status_code == 500  # authorised; fails later only because there is no DB
