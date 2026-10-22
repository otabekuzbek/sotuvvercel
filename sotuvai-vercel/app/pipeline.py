"""Qo'ng'iroqni boshidan oxirigacha qayta ishlash: yozuv -> matn -> AI tahlil -> amoCRM."""
import html
import logging
import time
from datetime import datetime, timedelta

from . import amocrm, analyzer, config, stt, telegram
from .db import Call, SessionLocal, get_script, kv_get, utcnow, DEFAULT_COMPANY_CONTEXT
from .stats import kpis, local_day_bounds, manager_table, today_local

log = logging.getLogger("pipeline")
MIN_WORDS = 25


def process_call(db, call: Call) -> None:
    call.attempts = (call.attempts or 0) + 1
    try:
        client = amocrm.AmoClient(db)
        if not call.transcript_json:
            call.status = "transcribing"
            db.commit()
            audio = client.download_recording(call.recording_url)
            segments = stt.transcribe(audio)
            call.transcript_json = analyzer.dumps(segments)
            db.commit()
        segments = call.transcript
        words = sum(len(s["text"].split()) for s in segments)
        if words < MIN_WORDS:
            call.status, call.error = "skipped", f"Suhbat juda qisqa yoki tanilmadi ({words} so'z)"
            db.commit()
            return

        call.status = "analyzing"
        db.commit()
        script = get_script(db)
        result = analyzer.analyze(
            segments, script, kv_get(db, "company_context", DEFAULT_COMPANY_CONTEXT),
            {"direction": call.direction, "duration": call.duration,
             "manager": call.manager.name if call.manager else None},
        )
        if not result.get("is_sales_call", True):
            call.status, call.error = "skipped", "Sotuv suhbati emas: " + result.get("summary", "")[:200]
            call.analysis_json = analyzer.dumps(result)
            db.commit()
            return
        call.analysis_json = analyzer.dumps(result)
        call.overall_score = result["overall_score"]
        call.status, call.error, call.analyzed_at = "done", None, utcnow()
        db.commit()
        push_to_amocrm(db, client, call)
        alert_low_score(call)
    except Exception as e:  # noqa: BLE001 — har qanday xatoni qo'ng'iroqqa yozib qo'yamiz
        log.exception("Qo'ng'iroq #%s qayta ishlanmadi", call.id)
        call.status, call.error = "failed", str(e)[:1000]
        db.commit()


def push_to_amocrm(db, client: "amocrm.AmoClient", call: Call) -> None:
    a = call.analysis
    url = f"{config.PUBLIC_URL}/calls/{call.id}"
    try:
        if config.AMO_ADD_NOTES and call.entity_id:
            client.add_note(call.entity_type, call.entity_id, analyzer.analysis_note_text(a, url))
        if config.AMO_CREATE_TASKS and call.entity_id and not call.tasks_created and a.get("next_steps"):
            responsible = call.manager.amo_user_id if call.manager else None
            tasks = []
            for step in a["next_steps"][:3]:
                due = datetime.now(config.TIMEZONE) + timedelta(days=max(step.get("due_in_days", 1), 0))
                due = due.replace(hour=18, minute=0, second=0)
                task = {"text": f"[AI] {step['action']}", "complete_till": int(due.timestamp()),
                        "entity_id": call.entity_id, "entity_type": call.entity_type, "task_type_id": 1}
                if responsible:
                    task["responsible_user_id"] = responsible
                tasks.append(task)
            client.create_tasks(tasks)
            call.tasks_created = True
            db.commit()
    except Exception as e:  # noqa: BLE001 — tahlil saqlangan, amoCRM'ga yozish ixtiyoriy
        log.warning("amoCRM'ga yozilmadi (#%s): %s", call.id, e)


def alert_low_score(call: Call) -> None:
    if not config.LOW_SCORE_ALERT or call.overall_score is None or call.overall_score >= config.LOW_SCORE_ALERT:
        return
    name = html.escape(call.manager.name if call.manager else "Nomaʼlum")
    summary = html.escape(call.analysis.get("summary", ""))
    telegram.send(f"⚠️ <b>Past baho: {call.overall_score:.0f}/100</b>\nMenejer: {name}\n{summary}\n"
                  f"{config.PUBLIC_URL}/calls/{call.id}")


def sync_and_process(time_budget: int | None = None) -> dict:
    """Yangi qo'ng'iroqlarni olib, navbatdagilarni vaqt chegarasi ichida qayta ishlaydi.

    Rejalashtiruvchi (Docker) yoki cron endpoint (Vercel) chaqiradi.
    """
    deadline = time.monotonic() + (time_budget or config.PROCESS_TIME_BUDGET)
    db = SessionLocal()
    result = {"added": 0, "processed": 0, "remaining": 0, "error": None}
    try:
        if amocrm.is_configured() and amocrm.is_connected(db):
            result["added"] = amocrm.sync_calls(db)
        # Serverless funksiya yarim yo'lda to'xtagan bo'lsa, osilib qolgan qo'ng'iroqlarni navbatga qaytaramiz
        stale = utcnow() - timedelta(minutes=15)
        for c in db.query(Call).filter(Call.status.in_(["transcribing", "analyzing"]), Call.updated_at < stale):
            c.status = "new"
        db.commit()
        pending_q = db.query(Call).filter(Call.status.in_(["new", "failed"]), Call.attempts < 3)
        for call in pending_q.order_by(Call.call_time.desc()).limit(config.PROCESS_BATCH).all():
            # Bitta qo'ng'iroq ~30-90 s oladi; vaqt qolmasa keyingi chaqiruvga qoldiramiz
            if time.monotonic() > deadline - 90:
                break
            process_call(db, call)
            result["processed"] += 1
        result["remaining"] = pending_q.count()
    except Exception as e:  # noqa: BLE001
        log.exception("Sinxronlash xatosi")
        result["error"] = str(e)
    finally:
        db.close()
    return result


def daily_report_text(db) -> str:
    start, end = local_day_bounds(today_local())
    k = kpis(db, start, end)
    rows = manager_table(db, start, end)
    lines = [f"📊 <b>Kunlik hisobot — {today_local():%d.%m.%Y}</b>",
             f"Qo'ng'iroqlar: {k['total']} (tahlil qilindi: {k['analyzed']})",
             f"O'rtacha baho: {k['avg']}%", f"Gaplashilgan vaqt: {k['minutes']} daqiqa", ""]
    if rows:
        lines.append("<b>Menejerlar reytingi:</b>")
        for i, r in enumerate(rows, 1):
            avg = f"{r['avg']:.0f}%" if r["avg"] is not None else "—"
            flag = " ⚠️" if r["needs_coaching"] else ""
            lines.append(f"{i}. {html.escape(r['manager'].name)} — {avg}, {r['calls']} ta{flag}")
        coach = [r for r in rows if r["needs_coaching"]]
        if coach:
            lines.append("")
            lines.append("<b>Kouching kerak:</b>")
            lines += [f"• {html.escape(r['manager'].name)}: {html.escape(r['weakest'] or '')}" for r in coach]
    lines.append(f"\n{config.PUBLIC_URL}")
    return "\n".join(lines)


def send_daily_report() -> bool:
    db = SessionLocal()
    try:
        return telegram.send(daily_report_text(db))
    finally:
        db.close()
