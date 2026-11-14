"""Push high-score matches to the owner's own chat via the Telegram Bot API.

This is the only outbound message Lead Hub ever sends, and it goes to the
user themself (TG_NOTIFY_CHAT_ID) — never to the monitored chats.
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings
from .db import NewLead

log = logging.getLogger(__name__)
MAX_PREVIEW_CHARS = 600
TIMEOUT_S = 15.0


def format_message(lead: NewLead) -> str:
    preview = lead.text if len(lead.text) <= MAX_PREVIEW_CHARS else lead.text[:MAX_PREVIEW_CHARS] + "…"
    lines = [f"Lead Hub · {lead.score} баллов · {lead.source_name or lead.source}", "", preview]
    if lead.link:
        lines += ["", lead.link]
    return "\n".join(lines)


async def notify_lead(settings: Settings, lead: NewLead, client: httpx.AsyncClient | None = None) -> bool:
    if not settings.notify_configured or lead.score < settings.notify_min_score:
        return False
    url = f"https://api.telegram.org/bot{settings.tg_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.tg_notify_chat_id,
        "text": format_message(lead),  # plain text: no parse_mode, so post content cannot inject markup
        "disable_web_page_preview": True,
    }
    owns = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        response = await client.post(url, json=payload)
        if response.status_code != 200:
            log.warning("Bot API notification failed with HTTP %s", response.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        # Never log the URL: it contains the bot token.
        log.warning("Bot API notification failed: %s", type(exc).__name__)
        return False
    finally:
        if owns:
            await client.aclose()
