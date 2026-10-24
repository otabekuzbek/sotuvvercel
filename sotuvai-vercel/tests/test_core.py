"""Asosiy oqimlar uchun testlar (tashqi API'lar soxtalashtirilgan)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(DATA_DIR=TMP, DEMO_MODE="true", ADMIN_PASSWORD="test-pass", SECRET_KEY="x" * 32,
                  ANTHROPIC_API_KEY="test", GOOGLE_API_KEY="test", AMO_SUBDOMAIN="demo",
                  AMO_LONG_LIVED_TOKEN="tok", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import amocrm, analyzer, pipeline, stt  # noqa: E402
from app.db import DEFAULT_SCRIPT, Call, Manager, SessionLocal, init_db  # noqa: E402

init_db()


def test_note_parsing():
    note = {"id": 5, "entity_id": 77, "note_type": "call_in", "created_at": 1760000000, "responsible_user_id": 3,
            "_entity": "contacts", "params": {"link": "https://x/rec.mp3", "duration": 125, "phone": "+998901234567"}}
    f = amocrm.note_to_call_fields(note)
    assert f["direction"] == "in" and f["duration"] == 125 and f["entity_type"] == "contacts" and f["amo_user_id"] == 3
    assert amocrm.note_to_call_fields({"id": 1, "params": {}}) is None


def test_weighted_score():
    stages = [{"key": s["key"], "score": 100 if s["key"] == "needs" else 0} for s in DEFAULT_SCRIPT]
    assert analyzer.weighted_score(stages, DEFAULT_SCRIPT) == 25.0


def test_merge_tracks():
    a = [{"speaker": "Kanal 1", "start": 0, "end": 2, "text": "Salom"}, {"speaker": "Kanal 1", "start": 2.5, "end": 4, "text": "qulaymi?"}]
    b = [{"speaker": "Kanal 2", "start": 5, "end": 6, "text": "Ha"}]
    out = stt.merge_tracks(a, b)
    assert [s["speaker"] for s in out] == ["Kanal 1", "Kanal 2"] and out[0]["text"] == "Salom qulaymi?"


def test_stereo_split_and_chunks(tmp_path):
    src = tmp_path / "in.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=300:d=130", "-f", "lavfi",
                    "-i", "sine=f=500:d=130", "-filter_complex", "[0][1]amerge=inputs=2", str(src)], check=True)
    assert stt.channel_count(src) == 2
    chunks = stt.split_to_chunks(src, tmp_path, 0)
    assert len(chunks) == 3  # 130 s -> 55 + 55 + 20


def fake_claude_tool(*args, **kwargs):
    return {"content": [{"type": "tool_use", "name": "submit_call_analysis", "input": {
        "manager_speaker": "Kanal 1", "is_sales_call": True, "summary": "Test xulosa", "outcome": "qayta_aloqa",
        "client_sentiment": "neytral", "stages": [{"key": s["key"], "score": 80, "comment": "ok"} for s in DEFAULT_SCRIPT],
        "strengths": ["a"], "weaknesses": ["b"], "recommendations": [{"title": "T", "detail": "D"}],
        "next_steps": [{"action": "Qayta qo'ng'iroq", "due_in_days": 1}]}}]}


def test_pipeline_end_to_end(monkeypatch):
    segs = [{"speaker": "Kanal 1", "start": 0, "end": 30, "text": "so'z " * 40}]
    monkeypatch.setattr(amocrm.AmoClient, "download_recording", lambda self, url: b"audio")
    monkeypatch.setattr(stt, "transcribe", lambda audio: segs)
    monkeypatch.setattr(analyzer, "claude", fake_claude_tool)
    created = {}
    monkeypatch.setattr(amocrm.AmoClient, "create_tasks", lambda self, t: created.setdefault("tasks", t))
    monkeypatch.setattr(amocrm.AmoClient, "add_note", lambda self, e, i, txt: created.setdefault("note", txt))
    with SessionLocal() as db:
        m = Manager(amo_user_id=555, name="Test")
        db.add(m)
        db.flush()
        c = Call(amo_note_id=999999, entity_type="leads", entity_id=1, manager_id=m.id, duration=90,
                 recording_url="https://x/r.mp3", status="new")
        db.add(c)
        db.commit()
        pipeline.process_call(db, c)
        assert c.status == "done", c.error
        assert c.overall_score == 80.0
        assert created["tasks"][0]["responsible_user_id"] == 555 and created["tasks"][0]["entity_type"] == "leads"
        assert "80/100" in created["note"]


def test_short_transcript_skipped(monkeypatch):
    monkeypatch.setattr(amocrm.AmoClient, "download_recording", lambda self, url: b"audio")
    monkeypatch.setattr(stt, "transcribe", lambda audio: [{"speaker": "Suhbat", "start": 0, "end": 1, "text": "alo"}])
    with SessionLocal() as db:
        c = Call(amo_note_id=999998, duration=40, recording_url="u", status="new")
        db.add(c)
        db.commit()
        pipeline.process_call(db, c)
        assert c.status == "skipped"


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "admin", "password": "test-pass"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


def test_login_required():
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).status_code in (302, 307)
        assert c.post("/login", data={"username": "admin", "password": "bad"}).status_code == 401


@pytest.mark.parametrize("path", ["/", "/?period=7", "/?period=30", "/calls", "/calls?score=low&status=done",
                                  "/managers", "/managers?period=30", "/script", "/settings", "/assistant"])
def test_pages(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]


def test_call_and_manager_detail(client):
    with SessionLocal() as db:
        c = db.query(Call).filter(Call.status == "done").first()
        m = db.query(Manager).first()
    assert client.get(f"/calls/{c.id}").status_code == 200
    assert client.get(f"/managers/{m.id}").status_code == 200


def test_script_save(client):
    r = client.post("/script", data={"key": ["greeting", ""], "name": ["Salom", "Yangi"], "weight": ["40", "60"],
                                     "criteria": ["a", "b"], "company_context": "Test kompaniya"}, follow_redirects=False)
    assert r.status_code == 303
    from app.db import get_script, kv_get
    with SessionLocal() as db:
        s = get_script(db)
        assert [x["name"] for x in s] == ["Salom", "Yangi"] and kv_get(db, "company_context") == "Test kompaniya"
        from app.db import kv_set
        kv_set(db, "script", DEFAULT_SCRIPT)


def test_assistant(client, monkeypatch):
    monkeypatch.setattr(analyzer, "claude", lambda *a, **k: {"content": [{"type": "text", "text": "Javob"}]})
    r = client.post("/api/assistant", json={"question": "Bugun qanday?", "history": [{"role": "assistant", "content": "x"}]})
    assert r.json() == {"answer": "Javob"}


def test_daily_report_text():
    with SessionLocal() as db:
        text = pipeline.daily_report_text(db)
    assert "Kunlik hisobot" in text


def test_cron_requires_secret(client):
    # login'siz va kalitsiz cron chaqiruvi rad etiladi
    from app.main import app
    with TestClient(app) as anon:
        assert anon.get("/api/cron/sync").status_code == 401


def test_channel_count_with_bundled_ffmpeg(tmp_path, monkeypatch):
    import imageio_ffmpeg
    src = tmp_path / "mono.wav"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=2", str(src)], check=True)
    monkeypatch.setattr(stt.shutil, "which", lambda name: None)  # tizimda ffmpeg yo'qdek
    assert stt.ffmpeg_bin() == imageio_ffmpeg.get_ffmpeg_exe()
    assert stt.channel_count(src) == 1


VERCEL_SCRIPT = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from app import pipeline
from app.main import app, scheduler
pipeline.sync_and_process = lambda time_budget=None: {"added": 0, "processed": 0, "remaining": 0, "error": None}
with TestClient(app, base_url="https://testserver") as c:
    assert not scheduler.running, "Vercel'da scheduler ishlamasligi kerak"
    assert c.get("/api/cron/sync").status_code == 401
    assert c.get("/api/cron/sync", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.get("/api/cron/sync", headers={"Authorization": "Bearer cron-123"})
    assert r.status_code == 200 and r.json()["processed"] == 0, r.text
    assert c.get("/api/cron/sync?key=cron-123").status_code == 200
    c.post("/login", data={"username": "admin", "password": "p"})
    r = c.post("/settings/sync", follow_redirects=False)
    assert r.status_code == 303 and "tahlil" in r.headers["location"], r.headers["location"]
    assert c.get("/static/chart.umd.js").status_code == 200
print("VERCEL_OK")
'''


def test_vercel_mode(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("DATA_DIR",)}
    env.update(VERCEL="1", CRON_SECRET="cron-123", ADMIN_PASSWORD="p", DEMO_MODE="false",
               DATABASE_URL=f"sqlite:///{tmp_path / 'v.db'}", VERCEL_PROJECT_PRODUCTION_URL="sotuv.vercel.app")
    out = subprocess.run([sys.executable, "-c", VERCEL_SCRIPT, str(Path(__file__).resolve().parent.parent)],
                         env=env, capture_output=True, text=True)
    assert "VERCEL_OK" in out.stdout, out.stderr[-2000:]


def test_zero_config_vercel(tmp_path):
    """Faqat VERCEL=1 va baza manzili: admin yaratish, kalitlarni ilovada kiritish, ikkinchi nusxada sessiya."""
    script = str(Path(__file__).with_name("zeroconf_check.py"))
    root = str(Path(__file__).resolve().parent.parent)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "VERCEL": "1",
           "DATABASE_URL": f"sqlite:///{tmp_path / 'zc.db'}"}
    one = subprocess.run([sys.executable, script, root, "1"], env=env, capture_output=True, text=True)
    lines = dict(l.split(" ", 1) for l in one.stdout.splitlines() if l.startswith(("COOKIE", "CRONKEY")))
    assert "COOKIE" in lines, one.stderr[-2000:]
    two = subprocess.run([sys.executable, script, root, "2", lines["COOKIE"], lines["CRONKEY"]], env=env,
                         capture_output=True, text=True)
    assert "STEP2_OK" in two.stdout, two.stderr[-2000:]


def test_vercel_without_database_shows_steps(tmp_path):
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from fastapi.testclient import TestClient; from app.main import app;"
            "c = TestClient(app, base_url='https://x'); r = c.get('/');"
            "print(r.status_code, 'Neon' in r.text, c.get('/static/style.css').status_code)")
    out = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parent.parent)],
                         env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "VERCEL": "1"},
                         capture_output=True, text=True)
    assert out.stdout.strip().endswith("503 True 200"), out.stderr[-2000:]
