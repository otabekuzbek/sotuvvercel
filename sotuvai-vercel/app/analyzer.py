"""Claude yordamida qo'ng'iroqni sotuv skripti bo'yicha baholash."""
import json

import httpx

from . import config

API_URL = "https://api.anthropic.com/v1/messages"


class AnalyzerError(Exception):
    pass


def claude(messages: list[dict], system: str, max_tokens: int = 4000,
           tools: list[dict] | None = None, tool_choice: dict | None = None) -> dict:
    if not config.ANTHROPIC_API_KEY:
        raise AnalyzerError("ANTHROPIC_API_KEY sozlanmagan")
    body = {"model": config.CLAUDE_MODEL, "max_tokens": max_tokens, "system": system, "messages": messages}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    headers = {"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    for _ in range(3):
        resp = httpx.post(API_URL, json=body, headers=headers, timeout=180)
        if resp.status_code in (429, 529, 500, 503):
            continue
        if resp.status_code != 200:
            raise AnalyzerError(f"Claude API {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise AnalyzerError(f"Claude API band: {resp.status_code}")


def claude_text(messages: list[dict], system: str, max_tokens: int = 2000) -> str:
    data = claude(messages, system, max_tokens)
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def analysis_tool(script: list[dict]) -> dict:
    stage_keys = [s["key"] for s in script]
    return {
        "name": "submit_call_analysis",
        "description": "Qo'ng'iroq tahlili natijasini yuborish.",
        "input_schema": {
            "type": "object",
            "properties": {
                "manager_speaker": {"type": "string",
                                    "description": "Transkriptdagi qaysi so'zlovchi menejer (masalan 'Kanal 1'). Mono bo'lsa 'Suhbat'."},
                "is_sales_call": {"type": "boolean",
                                  "description": "Haqiqiy mijoz bilan suhbatmi (avtojavob, noto'g'ri raqam, bo'sh yozuv emas)."},
                "summary": {"type": "string", "description": "2-4 jumlali xulosa: mijoz kim, nima xohladi, nima bilan tugadi."},
                "outcome": {"type": "string",
                            "enum": ["sotuv", "kelishuv", "qayta_aloqa", "rad_etildi", "malumot", "boshqa"]},
                "client_sentiment": {"type": "string", "enum": ["ijobiy", "neytral", "salbiy"]},
                "client_needs": {"type": "array", "items": {"type": "string"}},
                "stages": {
                    "type": "array",
                    "description": "Skriptning HAR BIR bosqichi uchun baho.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "enum": stage_keys},
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "comment": {"type": "string", "description": "Nima uchun shu baho, transkriptdan misol bilan."},
                        },
                        "required": ["key", "score", "comment"],
                    },
                },
                "strengths": {"type": "array", "items": {"type": "string"}},
                "weaknesses": {"type": "array", "items": {"type": "string"}},
                "recommendations": {
                    "type": "array",
                    "description": "3 tagacha eng muhim, amaliy tavsiya.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "detail": {"type": "string"},
                            "example_phrase": {"type": "string", "description": "Menejer keyingi safar aytishi mumkin bo'lgan aniq gap."},
                        },
                        "required": ["title", "detail"],
                    },
                },
                "objections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "objection": {"type": "string"},
                            "handled": {"type": "boolean"},
                            "better_response": {"type": "string"},
                        },
                        "required": ["objection", "handled"],
                    },
                },
                "next_steps": {
                    "type": "array",
                    "description": "Suhbatda kelishilgan yoki zarur keyingi harakatlar.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "due_in_days": {"type": "integer", "minimum": 0, "maximum": 60},
                        },
                        "required": ["action", "due_in_days"],
                    },
                },
                "red_flags": {"type": "array", "items": {"type": "string"},
                              "description": "Qo'pollik, noto'g'ri ma'lumot, va'dani buzish kabi jiddiy xatolar."},
            },
            "required": ["manager_speaker", "is_sales_call", "summary", "outcome", "client_sentiment",
                         "stages", "strengths", "weaknesses", "recommendations", "next_steps"],
        },
    }


def build_system_prompt(script: list[dict], company_context: str) -> str:
    stages = "\n".join(f"- {s['key']} — {s['name']} (vazn {s['weight']}%): {s['criteria']}" for s in script)
    return f"""Siz tajribali sotuv bo'limi rahbari va kouchsiz. Vazifangiz — menejerning mijoz bilan telefon suhbatini
kompaniya sotuv skripti bo'yicha xolis baholash va menejer ertangi kundan qo'llay oladigan aniq tavsiyalar berish.

KOMPANIYA HAQIDA:
{company_context}

SOTUV SKRIPTI BOSQICHLARI:
{stages}

QOIDALAR:
- Transkript avtomatik tanilgan, unda xatolar bo'lishi mumkin; ma'noga qarab baholang. Suhbat o'zbek va rus tillarida aralash bo'lishi mumkin.
- Har bir bosqichni 0-100 ball bilan baholang. Bosqich umuman bajarilmagan bo'lsa past ball bering; suhbat tabiatiga ko'ra
  kerak bo'lmagan bosqich (masalan, e'tiroz bo'lmagan) uchun 70 qo'ying va izohda shuni ayting.
- Izoh va tavsiyalarda transkriptdan qisqa misol keltiring. Umumiy gaplar emas, aniq harakat yozing.
- Barcha matnni o'zbek tilida (lotin yozuvida) yozing.
- Natijani faqat submit_call_analysis vositasi orqali yuboring."""


def format_transcript(segments: list[dict]) -> str:
    def ts(sec: float) -> str:
        return f"{int(sec // 60):02d}:{int(sec % 60):02d}"
    return "\n".join(f"[{ts(s['start'])}] {s['speaker']}: {s['text']}" for s in segments)


def weighted_score(stages: list[dict], script: list[dict]) -> float:
    weights = {s["key"]: float(s.get("weight") or 0) for s in script}
    total = sum(weights.get(st["key"], 0) for st in stages)
    if not total:
        return round(sum(st["score"] for st in stages) / max(len(stages), 1), 1)
    return round(sum(st["score"] * weights.get(st["key"], 0) for st in stages) / total, 1)


def analyze(segments: list[dict], script: list[dict], company_context: str, meta: dict) -> dict:
    """Transkriptni tahlil qiladi va {analysis..., overall_score} qaytaradi."""
    header = (f"Qo'ng'iroq: {'kiruvchi' if meta.get('direction') == 'in' else 'chiquvchi'}, "
              f"davomiyligi {meta.get('duration', 0)} s, menejer: {meta.get('manager', 'nomaʼlum')}.")
    messages = [{"role": "user", "content": f"{header}\n\nTRANSKRIPT:\n{format_transcript(segments)}"}]
    tool = analysis_tool(script)
    data = claude(messages, build_system_prompt(script, company_context), max_tokens=4000,
                  tools=[tool], tool_choice={"type": "tool", "name": tool["name"]})
    block = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
    if not block:
        raise AnalyzerError("Claude tahlil qaytarmadi")
    result = block["input"]
    names = {s["key"]: s["name"] for s in script}
    result["stages"] = [dict(st, name=names.get(st["key"], st["key"]))
                        for st in result.get("stages", []) if st.get("key") in names]
    result["overall_score"] = weighted_score(result["stages"], script) if result["stages"] else 0.0
    return result


def analysis_note_text(analysis: dict, app_url: str) -> str:
    """amoCRM kartochkasiga yoziladigan qisqa izoh."""
    lines = [f"🤖 AI tahlil: {analysis.get('overall_score', 0):.0f}/100", analysis.get("summary", "")]
    if analysis.get("recommendations"):
        lines.append("Tavsiyalar:")
        lines += [f"• {r['title']}" for r in analysis["recommendations"][:3]]
    lines.append(app_url)
    return "\n".join(l for l in lines if l)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
