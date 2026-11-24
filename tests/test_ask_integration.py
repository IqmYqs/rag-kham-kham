"""/ask and /random against the real database (hybrid-search SQL, scope, answer records),
with a fake LLM. Opt-in: see tests/integration.py. The real library may hold other files,
so every test uses unique marker words.
"""

import random
import string
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.security import generate_api_key, hash_api_key
from tests.integration import H, name, sql, upload
from tests.integration import pytestmark as pytestmark


def _word() -> str:
    """A made-up keyword that tokenises as one word and matches nothing else."""
    return "zq" + "".join(random.choice(string.ascii_lowercase) for _ in range(8))


def _library_file(api: TestClient, rows: list[tuple[str, str]], **form: str) -> str:
    csv = "คำ,มุก\n" + "".join(f"{k},{v}\n" for k, v in rows)
    resp = upload(api, (name("csv"), csv.encode()), **form)
    assert resp.status_code == 202, resp.text
    file_id = resp.json()["data"]["files"][0]["file_id"]
    assert api.post(f"/api/v1/files/{file_id}/approve", headers=H).status_code == 200
    return file_id


@pytest.fixture
def team_user(api: TestClient) -> Iterator[tuple[dict, str]]:
    """A real `user` API key in a new team: ({X-API-Key}, team_id)."""
    team = sql(
        "INSERT INTO teams (name) VALUES (:n) RETURNING id", n=f"__test__team_{uuid.uuid4().hex}"
    )[0][0]
    raw = generate_api_key()
    key_id = sql(
        "INSERT INTO api_keys (key_prefix, key_hash, owner_name, role, team_id)"
        " VALUES (:p, :h, '__test__user', 'user', :t) RETURNING id",
        p=raw[:8],
        h=hash_api_key(raw),
        t=team,
    )[0][0]
    yield {"X-API-Key": raw}, str(team)
    sql("DELETE FROM answers WHERE api_key_id = :k", k=key_id)
    sql("DELETE FROM api_keys WHERE id = :k", k=key_id)
    sql("DELETE FROM files WHERE team_id = :t", t=team)
    sql("DELETE FROM teams WHERE id = :t", t=team)


def test_lookup_is_answered_directly_from_the_library(api: TestClient) -> None:
    word, joke = _word(), "มุก" + uuid.uuid4().hex[:6]
    file_id = _library_file(api, [(word, joke), (_word(), "อย่างอื่น")])

    resp = api.post("/api/v1/ask", headers=H, json={"message": word})
    assert resp.status_code == 200, resp.text
    d = resp.json()["data"]
    assert d["intent"] == "lookup"
    assert (d["source_stage"], d["direct"], d["is_ai_generated"]) == ("md", True, False)
    assert d["answer"] == f"{word} → {joke}"
    assert d["usage"]["input_tokens"] == 0
    assert d["citations"][0]["file_id"] == file_id
    assert d["citations"][0]["location"] == {"row": 1}
    assert d["answer_id"] is None  # bootstrap admin: not recorded


def test_user_scope_and_answer_record(api: TestClient, team_user: tuple[dict, str]) -> None:
    headers, team_id = team_user
    global_word, team_word, other_word = _word(), _word(), _word()
    _library_file(api, [(global_word, "มุกส่วนกลาง")])
    _library_file(api, [(team_word, "มุกของทีม")], scope="team", team_id=team_id)

    # A team the user does not belong to.
    other_team = sql(
        "INSERT INTO teams (name) VALUES (:n) RETURNING id", n=f"__test__team_{uuid.uuid4().hex}"
    )[0][0]
    try:
        _library_file(api, [(other_word, "มุกทีมอื่น")], scope="team", team_id=str(other_team))

        for word, expected in ((global_word, "มุกส่วนกลาง"), (team_word, "มุกของทีม")):
            d = api.post("/api/v1/ask", headers=headers, json={"message": word}).json()["data"]
            assert d["direct"] and d["answer"].endswith(expected)

        d = api.post(
            "/api/v1/ask", headers=headers, json={"message": other_word, "mode": "library_only"}
        ).json()["data"]
        assert "มุกทีมอื่น" not in d["answer"]
        assert all("ทีมอื่น" not in c["content"] for c in d["citations"])
    finally:
        sql("DELETE FROM files WHERE team_id = :t", t=other_team)
        sql("DELETE FROM teams WHERE id = :t", t=other_team)

    # Answers and usage are recorded for real keys.
    stored = sql(
        "SELECT a.question, a.source_stage, a.chunk_ids, u.requests FROM answers a"
        " JOIN usage_daily u ON u.api_key_id = a.api_key_id"
        " WHERE a.question = :q",
        q=global_word,
    )
    assert stored and stored[0][1] == "md" and len(stored[0][2]) == 1
    assert stored[0][3] >= 3


def test_random_respects_scope(api: TestClient, team_user: tuple[dict, str]) -> None:
    headers, _ = team_user
    resp = api.get("/api/v1/random", headers=headers)
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert resp.json()["data"]["content"]

    word = _word()
    _library_file(api, [(word, "มุกสุ่ม")], category="หมวด" + word)
    got = api.get("/api/v1/random", headers=H, params={"category": "หมวด" + word}).json()["data"]
    assert got["content"] == f"คำ: {word} | มุก: มุกสุ่ม"
    assert api.get("/api/v1/random", headers=H, params={"category": "ไม่มีหมวดนี้"}).status_code == 404


def test_ask_requires_a_key_and_a_message(api: TestClient) -> None:
    assert api.post("/api/v1/ask", json={"message": "x"}).status_code == 401
    assert api.post("/api/v1/ask", headers=H, json={"message": ""}).status_code == 422
    bad = api.post("/api/v1/ask", headers=H, json={"message": "x", "mode": "magic"})
    assert bad.status_code == 422
