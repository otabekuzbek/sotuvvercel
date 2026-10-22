"""Barcha sozlamalar .env faylidan o'qiladi."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, str(default)).lower() in ("1", "true", "yes", "ha")


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


APP_NAME = env("APP_NAME", "SotuvAI")
SECRET_KEY = env("SECRET_KEY", "change-me-please")
ADMIN_USERNAME = env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = env("ADMIN_PASSWORD", "admin")
# Vercel'da PUBLIC_URL berilmasa, loyihaning asosiy domeni avtomatik olinadi
_vercel_host = env("VERCEL_PROJECT_PRODUCTION_URL")
PUBLIC_URL = env("PUBLIC_URL", f"https://{_vercel_host}" if _vercel_host else "http://localhost:8000").rstrip("/")
TIMEZONE = ZoneInfo(env("TIMEZONE", "Asia/Tashkent"))

# Vercel serverless muhitida ishlayaptimi (Vercel o'zi VERCEL=1 qo'yadi)
IS_VERCEL = bool(env("VERCEL"))

# Vercel'da faqat /tmp yoziladi va u vaqtinchalik — doimiy ma'lumot Postgres'da saqlanadi
DATA_DIR = Path(env("DATA_DIR", "/tmp/sotuvai" if IS_VERCEL else str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _database_url() -> str:
    # Vercel Marketplace'dagi Neon/Postgres DATABASE_URL yoki POSTGRES_URL beradi
    url = env("DATABASE_URL") or env("POSTGRES_URL")
    if not url:
        return f"sqlite:///{DATA_DIR / 'sotuvai.db'}"
    # Drayverni aniq ko'rsatamiz (psycopg2-binary o'rnatilgan)
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


DATABASE_URL = _database_url()

# Cron so'rovlarini himoyalash: Vercel Cron shu qiymatni "Authorization: Bearer ..." bilan yuboradi
CRON_SECRET = env("CRON_SECRET")

# amoCRM
AMO_SUBDOMAIN = env("AMO_SUBDOMAIN")            # masalan: mycompany  (mycompany.amocrm.ru)
AMO_BASE_DOMAIN = env("AMO_BASE_DOMAIN", "amocrm.ru")
AMO_CLIENT_ID = env("AMO_CLIENT_ID")
AMO_CLIENT_SECRET = env("AMO_CLIENT_SECRET")
AMO_LONG_LIVED_TOKEN = env("AMO_LONG_LIVED_TOKEN")
AMO_REDIRECT_URI = env("AMO_REDIRECT_URI", f"{PUBLIC_URL}/oauth/amocrm/callback")
AMO_CREATE_TASKS = env_bool("AMO_CREATE_TASKS", True)     # keyingi qadamlarni vazifa qilib yaratish
AMO_ADD_NOTES = env_bool("AMO_ADD_NOTES", True)           # tahlil xulosasini lid kartochkasiga yozish

# Google Speech-to-Text
GOOGLE_API_KEY = env("GOOGLE_API_KEY")
STT_LANGUAGE = env("STT_LANGUAGE", "uz-UZ")
STT_ALT_LANGUAGES = [x.strip() for x in env("STT_ALT_LANGUAGES", "ru-RU").split(",") if x.strip()]
STT_SPLIT_CHANNELS = env_bool("STT_SPLIT_CHANNELS", True)  # stereo yozuvda har kanal alohida (menejer / mijoz)

# Claude
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")

# Telegram
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")
REPORT_TIME = env("REPORT_TIME", "19:00")
LOW_SCORE_ALERT = env_int("LOW_SCORE_ALERT", 50)   # shundan past baho bo'lsa darhol ogohlantirish (0 = o'chiq)

# Ish jarayoni
SYNC_INTERVAL_MIN = env_int("SYNC_INTERVAL_MIN", 5)
MIN_CALL_SECONDS = env_int("MIN_CALL_SECONDS", 30)
PROCESS_BATCH = env_int("PROCESS_BATCH", 10)
# Bitta cron chaqiruvida qayta ishlashga ajratilgan vaqt (Vercel Hobby funksiyasi maksimal 300 s)
PROCESS_TIME_BUDGET = env_int("PROCESS_TIME_BUDGET", 230)
COACHING_THRESHOLD = env_int("COACHING_THRESHOLD", 65)  # o'rtacha bahosi shundan past menejerga kouching kerak
DEMO_MODE = env_bool("DEMO_MODE", False)
