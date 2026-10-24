"""SotuvAI web ilovasi: dashboard, qo'ng'iroqlar, menejerlar, skript, sozlamalar, AI yordamchi."""
import hmac
from contextlib import asynccontextmanager
import logging
import secrets
import threading
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import amocrm, analyzer, assistant, config, pipeline, telegram
from . import settings_store as store
from .db import (DEFAULT_COMPANY_CONTEXT, Call, Manager, SessionLocal, get_script, init_db, kv_get,
                 kv_set)
from .stats import (daily_trend, kpis, local_day_bounds, manager_table, outcome_counts, period_bounds,
                    stage_averages, to_local, today_local)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

@asynccontextmanager
async def lifespan(_app):
    ensure_started()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title=config.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
templates.env.globals.update(app_name=config.APP_NAME, to_local=to_local)

STATUS_LABELS = {"new": "Navbatda", "transcribing": "Matnga o'girilmoqda", "analyzing": "Tahlil qilinmoqda",
                 "done": "Tayyor", "failed": "Xato", "skipped": "O'tkazib yuborildi"}
OUTCOME_LABELS = {"sotuv": "Sotuv", "kelishuv": "Kelishuv", "qayta_aloqa": "Qayta aloqa",
                  "rad_etildi": "Rad etildi", "malumot": "Ma'lumot", "boshqa": "Boshqa"}
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, OUTCOME_LABELS=OUTCOME_LABELS,
                             coaching_threshold=config.COACHING_THRESHOLD)

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
_sync_lock = threading.Lock()


def run_in_background(fn) -> None:
    """Docker'da fon oqimida, Vercel'da esa so'rov ichida bajaradi (serverless javobdan keyin to'xtaydi)."""
    if config.IS_VERCEL:
        fn()
    else:
        threading.Thread(target=fn, daemon=True).start()


def run_sync() -> dict:
    if not _sync_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        return pipeline.sync_and_process()
    finally:
        _sync_lock.release()


# Ilovani ishga tushirishga to'sqinlik qiladigan sozlash xatolari (sahifada ko'rsatiladi, ilova yiqilmaydi)
SETUP_ERRORS: list[str] = []
_started = False
_start_lock = threading.Lock()


def check_setup() -> list[str]:
    """Faqat chetlab bo'lmaydigan talab: Vercel'da doimiy baza. Qolgani avtomatik yoki ilova ichida sozlanadi."""
    errors = []
    if config.IS_VERCEL and not config.DEMO_MODE and not (config.env("DATABASE_URL") or config.env("POSTGRES_URL")):
        errors.append("db")
    return errors


def ensure_started() -> None:
    """Bir marta ishga tushirish. Lifespan yoki birinchi so'rov chaqiradi (serverless'da ikkalasi ham bo'lishi mumkin)."""
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        SETUP_ERRORS[:] = check_setup()
        if not SETUP_ERRORS:
            try:
                startup()
            except Exception as e:  # noqa: BLE001
                log.exception("Ishga tushirishda xato")
                SETUP_ERRORS.append(f"dberr:{e}")
        _started = True


def startup() -> None:
    init_db()
    # Maxfiy kalitlar: muhit o'zgaruvchisi bo'lmasa, bir marta yaratilib bazada saqlanadi
    config.SECRET_KEY = store.get_or_create_secret("secret_key", config.env("SECRET_KEY"))
    config.CRON_KEY = store.get_or_create_secret("cron_key", config.CRON_SECRET)
    store.apply(force=True)
    if config.DEMO_MODE:
        from .demo import seed_if_empty
        seed_if_empty()
    if config.IS_VERCEL:
        return  # Vercel'da fon jarayoni yo'q — ishni /api/cron/* endpointlari bajaradi
    hour, minute = (int(x) for x in config.REPORT_TIME.split(":"))
    scheduler.add_job(run_sync, "interval", minutes=config.SYNC_INTERVAL_MIN, id="sync",
                      next_run_time=datetime.now(config.TIMEZONE) + timedelta(seconds=20), replace_existing=True)
    scheduler.add_job(pipeline.send_daily_report, "cron", hour=hour, minute=minute, id="report", replace_existing=True)
    if not scheduler.running:
        scheduler.start()


# ---------- Avtorizatsiya ----------

PUBLIC_PATHS = ("/login", "/static", "/oauth/amocrm/callback", "/health", "/api/cron/", "/setup")


@app.middleware("http")
async def require_login(request: Request, call_next):
    ensure_started()
    path = request.url.path
    if SETUP_ERRORS and path.startswith(("/static", "/health")):
        return await call_next(request)
    if SETUP_ERRORS:
        err = SETUP_ERRORS[0]
        return templates.TemplateResponse(request, "setup.html", {
            "need_db": err == "db", "db_error": err[6:] if err.startswith("dberr:") else None,
            "is_vercel": config.IS_VERCEL}, status_code=503)
    store.apply()
    templates.env.globals["coaching_threshold"] = config.COACHING_THRESHOLD
    if not path.startswith(("/static", "/health", "/api/cron/", "/setup")):
        with SessionLocal() as db:
            if not store.admin_exists(db):
                return RedirectResponse("/setup")
    if not path.startswith(PUBLIC_PATHS) and not request.session.get("user"):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "login kerak"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


class LazySessionMiddleware(SessionMiddleware):
    """Kalit bazadan olinadi, shuning uchun imzolovchi birinchi so'rovda (ishga tushgandan keyin) o'rnatiladi."""

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            ensure_started()
            key = config.SECRET_KEY
            if getattr(self, "_key", None) != key:
                import itsdangerous
                self.signer = itsdangerous.TimestampSigner(str(key))
                self._key = key
            self.security_flags = "httponly; samesite=lax" + ("; secure" if scope.get("scheme") == "https" or config.IS_VERCEL else "")
        await super().__call__(scope, receive, send)


# Session middleware tashqi qatlam bo'lishi uchun require_login'dan keyin qo'shiladi
app.add_middleware(LazySessionMiddleware, secret_key="pending", max_age=14 * 86400, same_site="lax")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    with SessionLocal() as db:
        if store.admin_exists(db):
            return RedirectResponse("/login")
    return templates.TemplateResponse(request, "first_run.html", {"error": None})


@app.post("/setup")
def setup_create(request: Request, username: str = Form(...), password: str = Form(...), password2: str = Form(...)):
    username = username.strip()
    error = None
    if len(username) < 3:
        error = "Login kamida 3 belgi bo'lsin"
    elif len(password) < 8:
        error = "Parol kamida 8 belgi bo'lsin"
    elif password != password2:
        error = "Parollar bir xil emas"
    if error:
        return templates.TemplateResponse(request, "first_run.html", {"error": error}, status_code=400)
    with SessionLocal() as db:
        if not store.create_admin(db, username, password):
            return RedirectResponse("/login", status_code=303)
    request.session["user"] = username
    return RedirectResponse("/settings?msg=Xush+kelibsiz!+Endi+kalitlarni+kiriting", status_code=303)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with SessionLocal() as db:
        ok = store.check_login(db, username.strip(), password)
    if not ok:
        return templates.TemplateResponse(request, "login.html", {"error": "Login yoki parol noto'g'ri"}, status_code=401)
    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/health")
def health():
    return {"ok": not SETUP_ERRORS, "setup_errors": len(SETUP_ERRORS)}


# ---------- Sahifalar ----------

def period_from(request: Request) -> tuple[str, datetime, datetime]:
    period = request.query_params.get("period", "today")
    if period == "7":
        return period, *period_bounds(7)
    if period == "30":
        return period, *period_bounds(30)
    return "today", *local_day_bounds(today_local())


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    period, start, end = period_from(request)
    with SessionLocal() as db:
        rows = manager_table(db, start, end)
        ctx = {
            "period": period, "kpi": kpis(db, start, end), "rows": rows,
            "coaching": [r for r in rows if r["needs_coaching"]],
            "stages": stage_averages(db, start, end), "trend": daily_trend(db, 14),
            "outcomes": outcome_counts(db, start, end),
            "recent": db.query(Call).filter(Call.status == "done").order_by(Call.call_time.desc()).limit(8).all(),
            "lowest": (db.query(Call).filter(Call.status == "done", Call.call_time >= start, Call.call_time < end)
                       .order_by(Call.overall_score.asc()).limit(5).all()),
            "connected": amocrm.is_connected(db),
        }
        return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/calls", response_class=HTMLResponse)
def calls_page(request: Request, manager: int | None = None, status: str = "", score: str = "",
               date_from: str = "", date_to: str = "", page: int = 1):
    per_page = 30
    with SessionLocal() as db:
        q = db.query(Call)
        if manager:
            q = q.filter(Call.manager_id == manager)
        if status:
            q = q.filter(Call.status == status)
        if score == "low":
            q = q.filter(Call.overall_score < 50)
        elif score == "mid":
            q = q.filter(Call.overall_score >= 50, Call.overall_score < 75)
        elif score == "high":
            q = q.filter(Call.overall_score >= 75)
        try:
            if date_from:
                q = q.filter(Call.call_time >= local_day_bounds(date.fromisoformat(date_from))[0])
            if date_to:
                q = q.filter(Call.call_time < local_day_bounds(date.fromisoformat(date_to))[1])
        except ValueError:
            pass
        total = q.count()
        calls = q.order_by(Call.call_time.desc()).offset((page - 1) * per_page).limit(per_page).all()
        filters = {"manager": manager or "", "status": status, "score": score, "date_from": date_from, "date_to": date_to}
        base = {k: v for k, v in filters.items() if v}
        return templates.TemplateResponse(request, "calls.html", {
            "calls": calls, "total": total, "page": page, "pages": max(1, -(-total // per_page)),
            "filters": filters, "managers": db.query(Manager).order_by(Manager.name).all(),
            "qs": lambda p: urlencode({**base, "page": p}),
        })


@app.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(request: Request, call_id: int):
    with SessionLocal() as db:
        call = db.get(Call, call_id)
        if not call:
            return HTMLResponse("Topilmadi", status_code=404)
        _ = call.manager
        amo_link = (f"{amocrm.base_url()}/{call.entity_type}/detail/{call.entity_id}"
                    if config.AMO_SUBDOMAIN and call.entity_id else None)
        return templates.TemplateResponse(request, "call.html", {"call": call, "a": call.analysis, "amo_link": amo_link})


@app.post("/calls/{call_id}/reanalyze")
def reanalyze(call_id: int, retranscribe: str = Form("")):
    with SessionLocal() as db:
        call = db.get(Call, call_id)
        if call:
            if retranscribe:
                call.transcript_json = None
            call.status, call.error, call.attempts = "new", None, 0
            db.commit()

    def work():
        with SessionLocal() as db2:
            c = db2.get(Call, call_id)
            if c:
                pipeline.process_call(db2, c)
    run_in_background(work)
    return RedirectResponse(f"/calls/{call_id}", status_code=303)


@app.get("/managers", response_class=HTMLResponse)
def managers_page(request: Request):
    period, start, end = period_from(request)
    if period == "today":
        period, start, end = "7", *period_bounds(7)
    with SessionLocal() as db:
        return templates.TemplateResponse(request, "managers.html", {
            "period": period, "rows": manager_table(db, start, end), "script": get_script(db)})


@app.get("/managers/{manager_id}", response_class=HTMLResponse)
def manager_detail(request: Request, manager_id: int):
    with SessionLocal() as db:
        m = db.get(Manager, manager_id)
        if not m:
            return HTMLResponse("Topilmadi", status_code=404)
        start, end = period_bounds(30)
        row = next((r for r in manager_table(db, start, end) if r["manager"].id == m.id), None)
        calls = (db.query(Call).filter(Call.manager_id == m.id, Call.status == "done")
                 .order_by(Call.call_time.desc()).limit(50).all())
        recs: dict[str, int] = {}
        for c in calls:
            for r in c.analysis.get("recommendations", []):
                recs[r["title"]] = recs.get(r["title"], 0) + 1
        trend = []
        for i in range(13, -1, -1):
            d = today_local() - timedelta(days=i)
            s, e = local_day_bounds(d)
            day_calls = [c for c in calls if c.call_time and s <= c.call_time < e]
            trend.append({"day": d.strftime("%d.%m"),
                          "avg": round(sum(c.overall_score for c in day_calls) / len(day_calls), 1) if day_calls else None})
        return templates.TemplateResponse(request, "manager.html", {
            "m": m, "row": row, "calls": calls[:20], "script": get_script(db), "trend": trend,
            "top_recs": sorted(recs.items(), key=lambda kv: -kv[1])[:6]})


@app.get("/script", response_class=HTMLResponse)
def script_page(request: Request, saved: int = 0):
    with SessionLocal() as db:
        return templates.TemplateResponse(request, "script.html", {
            "script": get_script(db), "saved": saved,
            "company_context": kv_get(db, "company_context", DEFAULT_COMPANY_CONTEXT)})


@app.post("/script")
async def save_script(request: Request):
    form = await request.form()
    names, weights, criteria = form.getlist("name"), form.getlist("weight"), form.getlist("criteria")
    keys = form.getlist("key")
    script = []
    for i, name in enumerate(names):
        if not name.strip():
            continue
        key = (keys[i] if i < len(keys) and keys[i] else f"stage_{secrets.token_hex(3)}")
        try:
            weight = max(0, int(weights[i]))
        except (ValueError, IndexError):
            weight = 0
        script.append({"key": key, "name": name.strip(), "weight": weight,
                       "criteria": (criteria[i] if i < len(criteria) else "").strip()})
    with SessionLocal() as db:
        if script:
            kv_set(db, "script", script)
        kv_set(db, "company_context", str(form.get("company_context", "")).strip())
    return RedirectResponse("/script?saved=1", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    with SessionLocal() as db:
        account = None
        if amocrm.is_configured() and amocrm.is_connected(db):
            try:
                account = amocrm.AmoClient(db).account().get("name")
            except Exception as e:  # noqa: BLE001
                account = f"Xato: {e}"
        return templates.TemplateResponse(request, "settings.html", {
            "msg": msg, "account": account, "connected": amocrm.is_connected(db),
            "amo_configured": amocrm.is_configured(), "uses_long_token": bool(config.AMO_LONG_LIVED_TOKEN),
            "redirect_uri": config.AMO_REDIRECT_URI, "last_sync": kv_get(db, "amo_last_sync"),
            "keys": {"Google STT": bool(config.GOOGLE_API_KEY), "Claude": bool(config.ANTHROPIC_API_KEY),
                     "Telegram": telegram.is_configured()},
            "cfg": config, "managers": db.query(Manager).order_by(Manager.name).all(),
            "fields": store.FIELDS, "values": store.current_values(),
            "cron_url": f"{config.PUBLIC_URL}/api/cron/sync?key={config.CRON_KEY}",
            "has_db_admin": bool(kv_get(db, "admin")),
            "counts": {s: db.query(Call).filter(Call.status == s).count() for s in STATUS_LABELS},
        })


@app.get("/oauth/amocrm/start")
def amo_start(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["amo_state"] = state
    return RedirectResponse(amocrm.authorize_url(state))


@app.get("/oauth/amocrm/callback")
def amo_callback(request: Request, code: str = "", state: str = "", referer: str = ""):
    if not request.session.get("user"):
        return RedirectResponse("/login")
    if not code or state != request.session.get("amo_state"):
        return RedirectResponse("/settings?msg=" + "amoCRM ulanmadi: noto'g'ri javob")
    if referer and config.AMO_SUBDOMAIN and not referer.startswith(config.AMO_SUBDOMAIN + "."):
        return RedirectResponse("/settings?msg=" + f"Boshqa akkaunt ({referer}). AMO_SUBDOMAIN ni tekshiring")
    with SessionLocal() as db:
        try:
            amocrm.exchange_code(db, code)
            amocrm.sync_managers(db, amocrm.AmoClient(db))
        except Exception as e:  # noqa: BLE001
            return RedirectResponse("/settings?" + urlencode({"msg": f"Xato: {e}"}))
    return RedirectResponse("/settings?msg=amoCRM+muvaffaqiyatli+ulandi")


@app.post("/settings/sync")
def sync_now():
    if config.IS_VERCEL:
        r = pipeline.sync_and_process()
        msg = (f"Xato: {r['error']}" if r.get("error") else
               f"{r['added']} ta yangi, {r['processed']} ta tahlil qilindi, navbatda {r['remaining']} ta")
        return RedirectResponse("/settings?" + urlencode({"msg": msg}), status_code=303)
    threading.Thread(target=run_sync, daemon=True).start()
    return RedirectResponse("/settings?msg=Sinxronlash+boshlandi.+Bir+necha+daqiqadan+so'ng+yangilang", status_code=303)


@app.post("/settings/integrations")
async def save_integrations(request: Request):
    form = await request.form()
    with SessionLocal() as db:
        store.save(db, {k: form.get(k) for k in store.FIELD_TYPES if k in form or store.FIELD_TYPES[k][0] == "bool"})
    return RedirectResponse("/settings?msg=Sozlamalar+saqlandi#ulanishlar", status_code=303)


@app.post("/settings/telegram-detect")
async def telegram_detect(request: Request):
    form = await request.form()   # formadagi yangi token avval saqlanadi
    with SessionLocal() as db:
        store.save(db, {k: form.get(k) for k in store.FIELD_TYPES if k in form or store.FIELD_TYPES[k][0] == "bool"})
    if not config.TELEGRAM_BOT_TOKEN:
        return RedirectResponse("/settings?msg=Avval+bot+tokenini+saqlang#ulanishlar", status_code=303)
    chat = telegram.detect_chat_id()
    if not chat:
        return RedirectResponse("/settings?" + urlencode({"msg": "Chat topilmadi: botni guruhga qo'shing, guruhga istalgan xabar yozing va qayta bosing"}) + "#ulanishlar", status_code=303)
    with SessionLocal() as db:
        store.save(db, {"TELEGRAM_CHAT_ID": chat["id"]})
    return RedirectResponse("/settings?" + urlencode({"msg": f"Chat aniqlandi: {chat['title']}"}) + "#ulanishlar", status_code=303)


@app.post("/settings/password")
def change_password(request: Request, password: str = Form(...), password2: str = Form(...)):
    if len(password) < 8 or password != password2:
        return RedirectResponse("/settings?msg=Xato:+parol+kamida+8+belgi+va+ikkalasi+bir+xil+bo'lsin", status_code=303)
    with SessionLocal() as db:
        store.change_password(db, password)
    return RedirectResponse("/settings?msg=Parol+o'zgartirildi", status_code=303)


@app.post("/settings/managers")
async def save_managers(request: Request):
    form = await request.form()
    active = {int(x) for x in form.getlist("active")}
    with SessionLocal() as db:
        for m in db.query(Manager).all():
            m.active = m.id in active
        db.commit()
    return RedirectResponse("/settings?msg=Saqlandi", status_code=303)


@app.post("/settings/telegram-test")
def telegram_test():
    with SessionLocal() as db:
        ok = telegram.send(pipeline.daily_report_text(db))
    return RedirectResponse("/settings?msg=" + ("Telegram'ga+yuborildi" if ok else "Telegram+xatosi:+token+va+chat+ID+ni+tekshiring"), status_code=303)


@app.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request):
    return templates.TemplateResponse(request, "assistant.html", {"enabled": bool(config.ANTHROPIC_API_KEY)})


@app.post("/api/assistant")
async def assistant_api(request: Request):
    body = await request.json()
    question = str(body.get("question", "")).strip()[:2000]
    if not question:
        return JSONResponse({"error": "Savol bo'sh"}, status_code=400)
    with SessionLocal() as db:
        try:
            answer = assistant.ask(db, question, body.get("history", []))
        except analyzer.AnalyzerError as e:
            return JSONResponse({"error": str(e)}, status_code=503)
    return {"answer": answer}


@app.get("/api/calls/{call_id}/status")
def call_status(call_id: int):
    with SessionLocal() as db:
        c = db.get(Call, call_id)
        return {"status": c.status if c else None}


# ---------- Cron (Vercel Cron yoki tashqi cron xizmati chaqiradi) ----------

_last_ua_cron = {"at": float("-inf")}


def cron_authorized(request: Request) -> bool:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("key", "")
    for secret in (config.CRON_SECRET, getattr(config, "CRON_KEY", "")):
        if secret and token and hmac.compare_digest(token, secret):
            return True
    # CRON_SECRET qo'yilmagan bo'lsa Vercel Cron sarlavhasiz keladi — uni taniymiz, lekin tez-tez chaqirtirmaymiz
    if not config.CRON_SECRET and config.IS_VERCEL and request.headers.get("user-agent", "").startswith("vercel-cron"):
        import time as _t
        if _t.monotonic() - _last_ua_cron["at"] > 120:
            _last_ua_cron["at"] = _t.monotonic()
            return True
    return False


@app.get("/api/cron/sync")
def cron_sync(request: Request):
    if not cron_authorized(request):
        return JSONResponse({"error": "ruxsat yo'q"}, status_code=401)
    return run_sync()


@app.get("/api/cron/report")
def cron_report(request: Request):
    if not cron_authorized(request):
        return JSONResponse({"error": "ruxsat yo'q"}, status_code=401)
    return {"sent": pipeline.send_daily_report()}
