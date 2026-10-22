"""Ma'lumotlar bazasi modellari (SQLite yoki PostgreSQL)."""
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import NullPool

from . import config

if config.DATABASE_URL.startswith("sqlite"):
    engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False}, future=True)
elif config.IS_VERCEL:
    # Serverless: har so'rovda yangi ulanish, eski ulanishlar osilib qolmaydi
    engine = create_engine(config.DATABASE_URL, poolclass=NullPool, future=True)
else:
    engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, pool_size=5, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Manager(Base):
    __tablename__ = "managers"
    id = Column(Integer, primary_key=True)
    amo_user_id = Column(Integer, unique=True, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200))
    active = Column(Boolean, default=True)
    calls = relationship("Call", back_populates="manager")


class Call(Base):
    __tablename__ = "calls"
    id = Column(Integer, primary_key=True)
    amo_note_id = Column(Integer, unique=True, index=True)
    entity_type = Column(String(20))          # leads / contacts
    entity_id = Column(Integer)
    manager_id = Column(Integer, ForeignKey("managers.id"), index=True)
    direction = Column(String(10))            # in / out
    phone = Column(String(50))
    duration = Column(Integer, default=0)      # soniya
    recording_url = Column(Text)
    call_time = Column(DateTime, index=True, default=utcnow)   # UTC
    # new -> transcribing -> analyzing -> done | failed | skipped
    status = Column(String(20), default="new", index=True)
    error = Column(Text)
    attempts = Column(Integer, default=0)
    transcript_json = Column(Text)            # [{"speaker": "A", "start": 0.0, "text": "..."}]
    overall_score = Column(Float, index=True)
    analysis_json = Column(Text)
    tasks_created = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    analyzed_at = Column(DateTime)

    manager = relationship("Manager", back_populates="calls")

    @property
    def transcript(self) -> list[dict]:
        return json.loads(self.transcript_json) if self.transcript_json else []

    @property
    def analysis(self) -> dict:
        return json.loads(self.analysis_json) if self.analysis_json else {}


class KV(Base):
    """Kichik sozlamalar: amoCRM tokenlari, skript, kompaniya konteksti."""
    __tablename__ = "kv"
    key = Column(String(100), primary_key=True)
    value = Column(Text)


def kv_get(db, key: str, default: Any = None) -> Any:
    row = db.get(KV, key)
    return json.loads(row.value) if row and row.value is not None else default


def kv_set(db, key: str, value: Any) -> None:
    row = db.get(KV, key)
    data = json.dumps(value, ensure_ascii=False)
    if row:
        row.value = data
    else:
        db.add(KV(key=key, value=data))
    db.commit()


DEFAULT_SCRIPT = [
    {"key": "greeting", "name": "Salomlashish va tanishtirish", "weight": 15,
     "criteria": "Menejer salomlashdi, o'zini va kompaniyani tanishtirdi, mijozning ismini so'radi va ishlatdi, qo'ng'iroq maqsadini aytdi."},
    {"key": "needs", "name": "Ehtiyojni aniqlash", "weight": 25,
     "criteria": "Ochiq savollar berdi, mijozning muammosi, maqsadi, byudjeti va muddatini aniqladi, faol tingladi, gapni bo'lmadi."},
    {"key": "presentation", "name": "Taklifni tushuntirish", "weight": 20,
     "criteria": "Mahsulotni mijozning ehtiyojiga bog'lab tushuntirdi, xususiyat emas foyda haqida gapirdi, narxni qiymat bilan asosladi."},
    {"key": "objections", "name": "E'tirozlar bilan ishlash", "weight": 20,
     "criteria": "E'tirozni tingladi, qo'shildi, aniqlashtiruvchi savol berdi va dalil bilan javob berdi. Bahslashmadi."},
    {"key": "closing", "name": "Yakunlash va kelishuv", "weight": 20,
     "criteria": "Aniq keyingi qadamni taklif qildi (to'lov, uchrashuv, qayta qo'ng'iroq sanasi), kelishuvni mustahkamladi, xayrlashdi."},
]

DEFAULT_COMPANY_CONTEXT = (
    "Kompaniya nomi, mahsulot/xizmatlar, narxlar, asosiy afzalliklar va tez-tez uchraydigan "
    "e'tirozlarni shu yerga yozing. AI shu ma'lumotga tayanib tavsiya beradi."
)


def get_script(db) -> list[dict]:
    return kv_get(db, "script", DEFAULT_SCRIPT)


def init_db() -> None:
    Base.metadata.create_all(engine)
