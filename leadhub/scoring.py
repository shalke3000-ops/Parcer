"""Keyword scoring for "someone wants to hire a developer" posts.

score = min(intent_hits, MAX_INTENT_HITS) * INTENT_WEIGHT
      + min(topic_hits,  MAX_TOPIC_HITS)  * TOPIC_WEIGHT
A post needs at least one intent AND one topic hit, and any exclude hit
(job-seeker / "[for hire]" / resume posts) drops it to zero.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .offers import looks_like_offer

INTENT_WEIGHT = 3
TOPIC_WEIGHT = 2
MAX_INTENT_HITS = 2
MAX_TOPIC_HITS = 3
MAX_SCORE = MAX_INTENT_HITS * INTENT_WEIGHT + MAX_TOPIC_HITS * TOPIC_WEIGHT


@dataclass(frozen=True)
class Keywords:
    intent: tuple[tuple[str, re.Pattern[str]], ...]
    topic: tuple[tuple[str, re.Pattern[str]], ...]
    exclude: tuple[tuple[str, re.Pattern[str]], ...]


@dataclass(frozen=True)
class ScoreResult:
    score: int
    matched: tuple[str, ...]
    excluded: bool


def _compile(phrase: str) -> re.Pattern[str]:
    # Whole-word/phrase match that also works for Cyrillic (\w is Unicode-aware).
    # Inner spaces/hyphens match any run of whitespace or a hyphen.
    body = r"[\s\-]+".join(re.escape(part) for part in re.split(r"[\s\-]+", phrase.strip()))
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _compile_list(data: dict, key: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    values = data.get(key)
    if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
        raise ValueError(f"keywords file must contain a non-empty string list {key!r}")
    unique = dict.fromkeys(v.strip().lower() for v in values)
    return tuple((phrase, _compile(phrase)) for phrase in unique)


def load_keywords(path: Path) -> Keywords:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Keywords(
        intent=_compile_list(data, "intent"),
        topic=_compile_list(data, "topic"),
        exclude=_compile_list(data, "exclude"),
    )


def _hits(text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    return [phrase for phrase, pattern in patterns if pattern.search(text)]


def score_text(text: str, keywords: Keywords) -> ScoreResult:
    if _hits(text, keywords.exclude) or looks_like_offer(text):
        return ScoreResult(score=0, matched=(), excluded=True)

    intent = _hits(text, keywords.intent)
    topic = _hits(text, keywords.topic)
    if not intent or not topic:
        return ScoreResult(score=0, matched=tuple(intent + topic), excluded=False)

    score = min(len(intent), MAX_INTENT_HITS) * INTENT_WEIGHT + min(len(topic), MAX_TOPIC_HITS) * TOPIC_WEIGHT
    return ScoreResult(score=score, matched=tuple(intent + topic), excluded=False)
