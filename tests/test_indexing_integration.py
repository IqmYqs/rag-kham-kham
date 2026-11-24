"""Approve -> chunks -> full-text + vectors -> tags (US-C05) against the real database, with
a fake LLM. Opt-in: see tests/integration.py.
"""

import uuid

from fastapi.testclient import TestClient

from tests.integration import FakeLLM, H, name, sql, upload
from tests.integration import pytestmark as pytestmark


def _approved(api: TestClient, text: str, **form: str) -> str:
    resp = upload(api, (name(), text.encode()), **form)
    file_id = resp.json()["data"]["files"][0]["file_id"]
    approved = api.post(f"/api/v1/files/{file_id}/approve", headers=H)
    assert approved.status_code == 200, approved.text
    return file_id


def test_approve_indexes_embeds_and_tags(api: TestClient, fake_llm: FakeLLM) -> None:
    marker = uuid.uuid4().hex[:10]
    text = f"มุกคำพ้อง {marker}\n\nรองเท้าภาษาอะไร กีโต้\nมะม่วงภาษาอังกฤษ แมงโก้\nโทร 0812345678 ได้\n"
    file_id = _approved(api, text)

    got = api.get(f"/api/v1/files/{file_id}", headers=H).json()["data"]
    assert got["status"] == "approved"
    assert got["index"] == {"chunks": 3, "embedded": 3}  # background task already ran
    assert got["summary"] == "มุกทดสอบ"
    assert (got["category"], got["tags"], got["tags_source"]) == (
        "มุกคำพ้องเสียง",
        ["ทดสอบ", "ฮา"],
        "ai",
    )

    rows = sql(
        "SELECT content, search_text, location, embedding_model FROM chunks"
        " WHERE file_id = :id ORDER BY seq",
        id=file_id,
    )
    assert [r[0] for r in rows] == [
        "รองเท้าภาษาอะไร กีโต้",
        "มะม่วงภาษาอังกฤษ แมงโก้",
        "โทร [ลบข้อมูล] ได้",
    ]
    assert rows[0][1] == "รองเท้า ภาษา อะไร กี โต้"
    assert rows[0][2] == {"section": f"มุกคำพ้อง {marker}"}
    assert rows[0][3] == "fake-embedding"
    assert fake_llm.embedded[-3:] == [r[0] for r in rows]

    # Full-text: a pun ending found through the tokenised index.
    hits = sql(
        "SELECT content FROM chunks WHERE file_id = :id AND tsv @@ plainto_tsquery('simple', 'โต้')",
        id=file_id,
    )
    assert [h[0] for h in hits] == ["รองเท้าภาษาอะไร กีโต้"]

    # Vector: the stored embedding is the nearest neighbour of itself.
    nearest = sql(
        "SELECT c.content FROM chunks c, (SELECT embedding FROM chunks WHERE file_id = :id"
        " ORDER BY seq LIMIT 1) q WHERE c.file_id = :id"
        " ORDER BY c.embedding <=> q.embedding LIMIT 1",
        id=file_id,
    )
    assert nearest[0][0] == "รองเท้าภาษาอะไร กีโต้"


def test_user_category_and_tags_win(api: TestClient) -> None:
    file_id = _approved(
        api, f"มุก {uuid.uuid4().hex}\nบรรทัดสอง ฮาๆ\n", category="หมวดของฉัน", tags="ก, ข"
    )
    got = api.get(f"/api/v1/files/{file_id}", headers=H).json()["data"]
    assert (got["category"], got["tags"], got["tags_source"]) == ("หมวดของฉัน", ["ก", "ข"], "user")
    assert got["summary"] == "มุกทดสอบ"


def test_edit_and_reprocess_drop_chunks_until_reapproved(api: TestClient) -> None:
    file_id = _approved(api, f"มุก {uuid.uuid4().hex}\nบรรทัดสอง ฮาๆ\n")
    count = "SELECT count(*) FROM chunks WHERE file_id = :id"
    assert sql(count, id=file_id)[0][0] == 2

    api.put(f"/api/v1/files/{file_id}/markdown", headers=H, json={"markdown": "- แก้แล้ว ฮา"})
    assert sql(count, id=file_id)[0][0] == 0
    api.post(f"/api/v1/files/{file_id}/approve", headers=H)
    assert sql(count, id=file_id)[0][0] == 1

    api.post(f"/api/v1/files/{file_id}/reprocess", headers=H)
    assert sql(count, id=file_id)[0][0] == 0


def test_approve_with_nothing_indexable_is_refused(api: TestClient) -> None:
    resp = upload(api, (name(), f"มุก {uuid.uuid4().hex}".encode()))
    file_id = resp.json()["data"]["files"][0]["file_id"]
    api.put(f"/api/v1/files/{file_id}/markdown", headers=H, json={"markdown": "---"})
    refused = api.post(f"/api/v1/files/{file_id}/approve", headers=H)
    assert refused.status_code == 409
    got = api.get(f"/api/v1/files/{file_id}", headers=H).json()["data"]
    assert got["status"] == "pending_review"  # approval rolled back


def test_unfinished_embeddings_resume_on_status_check(api: TestClient) -> None:
    file_id = _approved(api, f"มุก {uuid.uuid4().hex}\nบรรทัดสอง ฮาๆ\nบรรทัดสาม ฮาๆ\n")
    sql("UPDATE chunks SET embedding = NULL WHERE file_id = :id", id=file_id)
    api.get("/api/v1/files?page_size=1", headers=H)  # recovery schedules the embedding run
    got = api.get(f"/api/v1/files/{file_id}", headers=H).json()["data"]
    assert got["index"] == {"chunks": 3, "embedded": 3}
