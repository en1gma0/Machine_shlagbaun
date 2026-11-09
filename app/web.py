"""Веб-интерфейс: монитор, журнал, белый список, категории, отчёты, настройки, Excel."""
import base64
import csv
import io
import secrets
import time
import zipfile
from datetime import date, datetime, timedelta
from urllib.parse import quote

import cv2
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook, load_workbook

from . import config, db, gate
from .recognizer import hub

app = FastAPI(title="Учёт проездов")
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
DIR_RU = {"entry": "Въезд", "exit": "Выезд"}
TITLES = {"index": "Монитор", "journal": "Журнал", "reports": "Отчёты", "whitelist": "Белый список",
          "categories": "Категории", "settings": "Настройки"}
MESSAGES = {"saved": "Сохранено", "deleted": "Удалено", "toggled": "Статус изменён",
            "imported": "Импорт выполнен", "corrected": "Номер сохранён", "settings": "Настройки сохранены и уже применяются"}
templates.env.globals.update(DIR_RU=DIR_RU, SITE_NAME=config.SITE_NAME, DAYS_RU=db.DAYS_RU,
                             schedule_text=db.schedule_text, TITLES=TITLES, MESSAGES=MESSAGES)


def periods(date_from: str, date_to: str):
    """Быстрые периоды для фильтров: (название, с, по, выбран)."""
    t = date.today()
    items = [("Сегодня", t, t), ("Вчера", t - timedelta(1), t - timedelta(1)),
             ("7 дней", t - timedelta(6), t), ("30 дней", t - timedelta(29), t),
             ("Этот месяц", t.replace(day=1), t)]
    return [(n, a.isoformat(), b.isoformat(), (a.isoformat(), b.isoformat()) == (date_from, date_to))
            for n, a, b in items]


def go(url: str, msg: str):
    return RedirectResponse(f"{url}{'&' if '?' in url else '?'}msg={msg}", status_code=303)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.WEB_PASSWORD:
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.WEB_USER) and secrets.compare_digest(pwd, config.WEB_PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="gate"'})
    return await call_next(request)


def _period(date_from: str, date_to: str):
    today = date.today()
    return date_from or (today - timedelta(days=6)).isoformat(), date_to or today.isoformat()


def _cat_id(v: str):
    return int(v) if v and v.isdigit() else None


# ---------------- монитор ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"page": "index", "cameras": hub.cameras})


@app.get("/api/status")
def api_status():
    today = date.today().isoformat()
    day = db.report_by_day(today, today)
    return {
        "models_loaded": hub.alpr is not None,
        "cameras": [_cam_state(c) for c in hub.cameras],
        "readings": list(hub.readings)[:15],
        "events": [dict(r) for r in db.events(limit=15)],
        "inside": len(db.inside_now()),
        "today": dict(day[0]) if day else {"entries": 0, "exits": 0, "unique_cars": 0, "unknown": 0, "denied": 0},
        "gate_mode": config.GATE_MODE,
    }


def _cam_state(c):
    age = c.frame_age
    rebooting = c.rebooting_until > time.time()
    if rebooting and not (c.connected and age is not None and age < 5):
        state = "rebooting"
    elif not c.connected:
        state = "offline"
    elif age is None or age > 10:
        state = "frozen"
    else:
        state = "ok"
    return {"id": c.id, "name": c.name, "connected": state == "ok", "state": state,
            "fps": round(c.fps, 1), "frame_age": age, "reconnects": c.reconnects}


@app.get("/api/cameras")
def api_cameras():
    return [_cam_state(c) for c in hub.cameras]


@app.post("/camera/{cid}/reconnect")
def camera_reconnect(cid: int):
    cam = hub.camera(cid)
    if not cam:
        return JSONResponse({"ok": False, "error": "нет такой камеры"}, status_code=404)
    cam.reconnect()
    return {"ok": True, "message": f"«{cam.name}»: поток переподключается, картинка появится через несколько секунд"}


@app.post("/camera/{cid}/reboot")
def camera_reboot(cid: int):
    cam = hub.camera(cid)
    if not cam:
        return JSONResponse({"ok": False, "error": "нет такой камеры"}, status_code=404)
    try:
        cam.reboot()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"камера не приняла команду: {e}"}, status_code=502)
    return {"ok": True, "message": f"«{cam.name}» перезагружается, это займёт 1–2 минуты. Поток подключится сам."}


@app.get("/video/{cid}")
def video(cid: int):
    cam = hub.camera(cid)
    if not cam:
        return Response(status_code=404)

    def gen():
        while True:
            jpg = cam.annotated
            if jpg:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            time.sleep(0.3)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/frame/{cid}.jpg")
def frame(cid: int):
    """Текущий кадр без разметки (для выбора зоны на странице настроек)."""
    cam = hub.camera(cid)
    img = cam.latest_frame() if cam else None
    if img is None:
        return Response(status_code=404)
    img = cv2.resize(img, (1280, int(1280 * img.shape[0] / img.shape[1])))
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return Response(jpg.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/gate/open")
def gate_open(plate: str = Form(""), direction: str = Form("entry")):
    opened = gate.open_gate()
    plate = db.normalize(plate)
    direction = direction if direction in DIR_RU else "entry"
    match = db.whitelist_find(plate) if plate else None
    db.add_event(plate, 1.0, direction, True, opened, match=match, reason="открыто вручную", source="manual")
    return JSONResponse({"opened": opened, "mode": config.GATE_MODE})


@app.get("/snap/{path:path}")
def snap(path: str):
    f = (config.SNAP_DIR / path).resolve()
    if config.SNAP_DIR.resolve() not in f.parents or not f.exists():
        return Response(status_code=404)
    return FileResponse(f)


# ---------------- категории ----------------
@app.get("/categories", response_class=HTMLResponse)
def categories(request: Request, edit: int = 0):
    rows = db.categories()
    current = next((r for r in rows if r["id"] == edit), None)
    return templates.TemplateResponse(request, "categories.html",
                                      {"page": "categories", "rows": rows, "cur": current})


@app.post("/categories/save")
async def categories_save(request: Request):
    f = await request.form()
    days = "".join("1" if f.get(f"d{i}") else "0" for i in range(7))
    if (f.get("name") or "").strip():
        db.category_save(_cat_id(f.get("id", "")), f["name"], f.get("color", "#2563eb"), days,
                         f.get("time_from", "00:00"), f.get("time_to", "23:59"), bool(f.get("exit_any_time")))
    return go("/categories", "saved")


@app.post("/categories/{cid}/delete")
def categories_delete(cid: int):
    db.category_delete(cid)
    return go("/categories", "deleted")


# ---------------- белый список ----------------
@app.get("/whitelist", response_class=HTMLResponse)
def whitelist(request: Request, add: str = "", edit: int = 0, cat: str = ""):
    return templates.TemplateResponse(request, "whitelist.html", {
        "page": "whitelist", "rows": db.whitelist_all(cat), "prefill": add, "cat": cat,
        "cur": db.whitelist_get(edit) if edit else None, "categories": db.categories()})


@app.post("/whitelist/save")
def whitelist_save(plate: str = Form(...), owner: str = Form(""), phone: str = Form(""), note: str = Form(""),
                   valid_until: str = Form(""), category_id: str = Form(""), id: str = Form("")):
    if db.normalize(plate):
        if id:
            db.whitelist_update(int(id), plate, owner, phone, note, valid_until, _cat_id(category_id))
        else:
            db.whitelist_add(plate, owner, phone, note, valid_until, category_id=_cat_id(category_id))
    return go("/whitelist", "saved")


@app.post("/whitelist/{pid}/delete")
def whitelist_delete(pid: int):
    db.whitelist_delete(pid)
    return go("/whitelist", "deleted")


@app.post("/whitelist/{pid}/toggle")
def whitelist_toggle(pid: int):
    db.whitelist_toggle(pid)
    return go("/whitelist", "toggled")


@app.post("/whitelist/import")
async def whitelist_import(file: UploadFile):
    """Excel: Номер | Владелец | Телефон | Примечание | Действует до | Категория (1-я строка - заголовок)."""
    wb = load_workbook(io.BytesIO(await file.read()), read_only=True)
    for row in list(wb.active.iter_rows(values_only=True))[1:]:
        if not row or not row[0]:
            continue
        vals = [("" if v is None else (v.strftime("%Y-%m-%d") if isinstance(v, (date, datetime)) else str(v)))
                for v in row] + [""] * 6
        cat = db.category_by_name(vals[5])
        db.whitelist_add(vals[0], vals[1], vals[2], vals[3], vals[4][:10], category_id=cat["id"] if cat else None)
    return go("/whitelist", "imported")


@app.get("/whitelist/export")
def whitelist_export():
    rows = [[r["plate"], r["owner"], r["phone"], r["note"], r["valid_until"], r["cat_name"] or "",
             "да" if r["active"] else "нет"] for r in db.whitelist_all()]
    return _xlsx("белый_список", ["Номер", "Владелец", "Телефон", "Примечание", "Действует до", "Категория",
                                  "Активен"], rows)


# ---------------- журнал ----------------
@app.get("/journal", response_class=HTMLResponse)
def journal(request: Request, date_from: str = "", date_to: str = "", plate: str = "",
            allowed: str = "", direction: str = "", category: str = ""):
    date_from, date_to = _period(date_from, date_to)
    rows = db.events(date_from, date_to, plate, allowed, direction, category)
    f = {"date_from": date_from, "date_to": date_to, "plate": plate, "allowed": allowed,
         "direction": direction, "category": category}
    rest = "&".join(f"{k}={quote(v)}" for k, v in f.items() if k not in ("date_from", "date_to") and v)
    return templates.TemplateResponse(request, "journal.html", {
        "page": "journal", "rows": rows, "f": f, "categories": db.categories(),
        "periods": periods(date_from, date_to), "rest": rest,
        "qs": "&".join(f"{k}={quote(v)}" for k, v in f.items()), "ds": db.dataset_stats()})


@app.post("/journal/{eid}/correct")
def journal_correct(eid: int, plate: str = Form(...), back: str = Form("/journal")):
    db.correct_event(eid, plate)
    back = back if back.startswith("/journal") else "/journal"
    return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg=corrected#e{eid}", status_code=303)


@app.get("/journal/export")
def journal_export(date_from: str = "", date_to: str = "", plate: str = "", allowed: str = "",
                   direction: str = "", category: str = ""):
    date_from, date_to = _period(date_from, date_to)
    rows = [[r["ts"], r["plate"], r["raw_plate"] if r["raw_plate"] != r["plate"] else "",
             DIR_RU.get(r["direction"], r["direction"]), r["category"], r["owner"],
             "разрешён" if r["allowed"] else "отказ", r["reason"], "да" if r["gate_opened"] else "нет",
             r["camera"] or ("вручную" if r["source"] == "manual" else ""), r["confidence"]]
            for r in db.events(date_from, date_to, plate, allowed, direction, category, limit=100000)]
    return _xlsx(f"журнал_{date_from}_{date_to}",
                 ["Время", "Номер", "Прочитано камерой", "Направление", "Категория", "Владелец", "Доступ",
                  "Причина", "Шлагбаум открыт", "Камера", "Уверенность"], rows)


@app.get("/dataset/export")
def dataset_export():
    """Архив для дообучения fast-plate-ocr: images/*.jpg + labels.csv (image_path,plate_text)."""
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["image_path", "plate_text"])
        for r in db.dataset_rows():
            img = config.SNAP_DIR / f"{r['snapshot']}_plate.jpg"
            if img.exists():
                name = f"images/{r['id']}_{r['plate']}.jpg"
                z.write(img, name)
                w.writerow([name, r["plate"]])
                n += 1
        z.writestr("labels.csv", out.getvalue())
        z.writestr("README.txt",
                   f"Проверенных номеров: {n}\n"
                   "Формат: labels.csv (image_path, plate_text) для fast-plate-ocr.\n"
                   "Дообучение: https://github.com/ankandrew/fast-plate-ocr (раздел Train)\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=dataset_{date.today()}.zip"})


# ---------------- отчёты ----------------
@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, date_from: str = "", date_to: str = ""):
    date_from, date_to = _period(date_from, date_to)
    by_day = db.report_by_day(date_from, date_to)
    by_hour = {r["hour"]: r["cnt"] for r in db.report_by_hour(date_from, date_to)}
    totals = {k: sum(r[k] or 0 for r in by_day) for k in ("entries", "exits", "unknown", "denied", "opened")}
    return templates.TemplateResponse(request, "reports.html", {
        "page": "reports", "date_from": date_from, "date_to": date_to, "periods": periods(date_from, date_to),
        "by_day": by_day, "by_hour": [by_hour.get(h, 0) for h in range(24)],
        "by_cat": db.report_by_category(date_from, date_to), "denied": db.report_denied(date_from, date_to),
        "top": db.report_top(date_from, date_to), "visits": db.visits(date_from, date_to)[:500],
        "inside": db.inside_now(), "totals": totals})


@app.get("/reports/export")
def reports_export(date_from: str = "", date_to: str = ""):
    date_from, date_to = _period(date_from, date_to)
    wb = Workbook()
    ws = wb.active
    ws.title = "По дням"
    ws.append(["День", "Въездов", "Выездов", "Разных машин", "Неизвестных", "Отказов", "Открытий"])
    for r in db.report_by_day(date_from, date_to):
        ws.append([r["day"], r["entries"], r["exits"], r["unique_cars"], r["unknown"], r["denied"], r["opened"]])
    ws = wb.create_sheet("По категориям")
    ws.append(["Категория", "Въездов", "Выездов", "Разных машин", "Отказов"])
    for r in db.report_by_category(date_from, date_to):
        ws.append([r["cat"], r["entries"], r["exits"], r["unique_cars"], r["denied"]])
    ws = wb.create_sheet("Визиты")
    ws.append(["Номер", "Владелец", "Категория", "Въезд", "Выезд", "На территории (ч:мм)"])
    for v in db.visits(date_from, date_to):
        ws.append([v["plate"], v["owner"], v["category"], v["start"], v["end"], v["duration"]])
    ws = wb.create_sheet("Отказы")
    ws.append(["Время", "Номер", "Направление", "Категория", "Владелец", "Причина"])
    for r in db.report_denied(date_from, date_to, limit=100000):
        ws.append([r["ts"], r["plate"], DIR_RU.get(r["direction"]), r["category"], r["owner"], r["reason"]])
    ws = wb.create_sheet("Сейчас на территории")
    ws.append(["Номер", "Владелец", "Категория", "Въехал"])
    for r in db.inside_now():
        ws.append([r["plate"], r["owner"], r["category"], r["ts"]])
    return _wb_response(wb, f"отчёт_{date_from}_{date_to}")


# ---------------- настройки ----------------
@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings", "cameras": hub.cameras,
        "s": {"MIN_OCR_CONF": config.MIN_OCR_CONF, "CONFIRM_HITS": config.CONFIRM_HITS,
              "COOLDOWN_SEC": config.COOLDOWN_SEC, "WHITELIST_MAX_DISTANCE": config.WHITELIST_MAX_DISTANCE}})


@app.post("/settings/camera/{cid}")
def settings_camera(cid: int, roi: str = Form(""), scale: float = Form(1.0), direction: str = Form("auto"),
                    name: str = Form("")):
    cam = hub.camera(cid)
    if cam:
        scale = max(1.0, min(3.0, scale))
        direction = direction if direction in ("auto", "entry", "exit") else "auto"
        cam.cfg.update(roi=roi, scale=scale, direction=direction, name=name.strip() or cam.name)
        config.set_env(f"CAMERA{cid}_ROI", roi)
        config.set_env(f"CAMERA{cid}_SCALE", scale)
        config.set_env(f"CAMERA{cid}_DIRECTION", direction)
        config.set_env(f"CAMERA{cid}_NAME", cam.name)
    return go("/settings", "settings")


@app.post("/settings/recognition")
def settings_recognition(MIN_OCR_CONF: float = Form(...), CONFIRM_HITS: int = Form(...),
                         COOLDOWN_SEC: float = Form(...), WHITELIST_MAX_DISTANCE: int = Form(...)):
    values = {"MIN_OCR_CONF": max(0.0, min(1.0, MIN_OCR_CONF)), "CONFIRM_HITS": max(1, min(10, CONFIRM_HITS)),
              "COOLDOWN_SEC": max(5.0, COOLDOWN_SEC), "WHITELIST_MAX_DISTANCE": max(0, min(2, WHITELIST_MAX_DISTANCE))}
    for k, v in values.items():
        setattr(config, k, v)
        config.set_env(k, v)
    return go("/settings", "settings")


# ---------------- Excel ----------------
def _xlsx(name, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    return _wb_response(wb, name)


def _wb_response(wb, name):
    for ws in wb.worksheets:
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = max(12, min(45, max(len(str(c.value or "")) for c in col) + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.xlsx"})
