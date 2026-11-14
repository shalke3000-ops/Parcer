"""Lead Hub as a desktop app: `pythonw -m leadhub.desktop` (used by the shortcut).

Opens a native window (Edge WebView2) with no console and no browser tab. The
site and the order search run inside this process, so they work exactly while
the window is open; closing the window stops everything.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import webview

from . import run
from .config import PROJECT_ROOT, load_settings

APP_TITLE = "Lead Hub"
APP_ID = "LeadHub.Desktop"
ICON_PATH = Path(__file__).resolve().parent / "static" / "app.ico"
START_TIMEOUT_S = 40
STOP_TIMEOUT_S = 8
LOG_MAX_BYTES = 1_000_000
MB_ICONERROR = 0x10
WM_SETICON = 0x0080
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
ICON_SMALL, ICON_BIG = 0, 1

log = logging.getLogger("leadhub.desktop")


def _alert(message: str) -> None:
    # pythonw has no console, so failures must be shown in a dialog.
    ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, MB_ICONERROR)


def _configure_logging(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "leadhub.log"
    handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO; the Bot API URL contains the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    return path


def _already_running(url: str) -> bool:
    try:
        return "authenticated" in httpx.get(f"{url}api/session", timeout=1.5).json()
    except (httpx.HTTPError, ValueError):
        return False


def _start_backend():
    """Start web + monitors on a background thread; returns (server, thread) once serving."""
    # log_config=None: uvicorn's default handlers need a console, which pythonw doesn't have.
    server = run.build_server(log_config=None)
    thread = threading.Thread(target=lambda: asyncio.run(run.run_all(server)), name="leadhub-backend", daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT_S
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Сервер Lead Hub не запустился. Подробности в data\\leadhub.log")
        time.sleep(0.05)
    return server, thread


def _apply_window_icon() -> None:
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, APP_TITLE)
    icon = user32.LoadImageW(None, str(ICON_PATH), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
    if hwnd and icon:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, icon)
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, icon)


def _run() -> int:
    settings = load_settings()
    data_dir = settings.db_path.parent
    log_path = _configure_logging(data_dir)
    log.info("Starting Lead Hub desktop (log: %s)", log_path)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # own taskbar icon

    host, port = run.address()
    url = f"http://{host}:{port}/"
    # A second click just opens another window on the instance that is already running.
    server, thread = (None, None) if _already_running(url) else _start_backend()

    webview.settings["ALLOW_DOWNLOADS"] = True  # CSV export
    window = webview.create_window(APP_TITLE, url, width=1360, height=900, min_size=(420, 640),
                                   background_color="#0A0908")
    window.events.shown += _apply_window_icon
    webview.start(private_mode=False, storage_path=str(data_dir / "webview"))  # keeps you logged in

    if server is not None:
        server.should_exit = True
        thread.join(STOP_TIMEOUT_S)
    log.info("Lead Hub desktop closed")
    return 0


def main() -> int:
    try:
        return _run()
    except Exception as exc:  # no console under pythonw: always tell the user
        log.exception("Lead Hub desktop failed")
        _alert(f"Не удалось запустить Lead Hub:\n\n{exc}\n\nПапка программы: {PROJECT_ROOT}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
