"""Hybrid search: full-text + vector, fused with RRF (US-D02, DESIGN.md 6.3).

- Full-text uses OR over the query's content words (PyThaiNLP tokens minus stopwords) and
  ranks with ts_rank_cd; AND (`plainto_tsquery`) found nothing for natural questions.
- Vector search uses the HNSW cosine index. Chunks still waiting for an embedding are only
  reachable through full-text.
- Only `approved` files in the caller's scope: admins see everything, users see global
  files plus their team's (US-A02).
- RRF scores are not calibrated, so decisions use the best cosine plus `exact` (the
  query word is the left-hand side of a chunk, e.g. "คำ: รองเท้า | มุก: กีโต้").
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.services.thai import search_tokens

logger = logging.getLogger(__name__)

# Words that say "I want a joke" rather than what the joke is about.
DOMAIN_STOPWORDS = {
    "มุก", "มุข", "ตลก", "ขำ", "ฮา", "ขำๆ", "แต่ง", "คิด", "สร้าง", "แนว", "หน่อย",
    "joke", "jokes", "เล่า", "ช่วย", "เกี่ยว", "กับ", "บ้าง", "ที", "นะ", "ครับ", "ค่ะ", "คะ",
    "แคป", "ชั่น", "แคปชั่น", "กี่", "จำนวน", "ทั้งหมด", "เรื่อง", "ใหม่", "อัน",
}  # fmt: skip


@lru_cache
def _stopwords() -> frozenset[str]:
    from pythainlp.corpus import thai_stopwords

    return frozenset(thai_stopwords()) | DOMAIN_STOPWORDS


def content_terms(query: str) -> list[str]:
    """Query words worth matching on, in order, without duplicates."""
    stop = _stopwords()
    return list(dict.fromkeys(t for t in search_tokens(query) if t not in stop))


def tsquery_or(terms: list[str]) -> str:
    """`'a' | 'b'` for to_tsquery('simple', ...), each term quoted as a literal lexeme."""
    return " | ".join("'" + t.replace("\\", "\\\\").replace("'", "''") + "'" for t in terms)


@dataclass
class Hit:
    chunk_id: uuid.UUID
    file_id: uuid.UUID
    file_name: str
    content: str
    location: dict[str, Any] | None
    cosine: float | None = None
    ft_rank: int | None = None
    vec_rank: int | None = None
    rrf: float = 0.0
    exact: bool = False


@dataclass
class SearchResult:
    hits: list[Hit]
    terms: list[str]
    vector_used: bool
    candidates: list[Hit] = field(default_factory=list)

    @property
    def best_cosine(self) -> float:
        return max((h.cosine or 0.0 for h in self.hits), default=0.0)

    @property
    def exact_hits(self) -> list[Hit]:
        return [h for h in self.hits if h.exact]


def _scope_sql(principal: Principal) -> tuple[str, dict[str, Any]]:
    if principal.role == "admin":
        return "f.status = 'approved'", {}
    if principal.team_id is None:
        return "f.status = 'approved' AND f.scope = 'global'", {}
    return (
        "f.status = 'approved' AND (f.scope = 'global' OR f.team_id = :team_id)",
        {"team_id": principal.team_id},
    )


_LABELLED = re.compile(r"^[^|:?]{1,30}:\s*([^|]+)")  # "คำ: รองเท้า | มุก: กีโต้"
_KEY_END = re.compile(r"\s*(?:\||=|→|\?|,|:)\s*")


def chunk_key(content: str) -> str:
    """The part of a chunk that names its subject: first table cell, or text before =/?/:"""
    content = content.strip()
    if m := _LABELLED.match(content):
        return m.group(1).strip()
    return _KEY_END.split(content, maxsplit=1)[0]


def is_exact(content: str, terms: list[str]) -> bool:
    """The query words open the chunk's key, e.g. "รองเท้า" for "รองเท้าภาษาอะไร? กีโต้"."""
    if not terms:
        return False
    key = [t for t in search_tokens(chunk_key(content)) if t not in _stopwords()]
    return key[: len(terms)] == terms


async def hybrid_search(
    session: AsyncSession,
    principal: Principal,
    query: str,
    query_vector: list[float] | None,
    *,
    top_k: int = 4,
    candidates: int = 20,
    rrf_k: int = 60,
    exclude_chunk_ids: list[uuid.UUID] | None = None,
) -> SearchResult:
    scope, params = _scope_sql(principal)
    exclude = ""
    if exclude_chunk_ids:
        exclude = " AND c.id <> ALL(:exclude)"
        params["exclude"] = exclude_chunk_ids
    terms = content_terms(query)
    found: dict[uuid.UUID, Hit] = {}

    def hit(row: Any) -> Hit:
        h = found.get(row.id)
        if h is None:
            h = Hit(row.id, row.file_id, row.name, row.content, row.location)
            found[row.id] = h
        return h

    if terms:
        rows = await session.execute(
            text(
                f"""
                SELECT c.id, c.file_id, f.name, c.content, c.location,
                       ts_rank_cd(c.tsv, q) AS rank
                  FROM chunks c JOIN files f ON f.id = c.file_id,
                       to_tsquery('simple', :tsq) q
                 WHERE c.tsv @@ q AND {scope}{exclude}
                 ORDER BY rank DESC, c.seq
                 LIMIT :n
                """
            ),
            {**params, "tsq": tsquery_or(terms), "n": candidates},
        )
        for rank, row in enumerate(rows, start=1):
            hit(row).ft_rank = rank

    if query_vector is not None:
        rows = await session.execute(
            text(
                f"""
                SELECT c.id, c.file_id, f.name, c.content, c.location,
                       1 - (c.embedding <=> CAST(:qvec AS vector)) AS cosine
                  FROM chunks c JOIN files f ON f.id = c.file_id
                 WHERE c.embedding IS NOT NULL AND {scope}{exclude}
                 ORDER BY c.embedding <=> CAST(:qvec AS vector)
                 LIMIT :n
                """
            ),
            {**params, "qvec": str(query_vector), "n": candidates},
        )
        for rank, row in enumerate(rows, start=1):
            h = hit(row)
            h.vec_rank, h.cosine = rank, float(row.cosine)

    for h in found.values():
        h.rrf = sum(1 / (rrf_k + r) for r in (h.ft_rank, h.vec_rank) if r is not None)
        h.exact = is_exact(h.content, terms)
    ranked = sorted(found.values(), key=lambda h: (h.exact, h.rrf), reverse=True)
    return SearchResult(
        hits=ranked[:top_k], terms=terms, vector_used=query_vector is not None, candidates=ranked
    )


async def pattern_examples(
    session: AsyncSession, principal: Principal, fragment: str, *, limit: int = 8
) -> list[Hit]:
    """Chunks containing a sound pattern such as "โต้" (RISK-06): for 'แต่งมุกแนว -โต้'."""
    scope, params = _scope_sql(principal)
    escaped = fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = await session.execute(
        text(
            f"""
            SELECT c.id, c.file_id, f.name, c.content, c.location
              FROM chunks c JOIN files f ON f.id = c.file_id
             WHERE c.content ILIKE :pattern AND {scope}
             LIMIT :n
            """
        ),
        {**params, "pattern": f"%{escaped}%", "n": limit},
    )
    return [Hit(r.id, r.file_id, r.name, r.content, r.location) for r in rows]


async def random_chunk(
    session: AsyncSession, principal: Principal, category: str | None = None
) -> Hit | None:
    scope, params = _scope_sql(principal)
    where = scope
    if category:
        where += " AND f.category = :category"
        params["category"] = category
    row = (
        await session.execute(
            text(
                f"""
                SELECT c.id, c.file_id, f.name, c.content, c.location
                  FROM chunks c JOIN files f ON f.id = c.file_id
                 WHERE {where}
                 ORDER BY random()
                 LIMIT 1
                """
            ),
            params,
        )
    ).first()
    return Hit(row.id, row.file_id, row.name, row.content, row.location) if row else None
