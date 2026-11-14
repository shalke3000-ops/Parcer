"""`python -m leadhub.run` — one window: web UI + monitors, browser opens itself.

Everything runs while this window is open. On every start the monitors catch up
on what appeared while it was closed (RSS/Reddit feeds immediately, Telegram
history for the last TG_BACKFILL_HOURS). Close the window or press Ctrl+C to stop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import webbrowser

import uvicorn

from . import monitor
from .main import create_app

OPEN_BROWSER_TIMEOUT_S = 30


async def _open_browser_when_ready(server: uvicorn.Server, url: str) -> None:
    for _ in range(OPEN_BROWSER_TIMEOUT_S * 10):
        if server.started:
            webbrowser.open(url)
            return
        await asyncio.sleep(0.1)


def address() -> tuple[str, int]:
    return os.getenv("LEADHUB_HOST", "127.0.0.1"), int(os.getenv("LEADHUB_PORT", "8000"))


def build_server(**config) -> uvicorn.Server:
    host, port = address()
    return uvicorn.Server(uvicorn.Config(create_app(), host=host, port=port, log_level="warning",
                                         proxy_headers=False, **config))


async def run_all(server: uvicorn.Server) -> None:
    """Web server + monitors in one event loop; stopping the server stops the monitors."""
    monitors = asyncio.create_task(monitor.main(), name="monitors")
    try:
        await server.serve()
    finally:
        monitors.cancel()
        await asyncio.gather(monitors, return_exceptions=True)


async def serve() -> None:
    host, port = address()
    server = build_server()
    opener = asyncio.create_task(_open_browser_when_ready(server, f"http://{host}:{port}/"))
    print(f"\n  Lead Hub: http://{host}:{port}/   (закройте это окно или Ctrl+C, чтобы остановить)\n")
    try:
        await run_all(server)
    finally:
        opener.cancel()


def cli() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO; the Bot API URL contains the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
