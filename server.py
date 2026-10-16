#!/usr/bin/env python3
"""
Локальный дашборд контент-завода.
- Список видео в incoming/
- Кнопка "Обнаружить" (сканирует папку)
- Кнопка "Монтировать" (запускает montage.py)
- Карточки готовых клипов: превью, аккаунт, описание, хештеги, статус
- Отметка "Выложено"
"""
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
INCOMING = BASE / "incoming"
READY = BASE / "ready"
JOBS = BASE / "jobs.json"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

# ——— Параметры планировщика выкладки ———
SCHED_START_HOUR = 14      # первая выкладка в день
SCHED_END_HOUR = 24        # последний слот (24 = не публикуем посты, граница)
SCHED_STEP_MIN = 60        # интервал между постами, минут
SCHED_DAILY_LIMIT = 10     # максимум роликов в день


def compute_schedule(jobs: list) -> None:
    """Для каждой нарезки без scheduled_at присваивает дату/время выкладки.
    Окно выкладки: ежедневно с SCHED_START_HOUR, максимум per_day слотов
    с шагом SCHED_STEP_MIN; слоты публикуются только в текущем дне — если час
    вышел за границу дня/окна, переносим на завтра с SCHED_START_HOUR."""
    pending = [j for j in jobs if not j.get("scheduled_at")]
    if not pending:
        return
    per_day = min(SCHED_DAILY_LIMIT, SCHED_END_HOUR - SCHED_START_HOUR)
    now = datetime.now()

    def day_slot(day):
        """Первый слот дня с SCHED_START_HOUR."""
        return day.replace(hour=SCHED_START_HOUR, minute=0, second=0, microsecond=0)

    day = now
    slot = day_slot(day)
    # если 14:00 сегодня уже прошло — со следующего будущего часа сегодня
    if slot <= now:
        slot = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    count_today = 0
    for j in pending:
        # проверяем: не вышли ли за лимит дня ИЛИ за окно (слот попал на след. сутки)
        if count_today >= per_day or slot.day != day.day:
            day = day + timedelta(days=1)
            slot = day_slot(day)
            count_today = 0
            # завтра 14:00 — точно в будущем
        j["scheduled_at"] = slot.isoformat(timespec="minutes")
        slot += timedelta(minutes=SCHED_STEP_MIN)
        count_today += 1

app = FastAPI(title="Content Factory")


def load_jobs() -> dict:
    if JOBS.exists():
        try:
            return json.loads(JOBS.read_text())
        except Exception:
            pass
    return {"jobs": [], "seq": {}}


def save_jobs(data: dict):
    JOBS.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def incoming_files() -> list:
    return sorted(
        p.name for p in INCOMING.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "templates" / "index.html").read_text()


@app.get("/api/state")
def state():
    jobs = load_jobs()
    # досчитываем расписание для нарезок, у которых его ещё нет
    compute_schedule(jobs.get("jobs", []))
    if any(j.get("scheduled_at") for j in jobs.get("jobs", [])):
        save_jobs(jobs)
    # Группируем нарезки по исходному видео (история: одно видео -> его части)
    groups = {}
    order = []
    for j in jobs.get("jobs", []):
        g = j.get("group") or "Разное"
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(j)
    return {
        "incoming": incoming_files(),
        "groups": [{"name": g, "jobs": groups[g], "count": len(groups[g])} for g in order],
        "has_bgm": (BASE / "assets" / "bgm.mp3").exists() or any(
            p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg"} and not p.name.startswith(".")
            for p in (BASE / "assets").iterdir()
        ),
    }


@app.post("/api/discover")
def discover():
    """Все файлы в incoming — уже "обнаружены" (они там лежат). Ничего не делаем,
    просто возвращаем список. Монтирование отдельно."""
    return {"incoming": incoming_files()}


# ——— Фоновый монтаж с прогрессом ———
MONTAGE = {"running": False, "log": [], "ok": None, "done": False}
MONTAGE_LOCK = threading.Lock()


def _montage_worker():
    """Запускает montage.py в фоне, копируя вывод построчно в MONTAGE['log']."""
    try:
        p = subprocess.Popen(
            ["python3", str(BASE / "montage.py")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in p.stdout:
            line = line.rstrip("\n")
            if line:
                with MONTAGE_LOCK:
                    MONTAGE["log"].append(line)
        rc = p.wait()
        with MONTAGE_LOCK:
            MONTAGE["ok"] = (rc == 0)
            MONTAGE["running"] = False
            MONTAGE["done"] = True
    except Exception as e:
        with MONTAGE_LOCK:
            MONTAGE["ok"] = False
            MONTAGE["running"] = False
            MONTAGE["done"] = True
            MONTAGE["log"].append(f"ОШИБКА: {e}")


@app.post("/api/montage")
def run_montage():
    """Запускает монтаж в фоне и сразу возвращается (не блокирует браузер)."""
    with MONTAGE_LOCK:
        if MONTAGE["running"]:
            return {"ok": True, "already": True}
        MONTAGE.update(running=True, log=[], ok=None, done=False)
    t = threading.Thread(target=_montage_worker, daemon=True)
    t.start()
    return {"ok": True, "started": True}


@app.get("/api/montage_status")
def montage_status():
    """Возвращает текущий прогресс фонового монтажа."""
    with MONTAGE_LOCK:
        return {
            "running": MONTAGE["running"],
            "done": MONTAGE["done"],
            "ok": MONTAGE["ok"],
            "log": list(MONTAGE["log"]),
        }


class PostUpdate(BaseModel):
    index: Optional[int] = None
    account: Optional[str] = None
    caption: Optional[str] = None
    tags: Optional[list] = None
    status: Optional[str] = None


class GroupUpdate(BaseModel):
    """Массовое обновление всех нарезок одной группы (исходного видео)."""
    group: str
    account: Optional[str] = None
    caption: Optional[str] = None
    tags: Optional[list] = None


def part_number(filename: str) -> int:
    """Вытаскивает номер из имени файла: 'x_part003.mp4' -> 3. Иначе 0."""
    m = re.search(r"part(\d+)", filename, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def strip_part(caption: str) -> str:
    """Убирает суффикс '| Part N' из введённого описания, если он есть."""
    return re.sub(r"\s*\|?\s*Part\s*\d+\s*$", "", caption, flags=re.IGNORECASE).strip()


def apply_caption(base: str, num: int) -> str:
    """Собирает финальное описание: базовое + ' | Part N' (если базовое непусто)."""
    base = strip_part(base)
    if not base:
        return f"Part {num}"
    return f"{base} | Part {num}"


@app.post("/api/update_group")
def update_group(up: GroupUpdate):
    jobs = load_jobs()
    changed = 0
    for j in jobs.get("jobs", []):
        if (j.get("group") or "Разное") != up.group:
            continue
        if up.account is not None:
            j["account"] = up.account
        if up.caption is not None:
            # Базовое описание из ввода + автоматический Part N по номеру файла
            j["caption"] = apply_caption(up.caption, part_number(j.get("file", "")))
        if up.tags is not None:
            j["tags"] = up.tags
        changed += 1
    save_jobs(jobs)
    return {"ok": True, "changed": changed}


@app.post("/api/schedule")
def recalc_schedule():
    """Пересчитать расписание выкладки для всех нарезок заново."""
    jobs = load_jobs()
    for j in jobs.get("jobs", []):
        j.pop("scheduled_at", None)
    compute_schedule(jobs.get("jobs", []))
    save_jobs(jobs)
    return {"ok": True, "n": len(jobs.get("jobs", []))}


@app.post("/api/update")
def update_job(up: PostUpdate):
    if up.index is None:
        raise HTTPException(400, "index required")
    jobs = load_jobs()
    try:
        job = jobs["jobs"][up.index]
    except IndexError:
        raise HTTPException(404, "job not found")
    if up.account is not None:
        job["account"] = up.account
    if up.caption is not None:
        job["caption"] = up.caption
    if up.tags is not None:
        job["tags"] = up.tags
    if up.status is not None:
        job["status"] = up.status
    save_jobs(jobs)
    return {"ok": True}


@app.get("/media/{name}")
def media(name: str):
    p = READY / name
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="video/mp4")


@app.get("/download/{name}")
def download(name: str):
    """Принудительная загрузка файла (Content-Disposition: attachment)."""
    p = READY / name
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="video/mp4", filename=name)


# отдаём static-ассеты (превью/стили) если появятся
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
