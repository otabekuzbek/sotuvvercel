"""amoCRM API v4 bilan ishlash: OAuth, qo'ng'iroqlar, menejerlar, vazifalar, izohlar."""
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from . import config
from .db import Call, Manager, kv_get, kv_set

log = logging.getLogger("amocrm")

CALL_NOTE_TYPES = ("call_in", "call_out")
ENTITIES = ("leads", "contacts")


class AmoError(Exception):
    pass


def is_configured() -> bool:
    return bool(config.AMO_SUBDOMAIN and (config.AMO_LONG_LIVED_TOKEN or config.AMO_CLIENT_ID))


def base_url(subdomain: str | None = None) -> str:
    return f"https://{subdomain or config.AMO_SUBDOMAIN}.{config.AMO_BASE_DOMAIN}"


def authorize_url(state: str) -> str:
    """amoCRM'da integratsiyaga ruxsat berish sahifasi."""
    params = {"client_id": config.AMO_CLIENT_ID, "state": state, "mode": "post_message"}
    return f"https://www.{config.AMO_BASE_DOMAIN}/oauth?{urlencode(params)}"


def _save_tokens(db, data: dict) -> None:
    kv_set(db, "amo_tokens", {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": int(time.time()) + int(data.get("expires_in", 86400)) - 60,
    })


def exchange_code(db, code: str) -> None:
    resp = httpx.post(f"{base_url()}/oauth2/access_token", json={
        "client_id": config.AMO_CLIENT_ID,
        "client_secret": config.AMO_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.AMO_REDIRECT_URI,
    }, timeout=30)
    if resp.status_code != 200:
        raise AmoError(f"Token olinmadi: {resp.status_code} {resp.text[:300]}")
    _save_tokens(db, resp.json())


def _refresh(db, refresh_token: str) -> str:
    resp = httpx.post(f"{base_url()}/oauth2/access_token", json={
        "client_id": config.AMO_CLIENT_ID,
        "client_secret": config.AMO_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": config.AMO_REDIRECT_URI,
    }, timeout=30)
    if resp.status_code != 200:
        raise AmoError(f"Token yangilanmadi, amoCRM'ni qayta ulang: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    _save_tokens(db, data)
    return data["access_token"]


def access_token(db) -> str:
    if config.AMO_LONG_LIVED_TOKEN:
        return config.AMO_LONG_LIVED_TOKEN
    tokens = kv_get(db, "amo_tokens")
    if not tokens:
        raise AmoError("amoCRM ulanmagan. Sozlamalar sahifasida «amoCRM'ni ulash» tugmasini bosing.")
    if tokens["expires_at"] <= time.time():
        return _refresh(db, tokens["refresh_token"])
    return tokens["access_token"]


def is_connected(db) -> bool:
    return bool(config.AMO_LONG_LIVED_TOKEN or kv_get(db, "amo_tokens"))


class AmoClient:
    def __init__(self, db):
        self.db = db
        self.http = httpx.Client(base_url=base_url(), timeout=30)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {access_token(self.db)}"}

    def request(self, method: str, path: str, **kwargs) -> dict | list | None:
        for attempt in range(4):
            resp = self.http.request(method, path, headers=self._headers(), **kwargs)
            if resp.status_code == 429:          # amoCRM limiti: soniyasiga ~7 so'rov
                time.sleep(1 + attempt)
                continue
            if resp.status_code == 401 and not config.AMO_LONG_LIVED_TOKEN and attempt == 0:
                tokens = kv_get(self.db, "amo_tokens") or {}
                if tokens.get("refresh_token"):
                    _refresh(self.db, tokens["refresh_token"])
                    continue
            if resp.status_code == 204:
                return None
            if resp.status_code >= 400:
                raise AmoError(f"{method} {path}: {resp.status_code} {resp.text[:300]}")
            time.sleep(0.15)
            return resp.json()
        raise AmoError(f"{method} {path}: juda ko'p so'rov (429)")

    # ---- Ma'lumot olish ----

    def account(self) -> dict:
        return self.request("GET", "/api/v4/account") or {}

    def users(self) -> list[dict]:
        out, page = [], 1
        while True:
            data = self.request("GET", "/api/v4/users", params={"page": page, "limit": 250})
            items = (data or {}).get("_embedded", {}).get("users", [])
            out += items
            if len(items) < 250:
                return out
            page += 1

    def call_notes(self, since_ts: int) -> list[dict]:
        """Berilgan vaqtdan keyin yangilangan barcha qo'ng'iroq izohlari (lid va kontaktlarda)."""
        out = []
        for entity in ENTITIES:
            page = 1
            while True:
                params = [("filter[note_type][]", t) for t in CALL_NOTE_TYPES]
                params += [("filter[updated_at][from]", since_ts), ("limit", 250), ("page", page)]
                data = self.request("GET", f"/api/v4/{entity}/notes", params=params)
                items = (data or {}).get("_embedded", {}).get("notes", [])
                for n in items:
                    n["_entity"] = entity
                out += items
                if len(items) < 250:
                    break
                page += 1
        return out

    # ---- Yozish ----

    def create_tasks(self, tasks: list[dict]) -> None:
        if tasks:
            self.request("POST", "/api/v4/tasks", json=tasks)

    def add_note(self, entity: str, entity_id: int, text: str) -> None:
        self.request("POST", f"/api/v4/{entity}/notes", json=[{
            "entity_id": entity_id, "note_type": "common", "params": {"text": text[:15000]},
        }])

    def download_recording(self, url: str) -> bytes:
        """Yozuvni yuklab olish. Ba'zi telefoniyalar amoCRM tokenini talab qiladi."""
        with httpx.Client(follow_redirects=True, timeout=120) as c:
            resp = c.get(url)
            if resp.status_code in (401, 403):
                resp = c.get(url, headers=self._headers())
            if resp.status_code >= 400 or len(resp.content) < 1000:
                raise AmoError(f"Yozuv yuklanmadi ({resp.status_code}): {url[:120]}")
            return resp.content


# ---- Sinxronlash ----

def sync_managers(db, client: AmoClient) -> int:
    count = 0
    for u in client.users():
        m = db.query(Manager).filter_by(amo_user_id=u["id"]).first()
        if not m:
            m = Manager(amo_user_id=u["id"], name=u.get("name") or f"#{u['id']}")
            db.add(m)
            count += 1
        m.name = u.get("name") or m.name
        m.email = u.get("email")
        m.active = bool(u.get("rights", {}).get("is_active", True))
    db.commit()
    return count


def note_to_call_fields(note: dict) -> dict | None:
    """amoCRM izohini qo'ng'iroq maydonlariga aylantiradi. Yozuvsiz bo'lsa None."""
    p = note.get("params") or {}
    link = p.get("link")
    if not link:
        return None
    return {
        "amo_note_id": note["id"],
        "entity_type": note.get("_entity", "leads"),
        "entity_id": note.get("entity_id"),
        "direction": "in" if note.get("note_type") == "call_in" else "out",
        "phone": str(p.get("phone") or ""),
        "duration": int(p.get("duration") or 0),
        "recording_url": link,
        "call_time": datetime.fromtimestamp(note.get("created_at") or time.time(), tz=timezone.utc).replace(tzinfo=None),
        "amo_user_id": note.get("responsible_user_id") or note.get("created_by"),
    }


def sync_calls(db) -> int:
    """amoCRM'dan yangi qo'ng'iroqlarni olib, bazaga 'new' holatida qo'shadi."""
    client = AmoClient(db)
    if not db.query(Manager).count():
        sync_managers(db, client)
    since = kv_get(db, "amo_last_sync", int(time.time()) - 3 * 86400)
    started = int(time.time())
    notes = client.call_notes(since - 300)
    managers = {m.amo_user_id: m for m in db.query(Manager).all()}
    added = 0
    for note in notes:
        fields = note_to_call_fields(note)
        if not fields or db.query(Call).filter_by(amo_note_id=fields["amo_note_id"]).first():
            continue
        amo_user_id = fields.pop("amo_user_id")
        if amo_user_id and amo_user_id not in managers:
            sync_managers(db, client)
            managers = {m.amo_user_id: m for m in db.query(Manager).all()}
        manager = managers.get(amo_user_id)
        call = Call(**fields, manager_id=manager.id if manager else None)
        if call.duration < config.MIN_CALL_SECONDS:
            call.status, call.error = "skipped", f"Qisqa qo'ng'iroq ({call.duration} s)"
        db.add(call)
        added += 1
    db.commit()
    kv_set(db, "amo_last_sync", started)
    log.info("amoCRM: %s ta izoh, %s ta yangi qo'ng'iroq", len(notes), added)
    return added
