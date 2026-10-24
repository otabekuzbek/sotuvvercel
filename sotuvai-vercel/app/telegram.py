"""Telegram'ga hisobot va ogohlantirish yuborish."""
import logging

import httpx

from . import config

log = logging.getLogger("telegram")


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send(text: str, chat_id: str | None = None) -> bool:
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id or config.TELEGRAM_CHAT_ID, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning("Telegram xatosi: %s", resp.text[:200])
        return resp.status_code == 200
    except httpx.HTTPError as e:
        log.warning("Telegram ulanmadi: %s", e)
        return False


def detect_chat_id() -> dict | None:
    """Botga kelgan oxirgi xabarlardan guruh (yoki shaxsiy chat) ID sini topadi."""
    try:
        resp = httpx.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates", timeout=20)
        updates = resp.json().get("result", []) if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    chats = []
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats.append(chat)
    if not chats:
        return None
    groups = [c for c in chats if c.get("type") in ("group", "supergroup", "channel")]
    chat = (groups or chats)[-1]
    return {"id": str(chat["id"]), "title": chat.get("title") or chat.get("first_name") or str(chat["id"])}
