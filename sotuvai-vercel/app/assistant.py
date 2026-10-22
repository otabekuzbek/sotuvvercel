"""AI yordamchi: rahbar savollariga sotuv ma'lumotlari asosida javob beradi."""
from datetime import timedelta

from . import analyzer
from .db import Call
from .stats import kpis, local_day_bounds, manager_table, period_bounds, stage_averages, today_local, to_local

SYSTEM = """Siz sotuv bo'limi rahbarining AI yordamchisisiz. Faqat quyida berilgan ma'lumotlarga tayanib,
o'zbek tilida qisqa va aniq javob bering. Raqamlarni ma'lumotdan oling, o'ylab topmang. Ma'lumot yetarli
bo'lmasa, shuni ayting. Kouching so'ralsa, menejerning eng zaif bosqichi va real qo'ng'iroqlaridagi
xatolariga asoslanib amaliy maslahat bering."""


def build_context(db) -> str:
    parts = []
    for label, (start, end) in {
        "BUGUN": local_day_bounds(today_local()),
        "KECHA": local_day_bounds(today_local() - timedelta(days=1)),
        "OXIRGI 7 KUN": period_bounds(7),
        "OXIRGI 30 KUN": period_bounds(30),
    }.items():
        k = kpis(db, start, end)
        parts.append(f"## {label}: {k['total']} qo'ng'iroq, {k['analyzed']} tahlil, o'rtacha {k['avg']}%, "
                     f"{k['minutes']} daqiqa")
        for r in manager_table(db, start, end):
            stages = ", ".join(f"{name}: {v}" for name, v in r["stages"].items() if v is not None)
            parts.append(f"- {r['manager'].name}: {r['calls']} qo'ng'iroq, o'rtacha {r['avg']}, "
                         f"sotuv/kelishuv {r['sales']}, eng zaif: {r['weakest']}, bosqichlar [{stages}]")
    start, end = period_bounds(7)
    parts.append("## Bosqichlar o'rtachasi (7 kun): " +
                 ", ".join(f"{s['name']} {s['avg']}" for s in stage_averages(db, start, end)))
    parts.append("## Oxirgi tahlil qilingan qo'ng'iroqlar:")
    recent = (db.query(Call).filter(Call.status == "done").order_by(Call.call_time.desc()).limit(25).all())
    for c in recent:
        a = c.analysis
        weak = "; ".join(a.get("weaknesses", [])[:2])
        parts.append(f"- #{c.id} {to_local(c.call_time or c.created_at):%d.%m %H:%M} {c.manager.name if c.manager else '?'} "
                     f"{(c.overall_score or 0):.0f}% [{a.get('outcome')}] {a.get('summary', '')[:200]} | Zaif: {weak}")
    return "\n".join(parts)


def ask(db, question: str, history: list[dict]) -> str:
    context = build_context(db)
    messages = [{"role": m["role"], "content": str(m["content"])[:4000]} for m in history[-8:]
                if m.get("role") in ("user", "assistant") and m.get("content")]
    while messages and (messages[0]["role"] != "user" or messages[-1]["role"] != "assistant"):
        messages.pop(0) if messages[0]["role"] != "user" else messages.pop()
    messages.append({"role": "user", "content": f"MA'LUMOTLAR:\n{context}\n\nSAVOL: {question}"})
    return analyzer.claude_text(messages, SYSTEM, max_tokens=1500)
