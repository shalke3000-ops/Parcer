"""Listens to Telegram groups/channels the user is ALREADY a member of.

Strictly read-only: this module only registers a NewMessage handler. It never
sends, replies, reacts, forwards, marks as read or joins anything. Chats listed
in TG_CHATS that the account is not a member of are skipped with a warning —
they are never joined automatically.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel

from ..config import Settings
from ..db import Database
from ..scoring import Keywords
from .common import RawPost, iso_from_timestamp, process_post

log = logging.getLogger(__name__)
SOURCE = "telegram"


def message_link(chat, message_id: int, peer) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if isinstance(peer, PeerChannel):
        return f"https://t.me/c/{peer.channel_id}/{message_id}"
    return ""  # basic groups have no public message links


def _matches(dialog, wanted: set[str]) -> bool:
    entity = dialog.entity
    candidates = {str(dialog.id), str(getattr(entity, "id", "")), (dialog.name or "").lower()}
    username = getattr(entity, "username", None)
    if username:
        candidates |= {username.lower(), f"@{username.lower()}"}
    return bool(candidates & wanted)


async def resolve_member_chats(client: TelegramClient, configured: tuple[str, ...]) -> list:
    dialogs = [d for d in await client.get_dialogs() if d.is_group or d.is_channel]
    if not configured:
        return [d.entity for d in dialogs]
    wanted = {c.strip().lower() for c in configured}
    selected = [d for d in dialogs if _matches(d, wanted)]
    found = set()
    for dialog in selected:
        found |= {
            str(dialog.id), str(getattr(dialog.entity, "id", "")), (dialog.name or "").lower(),
            (getattr(dialog.entity, "username", "") or "").lower(),
        }
    for name in wanted - found - {f"@{n}" for n in found}:
        log.warning("TG_CHATS entry %r is not a chat you are a member of — skipped (never auto-joined).", name)
    return [d.entity for d in selected]


BACKFILL_MESSAGES_PER_CHAT = 300  # safety cap per chat on each start
BACKFILL_PAUSE_S = 0.5            # gentle pacing between chats (flood limits)


async def _message_to_post(message, chat) -> RawPost | None:
    text = message.raw_text or ""
    if not text.strip() or getattr(message, "out", False):
        return None
    sender = await message.get_sender()
    author = ""
    if sender is not None:
        author = f"@{sender.username}" if getattr(sender, "username", None) else (
            getattr(sender, "first_name", None) or getattr(sender, "title", "") or ""
        )
    return RawPost(
        source=SOURCE,
        source_name=getattr(chat, "title", "") or "Telegram",
        external_id=f"{message.chat_id}:{message.id}",
        text=text,
        link=message_link(chat, message.id, message.peer_id),
        author=author,
        posted_at=iso_from_timestamp(message.date.timestamp() if message.date else None),
    )


async def backfill_recent(client: TelegramClient, chats: list, settings: Settings, db: Database,
                          keywords: Keywords) -> int:
    """Catch up on what was posted while the monitor was closed (read-only history fetch).

    Already-seen messages are skipped by the shared dedup table, so re-reading
    overlapping history is harmless. Returns how many messages were checked.
    """
    if settings.tg_backfill_hours <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.tg_backfill_hours)
    checked = 0
    for chat in chats:
        try:
            async for message in client.iter_messages(chat, limit=BACKFILL_MESSAGES_PER_CHAT):
                if message.date < cutoff:
                    break  # newest-first: everything after this is older
                post = await _message_to_post(message, chat)
                if post is not None:
                    checked += 1
                    await process_post(settings, db, keywords, post)
        except Exception as exc:  # one broken chat must not stop the rest
            log.warning("Backfill skipped for %s (%s)", getattr(chat, "title", chat), type(exc).__name__)
        await asyncio.sleep(BACKFILL_PAUSE_S)
    return checked


async def run_telegram(settings: Settings, db: Database, keywords: Keywords) -> None:
    settings.tg_session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(settings.tg_session_path), settings.tg_api_id, settings.tg_api_hash)
    await client.start()  # first run asks for phone + login code in this terminal
    chats = await resolve_member_chats(client, settings.tg_chats)
    if not chats:
        db.record_monitor_run(SOURCE, 0, "Нет групп/каналов для мониторинга")
        log.warning("No Telegram groups/channels to monitor.")
        return
    log.info("Listening to %d Telegram chats (read-only).", len(chats))
    counter = {"checked": 0}

    @client.on(events.NewMessage(chats=chats, incoming=True))
    async def on_message(event) -> None:
        post = await _message_to_post(event.message, await event.get_chat())
        if post is None:
            return
        counter["checked"] += 1
        await process_post(settings, db, keywords, post)

    async def catch_up() -> None:
        counter["checked"] += await backfill_recent(client, chats, settings, db, keywords)
        log.info("Telegram: caught up on the last %dh of history.", settings.tg_backfill_hours)

    async def heartbeat() -> None:
        # Runs for the client's lifetime (cancelled in `finally`); a reconnect or a
        # transient DB error must not end it silently.
        while True:
            try:
                checked, counter["checked"] = counter["checked"], 0
                error = "" if client.is_connected() else "нет соединения, переподключение…"
                db.record_monitor_run(SOURCE, checked, error)
            except Exception:
                log.exception("Telegram heartbeat failed")
            await asyncio.sleep(settings.poll_seconds)

    beat = asyncio.create_task(heartbeat())
    catch = asyncio.create_task(catch_up())  # live messages are handled meanwhile
    try:
        await client.run_until_disconnected()
    finally:
        beat.cancel()
        catch.cancel()
        await client.disconnect()  # releases the session file for the next start
