"""Split approved Markdown into chunks (US-C05 AC1, DESIGN.md 5.5).

The splitter picks a strategy per block (blocks are separated by blank lines, headings,
`<!-- loc -->` comments and code fences):

- table             -> one chunk per data row, written "column: value | column: value"
- list              -> one chunk per item
- several short lines (one joke per line, like the sample captions) -> one chunk per line
- anything else     -> one chunk, or ~max_chars windows with overlap when it is long

A lone short line at the very top of a document (before any heading or loc comment) is
taken as its title. Headings and the
title become `location.section`; loc comments add page/slide. Identical chunks within one
file are kept once (duplicates across files are left alone, US-C05 AC5).
"""

import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LOC = re.compile(r"^<!--\s*loc:\s*(\{.*\})\s*-->$")
_COMMENT = re.compile(r"^<!--.*-->$")
_FENCE = re.compile(r"^(```|~~~)")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_WORD = re.compile(r"\w")

TITLE_MAX_CHARS = 100


@dataclass
class ChunkDraft:
    content: str
    location: dict[str, Any] = field(default_factory=dict)


@dataclass
class _State:
    section: str | None = None
    loc: dict[str, Any] = field(default_factory=dict)
    seen_content: bool = False

    def location(self, **extra: Any) -> dict[str, Any]:
        where: dict[str, Any] = dict(self.loc)
        if self.section:
            where["section"] = self.section
        where.update(extra)
        return where


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    parts = re.split(r"(?<!\\)\|", line)
    return [p.strip().replace("\\|", "|") for p in parts]


def _is_table(lines: list[str]) -> bool:
    return (
        len(lines) >= 2
        and lines[0].lstrip().startswith("|")
        and bool(_TABLE_SEP.match(lines[1].strip()))
    )


def _windows(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = re.split(r"(\s+)", text)
    out: list[str] = []
    current = ""
    for piece in words:
        if len(current) + len(piece) > max_chars and current.strip():
            out.append(current.strip())
            tail = current[-overlap:] if overlap else ""
            # start the overlap on a word boundary
            current = tail[tail.find(" ") + 1 :] if " " in tail else tail
        current += piece
        while len(current) > max_chars:  # one enormous "word" (e.g. Thai with no spaces)
            out.append(current[:max_chars])
            current = current[max_chars - overlap :]
    if current.strip():
        out.append(current.strip())
    return out


def chunk_markdown(
    markdown: str, *, max_chars: int = 1200, overlap_chars: int = 120, short_line_chars: int = 200
) -> list[ChunkDraft]:
    state = _State()
    drafts: list[ChunkDraft] = []

    def emit(content: str, **extra: Any) -> None:
        content = content.strip()
        if len(_WORD.findall(content)) >= 2:
            drafts.append(ChunkDraft(content, state.location(**extra)))

    def flush(block: list[str]) -> None:
        lines = [ln.rstrip() for ln in block if ln.strip()]
        if not lines:
            return
        if not state.seen_content and not state.section and not state.loc and len(lines) == 1:
            line = lines[0].strip()
            if len(line) <= TITLE_MAX_CHARS and not _LIST_ITEM.match(line):
                state.section = line  # document title
                state.seen_content = True
                return
        state.seen_content = True

        if _is_table(lines):
            header = _cells(lines[0])
            for row_no, line in enumerate(lines[2:], start=1):
                values = _cells(line)
                pairs = [
                    (header[i] if i < len(header) else "", v) for i, v in enumerate(values) if v
                ]
                if len(header) == 1 or len(pairs) == 1 and not pairs[0][0]:
                    content = " ".join(v for _, v in pairs)
                else:
                    content = " | ".join(f"{h}: {v}" if h else v for h, v in pairs)
                emit(content, row=row_no)
            return

        if all(_LIST_ITEM.match(ln) or ln.startswith((" ", "\t")) for ln in lines) and (
            _LIST_ITEM.match(lines[0])
        ):
            items: list[str] = []
            for ln in lines:
                m = _LIST_ITEM.match(ln)
                if m:
                    items.append(m.group(1))
                else:
                    items[-1] += " " + ln.strip()
            for item in items:
                for piece in _windows(item, max_chars, overlap_chars):
                    emit(piece)
            return

        if len(lines) > 1 and all(len(ln) <= short_line_chars for ln in lines):
            for ln in lines:
                m = _LIST_ITEM.match(ln)
                emit(m.group(1) if m else ln)
            return

        for piece in _windows("\n".join(ln.strip() for ln in lines), max_chars, overlap_chars):
            emit(piece)

    block: list[str] = []
    in_fence = False
    for raw in markdown.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if _FENCE.match(stripped):
            if in_fence:
                block.append(line)
                for piece in _windows("\n".join(block), max_chars, overlap_chars):
                    emit(piece)
                state.seen_content = True
                block, in_fence = [], False
            else:
                flush(block)
                block, in_fence = [line], True
            continue
        if in_fence:
            block.append(line)
            continue
        if not stripped:
            flush(block)
            block = []
            continue
        if m := _LOC.match(stripped):
            flush(block)
            block = []
            with contextlib.suppress(json.JSONDecodeError):
                state.loc = {**state.loc, **json.loads(m.group(1))}
            continue
        if _COMMENT.match(stripped):
            continue
        if m := _HEADING.match(stripped):
            flush(block)
            block = []
            state.section = m.group(2) or state.section
            state.seen_content = True
            continue
        block.append(line)
    if in_fence:
        block = block[1:]  # unterminated fence: treat as text
    flush(block)

    unique: list[ChunkDraft] = []
    seen: set[str] = set()
    for d in drafts:
        key = re.sub(r"\s+", " ", d.content)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique
