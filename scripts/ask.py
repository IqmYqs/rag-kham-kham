"""Ask the real library from the command line (in-process app, real DB + Gemini).

Usage:
    python -m scripts.ask "รองเท้า" "ขอมุกเกี่ยวกับมะม่วงหน่อย"
    python -m scripts.ask --mode library_only "วันนี้เหนื่อยจัง"
"""

import argparse
import os

from fastapi.testclient import TestClient

from app.core.aio import loop_factory

TEMP_KEY = "rkk_cli_admin"


def main(questions: list[str], mode: str) -> None:
    key = os.environ.get("ADMIN_BOOTSTRAP_KEY") or TEMP_KEY
    os.environ["ADMIN_BOOTSTRAP_KEY"] = key
    from app.main import create_app

    with TestClient(create_app(), backend_options={"loop_factory": loop_factory()}) as api:
        for q in questions:
            resp = api.post(
                "/api/v1/ask", headers={"X-API-Key": key}, json={"message": q, "mode": mode}
            )
            body = resp.json()
            if not body["success"]:
                print(f"\n❓ {q}\n   ERROR {body['error']}")
                continue
            d = body["data"]
            u = d["usage"]
            print(f"\n❓ {q}")
            print(
                f"   [{d['intent']} → {d['source_stage']}{' direct' if d['direct'] else ''}"
                f"{' AI' if d['is_ai_generated'] else ''}] tried={d['stages_tried']}"
                f" cos={d['confidence']['retrieval']} conf={d['confidence']['llm']}"
                f" tokens={u['input_tokens']}+{u['output_tokens']} {d['latency_ms']}ms"
            )
            for line in d["answer"].splitlines():
                print(f"   │ {line}")
            if d["notice"]:
                print(f"   ⚠ {d['notice']}")
            for c in d["citations"]:
                print(f"   📄 {c['file_name']} {c['location'] or ''}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("questions", nargs="+")
    parser.add_argument("--mode", default="auto", choices=["auto", "ai_only", "library_only"])
    args = parser.parse_args()
    main(args.questions, args.mode)
