"""Runtime configuration, read once from environment variables / `.env`.

Secrets live only here (server side). Nothing in this module is ever sent to
the frontend except the boolean "is this source configured" flags.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIN_PASSWORD_LENGTH = 10
MIN_POLL_SECONDS = 60

load_dotenv(PROJECT_ROOT / ".env")


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    password: str
    secret_key: str
    secret_key_is_ephemeral: bool
    cookie_secure: bool
    session_hours: int
    db_path: Path
    keywords_path: Path
    # Telegram user client — read-only listening
    tg_api_id: int | None
    tg_api_hash: str
    tg_session_path: Path
    tg_chats: tuple[str, ...]
    tg_backfill_hours: int  # on start, re-read this much recent chat history (0 = off)
    # Telegram Bot API — only pushes notifications to the owner
    tg_bot_token: str
    tg_notify_chat_id: str
    notify_min_score: int
    # Reddit — read-only API access
    reddit_client_id: str
    reddit_client_secret: str
    reddit_user_agent: str
    reddit_subreddits: tuple[str, ...]
    # RSS
    rss_feeds: tuple[str, ...]
    poll_seconds: int
    min_score: int
    # OpenStreetMap
    osm_user_agent: str
    nominatim_url: str
    overpass_url: str
    overpass_mirrors: tuple[str, ...]

    @property
    def telegram_configured(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash)

    @property
    def notify_configured(self) -> bool:
        return bool(self.tg_bot_token and self.tg_notify_chat_id)

    @property
    def reddit_configured(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def rss_configured(self) -> bool:
        return bool(self.rss_feeds)


def load_settings(require_password: bool = True) -> Settings:
    password = os.getenv("LEADHUB_PASSWORD", "")
    if require_password and len(password) < MIN_PASSWORD_LENGTH:
        raise RuntimeError(
            f"LEADHUB_PASSWORD must be set to at least {MIN_PASSWORD_LENGTH} characters (see .env.example)."
        )

    secret_key = os.getenv("LEADHUB_SECRET_KEY", "")
    ephemeral = not secret_key
    if ephemeral:
        # Sessions will not survive a restart; README explains how to persist the key.
        secret_key = secrets.token_urlsafe(48)

    tg_api_id_raw = os.getenv("TG_API_ID", "").strip()
    data_dir = Path(os.getenv("LEADHUB_DATA_DIR", str(PROJECT_ROOT / "data")))

    return Settings(
        password=password,
        secret_key=secret_key,
        secret_key_is_ephemeral=ephemeral,
        cookie_secure=os.getenv("LEADHUB_COOKIE_SECURE", "0") == "1",
        session_hours=_int_env("LEADHUB_SESSION_HOURS", 24 * 14),
        db_path=Path(os.getenv("LEADHUB_DB_PATH", str(data_dir / "leadhub.sqlite3"))),
        keywords_path=Path(os.getenv("LEADHUB_KEYWORDS_FILE", str(PROJECT_ROOT / "keywords.json"))),
        tg_api_id=int(tg_api_id_raw) if tg_api_id_raw.isdigit() else None,
        tg_api_hash=os.getenv("TG_API_HASH", ""),
        tg_session_path=Path(os.getenv("TG_SESSION_PATH", str(data_dir / "telegram"))),
        tg_chats=_csv_env("TG_CHATS"),
        tg_backfill_hours=max(0, _int_env("TG_BACKFILL_HOURS", 24)),
        tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_notify_chat_id=os.getenv("TG_NOTIFY_CHAT_ID", ""),
        notify_min_score=_int_env("NOTIFY_MIN_SCORE", 8),
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", "python:leadhub-personal:1.0 (read-only monitor)"),
        reddit_subreddits=_csv_env("REDDIT_SUBREDDITS", "forhire,slavelabour,DoneDirtCheap"),
        rss_feeds=_csv_env("RSS_FEEDS"),
        poll_seconds=max(MIN_POLL_SECONDS, _int_env("POLL_SECONDS", 300)),
        min_score=_int_env("MIN_SCORE", 5),
        osm_user_agent=os.getenv("OSM_USER_AGENT", "LeadHub/1.0 (personal single-user lead tracker)"),
        nominatim_url=os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"),
        overpass_url=os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
        overpass_mirrors=_csv_env("OVERPASS_MIRRORS", "https://overpass.kumi.systems/api/interpreter"),
    )
