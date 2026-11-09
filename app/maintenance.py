"""Обслуживание: резервные копии, очистка старых фото, мониторинг (камеры, диск), уведомления."""
import json
import logging
import shutil
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from . import config, db

log = logging.getLogger("maintenance")

# активные тревоги: ключ -> {"level", "text", "since"}
alerts: dict[str, dict] = {}
_lock = threading.Lock()
state = {"last_backup": "", "last_backup_error": "", "last_cleanup": "", "last_cleanup_removed": 0}


# ---------- уведомления ----------

def notify(text: str) -> None:
    """Telegram (если настроен). Ошибки не мешают работе."""
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return

    def send():
        try:
            data = urllib.parse.urlencode({"chat_id": config.TELEGRAM_CHAT_ID,
                                           "text": f"🚧 {config.SITE_NAME}\n{text}"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                   data=data, timeout=10).read()
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram: не удалось отправить: %s", e)
    threading.Thread(target=send, daemon=True).start()


def raise_alert(key: str, text: str, level: str = "error") -> None:
    with _lock:
        if key in alerts:
            alerts[key]["text"] = text
            return
        alerts[key] = {"level": level, "text": text, "since": db.now()}
    log.warning("ТРЕВОГА: %s", text)
    db.audit("система", "тревога", text)
    notify(f"⚠️ {text}")


def clear_alert(key: str, text: str = "") -> None:
    with _lock:
        a = alerts.pop(key, None)
    if a:
        msg = text or f"Норма: {a['text']}"
        log.info(msg)
        db.audit("система", "тревога снята", msg)
        notify(f"✅ {msg}")


def alerts_list():
    with _lock:
        return [{"key": k, **v} for k, v in alerts.items()]


# ---------- диск ----------

def disk_info() -> dict:
    total, used, free = shutil.disk_usage(config.DATA_DIR)
    return {"total_gb": round(total / 2**30, 1), "free_gb": round(free / 2**30, 1),
            "free_pct": round(free / total * 100, 1)}


def snaps_info() -> dict:
    days = sorted(p.name for p in config.SNAP_DIR.iterdir() if p.is_dir())
    size = sum(f.stat().st_size for f in config.SNAP_DIR.rglob("*.jpg"))
    return {"days": len(days), "oldest": days[0] if days else "", "size_mb": round(size / 2**20, 1)}


# ---------- резервные копии ----------

def backups_list():
    files = sorted(config.BACKUP_DIR.glob("gate_*.db"), reverse=True)
    return [{"name": f.name, "size_mb": round(f.stat().st_size / 2**20, 2),
             "time": datetime.fromtimestamp(f.stat().st_mtime).strftime(db.TS_FMT)} for f in files]


def backup_now(user: str = "система") -> str:
    """Копия базы (+ .env с настройками) в BACKUP_DIR. Старые копии сверх BACKUP_KEEP удаляются."""
    name = f"gate_{datetime.now().strftime('%Y-%m-%d_%H%M')}.db"
    try:
        db.backup_to(str(config.BACKUP_DIR / name))
        if config.ENV_PATH.exists():
            shutil.copy2(config.ENV_PATH, config.BACKUP_DIR / "env_latest.txt")
        for old in sorted(config.BACKUP_DIR.glob("gate_*.db"), reverse=True)[max(1, config.BACKUP_KEEP):]:
            old.unlink(missing_ok=True)
        state.update(last_backup=db.now(), last_backup_error="")
        clear_alert("backup")
        db.audit(user, "резервная копия", name)
        log.info("Резервная копия: %s", name)
        return name
    except Exception as e:  # noqa: BLE001
        state["last_backup_error"] = str(e)
        raise_alert("backup", f"Не удалось сделать резервную копию базы: {e}")
        raise


# ---------- очистка фото ----------

def cleanup_snaps(user: str = "система") -> int:
    """Удаляет папки с фото старше SNAP_KEEP_DAYS дней. Записи журнала остаются (без фото)."""
    if config.SNAP_KEEP_DAYS <= 0:
        return 0
    limit = (date.today() - timedelta(days=config.SNAP_KEEP_DAYS)).isoformat()
    removed = 0
    for d in config.SNAP_DIR.iterdir():
        if d.is_dir() and len(d.name) == 10 and d.name < limit:
            removed += sum(1 for _ in d.glob("*.jpg"))
            shutil.rmtree(d, ignore_errors=True)
    state.update(last_cleanup=db.now(), last_cleanup_removed=removed)
    if removed:
        db.audit(user, "очистка фото", f"удалено {removed} фото старше {config.SNAP_KEEP_DAYS} дн. (до {limit})")
        log.info("Очистка: удалено %s фото до %s", removed, limit)
    return removed


# ---------- фоновые проверки ----------

def _daily_done_today() -> bool:
    today = date.today().strftime("%Y-%m-%d")
    return any(b["name"].startswith(f"gate_{today}") for b in backups_list())


def _check_cameras(hub) -> None:
    for cam in hub.cameras:
        key = f"camera{cam.id}"
        age = cam.frame_age
        bad_since = None
        if age is None:
            bad_since = cam.started_ts
        elif age > 10:
            bad_since = cam.last_frame_ts
        if bad_since and time.time() - bad_since > config.CAMERA_ALERT_MIN * 60:
            minutes = int((time.time() - bad_since) / 60)
            raise_alert(key, f"Камера «{cam.name}» не передаёт видео уже {minutes} мин")
        elif not bad_since and key in alerts:
            clear_alert(key, f"Камера «{cam.name}» снова работает")


def _check_disk() -> None:
    d = disk_info()
    if d["free_gb"] < config.DISK_MIN_GB:
        raise_alert("disk", f"Мало места на диске: свободно {d['free_gb']} ГБ ({d['free_pct']}%)")
    elif "disk" in alerts and d["free_gb"] > config.DISK_MIN_GB * 1.2:
        clear_alert("disk", f"Место на диске в норме: свободно {d['free_gb']} ГБ")


def _loop(hub) -> None:
    time.sleep(20)  # дать камерам подключиться после старта
    while True:
        try:
            _check_cameras(hub)
            _check_disk()
            if datetime.now().hour >= config.BACKUP_HOUR and not _daily_done_today():
                cleanup_snaps()
                backup_now()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка фоновой проверки")
        time.sleep(30)


def start(hub) -> None:
    threading.Thread(target=_loop, args=(hub,), daemon=True, name="maintenance").start()


def status_json() -> str:
    return json.dumps({"alerts": alerts_list(), **state}, ensure_ascii=False)
