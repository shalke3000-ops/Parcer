"""Reads new submissions from public subreddits through the official API (PRAW).

Read-only: the client is created without a username/password, so PRAW runs in
read-only mode and *cannot* post, comment or vote even by accident.
"""

from __future__ import annotations

import asyncio
import logging

import praw

from ..config import Settings
from ..db import Database
from ..scoring import Keywords
from .common import RawPost, iso_from_timestamp, process_post

log = logging.getLogger(__name__)
SOURCE = "reddit"
FETCH_LIMIT = 50


def make_reddit(settings: Settings) -> praw.Reddit:
    reddit = praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
        check_for_async=False,
    )
    reddit.read_only = True
    return reddit


def submission_to_post(submission) -> RawPost:
    return RawPost(
        source=SOURCE,
        source_name=f"r/{submission.subreddit.display_name}",
        external_id=submission.fullname,
        text=f"{submission.title}\n\n{submission.selftext or ''}".strip(),
        link=f"https://www.reddit.com{submission.permalink}",
        author=f"u/{submission.author.name}" if submission.author else "",
        posted_at=iso_from_timestamp(submission.created_utc),
    )


def _fetch_new(reddit: praw.Reddit, subreddits: tuple[str, ...]) -> tuple[list[RawPost], list[str]]:
    """Fetch each subreddit independently: one banned/private/misspelled name must not block the rest."""
    posts, errors = [], []
    for name in subreddits:
        try:
            posts.extend(submission_to_post(s) for s in reddit.subreddit(name).new(limit=FETCH_LIMIT))
        except Exception as exc:  # praw/prawcore raise many transport/auth types
            errors.append(f"r/{name}: {type(exc).__name__}")
    return posts, errors


async def poll_reddit(settings: Settings, db: Database, keywords: Keywords, reddit: praw.Reddit) -> int:
    # PRAW is synchronous; keep it off the event loop.
    posts, errors = await asyncio.to_thread(_fetch_new, reddit, settings.reddit_subreddits)
    any_ok = len(errors) < len(settings.reddit_subreddits)
    for post in posts:
        await process_post(settings, db, keywords, post)
    db.record_monitor_run(SOURCE, len(posts), "; ".join(errors), ok=any_ok)
    if not any_ok:
        raise RuntimeError("all subreddits failed")  # lets the runner back off
    return len(posts)
