"""Markdown revision diffs (US-D04: keep a history of every change to a file's .md)."""

import difflib

MAX_DIFF_CHARS = 100_000


def make_diff(old: str | None, new: str) -> str | None:
    """Unified diff, or None for the first version. Truncated to keep rows small."""
    if old is None:
        return None
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), "before", "after", n=1
        )
    )
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (ตัดส่วนที่เหลือ)\n"
    return diff
