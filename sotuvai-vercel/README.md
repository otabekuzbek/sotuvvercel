# SotuvAI — sotuv qo'ng'iroqlarini AI bilan tahlil qilish platformasi

amoCRM'dagi qo'ng'iroq yozuvlarini avtomatik oladi, Google Speech-to-Text bilan matnga o'giradi (o'zbek + rus),
Claude bilan sotuv skriptingiz bo'yicha baholaydi va har bir menejerga aniq tavsiyalar beradi.

## Nima qiladi

| Qism | Tavsif |
|---|---|
| **amoCRM integratsiya** | OAuth yoki uzoq muddatli token. Har 5 daqiqada lid va kontaktlardagi yangi qo'ng'iroqlar (`call_in`, `call_out`) va menejerlar sinxronlanadi |
| **Nutq → matn** | Google STT, `uz-UZ` + `ru-RU`. Stereo yozuvda menejer va mijoz kanallari alohida tanilib, kim gapirgani aniq bo'ladi |
| **AI tahlil** | Skriptning har bosqichi 0–100 ball, vaznli umumiy baho, xulosa, natija (sotuv/kelishuv/…), yaxshi va zaif tomonlar, e'tirozlar va ularga yaxshiroq javob, misol gaplar bilan tavsiyalar, keyingi qadamlar |
| **amoCRM'ga qaytarish** | Lid kartochkasiga AI xulosasi izoh sifatida yoziladi, keyingi qadamlar mas'ul menejerga vazifa qilib qo'yiladi |
| **Dashboard** | Bugun/7/30 kun KPI, 14 kunlik dinamika, menejerlar reytingi, bosqichlar bo'yicha o'rtacha, eng past baholangan qo'ng'iroqlar |
| **Menejer sahifasi** | Bosqichlar bo'yicha kuchli/zaif joylar, baho dinamikasi, eng ko'p takrorlanadigan tavsiyalar |
| **AI yordamchi** | "Bugun natijalar qanday?", "Kimga kouching kerak?" kabi savollarga real ma'lumot asosida o'zbekcha javob |
| **Telegram** | Har kuni belgilangan vaqtda hisobot, past baholi qo'ng'iroqda darhol ogohlantirish |
| **Skript muharriri** | Bosqichlar, vaznlar, mezonlar va kompaniya haqida ma'lumotni interfeysdan o'zgartirish |

## Tezkor sinov (kalitlarsiz, demo ma'lumot bilan)

```bash
cp .env.example .env
# .env ichida: DEMO_MODE=true, ADMIN_PASSWORD=istalgan_parol
pip install -r requirements.txt      # ffmpeg ham o'rnatilgan bo'lishi kerak
uvicorn app.main:app --port 8000
```
`http://localhost:8000` ni oching, `admin` va parolingiz bilan kiring.

## Vercel'ga joylash (tavsiya etiladi)

Vercel serverless muhit: doimiy disk ham, fon jarayoni ham yo'q. Shuning uchun ilova u yerda shunday ishlaydi:
ma'lumotlar **Postgres**'da (Neon), qo'ng'iroqlarni olish va tahlil qilishni **cron** chaqiradi (`/api/cron/sync`),
ffmpeg esa `imageio-ffmpeg` paketi ichida keladi. Bir chaqiruv ~4 daqiqa ishlaydi va shu vaqtga sig'gancha
qo'ng'iroqni tahlil qiladi, qolgani keyingi chaqiruvga qoladi.

1. **GitHub** — loyihani o'z repozitoriyingizga yuklang (`.env` hech qachon yuklanmaydi, u `.gitignore`da).
2. **Vercel** → *Add New → Project* → repozitoriyni tanlang → *Framework Preset*: **FastAPI** (avtomatik aniqlanadi) → **Deploy**.
   Birinchi deploy muhit o'zgaruvchilarisiz xato berishi normal.
3. **Postgres**: loyiha → *Storage* → *Create Database* → **Neon** (bepul tarif yetadi) → loyihaga ulang.
   `DATABASE_URL` avtomatik qo'shiladi. Jadvallar birinchi ishga tushishda o'zi yaratiladi.
4. **Environment Variables** (*Settings → Environment Variables*, Production) — `.env.example` dagilarni kiriting. Eng kamida:
   `SECRET_KEY`, `ADMIN_PASSWORD`, `CRON_SECRET`, `AMO_SUBDOMAIN` + `AMO_LONG_LIVED_TOKEN` (yoki `AMO_CLIENT_ID`/`AMO_CLIENT_SECRET`),
   `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, ixtiyoriy `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.
   `DEMO_MODE` ni yoqmang — u bazaga soxta ma'lumot yozadi.
5. **Redeploy** (*Deployments → ⋯ → Redeploy*). Domen: `https://<loyiha>.vercel.app` yoki o'z domeningiz (*Settings → Domains*).
   OAuth ishlatsangiz amoCRM integratsiyasidagi Redirect URI: `https://<domen>/oauth/amocrm/callback`.
6. **Cron — muhim:**
   - `vercel.json` da ikkita kunlik cron bor: sinxronlash (UTC 13:00) va Telegram hisobot (UTC 14:00 = Toshkent 19:00).
   - **Hobby (bepul) tarifda cron kuniga faqat 1 marta** ishlaydi (±59 daqiqa aniqlik bilan). Qo'ng'iroqlar kun davomida tahlil qilinishi uchun:
     - **Vercel Pro**: `vercel.json` da sync jadvalini `*/10 * * * *` ga o'zgartiring; yoki
     - **bepul tashqi cron** (masalan cron-job.org): har 10 daqiqada `GET https://<domen>/api/cron/sync` ga
       `Authorization: Bearer <CRON_SECRET>` sarlavhasi bilan so'rov yuborsin (timeout 300 s).
   - Sozlamalar sahifasidagi «Hozir sinxronlash» tugmasi ham bitta to'plamni darhol qayta ishlaydi.
7. **Funksiya vaqti**: `vercel.json` da `maxDuration: 300` (Hobby maksimumi). Pro'da 800 gacha oshirib, `PROCESS_TIME_BUDGET` ni ham oshirish mumkin.

## Ishga tushirish (o'z serveringizda, Docker)

1. **Server**: Ubuntu VPS, Docker o'rnatilgan, domen (masalan `sotuv.kompaniya.uz`) va HTTPS (Nginx + Let's Encrypt yoki Caddy). amoCRM faqat `https` manzilga qaytaradi.
2. `cp .env.example .env` va to'ldiring:
   - `SECRET_KEY` — `openssl rand -hex 32`, `ADMIN_PASSWORD` — kuchli parol, `PUBLIC_URL` — domeningiz.
3. **amoCRM** (`AMO_SUBDOMAIN` = `mycompany.amocrm.ru` dagi `mycompany`). Ikki yo'ldan biri:
   - *Oson yo'l:* amoCRM → Sozlamalar → Integratsiyalar → «Integratsiya yaratish» → kalitlar bo'limidan **uzoq muddatli token** oling va `AMO_LONG_LIVED_TOKEN` ga qo'ying.
   - *OAuth:* integratsiya yaratishda Redirect URI: `https://domeningiz/oauth/amocrm/callback`, ruxsatlar: CRM ma'lumotlariga kirish. `AMO_CLIENT_ID` va `AMO_CLIENT_SECRET` ni yozing, so'ng ilovada Sozlamalar → «amoCRM'ni ulash».
   - Telefoniyangiz qo'ng'iroq yozuvlarini amoCRM kartochkasiga havola bilan yozishi kerak (Moizvonki, Binotel, Sipuni, UIS va boshqalar shunday qiladi).
4. **Google STT**: [console.cloud.google.com](https://console.cloud.google.com) → loyiha yarating → *Cloud Speech-to-Text API* ni yoqing → Credentials → API key. Kalitni faqat Speech-to-Text API bilan cheklang. → `GOOGLE_API_KEY`.
5. **Claude**: [console.anthropic.com](https://console.anthropic.com) → API Keys → `ANTHROPIC_API_KEY`.
6. **Telegram** (ixtiyoriy): @BotFather'dan bot yarating → `TELEGRAM_BOT_TOKEN`; botni rahbarlar guruhiga qo'shing va guruh ID sini `TELEGRAM_CHAT_ID` ga yozing.
7. Ishga tushiring:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
8. Ilovaga kiring → **Sotuv skripti** sahifasida bosqichlar va kompaniya haqidagi ma'lumotni (mahsulot, narxlar, e'tirozlarga javoblar) to'ldiring → **Sozlamalar** → «Hozir sinxronlash».

Ilova birinchi sinxronlashda oxirgi 3 kun qo'ng'iroqlarini oladi.

## Taxminiy xarajat (bitta qo'ng'iroq, 5 daqiqa)

- Google STT: daqiqasiga ~$0.016 (stereo yozuvda 2 kanal) → ~$0.08–0.16
- Claude tahlili: odatda bir necha sent
- Narxlar o'zgarishi mumkin — Google Cloud va Anthropic narx sahifalarini tekshiring. Qisqa qo'ng'iroqlar (`MIN_CALL_SECONDS`) tahlil qilinmaydi.

## Tuzilishi

```
app/
  main.py        — web sahifalar, API, rejalashtiruvchi
  amocrm.py      — amoCRM API: OAuth, qo'ng'iroqlar, menejerlar, vazifa va izohlar
  stt.py         — Google STT, ffmpeg bilan bo'laklash va kanallarga ajratish
  analyzer.py    — Claude bilan skript bo'yicha baholash
  pipeline.py    — yozuv → matn → tahlil → amoCRM, kunlik hisobot
  assistant.py   — AI yordamchi
  stats.py       — KPI va reytinglar
  telegram.py    — Telegram xabarlari
  demo.py        — demo ma'lumot
  templates/, static/
tests/           — testlar (pytest)
vercel.json      — Vercel funksiya va cron sozlamalari
pyproject.toml   — bog'liqliklar va Vercel kirish nuqtasi (app.main:app)
```

## Keyingi bosqichlar uchun g'oyalar

- Menejerlar uchun alohida login (har kim faqat o'z qo'ng'iroqlarini ko'radi)
- Bitrix24 va to'g'ridan-to'g'ri telefoniya (Binotel, FreePBX) integratsiyasi
- Bir nechta kompaniya (SaaS) rejimi va to'lov
- Qo'ng'iroqni jonli (real-time) tahlil qilish
- Ko'p qo'ng'iroq bo'lsa: alohida navbat (Vercel Queues yoki Redis/Celery) — har bir qo'ng'iroq o'z funksiyasida
