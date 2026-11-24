"""Remove personal data from converted Markdown (US-C03, DESIGN.md 5.4).

Regex-based and precise: Thai national ID (with checksum), Thai phone numbers, e-mail.
Person names (PyThaiNLP NER, best-effort) come in phase 3.
"""

import re

REDACTED = "[ลบข้อมูล]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 13 digits, optionally grouped 1-4-5-2-1 with dashes/spaces.
_THAI_ID = re.compile(r"(?<!\d)\d[-\s]?\d{4}[-\s]?\d{5}[-\s]?\d{2}[-\s]?\d(?!\d)")
_PHONE = re.compile(
    r"(?<![\d+])(?:"
    r"(?:\+66|0066)[-\s]?[689]\d[-\s]?\d{3}[-\s]?\d{4}"  # +66 mobile
    r"|0[689]\d[-\s]?\d{3}[-\s]?\d{4}"  # mobile 08x-xxx-xxxx
    r"|0[2-7][-\s]?\d{3}[-\s]?\d{4}"  # Bangkok/provincial landline 02-xxx-xxxx
    r")(?!\d)"
)


def valid_thai_id(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    total = sum(int(d) * (13 - i) for i, d in enumerate(digits[:12]))
    return (11 - total % 11) % 10 == int(digits[12])


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return the cleaned text and how many items of each kind were removed."""
    counts = {"email": 0, "thai_id": 0, "phone": 0}

    def _email(m: re.Match[str]) -> str:
        counts["email"] += 1
        return REDACTED

    def _thai_id(m: re.Match[str]) -> str:
        if not valid_thai_id(re.sub(r"\D", "", m.group())):
            return m.group()
        counts["thai_id"] += 1
        return REDACTED

    def _phone(m: re.Match[str]) -> str:
        counts["phone"] += 1
        return REDACTED

    text = _EMAIL.sub(_email, text)
    text = _THAI_ID.sub(_thai_id, text)
    text = _PHONE.sub(_phone, text)
    return text, {k: v for k, v in counts.items() if v}
