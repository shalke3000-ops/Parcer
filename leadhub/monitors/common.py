"""Shared pipeline for every monitor: dedup → score → store → notify owner.

Monitors are strictly read-only: nothing in this package posts, replies,
reacts, joins chats or otherwise writes to any monitored platform.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..db import Database, NewLead
from ..notify import notify_lead
from ..scoring import Keywords, score_text

log = logging.getLogger(__name__)
MAX_TEXT_CHARS = 5000
NOTIFY_MAX_AGE_HOURS = 24
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\xa0]+")


@dataclass(frozen=True)
class RawPost:
    source: str          # "telegram" | "reddit" | "rss"
    source_name: str     # chat title, "r/forhire", feed title
    external_id: str     # stable id within the source
    text: str
    link: str
    author: str
    posted_at: str       # ISO-8601 UTC


def safe_link(url: str) -> str:
    url = (url or "").strip()
    return url if re.match(r"^https?://", url, re.IGNORECASE) else ""


def strip_html(value: str) -> str:
    text = _TAG_RE.sub(" ", value or "")
    text = html.unescape(text)
    lines = (_WS_RE.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def iso_from_timestamp(ts: float | None) -> str:
    moment = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return moment.replace(microsecond=0).isoformat()


def _is_recent(posted_at: str, max_age_hours: int = NOTIFY_MAX_AGE_HOURS) -> bool:
    try:
        moment = datetime.fromisoformat(posted_at)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - moment <= timedelta(hours=max_age_hours)


async def process_post(settings: Settings, db: Database, keywords: Keywords, post: RawPost) -> NewLead | None:
    """Returns the stored lead if the post was new and matched, else None.

    Posts are marked seen only AFTER they are stored (or deliberately rejected), so a
    crash or a locked database never loses a lead. Rejected posts are not re-scored
    later, even if MIN_SCORE or keywords.json change.
    """
    if db.is_seen(post.source, post.external_id):
        return None  # already processed in this or a previous run

    text = post.text.strip()[:MAX_TEXT_CHARS]
    result = score_text(text, keywords)
    if result.excluded or result.score < settings.min_score:
        db.mark_seen(post.source, post.external_id)
        return None

    lead = NewLead(
        source=post.source,
        source_name=post.source_name[:200],
        external_id=post.external_id,
        text=text,
        link=safe_link(post.link),
        author=post.author[:120],
        posted_at=post.posted_at,
        score=result.score,
        matched=result.matched,
    )
    inserted = db.insert_lead(lead, skip_duplicates=True)  # the same ad in another chat or feed is not a new order
    db.mark_seen(post.source, post.external_id)
    if inserted is None:
        return None
    log.info("New lead from %s (%s), score %s", lead.source, lead.source_name, lead.score)
    # Skip pushes for backlog items (e.g. a feed's old entries on the very first poll).
    if _is_recent(lead.posted_at):
        await notify_lead(settings, lead)
    return lead
