"""Ilova ichidan o'zgartiriladigan sozlamalar va avtomatik yaratiladigan maxfiy kalitlar.

Vercel'da muhit o'zgaruvchilarini qo'lda kiritmaslik uchun: API kalitlari Sozlamalar sahifasida
kiritiladi va bazada saqlanadi, SECRET_KEY va CRON_SECRET esa birinchi ishga tushishda o'zi yaratiladi.
Muhit o'zgaruvchilari baribir ishlaydi; bazadagi qiymat bo'lsa, u ustun turadi.
"""
import hashlib
import hmac
import secrets
import threading
import time

from sqlalchemy.exc import IntegrityError

from . import config
from .db import KV, SessionLocal, kv_get, kv_set

# (kalit, nom, maxfiymi, turi, izoh)
FIELDS = {
    "amoCRM": [
        ("AMO_SUBDOMAIN", "Subdomen", False, "str", "mycompany.amocrm.ru bo'lsa — mycompany"),
        ("AMO_BASE_DOMAIN", "Domen", False, "str", "amocrm.ru yoki kommo.com"),
        ("AMO_LONG_LIVED_TOKEN", "Uzoq muddatli token", True, "str",
         "amoCRM → Sozlamalar → Integratsiyalar → Integratsiya yaratish → Kalitlar → uzoq muddatli token"),
        ("AMO_CLIENT_ID", "Integratsiya ID (OAuth, ixtiyoriy)", False, "str", "Token o'rniga OAuth ishlatsangiz"),
        ("AMO_CLIENT_SECRET", "Maxfiy kalit (OAuth, ixtiyoriy)", True, "str", ""),
    ],
    "AI xizmatlar": [
        ("GOOGLE_API_KEY", "Google Speech-to-Text API kaliti", True, "str",
         "console.cloud.google.com → Cloud Speech-to-Text API ni yoqing → Credentials → API key"),
        ("ANTHROPIC_API_KEY", "Claude API kaliti", True, "str", "console.anthropic.com → API Keys"),
        ("CLAUDE_MODEL", "Claude modeli", False, "str", "Odatda o'zgartirish shart emas"),
    ],
    "Telegram": [
        ("TELEGRAM_BOT_TOKEN", "Bot tokeni", True, "str", "@BotFather → /newbot"),
        ("TELEGRAM_CHAT_ID", "Chat ID", False, "str", "Botni guruhga qo'shing, guruhga xabar yozing va «Aniqlash» ni bosing"),
        ("REPORT_TIME", "Kunlik hisobot vaqti", False, "str", "SS:DD, masalan 19:00"),
        ("LOW_SCORE_ALERT", "Shu bahodan past bo'lsa ogohlantirish", False, "int", "0 — o'chiq"),
    ],
    "Tahlil": [
        ("MIN_CALL_SECONDS", "Eng qisqa qo'ng'iroq (soniya)", False, "int", "Bundan qisqalari tahlil qilinmaydi"),
        ("COACHING_THRESHOLD", "Kouching chegarasi (%)", False, "int", "O'rtacha bahosi shundan past menejer belgilanadi"),
        ("STT_LANGUAGE", "Asosiy nutq tili", False, "str", "uz-UZ"),
        ("AMO_ADD_NOTES", "Xulosani amoCRM kartochkasiga yozish", False, "bool", ""),
        ("AMO_CREATE_TASKS", "Keyingi qadamlarni amoCRM vazifasi qilish", False, "bool", ""),
    ],
}
FIELD_TYPES = {k: (t, secret) for group in FIELDS.values() for k, _, secret, t, _ in group}

_cache = {"at": 0.0}
_lock = threading.Lock()
CACHE_SECONDS = 20


def _cast(kind: str, value):
    if kind == "int":
        return int(value)
    if kind == "bool":
        return value in (True, "1", "true", "on", "ha")
    return str(value).strip()


def apply(db=None, force: bool = False) -> None:
    """Bazadagi sozlamalarni config moduliga qo'llaydi (bir necha serverless nusxa uchun qisqa kesh bilan)."""
    if not force and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return
    with _lock:
        own = db is None
        db = db or SessionLocal()
        try:
            stored = kv_get(db, "settings", {}) or {}
        finally:
            if own:
                db.close()
        for key, value in stored.items():
            if key in FIELD_TYPES and value not in (None, ""):
                try:
                    setattr(config, key, _cast(FIELD_TYPES[key][0], value))
                except (TypeError, ValueError):
                    pass
        if not config.env("AMO_REDIRECT_URI"):
            config.AMO_REDIRECT_URI = f"{config.PUBLIC_URL}/oauth/amocrm/callback"
        _cache["at"] = time.monotonic()


def save(db, values: dict) -> None:
    """Formadan kelgan qiymatlarni saqlaydi. Maxfiy maydon bo'sh qoldirilsa eski qiymat saqlanadi."""
    stored = kv_get(db, "settings", {}) or {}
    for key, (kind, secret) in FIELD_TYPES.items():
        if kind == "bool":
            stored[key] = "true" if values.get(key) else "false"
            continue
        if key not in values:
            continue
        value = str(values[key]).strip()
        if secret and not value:
            continue
        if value and kind == "int":
            try:
                int(value)
            except ValueError:
                continue
        stored[key] = value
    kv_set(db, "settings", stored)
    apply(db, force=True)


def current_values() -> dict:
    out = {}
    for key, (kind, secret) in FIELD_TYPES.items():
        value = getattr(config, key, "")
        if secret:
            out[key] = ("••••" + str(value)[-4:]) if value else ""
        else:
            out[key] = value
    return out


# ---------- Avtomatik maxfiy kalitlar ----------

def get_or_create_secret(name: str, env_value: str = "") -> str:
    if env_value and env_value != "change-me-please":
        return env_value
    db = SessionLocal()
    try:
        value = kv_get(db, name)
        if value:
            return value
        value = secrets.token_hex(32)
        try:
            db.add(KV(key=name, value=f'"{value}"'))
            db.commit()
            return value
        except IntegrityError:  # boshqa nusxa bir vaqtda yaratdi — o'shanisini olamiz
            db.rollback()
            return kv_get(db, name)
    finally:
        db.close()


# ---------- Administrator ----------

def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def env_admin() -> bool:
    return config.ADMIN_PASSWORD not in ("", "admin")


def admin_exists(db) -> bool:
    return env_admin() or bool(kv_get(db, "admin"))


def create_admin(db, username: str, password: str) -> bool:
    """Faqat administrator hali yo'q bo'lsa yaratadi."""
    if admin_exists(db):
        return False
    salt = secrets.token_bytes(16)
    try:
        db.add(KV(key="admin", value=__import__("json").dumps(
            {"username": username, "salt": salt.hex(), "hash": _hash(password, salt)})))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def change_password(db, password: str) -> None:
    admin = kv_get(db, "admin") or {"username": config.ADMIN_USERNAME}
    salt = secrets.token_bytes(16)
    admin.update(salt=salt.hex(), hash=_hash(password, salt))
    kv_set(db, "admin", admin)


def check_login(db, username: str, password: str) -> bool:
    admin = kv_get(db, "admin")
    if admin:
        ok_user = hmac.compare_digest(username, admin["username"])
        ok_pass = hmac.compare_digest(_hash(password, bytes.fromhex(admin["salt"])), admin["hash"])
        if ok_user and ok_pass:
            return True
    if env_admin():
        return hmac.compare_digest(username, config.ADMIN_USERNAME) and hmac.compare_digest(password, config.ADMIN_PASSWORD)
    return False
