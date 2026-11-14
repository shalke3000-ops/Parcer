"""Entry point for the read-only monitors: `python -m leadhub.monitor`.

Runs every source that is configured in `.env`; unconfigured sources are
skipped with a log line. Use `--once` to run a single RSS/Reddit pass.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_settings
from .db import Database
from .monitors import reddit_monitor, rss_monitor
from .scoring import load_keywords

log = logging.getLogger("leadhub.monitor")
MAX_BACKOFF_S = 3600


async def _poll_forever(name: str, poll, interval: int, once: bool) -> None:
    backoff = interval
    while True:
        try:
            checked = await poll()
            log.info("%s: checked %d items", name, checked)
            backoff = interval
        except Exception as exc:  # keep the other monitors alive; state is recorded in the DB
            backoff = min(backoff * 2, MAX_BACKOFF_S)
            log.error("%s poll failed (%s); retrying in %ds", name, type(exc).__name__, backoff)
        if once:
            return
        await asyncio.sleep(backoff)


async def _run_telegram_safely(settings, db: Database, keywords) -> None:
    """A Telegram failure (login, FloodWait, disconnect) must not take RSS/Reddit down with it."""
    from .monitors.telegram_monitor import SOURCE, run_telegram  # Telethon import only when used

    try:
        await run_telegram(settings, db, keywords)
    except Exception as exc:
        db.record_monitor_run(SOURCE, 0, type(exc).__name__)
        log.error("Telegram monitor stopped (%s); RSS/Reddit keep running.", type(exc).__name__)


async def main(once: bool = False) -> None:
    settings = load_settings(require_password=False)
    db = Database(settings.db_path)
    keywords = load_keywords(settings.keywords_path)
    tasks = []

    if settings.rss_configured:
        client = rss_monitor.make_client()
        tasks.append(_poll_forever("rss", lambda: rss_monitor.poll_feeds(settings, db, keywords, client),
                                   settings.poll_seconds, once))
    else:
        log.info("RSS: no RSS_FEEDS configured — skipped.")

    if settings.reddit_configured:
        reddit = reddit_monitor.make_reddit(settings)
        tasks.append(_poll_forever("reddit", lambda: reddit_monitor.poll_reddit(settings, db, keywords, reddit),
                                   settings.poll_seconds, once))
    else:
        log.info("Reddit: REDDIT_CLIENT_ID/SECRET not set — skipped.")

    if settings.telegram_configured and not once:
        tasks.append(_run_telegram_safely(settings, db, keywords))
    elif not settings.telegram_configured:
        log.info("Telegram: TG_API_ID/TG_API_HASH not set — skipped.")

    if not settings.notify_configured:
        log.info("Bot notifications: TG_BOT_TOKEN/TG_NOTIFY_CHAT_ID not set — disabled.")
    if not tasks:
        log.warning("Nothing to monitor. Configure at least one source in .env.")
        return
    await asyncio.gather(*tasks)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Lead Hub read-only monitors")
    parser.add_argument("--once", action="store_true", help="single RSS/Reddit pass, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO; the Bot API URL contains the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    asyncio.run(main(once=args.once))


if __name__ == "__main__":
    cli()
