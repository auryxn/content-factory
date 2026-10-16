#!/usr/bin/env python3
"""
Монтажный агент контент-завода.
Режет сырые видео из incoming/ на куски ровно по CLIP_SECONDS секунд,
накладывает фоновую мелодию (if present) и кладёт готовые файлы в ready/.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
INCOMING = BASE / "incoming"
READY = BASE / "ready"
ASSETS = BASE / "assets"
BGM = ASSETS / "bgm.mp3"
JOBS = BASE / "jobs.json"

CLIP_SECONDS = 120  # 2:00 под TikTok
DEFAULT_ACCOUNT = "main"
DEFAULT_TAGS = ["#survival", "#bushcraft", "#outdoors", "#wilderness", "#survivaltips"]
DEFAULT_CAPTION = "Survival skills you need to know"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def load_jobs() -> dict:
    if JOBS.exists():
        try:
            return json.loads(JOBS.read_text())
        except Exception:
            pass
    return {"jobs": [], "seq": {}}


def save_jobs(data: dict):
    JOBS.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def get_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def clip_cmd(src: Path, dst: Path, start: float) -> list:
    return [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}",
        "-i", str(src),
        "-t", str(CLIP_SECONDS),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ]


def resolve_bgm() -> Path:
    """Берём bgm.mp3 или любой первый .mp3/.m4a/.wav из assets/."""
    if BGM.exists():
        return BGM
    for p in sorted(ASSETS.iterdir()):
        if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg"} and not p.name.startswith("."):
            return p
    return None


def with_bgm_cmd(video: Path, dst: Path) -> list:
    """Накладываем мелодию с пониженной громкостью поверх оригинального звука."""
    bgm = resolve_bgm()
    if bgm is None:
        return None  # мелодии нет — оставляем как есть
    return [
        "ffmpeg", "-y",
        "-i", str(video),
        "-i", str(bgm),
        "-filter_complex",
        "[0:a]volume=1.0[orig];[1:a]volume=0.25,aloop=loop=-1:size=2e9[bg];"
        "[orig][bg]amix=inputs=2:duration=first:dropout_transition=3[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ]


def process_source(src: Path) -> list:
    """Режем один исходник на 2-минутные куски. Возвращает список созданных jobs."""
    dur = get_duration(src)
    # Плавающая точка: 1080.03 % 120 = 0.03 из-за джиттера в длительности. Округляем с эпсилоном,
    # чтобы не плодить фантомный пустой клип на самом конце (start >= dur).
    EPS = 0.5  # секунд — считаем хвост <0.5s недостойным отдельного клипа
    n_clips = max(1, int((dur + EPS) // CLIP_SECONDS))
    jobs = load_jobs()
    # Нумерация частей начинается с part001 у КАЖДОГО видео (а не продолжает глобальный счётчик).
    acc_seq = 0

    created = []
    for i in range(n_clips):
        acc_seq += 1
        clip_name = f"{src.stem}_part{acc_seq:03d}.mp4"
        # временный файл без фоновой музыки, потом финал с музыкой
        tmp = READY / f".tmp_{clip_name}"
        final = READY / clip_name
        start = i * CLIP_SECONDS

        print(f"  [{i+1}/{n_clips}] {clip_name} (from {start:.0f}s)")
        r = subprocess.run(clip_cmd(src, tmp, start), capture_output=True)
        # ffmpeg ругнулся (битый сегмент/конец файла) — не роняем весь батч, пропускаем клип
        if r.returncode != 0:
            print(f"    ! ffmpeg error {r.returncode} на {start:.0f}s — пропускаю")
            err = (r.stderr or b"").decode("utf-8", "replace")[-400:].strip()
            if err:
                print(f"      {err.splitlines()[-1]}")
            tmp.unlink(missing_ok=True)
            continue

        # Защита от фантомного клипа: если нарезка не дала видеокадров (конец файла), пропускаем.
        if not tmp.exists() or tmp.stat().st_size < 1024:
            print(f"    ! пустая нарезка на {start:.0f}s — пропускаю")
            tmp.unlink(missing_ok=True)
            break

        bg = with_bgm_cmd(tmp, final)
        if bg:
            subprocess.run(bg, check=True, capture_output=True)
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, final)

        jobs["jobs"].append({
            "file": clip_name,
            "group": src.stem,          # исходное видео (для группировки/истории)
            "account": DEFAULT_ACCOUNT,
            "platform": "tiktok",
            "caption": f"{DEFAULT_CAPTION} | Part {acc_seq}",
            "tags": DEFAULT_TAGS,
            "status": "ready",
        })
        created.append(clip_name)

    save_jobs(jobs)

    # переносим/удаляем исходник после обработки
    arc = BASE / "processed" / src.name
    arc.parent.mkdir(exist_ok=True)
    shutil.move(str(src), str(arc))
    return created


def main():
    sources = sorted(
        p for p in INCOMING.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )
    if not sources:
        print("Нет новых видео в incoming/")
        return 0

    made = 0
    for s in sources:
        print(f"Обработка: {s.name} ({get_duration(s):.0f}s)")
        made += len(process_source(s))
    print(f"Готово: {made} клипов в ready/")
    return made


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
