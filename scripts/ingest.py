"""Upload files into the real library, approve them and wait for embeddings.

Runs the app in-process (no server needed) with the bootstrap admin key from .env or a
temporary one. Files that are already in the library are skipped (409).

Usage:
    python -m scripts.ingest samples/*.txt
    python -m scripts.ingest jokes.xlsx --category "มุกคำพ้องเสียง" --tags "คำพ้อง,เสียง"
    python -m scripts.ingest a.txt --no-approve      # stop at pending_review
"""

import argparse
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.aio import loop_factory

TEMP_KEY = "rkk_ingest_admin"


def main(paths: list[Path], approve: bool, form: dict[str, str]) -> None:
    key = os.environ.get("ADMIN_BOOTSTRAP_KEY") or TEMP_KEY
    os.environ["ADMIN_BOOTSTRAP_KEY"] = key
    from app.main import create_app

    h = {"X-API-Key": key}
    with TestClient(create_app(), backend_options={"loop_factory": loop_factory()}) as api:
        for path in paths:
            resp = api.post(
                "/api/v1/files",
                headers=h,
                files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))],
                data=form,
            )
            body = resp.json()
            if resp.status_code != 202:
                err = body["error"]
                print(f"skip  {path.name}: {err['code']} {err['message']}")
                continue
            file_id = body["data"]["files"][0]["file_id"]
            got = api.get(f"/api/v1/files/{file_id}", headers=h).json()["data"]
            if got["status"] != "pending_review":
                print(
                    f"fail  {path.name}: {got['status']} {got['error_code']} {got['error_message']}"
                )
                continue
            if not approve:
                print(f"review {path.name}: {file_id} (pending_review)")
                continue
            t0 = time.perf_counter()
            result = api.post(f"/api/v1/files/{file_id}/approve", headers=h).json()
            if not result.get("success"):
                print(f"fail  {path.name}: {result['error']['message']}")
                continue
            got = api.get(f"/api/v1/files/{file_id}", headers=h).json()["data"]
            print(
                f"ok    {path.name}: {got['index']} category={got['category']!r} "
                f"tags={got['tags']} ({time.perf_counter() - t0:.0f}s)"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--no-approve", action="store_true")
    parser.add_argument("--category")
    parser.add_argument("--tags", help="comma-separated")
    args = parser.parse_args()
    form = {k: v for k, v in {"category": args.category, "tags": args.tags}.items() if v}
    main(args.paths, not args.no_approve, form)
