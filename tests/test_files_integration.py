"""Upload -> queue -> convert -> status -> delete against the real database (.env DATABASE_URL)
with an in-memory Storage. Opt-in because it writes to the database:

    RKK_INTEGRATION=1 pytest tests/test_files_integration.py

Every file it creates is named `__test__...` and removed afterwards.
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
from app.services.storage import ObjectNotFound, get_storage

pytestmark = pytest.mark.skipif(
    os.environ.get("RKK_INTEGRATION") != "1", reason="set RKK_INTEGRATION=1 to run"
)

KEY = "rkk_integration_admin"
H = {"X-API-Key": KEY}


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


def _sql(statement: str, **params: object) -> list:
    async def go() -> list:
        engine = db_session.make_engine(config.get_settings().database_url or "")
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result) if result.returns_rows else []

    return asyncio.run(go(), loop_factory=loop_factory())


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, storage: FakeStorage) -> Iterator[TestClient]:
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
    with TestClient(app, backend_options={"loop_factory": loop_factory()}) as client:
        yield client
    _sql("DELETE FROM files WHERE name LIKE '\\_\\_test\\_\\_%'")


def name(ext: str = "txt") -> str:
    return f"__test__{uuid.uuid4().hex[:8]}.{ext}"


def upload(api: TestClient, *files: tuple[str, bytes], **form: str):
    return api.post(
        "/api/v1/files",
        headers=H,
        files=[("files", (n, data, "application/octet-stream")) for n, data in files],
        data=form,
    )


def test_multipart_upload_converts_to_pending_review(api: TestClient, storage: FakeStorage) -> None:
    n = name()
    body = "มุกที่ 1\r\nรองเท้าภาษาอะไร กีโต้\n".encode()
    resp = upload(api, (n, body + uuid.uuid4().hex.encode()), category="ทดสอบ", tags="a, b")
    assert resp.status_code == 202, resp.text
    result = resp.json()["data"]["files"][0]
    assert result["status"] == "queued"

    # TestClient runs background tasks before returning, so conversion is already done.
    got = api.get(f"/api/v1/files/{result['file_id']}", headers=H).json()["data"]
    assert got["status"] == "pending_review"
    assert got["convert_method"] == "text"
    assert got["attempts"] == 1
    assert (got["category"], got["tags"]) == ("ทดสอบ", ["a", "b"])
    md = storage.objects[f"md/{result['file_id']}.md"].decode()
    assert md.startswith("มุกที่ 1\nรองเท้า")
    revisions = _sql("SELECT reason FROM md_revisions WHERE file_id = :id", id=result["file_id"])
    assert [r[0] for r in revisions] == ["convert"]


def test_duplicates_are_rejected_with_existing_id(api: TestClient) -> None:
    n, data = name(), uuid.uuid4().hex.encode() + b" joke"
    first = upload(api, (n, data)).json()["data"]["files"][0]["file_id"]

    same_name = upload(api, (n, b"different " + uuid.uuid4().hex.encode()))
    assert same_name.status_code == 409
    assert same_name.json()["error"]["code"] == "FILE_DUPLICATE"

    same_content = upload(api, (name(), data))
    assert same_content.status_code == 409
    details = same_content.json()["error"]["details"]["files"][0]["error"]["details"]
    assert details["file_id"] == first


def test_mixed_batch_reports_per_file(api: TestClient) -> None:
    good, big = name(), name()
    resp = upload(
        api,
        (good, uuid.uuid4().hex.encode() + b" ok"),
        ("__test__virus.exe", b"MZ"),
        (big, b"x" * (4 * 1024 * 1024 + 1)),
    )
    assert resp.status_code == 202
    files = {f["name"]: f for f in resp.json()["data"]["files"]}
    assert files[good]["status"] == "queued"
    assert files["__test__virus.exe"]["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    assert files[big]["error"]["code"] == "FILE_TOO_LARGE"
    assert "uploads" in files[big]["error"]["message"]


def test_unsupported_converter_marks_failed(api: TestClient) -> None:
    resp = upload(api, (name("pdf"), b"%PDF-1.4 " + uuid.uuid4().hex.encode()))
    file_id = resp.json()["data"]["files"][0]["file_id"]
    got = api.get(f"/api/v1/files/{file_id}", headers=H).json()["data"]
    assert (got["status"], got["error_code"]) == ("failed", "CONVERSION_FAILED")


def test_signed_url_flow(api: TestClient, storage: FakeStorage) -> None:
    n, data = name(), "ไฟล์ใหญ่ ".encode() + uuid.uuid4().hex.encode()
    resp = api.post(
        "/api/v1/files/uploads", headers=H, json={"files": [{"name": n, "size_bytes": len(data)}]}
    )
    assert resp.status_code == 202, resp.text
    item = resp.json()["data"]["files"][0]
    assert item["status"] == "awaiting_upload"
    assert item["upload_url"].startswith("fake://raw/")

    early = api.post("/api/v1/files/complete", headers=H, json={"file_ids": [item["file_id"]]})
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "INVALID_STATE"

    storage.objects[item["upload_url"].removeprefix("fake://")] = data  # the client's PUT
    done = api.post("/api/v1/files/complete", headers=H, json={"file_ids": [item["file_id"]]})
    assert done.status_code == 202
    assert done.json()["data"]["files"][0]["status"] == "queued"
    got = api.get(f"/api/v1/files/{item['file_id']}", headers=H).json()["data"]
    assert got["status"] == "pending_review"
    assert got["size_bytes"] == len(data)

    again = api.post("/api/v1/files/complete", headers=H, json={"file_ids": [item["file_id"]]})
    assert again.status_code == 202  # idempotent


def test_signed_url_retry_replaces_abandoned_request(api: TestClient) -> None:
    n = name()
    req = {"files": [{"name": n, "size_bytes": 10}]}
    first = api.post("/api/v1/files/uploads", headers=H, json=req).json()["data"]["files"][0]
    second = api.post("/api/v1/files/uploads", headers=H, json=req)
    assert second.status_code == 202
    assert api.get(f"/api/v1/files/{first['file_id']}", headers=H).status_code == 404


def test_signed_url_rejects_oversize_before_upload(api: TestClient) -> None:
    resp = api.post(
        "/api/v1/files/uploads",
        headers=H,
        json={"files": [{"name": name(), "size_bytes": 21 * 1024 * 1024}]},
    )
    assert resp.status_code == 413


def test_stuck_jobs_are_retried_then_failed(api: TestClient, storage: FakeStorage) -> None:
    ids = []
    for attempts in (1, 3):
        resp = upload(api, (name(), uuid.uuid4().hex.encode() + b" stuck"))
        file_id = resp.json()["data"]["files"][0]["file_id"]
        _sql(
            "UPDATE files SET status='converting', attempts=:a, md_path=NULL,"
            " locked_at = now() - interval '10 minutes' WHERE id=:id",
            a=attempts,
            id=file_id,
        )
        ids.append(file_id)
    _sql("UPDATE files SET updated_at = now() - interval '5 minutes' WHERE id = ANY(:ids)", ids=ids)

    api.get("/api/v1/files?page_size=1", headers=H)  # triggers recovery (+ background rerun)
    retried, gave_up = (api.get(f"/api/v1/files/{i}", headers=H).json()["data"] for i in ids)
    assert retried["status"] in ("queued", "pending_review")  # requeued, maybe already rerun
    assert (gave_up["status"], gave_up["error_code"]) == ("failed", "TIMEOUT")


def test_delete_removes_everything(api: TestClient, storage: FakeStorage) -> None:
    resp = upload(api, (name(), uuid.uuid4().hex.encode() + b" bye"))
    file_id = resp.json()["data"]["files"][0]["file_id"]
    assert any(file_id in p for p in storage.objects)

    deleted = api.delete(f"/api/v1/files/{file_id}", headers=H)
    assert deleted.status_code == 200
    assert api.get(f"/api/v1/files/{file_id}", headers=H).status_code == 404
    assert not any(file_id in p for p in storage.objects)
    assert _sql("SELECT count(*) FROM md_revisions WHERE file_id = :id", id=file_id)[0][0] == 0


def test_list_filters_and_paginates(api: TestClient) -> None:
    upload(api, (name(), uuid.uuid4().hex.encode() + b" 1"))
    upload(api, (name(), uuid.uuid4().hex.encode() + b" 2"))
    page = api.get("/api/v1/files?status=pending_review&page_size=1", headers=H).json()["data"]
    assert page["page_size"] == 1 and len(page["items"]) == 1
    assert page["total"] >= 2
    assert all(i["status"] == "pending_review" for i in page["items"])
