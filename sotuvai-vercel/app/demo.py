"""Demo ma'lumotlar: kalitlarsiz ham dashboardni ko'rish uchun (DEMO_MODE=true)."""
import random
from datetime import datetime, timedelta, timezone

from .analyzer import dumps, weighted_score
from .db import DEFAULT_SCRIPT, Call, Manager, SessionLocal, init_db

MANAGERS = [("Sardor Karimov", 82), ("Dilnoza Rahimova", 76), ("Javohir Tursunov", 68),
            ("Madina Yusupova", 61), ("Bekzod Aliyev", 54)]

SUMMARIES = [
    "Mijoz kurs narxi va to'lov bo'lib to'lash imkoniyati haqida so'radi. Menejer tariflarni tushuntirdi, mijoz ertaga javob berishini aytdi.",
    "Kiruvchi qo'ng'iroq: mijoz saytdan ariza qoldirgan. Ehtiyoj aniqlandi, demo uchrashuv payshanbaga belgilandi.",
    "Mijoz narxni qimmat deb hisobladi. Menejer chegirma taklif qildi, lekin qiymatni asoslamadi; mijoz o'ylab ko'rishini aytdi.",
    "Qayta aloqa qo'ng'iroqi: mijoz shartnomani tasdiqladi va to'lov havolasini Telegram orqali so'radi.",
    "Mijoz raqobatchi taklifini aytdi. Menejer farqlarni yaxshi tushuntirdi va bepul sinov muddatini taklif qildi.",
]
WEAK = ["Ehtiyojni aniqlashda yopiq savollar ko'p berildi", "Narx mijozning foydasi bilan bog'lanmadi",
        "Keyingi qadam aniq sana bilan kelishilmadi", "Mijozning gapi bir necha marta bo'lindi",
        "Kompaniya va o'zini tanishtirmadi"]
STRONG = ["Xushmuomala va ishonchli ohang", "Mahsulotni yaxshi biladi", "Mijozning ismini ishlatdi",
          "E'tirozga sabr bilan javob berdi"]
RECS = [
    ("Ehtiyojni ochiq savollar bilan aniqlang", "Taklifdan oldin kamida 3 ta ochiq savol bering.",
     "Sizga bu xizmat qaysi muammoni hal qilishi uchun kerak?"),
    ("Narxni qiymat bilan asoslang", "Narxni aytishdan oldin mijoz oladigan natijani eslating.",
     "Bu tarif bilan siz oyiga taxminan 20 soat vaqt tejaysiz, narxi esa 640 ming so'm."),
    ("Har bir qo'ng'iroqni aniq keyingi qadam bilan yakunlang", "Sana va vaqtni mijoz bilan birga belgilang.",
     "Ertaga soat 11:00 da qo'ng'iroq qilsam qulaymi?"),
    ("E'tirozni avval tan oling", "Bahslashmasdan, avval mijozning fikriga qo'shiling, keyin aniqlashtiring.",
     "Tushunaman, narx muhim. Aynan nima bilan solishtiryapsiz?"),
]
TRANSCRIPT = [
    ("Kanal 1", "Assalomu alaykum, kompaniyamizdan Sardor, sizga qulaymi?"),
    ("Kanal 2", "Va alaykum assalom, ha, eshitaman."),
    ("Kanal 1", "Saytimizda ariza qoldirgan ekansiz, qaysi xizmat qiziqtirdi?"),
    ("Kanal 2", "Narxlarini bilmoqchi edim, bo'lib to'lash bormi?"),
    ("Kanal 1", "Ha, bor. Avval aytingchi, xizmat sizga qaysi maqsadda kerak?"),
    ("Kanal 2", "Do'konimiz uchun, sotuvni hisobga olish qiyin bo'lyapti."),
    ("Kanal 1", "Tushunarli. Unda Business tarifi to'g'ri keladi, oyiga 640 ming so'm."),
    ("Kanal 2", "Biroz qimmat ekan, o'ylab ko'raman."),
    ("Kanal 1", "Mayli, ertaga soat 11 da qo'ng'iroq qilsam bo'ladimi?"),
    ("Kanal 2", "Bo'ladi, rahmat."),
]


def fake_analysis(rng: random.Random, skill: int) -> dict:
    stages = []
    for s in DEFAULT_SCRIPT:
        score = max(15, min(98, int(rng.gauss(skill + rng.choice([-12, -5, 0, 5]), 10))))
        stages.append({"key": s["key"], "name": s["name"], "score": score,
                       "comment": "Transkriptga ko'ra bosqich " + ("yaxshi bajarildi." if score >= 75 else
                                                                   "qisman bajarildi." if score >= 50 else "deyarli bajarilmadi.")})
    outcome = rng.choices(["sotuv", "kelishuv", "qayta_aloqa", "rad_etildi", "malumot"], [skill / 8, 3, 4, 2, 2])[0]
    recs = rng.sample(RECS, 2)
    return {
        "manager_speaker": "Kanal 1", "is_sales_call": True, "summary": rng.choice(SUMMARIES),
        "outcome": outcome, "client_sentiment": rng.choice(["ijobiy", "neytral", "neytral", "salbiy"]),
        "client_needs": rng.sample(["bo'lib to'lash", "tez yetkazish", "sinov muddati", "o'qitish"], 2),
        "stages": stages, "strengths": rng.sample(STRONG, 2), "weaknesses": rng.sample(WEAK, 2),
        "recommendations": [{"title": t, "detail": d, "example_phrase": e} for t, d, e in recs],
        "objections": [{"objection": "Narx qimmat", "handled": skill > 70,
                        "better_response": "Qiymatni hisoblab ko'rsating va bo'lib to'lashni taklif qiling."}],
        "next_steps": [{"action": "Mijozga qayta qo'ng'iroq qilish", "due_in_days": 1}],
        "red_flags": [] if skill > 58 or rng.random() > .2 else ["Mijozning gapini bo'lib, ovozini ko'tardi"],
        "overall_score": weighted_score(stages, DEFAULT_SCRIPT),
    }


def seed_if_empty(days: int = 14) -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(Call).count():
            return
        rng = random.Random(42)
        managers = []
        for i, (name, skill) in enumerate(MANAGERS):
            m = Manager(amo_user_id=900000 + i, name=name, email=f"user{i+1}@example.com")
            db.add(m)
            managers.append((m, skill))
        db.flush()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        note_id = 1
        for d in range(days, -1, -1):
            for m, skill in managers:
                for _ in range(rng.randint(3, 9) if d else rng.randint(2, 5)):
                    t = now - timedelta(days=d, hours=rng.uniform(0, 9 if d else 4))
                    a = fake_analysis(rng, skill)
                    segs = [{"speaker": sp, "start": i * 7.0, "end": i * 7.0 + 6, "text": tx}
                            for i, (sp, tx) in enumerate(TRANSCRIPT)]
                    db.add(Call(amo_note_id=note_id, entity_type="leads", entity_id=10000 + note_id, manager_id=m.id,
                                direction=rng.choice(["in", "out"]), phone=f"+99890{rng.randint(1000000, 9999999)}",
                                duration=rng.randint(60, 600), recording_url="", call_time=t, status="done",
                                transcript_json=dumps(segs), analysis_json=dumps(a), overall_score=a["overall_score"],
                                analyzed_at=t))
                    note_id += 1
        db.commit()
    finally:
        db.close()
