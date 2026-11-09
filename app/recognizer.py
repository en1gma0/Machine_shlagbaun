"""Чтение RTSP-потоков, распознавание номеров, фиксация проездов (несколько камер)."""
import logging
import os
import re
import threading
import time
import urllib.request
from collections import Counter, deque
from datetime import datetime
from urllib.parse import unquote, urlparse

import cv2

from . import config, db, gate

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")
log = logging.getLogger("recognizer")
FREEZE_SEC = 20  # нет новых кадров дольше - поток считается зависшим


def parse_roi(roi: str, w: int, h: int):
    if not roi:
        return 0, 0, w, h
    try:
        x1, y1, x2, y2 = (min(1.0, max(0.0, float(v))) for v in roi.split(","))
        if x2 - x1 < 0.02 or y2 - y1 < 0.02:
            raise ValueError
        return int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)
    except ValueError:
        return 0, 0, w, h


class Hub:
    """Общее для всех камер: модели, защита от повторов, регистрация проездов."""

    def __init__(self) -> None:
        self.alpr = None
        self.cameras: list[Camera] = [Camera(self, c) for c in config.CAMERAS]
        self.readings: deque = deque(maxlen=30)
        self.last_event = None
        self._recent: dict[str, float] = {}  # номер -> время последнего наблюдения
        self._lock = threading.Lock()

    def camera(self, cid: int):
        return next((c for c in self.cameras if c.id == cid), None)

    def start(self) -> None:
        for cam in self.cameras:
            cam.start_reader()
            if cam.cfg.get("acusense"):
                threading.Thread(target=listen_events, args=(cam,), daemon=True, name=f"events-{cam.id}").start()
        threading.Thread(target=self._load, daemon=True, name="alpr-load").start()
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()

    def _watchdog(self) -> None:
        """Если от камеры долго нет кадров (поток завис) - переподключаемся сами."""
        while True:
            time.sleep(5)
            for cam in self.cameras:
                if cam.rebooting_until > time.time():
                    continue
                stale = time.time() - (cam.last_frame_ts or cam.started_ts)
                if stale > FREEZE_SEC and time.time() - cam.last_reconnect_ts > FREEZE_SEC:
                    log.warning("[%s] нет кадров %d с - автоматическое переподключение", cam.name, stale)
                    cam.reconnect(auto=True)

    def _load(self) -> None:
        from fast_alpr import ALPR
        log.info("Загрузка моделей: %s / %s", config.DETECTOR_MODEL, config.OCR_MODEL)
        self.alpr = ALPR(detector_model=config.DETECTOR_MODEL, ocr_model=config.OCR_MODEL,
                         detector_conf_thresh=config.DETECTOR_CONF)
        log.info("Модели загружены")
        for cam in self.cameras:
            cam.start_worker()

    def in_cooldown(self, text: str, now: float) -> bool:
        """Номер (или похожий) уже зафиксирован недавно любой камерой - продлеваем и игнорируем."""
        with self._lock:
            for plate, seen in list(self._recent.items()):
                if now - seen > config.COOLDOWN_SEC:
                    del self._recent[plate]
                    continue
                if db.levenshtein(plate, text) <= 2:
                    self._recent[plate] = now
                    return True
        return False

    def register(self, cam, plate: str, conf: float, snap=None) -> int | None:
        with self._lock:
            now = time.time()
            for p, seen in self._recent.items():  # другая камера успела раньше
                if now - seen <= config.COOLDOWN_SEC and db.levenshtein(p, plate) <= 2:
                    return None
            self._recent[plate] = now
        direction = cam.direction if cam.direction in ("entry", "exit") else db.guess_direction(plate)
        d = db.decide(plate, direction)
        should_open = d["allowed"] or (direction == "exit" and config.EXIT_OPEN_ALL)
        opened = gate.open_gate() if should_open else False
        snapshot = save_snapshot(plate, snap) if snap else ""
        suggest = ""
        if not d["matched_plate"]:  # не нашли - подсказать похожий номер из списка
            s = db.suggest_plate([plate], max_distance=2)
            suggest = s[0] if s else ""
        eid = db.add_event(plate, round(conf, 3), direction, d, opened, snapshot=snapshot, camera=cam.name,
                           suggest=suggest)
        if d["guest"] is not None and direction == "entry" and d["allowed"]:
            db.guest_use(d["guest"]["id"])
        cam.last_plate_event_ts = time.time()
        self.last_event = {"id": eid, "plate": plate, "direction": direction, "allowed": d["allowed"]}
        log.info("Событие #%s [%s]: %s %s %s шлагбаум=%s %s", eid, cam.name, plate, direction,
                 "РАЗРЕШЁН" if d["allowed"] else "ОТКАЗ", opened, d["reason"])
        return eid

    def vehicle_event(self, cam) -> None:
        """Камера (AcuSense) сообщила о проезде машины. Ждём 4 с: если номер за это время распознан -
        проезд уже записан. Иначе пишем проезд без номера с фото и подсказкой из белого списка."""
        t0 = time.time()
        frame = cam.latest_frame()
        time.sleep(4)
        if time.time() - max(c.last_plate_event_ts for c in self.cameras) < 12:
            return  # номер распознан - событие уже есть
        texts = [r["text"] for r in list(self.readings) if r.get("cam") == cam.name and t0 - r.get("ts", 0) < 12]
        s = db.suggest_plate(texts, max_distance=3)
        suggest = s[0] if s else ""
        direction = cam.direction if cam.direction in ("entry", "exit") else (
            db.guess_direction(suggest) if suggest else "entry")
        snapshot = ""
        frame = cam.latest_frame() if frame is None else frame
        if frame is not None:
            h, w = frame.shape[:2]
            snapshot = save_snapshot("vehicle", (frame, parse_roi(cam.cfg["roi"], w, h)), box_is_zone=True)
        reason = "номер не распознан — проверьте фото" + (f"; похоже на {suggest} ({s[1]})" if s else "")
        eid = db.add_event("", 0, direction, None, False, snapshot=snapshot, source="vehicle", camera=cam.name,
                           raw_plate=texts[0] if texts else "", suggest=suggest, reason=reason)
        log.info("Событие #%s [%s]: машина без номера, подсказка=%s", eid, cam.name, suggest or "-")


class Camera:
    def __init__(self, hub: Hub, cfg: dict) -> None:
        self.hub = hub
        self.cfg = cfg  # изменяется со страницы «Настройки»
        self.id = cfg["id"]
        self.frame = None
        self.annotated = None
        self.connected = False
        self.fps = 0.0
        self._lock = threading.Lock()
        self._pending: list[tuple[str, float, float]] = []
        self._pending_snap = None
        self._regex = re.compile(config.PLATE_REGEX) if config.PLATE_REGEX else None
        self._gen = 0  # номер "поколения" потока: при переподключении старый поток сам завершается
        self.started_ts = time.time()
        self.last_frame_ts = 0.0
        self.last_reconnect_ts = 0.0
        self.rebooting_until = 0.0
        self.reconnects = 0
        self.last_plate_event_ts = 0.0
        self.events_ok_ts = 0.0  # когда последний раз пришло что-то по каналу событий камеры
        self.vehicle_events = 0

    @property
    def name(self) -> str:
        return self.cfg["name"]

    @property
    def direction(self) -> str:
        return self.cfg["direction"]

    @property
    def frame_age(self):
        return round(time.time() - self.last_frame_ts, 1) if self.last_frame_ts else None

    def start_reader(self) -> None:
        gen = self._gen
        threading.Thread(target=self._reader, args=(gen,), daemon=True, name=f"rtsp-{self.id}-{gen}").start()

    def start_worker(self) -> None:
        threading.Thread(target=self._worker, daemon=True, name=f"alpr-{self.id}").start()

    def reconnect(self, auto: bool = False) -> None:
        """Переоткрыть видеопоток. Старый поток (даже если завис в чтении) будет брошен."""
        self._gen += 1
        self.connected = False
        self.last_reconnect_ts = time.time()
        self.reconnects += 1
        if not auto:
            log.info("[%s] переподключение по кнопке", self.name)
        self.start_reader()

    def reboot(self) -> str:
        """Перезагрузить саму камеру Hikvision через ISAPI. Логин/пароль/IP берутся из RTSP-адреса."""
        u = urlparse(self.cfg["url"])
        host = f"{u.hostname}:{self.cfg.get('http_port') or 80}"
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, f"http://{host}", unquote(u.username or ""), unquote(u.password or ""))
        opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
        req = urllib.request.Request(f"http://{host}/ISAPI/System/reboot", method="PUT", data=b"")
        opener.open(req, timeout=10).read()
        log.warning("[%s] отправлена команда перезагрузки камеры", self.name)
        self.rebooting_until = time.time() + 150  # не дёргать переподключение, пока камера грузится
        self.connected = False
        return "ok"

    def _reader(self, gen: int) -> None:
        """Постоянно читает поток, чтобы всегда был свежий кадр (без накопления задержки)."""
        while gen == self._gen:
            cap = cv2.VideoCapture(self.cfg["url"], cv2.CAP_FFMPEG)
            if gen != self._gen:
                cap.release()
                return
            if not cap.isOpened():
                self.connected = False
                if time.time() > self.rebooting_until:
                    log.warning("[%s] камера недоступна, повтор через 5 с", self.name)
                time.sleep(5)
                continue
            self.connected = True
            log.info("[%s] камера подключена", self.name)
            fails = 0
            while gen == self._gen:
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 50:
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                if gen != self._gen:
                    break
                with self._lock:
                    self.frame = frame
                self.last_frame_ts = time.time()
            cap.release()
            if gen != self._gen:
                return  # заменён новым потоком
            self.connected = False
            log.warning("[%s] поток прервался, переподключение", self.name)
            time.sleep(2)

    def _worker(self) -> None:
        n, t0, done_ts = 0, time.time(), 0.0
        while True:
            start = time.time()
            if self.last_frame_ts == done_ts:  # нового кадра нет (поток завис или ещё не начался)
                time.sleep(0.2)
                if time.time() - t0 >= 5:
                    self.fps, n, t0 = 0.0, 0, time.time()
                continue
            done_ts = self.last_frame_ts
            frame = self.latest_frame()
            if frame is None:
                time.sleep(0.2)
                continue
            try:
                self.process_frame(frame)
            except Exception:  # noqa: BLE001
                log.exception("[%s] ошибка обработки кадра", self.name)
            n += 1
            if time.time() - t0 >= 5:
                self.fps = n / (time.time() - t0)
                n, t0 = 0, time.time()
            time.sleep(max(0.0, config.PROCESS_INTERVAL - (time.time() - start)))

    def latest_frame(self):
        with self._lock:
            return None if self.frame is None else self.frame.copy()

    # ---------- обработка ----------
    def detect(self, frame):
        """Список (text, conf, (x1,y1,x2,y2)) в координатах полного кадра."""
        h, w = frame.shape[:2]
        rx1, ry1, rx2, ry2 = parse_roi(self.cfg["roi"], w, h)
        crop = frame[ry1:ry2, rx1:rx2]
        scale = max(1.0, min(3.0, float(self.cfg.get("scale") or 1)))
        if scale > 1:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        out = []
        for r in self.hub.alpr.predict(crop):
            if r.ocr is None or not r.ocr.text:
                continue
            conf = r.ocr.confidence
            if isinstance(conf, (list, tuple)):
                conf = sum(conf) / len(conf) if conf else 0.0
            b = r.detection.bounding_box
            box = (int(b.x1 / scale) + rx1, int(b.y1 / scale) + ry1, int(b.x2 / scale) + rx1, int(b.y2 / scale) + ry1)
            out.append((db.normalize(r.ocr.text), float(conf), box))
        return out

    def process_frame(self, frame) -> None:
        now = time.time()
        found = self.detect(frame)
        vis = frame.copy()
        h, w = frame.shape[:2]
        rx1, ry1, rx2, ry2 = parse_roi(self.cfg["roi"], w, h)
        cv2.rectangle(vis, (rx1, ry1), (rx2, ry2), (255, 200, 0), 3)

        for text, conf, (x1, y1, x2, y2) in found:
            valid = len(text) >= 5 and conf >= config.MIN_OCR_CONF
            fmt_ok = not self._regex or bool(self._regex.match(text))
            valid = valid and fmt_ok
            color = (0, 220, 0) if valid else (0, 140, 255)
            cv2.rectangle(vis, (x1, y1), (x2, y2), color, 3)
            cv2.putText(vis, f"{text} {conf:.2f}", (x1, max(30, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)
            why = "" if valid else ("формат" if not fmt_ok else ("короткий" if len(text) < 5 else "низкая уверенность"))
            self.hub.readings.appendleft({"t": datetime.now().strftime("%H:%M:%S"), "cam": self.name,
                                          "text": text, "conf": round(conf, 2), "w": x2 - x1,
                                          "valid": valid, "why": why, "ts": now})
            if valid:
                self._add_reading(text, conf, now, frame, (x1, y1, x2, y2))

        self._maybe_finalize(now)
        small = cv2.resize(vis, (1280, int(1280 * h / w)))
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            self.annotated = jpg.tobytes()

    def _add_reading(self, text, conf, now, frame, box) -> None:
        if self.hub.in_cooldown(text, now):
            return
        if self._pending and not any(db.levenshtein(p[0], text) <= 2 for p in self._pending):
            self._finalize(force=True)  # в кадре другая машина
        self._pending.append((text, conf, now))
        if self._pending_snap is None or conf >= max(p[1] for p in self._pending):
            self._pending_snap = (frame, box)

    def _maybe_finalize(self, now: float) -> None:
        if not self._pending:
            return
        votes = Counter(p[0] for p in self._pending)
        if votes.most_common(1)[0][1] >= config.CONFIRM_HITS:
            self._finalize()
        elif now - self._pending[-1][2] > 3:  # номер пропал из кадра
            self._finalize(force=True)

    def _finalize(self, force: bool = False) -> None:
        pending, self._pending = self._pending, []
        snap, self._pending_snap = self._pending_snap, None
        if not pending:
            return
        scores: Counter = Counter()
        for text, conf, _ in pending:
            scores[text] += conf
        plate = scores.most_common(1)[0][0]
        conf = max(c for t, c, _ in pending if t == plate)
        if force and len(pending) < config.CONFIRM_HITS and conf < 0.85:
            return  # однократное неуверенное чтение - шум
        self.hub.register(self, plate, conf, snap)


def save_snapshot(plate, snap, box_is_zone: bool = False) -> str:
    """Сохраняет полный кадр с рамкой и вырезку (номер или, для проезда без номера, зону поиска)."""
    frame, (x1, y1, x2, y2) = snap
    day = datetime.now().strftime("%Y-%m-%d")
    folder = config.SNAP_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%H%M%S')}_{plate or 'car'}"
    img = frame.copy()
    cv2.rectangle(img, (x1, y1), (x2, y2), (255, 200, 0) if box_is_zone else (0, 220, 0), 3)
    img = cv2.resize(img, (1280, int(1280 * img.shape[0] / img.shape[1])))
    cv2.imwrite(str(folder / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    pad = 0 if box_is_zone else max(6, (x2 - x1) // 8)
    h, w = frame.shape[:2]
    crop = frame[max(0, y1 - pad):min(h, y2 + pad), max(0, x1 - pad):min(w, x2 + pad)]
    if crop.size:
        if crop.shape[1] > 480:
            crop = cv2.resize(crop, (480, int(480 * crop.shape[0] / crop.shape[1])))
        cv2.imwrite(str(folder / f"{name}_plate.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return f"{day}/{name}"


# ---------- события камеры Hikvision (AcuSense) ----------

VEHICLE_EVENTS = ("linedetection", "fielddetection", "regionentrance", "regionexiting", "vmd")


def _parse_alert(xml: str) -> dict:
    def tag(t):
        m = re.search(rf"<{t}>([^<]*)</{t}>", xml)
        return m.group(1).strip() if m else ""
    targets = re.findall(r"<(?:targetType|detectionTarget)>([^<]*)<", xml)
    return {"type": tag("eventType").lower(), "state": tag("eventState").lower(),
            "targets": [t.lower() for t in targets]}


def listen_events(cam) -> None:
    """Постоянное подключение к /ISAPI/Event/notification/alertStream камеры.
    Проезд = событие пересечения линии / вторжения в зону с целью «vehicle»."""
    u = urlparse(cam.cfg["url"])
    host = f"{u.hostname}:{cam.cfg.get('http_port') or 80}"
    last_trigger = 0.0
    while True:
        try:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, f"http://{host}", unquote(u.username or ""), unquote(u.password or ""))
            opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
            resp = opener.open(f"http://{host}/ISAPI/Event/notification/alertStream", timeout=60)
            log.info("[%s] канал событий камеры подключён", cam.name)
            buf = ""
            while True:
                line = resp.readline()
                if not line:
                    break
                cam.events_ok_ts = time.time()
                buf += line.decode("utf-8", "ignore")
                if "</EventNotificationAlert>" not in buf:
                    if len(buf) > 200_000:
                        buf = ""
                    continue
                xml, buf = buf.split("</EventNotificationAlert>", 1)
                ev = _parse_alert(xml)
                if ev["type"] not in VEHICLE_EVENTS or ev["state"] != "active":
                    continue
                if ev["type"] == "vmd" and "vehicle" not in ev["targets"]:
                    continue  # обычное движение (люди, тени) без классификации - не проезд
                if ev["targets"] and "vehicle" not in ev["targets"]:
                    continue  # AcuSense: цель - человек
                if time.time() - last_trigger < 8:
                    continue  # одна машина даёт несколько событий подряд
                last_trigger = time.time()
                cam.vehicle_events += 1
                log.info("[%s] камера сообщила о проезде машины (%s)", cam.name, ev["type"])
                threading.Thread(target=cam.hub.vehicle_event, args=(cam,), daemon=True).start()
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] канал событий: %s, повтор через 15 с", cam.name, e)
        time.sleep(15)


hub = Hub()
