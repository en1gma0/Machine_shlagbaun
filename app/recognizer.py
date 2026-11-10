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


def _iou(a, b) -> float:
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


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
        # (группа камер, номер) -> время последнего наблюдения. Группы: снаружи / внутри.
        self._recent: dict[tuple[str, str], float] = {}
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
        """Поток открыт, но кадры не идут (завис) - переподключаемся сами.
        Если камера просто недоступна, поток сам повторяет попытки - лишние подключения не плодим."""
        while True:
            time.sleep(5)
            for cam in self.cameras:
                if cam.rebooting_until > time.time() or not cam.connected:
                    continue
                stale = time.time() - (cam.last_frame_ts or cam.started_ts)
                if stale > FREEZE_SEC and time.time() - cam.last_reconnect_ts > FREEZE_SEC:
                    log.warning("[%s] нет кадров %d с - автоматическое переподключение", cam.name, stale)
                    cam.reconnect(auto=True)

    def _load(self) -> None:
        import onnxruntime as ort
        from fast_alpr import ALPR

        def opts():
            # Ограничиваем потоки и отключаем «холостое вращение»: иначе каждое распознавание
            # занимает все ядра процессора
            so = ort.SessionOptions()
            so.intra_op_num_threads = config.ORT_THREADS
            so.inter_op_num_threads = 1
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            return so

        log.info("Загрузка моделей: %s / %s", config.DETECTOR_MODEL, config.OCR_MODEL)
        self.alpr = ALPR(detector_model=config.DETECTOR_MODEL, ocr_model=config.OCR_MODEL,
                         detector_conf_thresh=config.DETECTOR_CONF,
                         detector_sess_options=opts(), ocr_sess_options=opts())
        log.info("Модели загружены")
        for cam in self.cameras:
            cam.start_worker()

    def in_cooldown(self, cam, text: str, now: float) -> bool:
        """Номер (или похожий) уже зафиксирован недавно камерами этой группы - продлеваем и игнорируем.
        Камеры снаружи и внутри считаются отдельно: «подъехала» и «заехала» - разные события."""
        with self._lock:
            for key, seen in list(self._recent.items()):
                if now - seen > config.COOLDOWN_SEC:
                    del self._recent[key]
                    continue
                if key[0] == cam.group and db.levenshtein(key[1], text) <= 2:
                    self._recent[key] = now
                    return True
        return False

    def register(self, cam, plate: str, conf: float, snap=None) -> int | None:
        with self._lock:
            now = time.time()
            for (grp, p), seen in self._recent.items():  # другая камера той же группы успела раньше
                if grp == cam.group and now - seen <= config.COOLDOWN_SEC and db.levenshtein(p, plate) <= 2:
                    return None
            self._recent[(cam.group, plate)] = now
        outside = cam.group == "out"
        if outside and db.recent_inner_event(plate, config.APPROACH_WINDOW_SEC):
            return None  # эта машина только что заехала или выезжает - «подъехала» не нужно
        direction = "approach" if outside else (
            cam.direction if cam.direction in ("entry", "exit") else db.guess_direction(plate))
        d = db.decide(plate, direction)
        # Снаружи только запоминаем номер и решение; шлагбаум открывает проезд у внутренней камеры
        should_open = not outside and (d["allowed"] or (direction == "exit" and config.EXIT_OPEN_ALL))
        opened = gate.open_gate() if should_open else False
        snapshot = save_snapshot(plate, snap) if snap else ""
        suggest = ""
        if not d["matched_plate"]:  # не нашли - подсказать похожий номер из списка
            sg = db.suggest_plate([plate], max_distance=2)
            suggest = sg[0] if sg else ""
        eid = db.add_event(plate, round(conf, 3), direction, d, opened, snapshot=snapshot, camera=cam.name,
                           suggest=suggest)
        if direction == "entry":
            if d["guest"] is not None and d["allowed"]:
                db.guest_use(d["guest"]["id"])
            db.link_approach(eid, plate, config.APPROACH_WINDOW_SEC)
        cam.last_plate_event_ts = time.time()
        self.last_event = {"id": eid, "plate": plate, "direction": direction, "allowed": d["allowed"]}
        log.info("Событие #%s [%s]: %s %s %s шлагбаум=%s %s", eid, cam.name, plate, direction,
                 "РАЗРЕШЁН" if d["allowed"] else "ОТКАЗ", opened, d["reason"])
        return eid

    def vehicle_event(self, cam) -> None:
        """Камера (AcuSense) сообщила о проезде машины. Ждём 4 с: если номер за это время распознан -
        проезд уже записан. Иначе пишем проезд без номера."""
        t0 = time.time()
        frame = cam.latest_frame()
        time.sleep(4)
        if time.time() - cam.last_plate_event_ts < 12:
            return  # номер распознан - событие уже есть
        texts = [r["text"] for r in list(self.readings) if r.get("cam") == cam.name and t0 - r.get("ts", 0) < 12]
        frame = cam.latest_frame() if frame is None else frame
        snap = None
        if frame is not None:
            h, w = frame.shape[:2]
            snap = (frame, parse_roi(cam.cfg["roi"], w, h))
        self.unknown_vehicle(cam, snap, texts, zone=True, why="камера сообщила о машине")

    def unknown_vehicle(self, cam, snap, texts: list[str], zone: bool = False, why: str = "") -> int | None:
        """Машина есть, номер не прочитан: запись «без номера» с фото и подсказкой.
        Внутренняя камера: машина заехала (или выезжает). Камера снаружи: подъехала.
        Номер потом дописывают в журнале."""
        with self._lock:  # камера (AcuSense) и распознавание могут сработать на одну и ту же машину
            if time.time() - cam.last_unknown_ts < 20:
                return None
            cam.last_unknown_ts = time.time()
        outside = cam.group == "out"
        sg = db.suggest_plate(texts, max_distance=3)
        suggest = sg[0] if sg else ""
        direction = "approach" if outside else (cam.direction if cam.direction in ("entry", "exit") else (
            db.guess_direction(suggest) if suggest else "entry"))
        snapshot = save_snapshot("", snap, box_is_zone=zone) if snap else ""
        best = Counter(t for t in texts if t).most_common(1)
        what = {"approach": "машина подъехала", "entry": "машина заехала", "exit": "машина выехала"}[direction]
        reason = f"{what}, номер не распознан — допишите по фото"
        eid = db.add_event("", 0, direction, None, False, snapshot=snapshot, source="vehicle", camera=cam.name,
                           raw_plate=best[0][0] if best else "", suggest=suggest, reason=reason)
        if direction == "entry":
            # номер могла прочитать камера с улицы - берём его как подсказку
            a = db.link_approach(eid, "", config.APPROACH_WINDOW_SEC)
            if a and a["plate"]:
                sg = (a["plate"], a["owner"] or "прочитала камера снаружи")
                suggest = a["plate"]
                db.ex("UPDATE events SET suggest=? WHERE id=?", (suggest, eid))
        if sg:
            db.ex("UPDATE events SET reason=? WHERE id=?", (f"{reason}; похоже на {sg[0]} ({sg[1]})", eid))
        self.last_event = {"id": eid, "plate": "", "direction": direction, "allowed": False}
        log.info("Событие #%s [%s]: %s без номера (%s), прочитано=%s, подсказка=%s", eid, cam.name, what, why,
                 best[0][0] if best else "-", suggest or "-")
        return eid


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
        self.frame_seq = 0  # номер последнего кадра, превращённого в картинку
        self.last_reconnect_ts = 0.0
        self.rebooting_until = 0.0
        self.reconnects = 0
        self.last_plate_event_ts = 0.0
        self.events_ok_ts = 0.0  # когда последний раз пришло что-то по каналу событий камеры
        self.vehicle_events = 0
        self.last_unknown_ts = 0.0
        # номера, за которыми следим в зоне (для записи «машина без номера»)
        self._tracks: list[dict] = []
        self._first_proc_ts = 0.0
        self._tile_cache: dict = {}

    @property
    def name(self) -> str:
        return self.cfg["name"]

    @property
    def direction(self) -> str:
        return self.cfg["direction"]

    @property
    def group(self) -> str:
        """out - камера снаружи (улица, «подъехала»); in - у шлагбаума / во дворе («заехала», «выехала»)."""
        return "out" if self.cfg["direction"] == "outside" else "in"

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
            fails, last_ret = 0, 0.0
            while gen == self._gen:
                # grab() только принимает кадр из потока. В картинку (retrieve) превращаем лишь те кадры,
                # которые пойдут на распознавание: камера шлёт 25 кадр/с, а нужно около 5.
                if not cap.grab():
                    fails += 1
                    if fails > 50:
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                if gen != self._gen:
                    break
                now = time.time()
                self.last_frame_ts = now  # поток жив
                if now - last_ret < config.PROCESS_INTERVAL * 0.8:
                    continue
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    continue
                last_ret = now
                with self._lock:
                    self.frame = frame
                self.frame_seq += 1
            cap.release()
            if gen != self._gen:
                return  # заменён новым потоком
            self.connected = False
            log.warning("[%s] поток прервался, переподключение", self.name)
            time.sleep(2)

    def _worker(self) -> None:
        n, t0, done_seq = 0, time.time(), 0
        while True:
            start = time.time()
            if self.frame_seq == done_seq:  # нового кадра нет (поток завис или ещё не начался)
                time.sleep(0.05)
                if time.time() - t0 >= 5:
                    self.fps, n, t0 = 0.0, 0, time.time()
                continue
            done_seq = self.frame_seq
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
    def _tiles(self, rx1, ry1, rx2, ry2, scale: float = 1.0):
        """Большую зону режем на куски с перекрытием: детектор сжимает вход до 640 px, и мелкий номер
        на большой зоне пропадает. По кускам он виден в полном разрешении (с учётом увеличения)."""
        T = max(160, int(config.TILE_SIZE / scale))
        step = int(T * 0.75)
        w, h = rx2 - rx1, ry2 - ry1
        if not config.TILING or (w <= T * 1.25 and h <= T * 1.25):
            return [(rx1, ry1, rx2, ry2)]
        xs = list(range(rx1, max(rx1 + 1, rx2 - T), step)) + [max(rx1, rx2 - T)]
        ys = list(range(ry1, max(ry1 + 1, ry2 - T), step)) + [max(ry1, ry2 - T)]
        return [(x, y, min(x + T, rx2), min(y + T, ry2)) for y in sorted(set(ys)) for x in sorted(set(xs))]

    def detect(self, frame):
        """Список (text, conf, (x1,y1,x2,y2), fresh) в координатах полного кадра.
        text может быть пустым: номер найден, но не прочитан. fresh=False - результат взят из кэша:
        кусок кадра не менялся (машина стоит или пусто), заново его не распознаём - экономим процессор."""
        h, w = frame.shape[:2]
        scale = max(1.0, min(3.0, float(self.cfg.get("scale") or 1)))
        now = time.time()
        raw = []
        tiles = self._tiles(*parse_roi(self.cfg["roi"], w, h), scale)
        for key in list(self._tile_cache):
            if key not in tiles:
                del self._tile_cache[key]
        refreshed = False
        for tile in tiles:
            tx1, ty1, tx2, ty2 = tile
            crop = frame[ty1:ty2, tx1:tx2]
            thumb = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (48, 48), interpolation=cv2.INTER_AREA)
            cached = self._tile_cache.get(tile)
            if cached and float(cv2.absdiff(thumb, cached["thumb"]).mean()) < config.TILE_DIFF:
                # кусок не менялся. Раз в TILE_REFRESH_SEC всё же перепроверяем, но не больше одного
                # куска за проход, чтобы не было всплесков нагрузки
                if now - cached["ts"] < config.TILE_REFRESH_SEC or refreshed:
                    raw += [(t, c, b, dc, False) for t, c, b, dc in cached["res"]]
                    continue
                refreshed = True
            if scale > 1:
                crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            res = []
            for r in self.hub.alpr.predict(crop):
                conf = r.ocr.confidence if r.ocr else 0.0
                if isinstance(conf, (list, tuple)):
                    conf = sum(conf) / len(conf) if conf else 0.0
                b = r.detection.bounding_box
                box = (int(b.x1 / scale) + tx1, int(b.y1 / scale) + ty1,
                       int(b.x2 / scale) + tx1, int(b.y2 / scale) + ty1)
                text = db.normalize(r.ocr.text) if r.ocr and r.ocr.text else ""
                res.append((text, float(conf), box, float(r.detection.confidence)))
            self._tile_cache[tile] = {"thumb": thumb, "res": res, "ts": now}
            raw += [(t, c, b, dc, True) for t, c, b, dc in res]
        # один номер в зоне перекрытия кусков - оставить лучший
        raw.sort(key=lambda x: (x[4], x[3] + x[1]), reverse=True)
        out = []
        for text, conf, box, _, fresh in raw:
            if all(_iou(box, o[2]) < 0.3 for o in out):
                out.append((text, conf, box, fresh))
        return out

    def process_frame(self, frame) -> None:
        now = time.time()
        found = self.detect(frame)
        vis = frame.copy()
        h, w = frame.shape[:2]
        rx1, ry1, rx2, ry2 = parse_roi(self.cfg["roi"], w, h)
        cv2.rectangle(vis, (rx1, ry1), (rx2, ry2), (255, 200, 0), 3)

        dets = []
        for text, conf, (x1, y1, x2, y2), fresh in found:
            valid = len(text) >= 5 and conf >= config.MIN_OCR_CONF
            fmt_ok = not self._regex or bool(self._regex.match(text))
            valid = valid and fmt_ok
            dets.append((text, (x1, y1, x2, y2), valid, fresh))
            color = (0, 220, 0) if valid else (0, 140, 255)
            cv2.rectangle(vis, (x1, y1), (x2, y2), color, 3)
            cv2.putText(vis, f"{text or '?'} {conf:.2f}", (x1, max(30, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)
            why = "" if valid else ("не читается" if not text else "формат" if not fmt_ok else
                                    "короткий" if len(text) < 5 else "низкая уверенность")
            if not fresh:
                continue  # кусок кадра не менялся: то же самое чтение повторно не учитываем
            self.hub.readings.appendleft({"t": datetime.now().strftime("%H:%M:%S"), "cam": self.name,
                                          "text": text or "?", "conf": round(conf, 2), "w": x2 - x1,
                                          "valid": valid, "why": why, "ts": now})
            if valid:
                self._add_reading(text, conf, now, frame, (x1, y1, x2, y2))

        self._maybe_finalize(now)
        self._track_presence(now, frame, dets)
        small = cv2.resize(vis, (1280, int(1280 * h / w)))
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            self.annotated = jpg.tobytes()

    def _track_presence(self, now, frame, dets) -> None:
        """Номер появился в зоне и виден несколько секунд, но так и не прочитан -> «машина без номера».
        dets: [(text, box, valid, fresh)]. За каждым номером следим по месту в кадре:
        - одна машина = одна запись, пока она не уедет;
        - то, что уже стояло в зоне при запуске (припаркованная техника), не считается;
        - слишком мелкие «номера» вдали не считаются."""
        if not config.UNKNOWN_EVENTS:
            return
        if not self._first_proc_ts:
            self._first_proc_ts = now
        warmup = now - self._first_proc_ts < 20
        self._tracks = [t for t in self._tracks if now - t["last"] <= (
            config.UNKNOWN_REMEMBER_SEC if t["reported"] else config.UNKNOWN_GONE_SEC)]
        for text, box, valid, fresh in dets:
            w = box[2] - box[0]
            if w < config.UNKNOWN_MIN_WIDTH:
                continue
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2

            def dist(t):
                b = t["box"]
                return ((cx - (b[0] + b[2]) / 2) ** 2 + (cy - (b[1] + b[3]) / 2) ** 2) ** 0.5

            tr = next((t for t in self._tracks if _iou(box, t["box"]) > 0.2), None)
            if tr is None:  # машина едет: номер сместился - ищем недавний след рядом
                near = [t for t in self._tracks if now - t["last"] < 3 and now - t["first"] < 30 and dist(t) < 500]
                tr = min(near, key=dist) if near else None
            if tr is None:
                tr = {"first": now, "last": now, "hits": 0, "texts": [], "box": box, "snap": None, "best_w": 0,
                      "reported": warmup}
                self._tracks.append(tr)
            tr.update(last=now, box=box)
            tr["hits"] += 1
            if valid:
                tr["reported"] = True  # номер прочитан - его запишет обычное распознавание
            if text and fresh:
                tr["texts"].append(text)
            if tr["reported"]:
                tr["snap"] = None
            elif w > tr["best_w"]:
                tr.update(best_w=w, snap=(frame, box))
        if self._pending:
            return  # идёт чтение номера - подождём результата
        for tr in self._tracks:
            if (not tr["reported"] and tr["hits"] >= config.UNKNOWN_MIN_HITS
                    and now - tr["first"] >= config.UNKNOWN_AFTER_SEC):
                tr["reported"] = True
                snap, tr["snap"] = tr["snap"], None
                threading.Thread(target=self.hub.unknown_vehicle, daemon=True,
                                 args=(self, snap, list(tr["texts"])),
                                 kwargs={"why": f"номер виден {now - tr['first']:.0f} с, но не прочитан"}).start()

    def _add_reading(self, text, conf, now, frame, box) -> None:
        if self.hub.in_cooldown(self, text, now):
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
