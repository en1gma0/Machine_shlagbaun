"""Настройки читаются из файла .env рядом с run.py (формат KEY=VALUE)."""
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
SNAP_DIR = DATA_DIR / "snaps"
DB_PATH = DATA_DIR / "gate.db"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(ENV_PATH)
_env_lock = threading.Lock()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool) -> bool:
    return _get(name, str(default)).lower() in ("1", "true", "yes", "on", "да")


def set_env(key: str, value) -> None:
    """Сохранить параметр в .env (заменить строку или дописать в конец) и в памяти."""
    value = str(value)
    with _env_lock:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ[key] = value


# --- Камеры ---
# CAMERA1_URL, CAMERA1_NAME, CAMERA1_DIRECTION, CAMERA1_ROI, CAMERA1_SCALE; CAMERA2_... и т.д.
# DIRECTION: entry - камера на въезде, exit - на выезде,
#            auto  - направление по истории (машина внутри -> выезд, иначе въезд)
# ROI: зона поиска "x1,y1,x2,y2" в долях кадра; SCALE: увеличение зоны перед распознаванием (1-3)
CAMERAS: list[dict] = []
for _i in range(1, 10):
    _url = _get(f"CAMERA{_i}_URL")
    if not _url and _i == 1:
        _url = _get("RTSP_URL")  # старый формат
    if _url:
        CAMERAS.append({
            "id": _i,
            "url": _url,
            "name": _get(f"CAMERA{_i}_NAME", f"Камера {_i}"),
            "direction": _get(f"CAMERA{_i}_DIRECTION", _get("CAMERA_DIRECTION", "auto")),
            "roi": _get(f"CAMERA{_i}_ROI", _get("ROI", "") if _i == 1 else ""),
            "scale": float(_get(f"CAMERA{_i}_SCALE", "1")),
            "http_port": int(_get(f"CAMERA{_i}_HTTP_PORT", "80")),  # веб-порт камеры (для перезагрузки)
        })

SITE_NAME = _get("SITE_NAME", _get("CAMERA_NAME", "Шлагбаум"))
AUTO_RESET_HOURS = float(_get("AUTO_RESET_HOURS", "24"))

# --- Распознавание ---
PROCESS_INTERVAL = float(_get("PROCESS_INTERVAL", "0.25"))  # сек между кадрами на распознавание
MIN_OCR_CONF = float(_get("MIN_OCR_CONF", "0.6"))
CONFIRM_HITS = int(_get("CONFIRM_HITS", "2"))  # сколько раз номер должен совпасть для события
COOLDOWN_SEC = float(_get("COOLDOWN_SEC", "60"))  # повторно тот же номер не фиксируется
DETECTOR_CONF = float(_get("DETECTOR_CONF", "0.3"))
PLATE_REGEX = _get("PLATE_REGEX", "")  # пусто = любой формат
DETECTOR_MODEL = _get("DETECTOR_MODEL", "yolo-v9-t-640-license-plate-end2end")
OCR_MODEL = _get("OCR_MODEL", "cct-s-v2-global-model")
WHITELIST_MAX_DISTANCE = int(_get("WHITELIST_MAX_DISTANCE", "0"))  # 1 = простить одну ошибку OCR

# --- Шлагбаум ---
# none      - только журнал
# hikvision - тревожный выход (реле) камеры Hikvision через ISAPI
# http      - GET/POST на произвольный URL (контроллер, реле, ESP)
GATE_MODE = _get("GATE_MODE", "none")
GATE_PULSE_SEC = float(_get("GATE_PULSE_SEC", "1"))
GATE_HIK_HOST = _get("GATE_HIK_HOST", "")
GATE_HIK_USER = _get("GATE_HIK_USER", "")
GATE_HIK_PASSWORD = _get("GATE_HIK_PASSWORD", "")
GATE_HIK_OUTPUT = _get("GATE_HIK_OUTPUT", "1")
GATE_HTTP_URL = _get("GATE_HTTP_URL", "")
GATE_HTTP_METHOD = _get("GATE_HTTP_METHOD", "GET")
EXIT_OPEN_ALL = _bool("EXIT_OPEN_ALL", True)  # на выезд открывать всем

# --- Веб ---
WEB_HOST = _get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(_get("WEB_PORT", "8000"))
WEB_USER = _get("WEB_USER", "admin")
WEB_PASSWORD = _get("WEB_PASSWORD", "")  # пусто = без пароля

DATA_DIR.mkdir(exist_ok=True)
SNAP_DIR.mkdir(exist_ok=True)
