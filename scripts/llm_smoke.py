"""Smoke test against real providers, with Thai (RISK-03). Uses a few free-tier requests.

Usage:
    python -m scripts.llm_smoke            # chat + embedding
    python -m scripts.llm_smoke --embed    # embedding only
"""

import argparse
import time
from pathlib import Path

from app.core.aio import run
from app.services.llm.client import get_llm

PUNS = [
    "รองเท้าภาษาอะไรเอ่ย? ตอบ: กีโต้ (รองเท้ากีฬา + โต้)",
    "มะม่วงภาษาอังกฤษว่าอะไร? ตอบ: แมงโก้",
    "สีเหลืองภาษาอังกฤษว่าอะไร? ตอบ: เยลโล่",
    "มีดอะไรเอ่ยใหญ่ที่สุด? ตอบ: อีโต้",
]

# (query, substring expected in the best match)
QUERIES = [
    ("มุกรองเท้า", "กีโต้"),
    ("มะม่วง ภาษาอังกฤษ", "แมงโก้"),
    ("ขอคำอวยพรวันเกิดกวนๆ ให้เพื่อน", "วันเกิด"),
    ("ลูกค้าขอแก้งานไม่จบ", "แก้"),
    ("ขี้เกียจทำงาน", "ประหยัดพลังงาน"),
    ("หัวหน้าสั่งงานด่วน", "หัวหน้า"),
]


def load_docs() -> list[str]:
    docs = list(PUNS)
    for path in sorted(Path("samples").glob("*.txt")):
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        docs += [ln for ln in lines[1:] if ln]  # first line is the title
    return docs


async def smoke_chat() -> None:
    llm = get_llm()
    messages = [
        {"role": "system", "content": "คุณเป็นนักเล่นมุกตลกภาษาไทย ตอบสั้น ไม่เกิน 2 บรรทัด"},
        {"role": "user", "content": "ขอมุกคำพ้องเสียงเกี่ยวกับวันจันทร์ 1 มุก"},
    ]
    t0 = time.perf_counter()
    res = await llm.chat("chat", messages)
    ms = (time.perf_counter() - t0) * 1000
    print(
        f"[chat] {res.provider}/{res.model} {ms:.0f}ms "
        f"in={res.input_tokens} out={res.output_tokens}"
    )
    print(f"  {res.text.strip()}\n")

    t0 = time.perf_counter()
    stream = await llm.stream_chat("chat", messages)
    first = None
    async for _ in stream:
        first = first or (time.perf_counter() - t0) * 1000
    print(
        f"[stream] {stream.provider}/{stream.model} first token {first or 0:.0f}ms, "
        f"in={stream.input_tokens} out={stream.output_tokens}"
    )
    print(f"  {stream.text.strip()}\n")


async def smoke_embed() -> None:
    llm = get_llm()
    docs = load_docs()
    t0 = time.perf_counter()
    doc_vecs = await llm.embed(docs, max_wait_seconds=120)
    q_vecs = await llm.embed([q for q, _ in QUERIES], max_wait_seconds=120)
    ms = (time.perf_counter() - t0) * 1000
    print(
        f"[embed] {llm.embedding_model} {len(docs)} docs + {len(QUERIES)} queries, "
        f"dim={len(doc_vecs[0])}, {ms:.0f}ms\n"
    )

    hits = 0
    for (query, expect), qv in zip(QUERIES, q_vecs, strict=True):
        scored = sorted(
            (
                (sum(a * b for a, b in zip(qv, dv, strict=True)), d)
                for dv, d in zip(doc_vecs, docs, strict=True)
            ),
            reverse=True,
        )
        ok = expect in scored[0][1]
        hits += ok
        print(f"{'✓' if ok else '✗'} {query}")
        for score, doc in scored[:3]:
            print(f"    {score:.3f}  {doc[:60]}")
    print(f"\ntop-1 hit: {hits}/{len(QUERIES)}")

    same = await llm.embed([docs[0]], max_wait_seconds=120)
    print(f"same text cosine: {sum(a * b for a, b in zip(same[0], doc_vecs[0], strict=True)):.4f}")


async def main(embed_only: bool) -> None:
    if not embed_only:
        await smoke_chat()
    await smoke_embed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--embed", action="store_true")
    run(main(parser.parse_args().embed))
