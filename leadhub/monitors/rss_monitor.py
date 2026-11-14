"""Polls public RSS/Atom feeds of job boards. Read-only HTTP GETs."""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
from urllib.parse import urlparse

import feedparser
import httpx

from ..config import Settings
from ..db import Database
from ..scoring import Keywords
from .common import RawPost, iso_from_timestamp, process_post, strip_html

log = logging.getLogger(__name__)
SOURCE = "rss"
TIMEOUT_S = 30.0
MAX_LABEL_CHARS = 60
_REDDIT_FOOTER = re.compile(r"\s*submitted by\s+\S+\s+to\s+r/\S+\s*\[link\]\s*\[comments\]\s*$", re.IGNORECASE)
# Sites whose feed titles say nothing about the source ("Вакансии", "Ask HN").
_SITE_LABELS = {"career.habr.com": "Habr Career", "hnrss.org": "Hacker News"}


def feed_label(feed_url: str, title: str) -> str:
    """Short human label: 'r/forhire' for Reddit feeds, else the feed title's lead part."""
    parsed = urlparse(feed_url)
    reddit = re.match(r"^/r/([\w-]+)", parsed.path)
    if parsed.netloc.endswith("reddit.com") and reddit:
        return f"r/{reddit.group(1)}"
    if parsed.netloc in _SITE_LABELS:
        return _SITE_LABELS[parsed.netloc]
    label = re.split(r"\s+[:|–—-]\s+|:\s+", title or "", maxsplit=1)[0].strip()
    return (label or parsed.netloc or feed_url)[:MAX_LABEL_CHARS]


def _subreddit_of(entry: feedparser.FeedParserDict) -> str:
    """A combined r/a+b feed tags each post with the subreddit it came from."""
    tags = entry.get("tags") or []
    term = tags[0].get("term", "") if tags else ""
    return f"r/{term}" if term else ""


def entries_to_posts(feed_url: str, parsed: feedparser.FeedParserDict) -> list[RawPost]:
    feed_title = feed_label(feed_url, parsed.feed.get("title", ""))
    is_reddit = urlparse(feed_url).netloc.endswith("reddit.com")
    posts = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        title = strip_html(entry.get("title", ""))
        summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
        if is_reddit:
            summary = _REDDIT_FOOTER.sub("", summary)
        identity = entry.get("id") or link or title
        if not identity:
            continue
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        posts.append(
            RawPost(
                source=SOURCE,
                source_name=(_subreddit_of(entry) if is_reddit else "") or feed_title,
                # Hash keeps ids short and uniform even when a feed uses full URLs as ids.
                external_id=hashlib.sha256(f"{feed_url}|{identity}".encode()).hexdigest()[:32],
                text=f"{title}\n\n{summary}".strip(),
                link=link,
                author=strip_html(entry.get("author", "")),
                posted_at=iso_from_timestamp(calendar.timegm(stamp) if stamp else None),
            )
        )
    return posts


async def poll_feeds(settings: Settings, db: Database, keywords: Keywords, client: httpx.AsyncClient) -> int:
    checked, errors, any_ok = 0, [], False
    for url in settings.rss_feeds:
        try:
            response = await client.get(url, headers={"User-Agent": settings.osm_user_agent})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            errors.append(f"{urlparse(url).netloc}: HTTP {exc.response.status_code}")
            continue
        except httpx.HTTPError as exc:
            errors.append(f"{urlparse(url).netloc}: {type(exc).__name__}")
            continue
        any_ok = True
        for post in entries_to_posts(url, feedparser.parse(response.content)):
            checked += 1
            await process_post(settings, db, keywords, post)
    db.record_monitor_run(SOURCE, checked, "; ".join(errors), ok=any_ok)
    return checked


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
