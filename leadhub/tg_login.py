"""One-time Telegram login by QR code: `python -m leadhub.tg_login`.

Works without a phone number or SMS: scan the QR in Telegram on a device where
the account is already signed in (Settings → Devices → Link Desktop Device).
Creates the same session file the monitor uses. Read-only monitoring only.
"""

from __future__ import annotations

import asyncio
import getpass
import sys

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from .config import load_settings

QR_TIMEOUT_S = 30
MAX_QR_ATTEMPTS = 6  # a QR token lives ~30 s; Telegram wants a fresh one after that


def _print_qr(url: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)


async def main() -> int:
    settings = load_settings(require_password=False)
    if not settings.telegram_configured:
        print("TG_API_ID / TG_API_HASH не заданы в .env")
        return 1
    settings.tg_session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(settings.tg_session_path), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("Уже вошли — повторный вход не нужен.")
            return 0
        qr_login = await client.qr_login()
        for _ in range(MAX_QR_ATTEMPTS):
            print("\nTelegram на телефоне → Настройки → Устройства → Подключить устройство → наведите на QR:\n")
            _print_qr(qr_login.url)
            try:
                await qr_login.wait(QR_TIMEOUT_S)
                break
            except asyncio.TimeoutError:
                await qr_login.recreate()  # expired: new QR
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("Облачный пароль Telegram (символы не видны): "))
                break
        if not await client.is_user_authorized():
            print("Не удалось войти: QR не отсканирован. Запустите команду ещё раз.")
            return 1
        me = await client.get_me()
        print(f"\nГотово: вход выполнен как {me.first_name or me.username}. Теперь запускайте монитор.")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
