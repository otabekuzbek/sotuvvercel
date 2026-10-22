"""Dashboard, hisobot va AI yordamchi uchun ko'rsatkichlar."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func

from . import config
from .db import Call, Manager, get_script


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Mahalliy kunning UTC chegaralari (bazada vaqt UTC'da)."""
    start = datetime.combine(day, time.min, tzinfo=config.TIMEZONE)
    end = start + timedelta(days=1)
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def today_local() -> date:
    return datetime.now(config.TIMEZONE).date()


def to_local(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(config.TIMEZONE)


def period_bounds(days: int) -> tuple[datetime, datetime]:
    end_day = today_local()
    start, _ = local_day_bounds(end_day - timedelta(days=days - 1))
    _, end = local_day_bounds(end_day)
    return start, end


def kpis(db, start: datetime, end: datetime) -> dict:
    q = db.query(Call).filter(Call.call_time >= start, Call.call_time < end)
    total = q.count()
    done = q.filter(Call.status == "done")
    analyzed = done.count()
    avg = done.with_entities(func.avg(Call.overall_score)).scalar()
    pending = q.filter(Call.status.in_(["new", "transcribing", "analyzing"])).count()
    return {"total": total, "analyzed": analyzed, "avg": round(avg or 0, 1), "pending": pending,
            "minutes": round((q.with_entities(func.sum(Call.duration)).scalar() or 0) / 60)}


def manager_table(db, start: datetime, end: datetime) -> list[dict]:
    script = get_script(db)
    rows = []
    for m in db.query(Manager).filter(Manager.active.is_(True)).all():
        calls = [c for c in m.calls if c.call_time and start <= c.call_time < end]
        done = [c for c in calls if c.status == "done" and c.overall_score is not None]
        if not calls:
            continue
        stage_sum, stage_n = defaultdict(float), defaultdict(int)
        for c in done:
            for st in c.analysis.get("stages", []):
                stage_sum[st["key"]] += st["score"]
                stage_n[st["key"]] += 1
        stages = {s["key"]: round(stage_sum[s["key"]] / stage_n[s["key"]]) if stage_n[s["key"]] else None
                  for s in script}
        weakest = min(((k, v) for k, v in stages.items() if v is not None), key=lambda kv: kv[1], default=None)
        avg = round(sum(c.overall_score for c in done) / len(done), 1) if done else None
        rows.append({
            "manager": m, "calls": len(calls), "analyzed": len(done), "avg": avg,
            "minutes": round(sum(c.duration or 0 for c in calls) / 60),
            "stages": stages,
            "weakest": next((s["name"] for s in script if weakest and s["key"] == weakest[0]), None),
            "needs_coaching": avg is not None and avg < config.COACHING_THRESHOLD,
            "sales": sum(1 for c in done if c.analysis.get("outcome") in ("sotuv", "kelishuv")),
        })
    rows.sort(key=lambda r: (r["avg"] is None, -(r["avg"] or 0)))
    return rows


def stage_averages(db, start: datetime, end: datetime) -> list[dict]:
    script = get_script(db)
    sums, counts = defaultdict(float), defaultdict(int)
    for c in db.query(Call).filter(Call.status == "done", Call.call_time >= start, Call.call_time < end):
        for st in c.analysis.get("stages", []):
            sums[st["key"]] += st["score"]
            counts[st["key"]] += 1
    return [{"name": s["name"], "avg": round(sums[s["key"]] / counts[s["key"]]) if counts[s["key"]] else 0}
            for s in script]


def daily_trend(db, days: int = 14) -> list[dict]:
    out = []
    for i in range(days - 1, -1, -1):
        day = today_local() - timedelta(days=i)
        start, end = local_day_bounds(day)
        k = kpis(db, start, end)
        out.append({"day": day.strftime("%d.%m"), "calls": k["total"], "avg": k["avg"] if k["analyzed"] else None})
    return out


def outcome_counts(db, start: datetime, end: datetime) -> dict:
    counts = defaultdict(int)
    for c in db.query(Call).filter(Call.status == "done", Call.call_time >= start, Call.call_time < end):
        counts[c.analysis.get("outcome", "boshqa")] += 1
    return dict(counts)
