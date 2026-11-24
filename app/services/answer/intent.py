"""Rule-based question routing — no tokens spent (DESIGN.md 6.2, US-D01 AC2).

Rules match whole words from PyThaiNLP, never substrings ("กี่" is inside "เกี่ยว",
"ขอ" inside "ของ"). Checked in order:

  generate  "แต่งมุกแนว -โต้ ให้หน่อย"   asks for a *new* joke
  data      "มีมุกกี่มุก"                 counting/statistics (Text-to-SQL in phase 4;
                                          answered from the library until then)
  lookup    "รองเท้า", "มะม่วง ภาษาอังกฤษ" at most 2 words, no request/joke word
  request   "ขอมุกเกี่ยวกับมะม่วงหน่อย"   asks for a joke about something
  chat      "วันนี้เหนื่อยจัง"             anything else: small talk, other questions

`followup` ("เอาอีกมุก") needs conversation history and arrives with phase 2.
"""

import re
from dataclasses import dataclass
from typing import Literal

from app.services.retrieval import content_terms
from app.services.thai import normalize, search_tokens

Intent = Literal["generate", "data", "lookup", "request", "chat"]

JOKE_WORDS = {"มุก", "มุข", "ตลก", "ขำ", "ฮา", "ขำๆ", "แคปชั่น", "แคป", "joke", "jokes"}
ASK_WORDS = {"ขอ", "เอา", "อยาก", "อยากได้", "หา", "ช่วย", "เล่า", "บ้าง", "หน่อย", "มีไหม"}
CREATE_WORDS = {"แต่ง", "คิด", "สร้าง"}
DATA_WORDS = {"กี่", "จำนวน", "ทั้งหมด", "สถิติ", "เยอะ", "มากที่สุด", "น้อยที่สุด"}

# "แนว -โต้", "แนวโต้", "ลงท้ายด้วย โต้" -> the sound to look for in the library
_PATTERN = re.compile(r"(?:แนว|ลงท้าย(?:ด้วย)?|เสียง)\s*[-–]?\s*([^\s,]+)")
_NOT_A_PATTERN = {"ให้", "หน่อย", "นะ", "ใหม่", "ไหน"}


@dataclass
class Routed:
    intent: Intent
    terms: list[str]
    pattern: str | None = None  # generate: sound fragment such as "โต้"


def route(message: str) -> Routed:
    text = normalize(message)
    words = set(search_tokens(text))
    terms = content_terms(text)
    joke, ask = bool(words & JOKE_WORDS), bool(words & ASK_WORDS)

    if words & CREATE_WORDS and (joke or "แนว" in words) or "แนว" in words and joke:
        m = _PATTERN.search(text)
        pattern = m.group(1).strip("-–\"'“”") if m else None
        if pattern in _NOT_A_PATTERN:
            pattern = None
        return Routed("generate", terms, pattern)
    if words & DATA_WORDS and joke:
        return Routed("data", terms)
    if terms and len(search_tokens(text)) <= 2 and not (joke or ask):
        return Routed("lookup", terms)
    if joke or ask and terms:
        return Routed("request", terms)
    return Routed("chat", terms)
