"""Google Speech-to-Text orqali qo'ng'iroqni matnga o'girish (o'zbek + rus).

Google'ning sinxron API'si bir so'rovda ~1 daqiqagacha audio qabul qiladi, shuning uchun
yozuv ffmpeg bilan 55 soniyalik bo'laklarga bo'linadi. Stereo yozuvlarda (ko'p telefoniyalar
menejer va mijozni alohida kanalga yozadi) har bir kanal alohida tanilib, so'zlovchi aniq bo'ladi.
"""
import base64
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from . import config

CHUNK_SECONDS = 55
SAMPLE_RATE = 16000
ENDPOINT = "https://speech.googleapis.com/v1p1beta1/speech:recognize"


class STTError(Exception):
    pass


def ffmpeg_bin() -> str:
    """Tizimdagi ffmpeg, bo'lmasa imageio-ffmpeg paketidagi tayyor binar (Vercel uchun)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        raise STTError(f"ffmpeg topilmadi: {e}") from e


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise STTError(f"ffmpeg xatosi: {proc.stderr[-400:]}")
    return proc.stdout


def channel_count(path: Path) -> int:
    """Kanallar sonini ffmpeg ma'lumotidan aniqlaydi (ffprobe shart emas)."""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    line = next((l for l in proc.stderr.splitlines() if "Audio:" in l), "")
    if not line:
        raise STTError("Faylda audio topilmadi")
    m = re.search(r"(\d+) channels", line)
    if m:
        return int(m.group(1))
    return 1 if " mono" in line else 2


def split_to_chunks(src: Path, workdir: Path, channel: int | None) -> list[Path]:
    """Audioni mono 16 kHz FLAC bo'laklarga bo'ladi. channel berilsa faqat o'sha kanal olinadi."""
    prefix = f"ch{channel if channel is not None else 'm'}"
    af = ["-af", f"pan=mono|c0=c{channel}"] if channel is not None else ["-ac", "1"]
    _run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(src), *af, "-ar", str(SAMPLE_RATE),
          "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-c:a", "flac",
          str(workdir / f"{prefix}_%03d.flac")])
    return sorted(workdir.glob(f"{prefix}_*.flac"))


def _seconds(value: str | None) -> float:
    return float(str(value or "0s").rstrip("s") or 0)


def recognize_chunk(path: Path) -> list[dict]:
    """Bitta bo'lakni tanib, [{start, end, text}] qaytaradi (bo'lak ichidagi vaqt bilan)."""
    body = {
        "config": {
            "encoding": "FLAC",
            "sampleRateHertz": SAMPLE_RATE,
            "languageCode": config.STT_LANGUAGE,
            "alternativeLanguageCodes": config.STT_ALT_LANGUAGES,
            "enableAutomaticPunctuation": True,
            "enableWordTimeOffsets": True,
        },
        "audio": {"content": base64.b64encode(path.read_bytes()).decode()},
    }
    resp = httpx.post(ENDPOINT, params={"key": config.GOOGLE_API_KEY}, json=body, timeout=120)
    if resp.status_code != 200:
        raise STTError(f"Google STT {resp.status_code}: {resp.text[:300]}")
    segments, prev_end = [], 0.0
    for result in resp.json().get("results", []):
        alt = (result.get("alternatives") or [{}])[0]
        text = (alt.get("transcript") or "").strip()
        if not text:
            continue
        words = alt.get("words") or []
        start = _seconds(words[0].get("startTime")) if words else prev_end
        end = _seconds(result.get("resultEndTime")) or start
        segments.append({"start": start, "end": end, "text": text})
        prev_end = end
    return segments


def transcribe_track(src: Path, workdir: Path, channel: int | None, speaker: str) -> list[dict]:
    out = []
    for i, chunk in enumerate(split_to_chunks(src, workdir, channel)):
        offset = i * CHUNK_SECONDS
        for seg in recognize_chunk(chunk):
            out.append({"speaker": speaker, "start": round(seg["start"] + offset, 2),
                        "end": round(seg["end"] + offset, 2), "text": seg["text"]})
    return out


def merge_tracks(*tracks: list[dict]) -> list[dict]:
    merged = sorted((s for t in tracks for s in t), key=lambda s: s["start"])
    # Ketma-ket bir xil so'zlovchi bo'laklarini birlashtiramiz
    out: list[dict] = []
    for seg in merged:
        if out and out[-1]["speaker"] == seg["speaker"] and seg["start"] - out[-1]["end"] < 1.5:
            out[-1]["text"] += " " + seg["text"]
            out[-1]["end"] = seg["end"]
        else:
            out.append(dict(seg))
    return out


def transcribe(audio: bytes) -> list[dict]:
    """Audio baytlarini transkriptga aylantiradi.

    Stereo bo'lsa so'zlovchilar "Kanal 1" / "Kanal 2", mono bo'lsa "Suhbat".
    Kim menejer ekanini keyin AI aniqlaydi.
    """
    if not config.GOOGLE_API_KEY:
        raise STTError("GOOGLE_API_KEY sozlanmagan")
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        src = workdir / "input.audio"
        src.write_bytes(audio)
        if config.STT_SPLIT_CHANNELS and channel_count(src) >= 2:
            return merge_tracks(transcribe_track(src, workdir, 0, "Kanal 1"),
                                transcribe_track(src, workdir, 1, "Kanal 2"))
        return merge_tracks(transcribe_track(src, workdir, None, "Suhbat"))
