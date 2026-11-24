"""File -> Markdown.

Step 5 placeholder: plain text and Markdown only. Step 6 replaces the body with MarkItDown,
OCR and PII removal while keeping `convert()` and `ConversionError` the same.
"""

from dataclasses import dataclass


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


def decode_text(data: bytes) -> str:
    # Thai files from Windows tools are often TIS-620 / cp874 rather than UTF-8.
    for encoding in ("utf-8-sig", "cp874"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConversionError("CONVERSION_FAILED", "อ่านข้อความไม่ได้ (encoding ไม่รองรับ)")


def convert(data: bytes, name: str, mime_type: str) -> ConversionResult:
    if mime_type in ("text/plain", "text/markdown"):
        text = decode_text(data).replace("\r\n", "\n").strip()
        if not text:
            raise ConversionError("EMPTY_CONTENT", "ไฟล์ไม่มีเนื้อหา")
        return ConversionResult(markdown=text + "\n", method="text")
    raise ConversionError("CONVERSION_FAILED", f"ยังแปลงไฟล์ชนิด {mime_type} ไม่ได้ (รองรับในขั้นถัดไป)")
