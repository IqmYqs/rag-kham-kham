"""Thai text helpers shared by indexing and search (US-C05 AC3, RET-04).

Postgres has no Thai word breaker, so `search_text` is pre-tokenised with PyThaiNLP
(`newmm`) and joined with spaces; the `simple` text-search config then indexes each word.
Queries must go through the same `search_tokens()` so both sides split words identically.
"""

import re
import unicodedata
from functools import lru_cache

_ZERO_WIDTH = re.compile(r"[​-‍﻿]")
_SPACES = re.compile(r"\s+")
_WORDLIKE = re.compile(r"\w", re.UNICODE)


def normalize(text: str) -> str:
    """NFC, Thai vowel/tone-mark ordering fixes, no zero-width chars, single spaces."""
    from pythainlp.util import normalize as thai_normalize

    text = unicodedata.normalize("NFC", _ZERO_WIDTH.sub("", text))
    return _SPACES.sub(" ", thai_normalize(text)).strip()


@lru_cache(maxsize=4096)
def _tokenize(text: str) -> tuple[str, ...]:
    from pythainlp.tokenize import word_tokenize

    return tuple(word_tokenize(text, engine="newmm", keep_whitespace=False))


def search_tokens(text: str) -> list[str]:
    """Lower-cased word tokens without punctuation-only tokens (ๆ counts as punctuation)."""
    tokens = _tokenize(normalize(text))
    return [t.lower() for t in tokens if _WORDLIKE.search(t) and t != "ๆ"]


def search_text(text: str) -> str:
    return " ".join(search_tokens(text))
