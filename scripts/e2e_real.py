"""End-to-end check against the REAL services (Supabase DB + Storage, Gemini).

Uploads one sample file as `__e2e__<name>`, waits for conversion, approves it, waits for
embeddings + tags, runs a semantic search for one query, then deletes the file.
Uses ~25 embedding requests and 1 tagging call from the free tier.

Usage:
    python -m scripts.e2e_real [path/to/file.txt] [--query "..."] [--keep]
"""

import argparse
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.aio import loop_factory, run
from app.core.config import get_settings
from app.db.session import make_engine
from app.services.llm.client import get_llm

KEY = "rkk_e2e_admin"


def main(path: Path, query: str, keep: bool) -> None:
    os.environ["ADMIN_BOOTSTRAP_KEY"] = KEY
    from app.main import create_app

    h = {"X-API-Key": KEY}
    with TestClient(create_app(), backend_options={"loop_factory": loop_factory()}) as api:
        t0 = time.perf_counter()
        resp = api.post(
            "/api/v1/files",
            headers=h,
            files=[("files", (f"__e2e__{path.name}", path.read_bytes(), "text/plain"))],
        )
        print("upload:", resp.status_code, resp.json()["data"]["files"][0])
        file_id = resp.json()["data"]["files"][0]["file_id"]
        try:
            got = api.get(f"/api/v1/files/{file_id}", headers=h).json()["data"]
            print("after conversion:", got["status"], got["convert_method"], got["error_code"])
            md = api.get(f"/api/v1/files/{file_id}/markdown", headers=h).json()["data"]["markdown"]
            print("markdown preview:", md[:120].replace("\n", " / "))

            approved = api.post(f"/api/v1/files/{file_id}/approve", headers=h).json()["data"]
            print("approve:", approved["status"], approved["index"])
            got = api.get(f"/api/v1/files/{file_id}", headers=h).json()["data"]
            print(
                "after background:",
                got["index"],
                "| category:",
                got["category"],
                "| tags:",
                got["tags"],
                "| summary:",
                got["summary"],
            )
            print(f"total {time.perf_counter() - t0:.1f}s")
            run(search(file_id, query))
        finally:
            if not keep:
                deleted = api.delete(f"/api/v1/files/{file_id}", headers=h)
                print("delete:", deleted.status_code)


async def search(file_id: str, query: str) -> None:
    from app.services.thai import search_text

    qvec = (await get_llm().embed([query]))[0]
    engine = make_engine(get_settings().database_url or "")
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT content, 1 - (embedding <=> CAST(:q AS vector)) AS cosine FROM chunks"
                " WHERE file_id = :id ORDER BY embedding <=> CAST(:q AS vector) LIMIT 3"
            ),
            {"q": str(qvec), "id": file_id},
        )
        print(f"vector search '{query}':")
        for content, cosine in rows:
            print(f"  {cosine:.3f}  {content}")
        rows = await conn.execute(
            text(
                "SELECT content FROM chunks WHERE file_id = :id"
                " AND tsv @@ plainto_tsquery('simple', :q) LIMIT 3"
            ),
            {"q": search_text(query), "id": file_id},
        )
        print(f"full-text '{search_text(query)}':", [r[0] for r in rows])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=sorted(Path("samples").glob("*.txt"))[2])
    parser.add_argument("--query", default="ลูกค้าขอแก้งาน")
    parser.add_argument("--keep", action="store_true", help="keep the file in the library")
    args = parser.parse_args()
    main(Path(args.path), args.query, args.keep)
