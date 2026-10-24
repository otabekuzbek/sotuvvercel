"""Vercel'ga hech qanday o'zgaruvchisiz deploy: faqat VERCEL=1 va Neon bergan POSTGRES_URL."""
import json, os, sys
sys.path.insert(0, sys.argv[1])
step = sys.argv[2]
from fastapi.testclient import TestClient
from app.main import app
from app import config
c = TestClient(app, base_url="https://sotuv.vercel.app")
c.__enter__()
if step == "1":
    r = c.get("/", follow_redirects=False); assert r.headers["location"] == "/setup", r.headers
    assert "Xush kelibsiz" in c.get("/setup").text
    assert c.post("/setup", data={"username": "boss", "password": "short", "password2": "short"}).status_code == 400
    r = c.post("/setup", data={"username": "boss", "password": "Parol12345", "password2": "Parol12345"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings"), r.headers
    page = c.get("/settings").text
    assert "Ulanishlar va kalitlar" in page and "Google STT" in page
    r = c.post("/settings/integrations", data={"AMO_SUBDOMAIN": "mycomp", "AMO_LONG_LIVED_TOKEN": "tok-secret-9876",
           "GOOGLE_API_KEY": "g-key-1111", "ANTHROPIC_API_KEY": "sk-ant-2222", "MIN_CALL_SECONDS": "45",
           "AMO_ADD_NOTES": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert config.AMO_SUBDOMAIN == "mycomp" and config.MIN_CALL_SECONDS == 45 and config.AMO_CREATE_TASKS is False
    # maxfiy maydon bo'sh yuborilsa eski qiymat qoladi
    c.post("/settings/integrations", data={"AMO_SUBDOMAIN": "mycomp", "GOOGLE_API_KEY": ""})
    assert config.GOOGLE_API_KEY == "g-key-1111"
    page = c.get("/settings").text
    assert "tok-secret-9876" not in page and "••••9876" in page
    # ikkinchi admin yaratib bo'lmaydi
    c2 = TestClient(app, base_url="https://sotuv.vercel.app")
    assert c2.post("/setup", data={"username": "hacker", "password": "x"*10, "password2": "x"*10}, follow_redirects=False).headers["location"] == "/login"
    print("COOKIE", json.dumps(dict(c.cookies)))
    print("CRONKEY", config.CRON_KEY)
else:
    # yangi serverless nusxa: o'sha kalit va sozlamalar bazadan olinadi, eski sessiya amal qiladi
    cookies, cron_key = json.loads(sys.argv[3]), sys.argv[4]
    c.cookies.update(cookies)
    r = c.get("/settings", follow_redirects=False); assert r.status_code == 200, (r.status_code, r.headers)
    assert config.AMO_SUBDOMAIN == "mycomp" and config.CRON_KEY == cron_key
    from app import pipeline
    pipeline.sync_and_process = lambda time_budget=None: {"added": 0, "processed": 0, "remaining": 0, "error": None}
    assert c.get("/api/cron/sync").status_code == 401
    assert c.get(f"/api/cron/sync?key={cron_key}").status_code == 200
    assert c.get("/api/cron/sync", headers={"user-agent": "vercel-cron/1.0"}).status_code == 200
    assert c.get("/api/cron/sync", headers={"user-agent": "vercel-cron/1.0"}).status_code == 401  # tez-tez emas
    anon = TestClient(app, base_url="https://sotuv.vercel.app")
    assert anon.post("/login", data={"username": "boss", "password": "wrong"}).status_code == 401
    assert anon.post("/login", data={"username": "boss", "password": "Parol12345"}, follow_redirects=False).status_code == 303
    print("STEP2_OK")
