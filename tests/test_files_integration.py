"""Upload -> queue -> convert -> status -> delete (US-B01, B03-B06) against the real
database. Opt-in: see tests/integration.py.
"""

import uuid

from fastapi.testclient import TestClient

from tests.integration import FakeStorage, H, name, sql, upload
from tests.integration import pytestmark as pytestmark


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
    revisions = sql("SELECT reason FROM md_revisions WHERE file_id = :id", id=result["file_id"])
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


def test_unreadable_file_marks_failed(api: TestClient) -> None:
    resp = upload(api, (name("png"), bytes([0x89]) + b"PNG" + uuid.uuid4().hex.encode()))
    file_id = resp.json()["data"]["files"][0]["file_id"]
    got = api.get(f"/api/v1/files/{file_id}", headers=H).json()["data"]
    assert (got["status"], got["error_code"]) == ("failed", "UNREADABLE_IMAGE")
    assert api.get(f"/api/v1/files/{file_id}/markdown", headers=H).status_code == 409


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
        sql(
            "UPDATE files SET status='converting', attempts=:a, md_path=NULL,"
            " locked_at = now() - interval '10 minutes' WHERE id=:id",
            a=attempts,
            id=file_id,
        )
        ids.append(file_id)
    sql("UPDATE files SET updated_at = now() - interval '5 minutes' WHERE id = ANY(:ids)", ids=ids)

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
    assert sql("SELECT count(*) FROM md_revisions WHERE file_id = :id", id=file_id)[0][0] == 0


def test_list_filters_and_paginates(api: TestClient) -> None:
    upload(api, (name(), uuid.uuid4().hex.encode() + b" 1"))
    upload(api, (name(), uuid.uuid4().hex.encode() + b" 2"))
    page = api.get("/api/v1/files?status=pending_review&page_size=1", headers=H).json()["data"]
    assert page["page_size"] == 1 and len(page["items"]) == 1
    assert page["total"] >= 2
    assert all(i["status"] == "pending_review" for i in page["items"])
