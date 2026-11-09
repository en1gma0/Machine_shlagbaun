"""Управление шлагбаумом."""
import logging
import threading
import time
import urllib.request

from . import config

log = logging.getLogger("gate")


def _hik_set(state: str) -> None:
    url = f"http://{config.GATE_HIK_HOST}/ISAPI/System/IO/outputs/{config.GATE_HIK_OUTPUT}/trigger"
    body = f"<IOPortData><outputState>{state}</outputState></IOPortData>".encode()
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"http://{config.GATE_HIK_HOST}", config.GATE_HIK_USER, config.GATE_HIK_PASSWORD)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    req = urllib.request.Request(url, data=body, method="PUT", headers={"Content-Type": "application/xml"})
    opener.open(req, timeout=5).read()


def _open_blocking() -> None:
    try:
        if config.GATE_MODE == "hikvision":
            _hik_set("high")
            time.sleep(config.GATE_PULSE_SEC)
            _hik_set("low")
        elif config.GATE_MODE == "http":
            req = urllib.request.Request(config.GATE_HTTP_URL, method=config.GATE_HTTP_METHOD)
            urllib.request.urlopen(req, timeout=5).read()
        log.info("Шлагбаум: команда открыть (%s)", config.GATE_MODE)
    except Exception as e:  # noqa: BLE001
        log.error("Шлагбаум: ошибка открытия: %s", e)


def open_gate() -> bool:
    """Открыть шлагбаум в фоне. Возвращает True, если управление настроено."""
    if config.GATE_MODE == "none":
        log.info("Шлагбаум: GATE_MODE=none, только запись в журнал")
        return False
    threading.Thread(target=_open_blocking, daemon=True).start()
    return True
