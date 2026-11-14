"""Recognises the same order posted more than once (other chats, feeds, reposts).

Compares the text after removing what changes between copies: links, mentions,
hashtags, leading tags like "[Hiring]", case and punctuation. It is deliberately
exact rather than fuzzy: on real data two different vacancies from one company
were 98% similar because they share the "about us" paragraph.
"""

from __future__ import annotations

import hashlib
import re

# Below this length a text is too generic to identify an order ("нужен бот"),
# so identical short messages only count as repeats from the same author.
MIN_DISTINCTIVE_CHARS = 40
FINGERPRINT_CHARS = 32

_LEADING_TAGS_RE = re.compile(r"^\s*(?:\[[^\]\n]{1,30}\]\s*[:\-–—|]?\s*)+")
_NOISE_RE = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+|[@#][\w.]+", re.IGNORECASE)


def normalize(text: str) -> str:
    body = _NOISE_RE.sub(" ", _LEADING_TAGS_RE.sub(" ", text)).casefold().replace("ё", "е")
    return " ".join("".join(ch if ch.isalnum() else " " for ch in body).split())


def fingerprint(text: str, author: str = "") -> str:
    body = normalize(text)
    if len(body) < MIN_DISTINCTIVE_CHARS:
        body = f"{author.casefold()}|{body or text.strip()}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]
