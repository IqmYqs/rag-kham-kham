"""File -> Markdown (US-C01, DESIGN.md 5.3).

- txt / md: decoded directly (UTF-8 or Thai cp874)
- pdf / docx / xlsx / pptx / csv / json: MarkItDown, then normalised:
  * source locations as `<!-- loc: {...} -->` comments (page / slide); sheets keep `## name`
  * embedded images dropped (US-C01 AC3), empty spreadsheet cells instead of "NaN"
  * a JSON list of flat objects becomes a table, other JSON a fenced block
- images and scanned PDFs: phase 3 (OCR / Vision)
- personal data removed from every result (US-C03)
"""

import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.conversion.pii import redact


class ConversionError(Exception):
    """Permanent failure: retrying the same file will not help."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ConversionResult:
    markdown: str
    method: str
    pii_removed: dict[str, int] = field(default_factory=dict)


MIN_PDF_TEXT_CHARS = 20

_MARKITDOWN_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
}
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_NAN_CELL = re.compile(r"(?<=\|) (?:NaN|nan) (?=\|)")
_SLIDE = re.compile(r"<!-- Slide number: (\d+) -->")


def loc(**where: Any) -> str:
    return f"<!-- loc: {json.dumps(where, ensure_ascii=False)} -->"


def decode_text(data: bytes) -> str:
    # Thai files from Windows tools are often TIS-620 / cp874 rather than UTF-8.
    for encoding in ("utf-8-sig", "cp874"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConversionError("CONVERSION_FAILED", "อ่านข้อความไม่ได้ (encoding ไม่รองรับ)")


def _markitdown(data: bytes, ext: str) -> str:
    from markitdown import MarkItDown, StreamInfo  # heavy import, only when needed

    try:
        result = MarkItDown(enable_plugins=False).convert_stream(
            io.BytesIO(data), stream_info=StreamInfo(extension=ext, charset="utf-8")
        )
    except Exception as exc:
        raise ConversionError("CONVERSION_FAILED", f"แปลงไฟล์ไม่สำเร็จ: {type(exc).__name__}") from exc
    return result.markdown or ""


def _pdf_pages(text: str) -> str:
    pages = text.split("\x0c")
    if len(re.sub(r"\s", "", text)) < MIN_PDF_TEXT_CHARS:
        raise ConversionError(
            "UNREADABLE_IMAGE", "PDF ไม่มีชั้นข้อความ (น่าจะเป็นไฟล์สแกน) — รองรับในเฟส 3 (OCR)"
        )
    return "\n\n".join(f"{loc(page=i)}\n{p.strip()}" for i, p in enumerate(pages, 1) if p.strip())


def _json_to_markdown(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConversionError("CONVERSION_FAILED", f"JSON ไม่ถูกต้อง: {exc.msg}") from exc
    flat = (
        isinstance(data, list)
        and data
        and all(
            isinstance(row, dict) and all(not isinstance(v, dict | list) for v in row.values())
            for row in data
        )
    )
    if not flat:
        return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"
    columns = list(dict.fromkeys(k for row in data for k in row))

    def cell(value: Any) -> str:
        return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    lines += ["| " + " | ".join(cell(row.get(c)) for c in columns) + " |" for row in data]
    return "\n".join(lines)


def _normalise(markdown: str) -> str:
    markdown = _IMAGE.sub("", markdown)
    markdown = _NAN_CELL.sub(" ", markdown)
    markdown = _SLIDE.sub(lambda m: loc(slide=int(m.group(1))), markdown)
    markdown = markdown.replace("\r\n", "\n")
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


def convert(data: bytes, name: str, mime_type: str) -> ConversionResult:
    if mime_type.startswith("image/"):
        raise ConversionError("UNREADABLE_IMAGE", "ยังไม่รองรับไฟล์รูปภาพ — รองรับในเฟส 3 (OCR/Vision)")

    if mime_type in ("text/plain", "text/markdown"):
        markdown, method = decode_text(data), "text"
    elif mime_type == "application/json":
        markdown, method = _json_to_markdown(decode_text(data)), "json"
    elif mime_type == "text/csv":
        # Decode ourselves so cp874 CSVs from Thai Excel work too.
        markdown, method = _markitdown(decode_text(data).encode(), ".csv"), "markitdown"
    elif ext := _MARKITDOWN_EXT.get(mime_type):
        markdown, method = _markitdown(data, ext), "markitdown"
        if ext == ".pdf":
            markdown = _pdf_pages(markdown)
    else:
        raise ConversionError("CONVERSION_FAILED", f"ไม่รองรับไฟล์ชนิด {mime_type}")

    markdown = _normalise(markdown)
    if not re.sub(r"<!--.*?-->|\s|[|\-#]", "", markdown):
        raise ConversionError("EMPTY_CONTENT", "ไฟล์ไม่มีเนื้อหา")
    markdown, removed = redact(markdown)
    return ConversionResult(markdown=markdown + "\n", method=method, pii_removed=removed)
