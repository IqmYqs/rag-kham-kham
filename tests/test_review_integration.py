"""Review flow (US-C04, US-D04) against the real database. Opt-in like
test_files_integration.py:  RKK_INTEGRATION=1 pytest tests/test_review_integration.py
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.integration import FakeStorage, H, name, upload
from tests.integration import pytestmark as pytestmark


def _file(api: TestClient, file_id: str) -> dict:
    return api.get(f"/api/v1/files/{file_id}", headers=H).json()["data"]


def _upload_one(api: TestClient, filename: str, data: bytes) -> str:
    resp = upload(api, (filename, data))
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]["files"][0]["file_id"]


def test_review_edit_approve_reject_flow(api: TestClient, storage: FakeStorage) -> None:
    text = f"มุก {uuid.uuid4().hex}\nโทร 0812345678\n"
    file_id = _upload_one(api, name(), text.encode())

    md = api.get(f"/api/v1/files/{file_id}/markdown", headers=H).json()["data"]
    assert "0812345678" not in md["markdown"]
    assert "[ลบข้อมูล]" in md["markdown"]

    edited = api.put(
        f"/api/v1/files/{file_id}/markdown",
        headers=H,
        json={"markdown": md["markdown"] + "- มุกเพิ่ม a@b.co\n"},
    ).json()["data"]
    assert edited["pii_removed"] == {"email": 1}
    assert storage.objects[f"md/{file_id}.md"].decode().endswith("- มุกเพิ่ม [ลบข้อมูล]\n")

    approved = api.post(f"/api/v1/files/{file_id}/approve", headers=H)
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["approved_at"]
    again = api.post(f"/api/v1/files/{file_id}/approve", headers=H)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "INVALID_STATE"

    # Editing an approved file sends it back for review.
    api.put(f"/api/v1/files/{file_id}/markdown", headers=H, json={"markdown": "แก้อีกรอบ"})
    back = _file(api, file_id)
    assert back["status"] == "pending_review"
    assert back["approved_at"] is None

    revs = api.get(f"/api/v1/files/{file_id}/revisions", headers=H).json()["data"]
    assert [r["reason"] for r in revs] == ["manual_edit", "manual_edit", "convert"]
    assert "+แก้อีกรอบ" in revs[0]["diff"]

    rejected = api.post(f"/api/v1/files/{file_id}/reject", headers=H).json()["data"]
    assert rejected["status"] == "rejected"


def test_rejected_name_can_be_uploaded_again(api: TestClient) -> None:
    n = name()
    first = _upload_one(api, n, uuid.uuid4().hex.encode())
    api.post(f"/api/v1/files/{first}/reject", headers=H)
    assert upload(api, (n, uuid.uuid4().hex.encode())).status_code == 202


def test_xlsx_upload_converts_with_markitdown(api: TestClient, storage: FakeStorage) -> None:
    from tests import fixtures_docs as docs

    data = docs.xlsx({"มุก": [["คำ", "มุก"], ["รองเท้า", uuid.uuid4().hex]]})
    file_id = _upload_one(api, name("xlsx"), data)
    got = _file(api, file_id)
    assert (got["status"], got["convert_method"]) == ("pending_review", "markitdown")
    assert "## มุก\n| คำ | มุก |" in storage.objects[f"md/{file_id}.md"].decode()


def test_reprocess_default_and_llm(
    api: TestClient, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import jobs

    body = f"รองเท้า กีโต้ มะม่วง แมงโก้ {uuid.uuid4().hex}"
    file_id = _upload_one(api, name(), body.encode())
    api.post(f"/api/v1/files/{file_id}/approve", headers=H)

    resp = api.post(f"/api/v1/files/{file_id}/reprocess", headers=H, json={"method": "default"})
    assert resp.status_code == 202
    assert resp.json()["data"]["status"] == "queued"
    got = _file(api, file_id)
    assert (got["status"], got["convert_method"], got["approved_at"]) == (
        "pending_review",
        "text",
        None,
    )

    class FakeLLM:
        async def chat(self, role, messages, **kwargs):
            return SimpleNamespace(text=f"- {body}", model="fake-lite")

    monkeypatch.setattr(jobs, "get_llm", lambda: FakeLLM())
    api.post(f"/api/v1/files/{file_id}/reprocess", headers=H, json={"method": "llm"})
    got = _file(api, file_id)
    assert (got["status"], got["convert_method"]) == ("pending_review", "llm:fake-lite")
    assert storage.objects[f"md/{file_id}.md"].decode() == f"- {body}\n"

    revs = api.get(f"/api/v1/files/{file_id}/revisions", headers=H).json()["data"]
    assert [r["reason"] for r in revs] == ["reprocess", "reprocess", "convert"]
    assert f"+- {body}" in revs[0]["diff"]

    bad = api.post(f"/api/v1/files/{file_id}/reprocess", headers=H, json={"method": "magic"})
    assert bad.status_code == 422


def test_reprocess_not_allowed_while_queued_or_rejected(api: TestClient) -> None:
    file_id = _upload_one(api, name(), uuid.uuid4().hex.encode())
    api.post(f"/api/v1/files/{file_id}/reject", headers=H)
    resp = api.post(f"/api/v1/files/{file_id}/reprocess", headers=H)
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["status"] == "rejected"
    missing = api.post(f"/api/v1/files/{uuid.uuid4()}/approve", headers=H)
    assert missing.status_code == 404
