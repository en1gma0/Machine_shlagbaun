"""Веб-интерфейс: вход и роли, монитор, журнал, белый список, гости, категории, отчёты, настройки, аудит."""
import csv
import io
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

from . import auth, config, db, gate, maintenance
from .recognizer import hub

app = FastAPI(title="Учёт проездов")
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
DIR_RU = {"entry": "Въезд", "exit": "Выезд"}
TITLES = {"index": "Монитор", "journal": "Журнал", "reports": "Отчёты", "whitelist": "Белый список",
          "categories": "Категории", "settings": "Настройки", "guests": "Гостевые пропуска",
          "users": "Пользователи", "audit": "Аудит", "account": "Мой пароль"}
MESSAGES = {"saved": "Сохранено", "deleted": "Удалено", "toggled": "Статус изменён",
            "imported": "Импорт выполнен", "corrected": "Номер сохранён", "settings": "Настройки сохранены и уже применяются",
            "revoked": "Пропуск отозван", "created": "Создано", "password": "Пароль изменён",
            "backup": "Резервная копия создана", "cleanup": "Очистка выполнена", "comment": "Комментарий сохранён"}
FLAGS = {"antipassback": "повторный въезд"}
templates.env.globals.update(DIR_RU=DIR_RU, SITE_NAME=config.SITE_NAME, DAYS_RU=db.DAYS_RU,
                             schedule_text=db.schedule_text, TITLES=TITLES, MESSAGES=MESSAGES,
                             ROLES=auth.ROLES, FLAGS=FLAGS, guest_status=db.guest_status)
PUBLIC = ("/login", "/setup", "/static/")


# ---------------- вход и права ----------------
def ip_of(request: Request) -> str:
    return request.client.host if request.client else ""


def user_of(request: Request):
    return getattr(request.state, "user", None)


def uname(request: Request) -> str:
    u = user_of(request)
    return u["username"] if u else "?"


def log_action(request: Request, action: str, details: str = "") -> None:
    db.audit(uname(request), action, details, ip_of(request))


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    request.state.user = None
    if path.startswith(PUBLIC):
        return await call_next(request)
    if auth.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    user = db.session_user(request.cookies.get(auth.COOKIE, ""))
    if user is None:
        if path.startswith(("/api/", "/video/", "/frame/", "/snap/")) or request.method != "GET":
            return JSONResponse({"ok": False, "error": "нужно войти в систему"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)
    request.state.user = user
    if not auth.can_access(user, path):
        if request.method == "GET" and not path.startswith("/api/"):
            return templates.TemplateResponse(request, "denied.html", {"page": ""}, status_code=403)
        return JSONResponse({"ok": False, "error": "недостаточно прав"}, status_code=403)
    return await call_next(request)


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str = ""):
    if not auth.needs_setup():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"page": "", "setup": True, "error": error})


@app.post("/setup")
def setup_save(request: Request, username: str = Form(...), password: str = Form(...),
               password2: str = Form(...), full_name: str = Form("")):
    if not auth.needs_setup():
        return RedirectResponse("/login", status_code=303)
    err = auth.password_problem(password) or ("Пароли не совпадают" if password != password2 else "") \
        or ("Укажите логин" if not username.strip() else "")
    if err:
        return RedirectResponse(f"/setup?error={quote(err)}", status_code=303)
    db.user_create(username, auth.hash_password(password), "admin", full_name)
    db.audit(username.strip(), "создан первый администратор", username.strip(), ip_of(request))
    return RedirectResponse("/login?msg=created", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    if auth.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"page": "", "setup": False, "error": error, "next": next})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    ip = ip_of(request)
    wait = auth.login_blocked(ip)
    if wait:
        return RedirectResponse(f"/login?error={quote(f'Слишком много попыток. Подождите {wait} с')}", status_code=303)
    u = db.user_by_name(username)
    if not u or not u["active"] or not auth.check_password(password, u["password_hash"]):
        auth.login_failed(ip)
        db.audit(username.strip() or "?", "неудачный вход", "неверный логин или пароль", ip)
        return RedirectResponse(f"/login?error={quote('Неверный логин или пароль')}", status_code=303)
    auth.login_ok(ip)
    token = auth.new_token()
    db.session_create(token, u["id"], config.SESSION_DAYS, ip)
    db.audit(u["username"], "вход", "", ip)
    resp = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    resp.set_cookie(auth.COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout(request: Request):
    log_action(request, "выход")
    db.session_delete(request.cookies.get(auth.COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "account.html", {"page": "account", "error": error})


@app.post("/account")
def account_save(request: Request, old: str = Form(...), new: str = Form(...), new2: str = Form(...)):
    u = user_of(request)
    err = ("Текущий пароль неверный" if not auth.check_password(old, u["password_hash"]) else "") \
        or auth.password_problem(new) or ("Пароли не совпадают" if new != new2 else "")
    if err:
        return RedirectResponse(f"/account?error={quote(err)}", status_code=303)
    db.user_update(u["id"], password_hash=auth.hash_password(new))
    log_action(request, "смена своего пароля")
    resp = RedirectResponse("/login?msg=password", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---------------- пользователи (админ) ----------------
@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, edit: int = 0, error: str = ""):
    return templates.TemplateResponse(request, "users.html", {
        "page": "users", "rows": db.users(), "cur": db.user_get(edit) if edit else None, "error": error})


@app.post("/users/save")
def users_save(request: Request, id: str = Form(""), username: str = Form(""), full_name: str = Form(""),
               role: str = Form("guard"), password: str = Form("")):
    role = role if role in auth.ROLES else "guard"
    if id:
        u = db.user_get(int(id))
        if not u:
            return go("/users", "saved")
        if u["role"] == "admin" and role != "admin" and db.admins_count() <= 1:
            return RedirectResponse(f"/users?error={quote('Нельзя убрать последнего администратора')}", status_code=303)
        if password:
            if auth.password_problem(password):
                return RedirectResponse(f"/users?edit={id}&error={quote(auth.password_problem(password))}", status_code=303)
            db.user_update(u["id"], password_hash=auth.hash_password(password))
        db.user_update(u["id"], full_name=full_name, role=role)
        log_action(request, "изменён пользователь", f"{u['username']}: роль {auth.ROLES[role]}" + ("; новый пароль" if password else ""))
    else:
        err = ("Укажите логин" if not username.strip() else "") or auth.password_problem(password) \
            or ("Такой логин уже есть" if db.user_by_name(username) else "")
        if err:
            return RedirectResponse(f"/users?error={quote(err)}", status_code=303)
        db.user_create(username, auth.hash_password(password), role, full_name)
        log_action(request, "создан пользователь", f"{username.strip()} ({auth.ROLES[role]})")
    return go("/users", "saved")


@app.post("/users/{uid}/toggle")
def users_toggle(request: Request, uid: int):
    u = db.user_get(uid)
    if u:
        if u["active"] and u["role"] == "admin" and db.admins_count() <= 1:
            return RedirectResponse(f"/users?error={quote('Нельзя отключить последнего администратора')}", status_code=303)
        if u["id"] == user_of(request)["id"]:
            return RedirectResponse(f"/users?error={quote('Нельзя отключить самого себя')}", status_code=303)
        db.user_update(uid, active=0 if u["active"] else 1)
        log_action(request, "пользователь " + ("отключён" if u["active"] else "включён"), u["username"])
    return go("/users", "toggled")


@app.post("/users/{uid}/delete")
def users_delete(request: Request, uid: int):
    u = db.user_get(uid)
    if u:
        if u["id"] == user_of(request)["id"]:
            return RedirectResponse(f"/users?error={quote('Нельзя удалить самого себя')}", status_code=303)
        if u["role"] == "admin" and u["active"] and db.admins_count() <= 1:
            return RedirectResponse(f"/users?error={quote('Нельзя удалить последнего администратора')}", status_code=303)
        db.user_delete(uid)
        log_action(request, "удалён пользователь", u["username"])
    return go("/users", "deleted")


# ---------------- аудит (админ) ----------------
@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, date_from: str = "", date_to: str = "", username: str = "", text: str = "",
               page: int = 1):
    date_from, date_to = _period(date_from, date_to)
    per = 100
    rows, total = db.audit_list(date_from, date_to, username, text, per, (max(1, page) - 1) * per)
    users = sorted({r["username"] for r in db.users()} | {"система"})
    f = {"date_from": date_from, "date_to": date_to, "username": username, "text": text}
    return templates.TemplateResponse(request, "audit.html", {
        "page": "audit", "rows": rows, "f": f, "users": users, "periods": periods(date_from, date_to),
        "pg": _pager(page, per, total), "qs": _qs(f)})


# ---------------- общие утилиты ----------------
def _period(date_from: str, date_to: str):
    today = date.today()
    return date_from or (today - timedelta(days=6)).isoformat(), date_to or today.isoformat()


def periods(date_from: str, date_to: str):
    """Быстрые периоды для фильтров: (название, с, по, выбран)."""
    t = date.today()
    items = [("Сегодня", t, t), ("Вчера", t - timedelta(1), t - timedelta(1)),
             ("7 дней", t - timedelta(6), t), ("30 дней", t - timedelta(29), t),
             ("Этот месяц", t.replace(day=1), t)]
    return [(n, a.isoformat(), b.isoformat(), (a.isoformat(), b.isoformat()) == (date_from, date_to))
            for n, a, b in items]


def _qs(f: dict, skip=()) -> str:
    return "&".join(f"{k}={quote(str(v))}" for k, v in f.items() if v and k not in skip)


def _pager(page: int, per: int, total: int) -> dict:
    pages = max(1, -(-total // per))
    page = min(max(1, page), pages)
    return {"page": page, "pages": pages, "total": total, "from": (page - 1) * per + 1 if total else 0,
            "to": min(total, page * per)}


def go(url: str, msg: str):
    return RedirectResponse(f"{url}{'&' if '?' in url else '?'}msg={msg}", status_code=303)


def _cat_id(v: str):
    return int(v) if v and v.isdigit() else None


# ---------------- монитор ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"page": "index", "cameras": hub.cameras})


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
    ev = None
    if c.cfg.get("acusense"):
        ev = bool(c.events_ok_ts and time.time() - c.events_ok_ts < 90)
    return {"id": c.id, "name": c.name, "connected": state == "ok", "state": state,
            "fps": round(c.fps, 1), "frame_age": age, "reconnects": c.reconnects, "events": ev,
            "vehicle_events": c.vehicle_events}


@app.get("/api/status")
def api_status():
    today = date.today().isoformat()
    day = db.report_by_day(today, today)
    return {
        "models_loaded": hub.alpr is not None,
        "cameras": [_cam_state(c) for c in hub.cameras],
        "readings": [{k: v for k, v in r.items() if k != "ts"} for r in list(hub.readings)[:15]],
        "events": [dict(r) for r in db.events(limit=15)],
        "inside": len(db.inside_now()),
        "today": dict(day[0]) if day else {"entries": 0, "exits": 0, "unique_cars": 0, "unknown": 0, "denied": 0},
        "gate_mode": config.GATE_MODE,
        "guests_active": len(db.guests_active()),
    }


@app.get("/api/cameras")
def api_cameras():
    return {"cameras": [_cam_state(c) for c in hub.cameras], "alerts": maintenance.alerts_list()}


@app.post("/camera/{cid}/reconnect")
def camera_reconnect(request: Request, cid: int):
    cam = hub.camera(cid)
    if not cam:
        return JSONResponse({"ok": False, "error": "нет такой камеры"}, status_code=404)
    cam.reconnect()
    log_action(request, "переподключение камеры", cam.name)
    return {"ok": True, "message": f"«{cam.name}»: поток переподключается, картинка появится через несколько секунд"}


@app.post("/camera/{cid}/reboot")
def camera_reboot(request: Request, cid: int):
    cam = hub.camera(cid)
    if not cam:
        return JSONResponse({"ok": False, "error": "нет такой камеры"}, status_code=404)
    try:
        cam.reboot()
    except Exception as e:  # noqa: BLE001
        log_action(request, "перезагрузка камеры: ошибка", f"{cam.name}: {e}")
        return JSONResponse({"ok": False, "error": f"камера не приняла команду: {e}"}, status_code=502)
    log_action(request, "перезагрузка камеры", cam.name)
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
def gate_open(request: Request, plate: str = Form(""), direction: str = Form("entry"), comment: str = Form("")):
    plate = db.normalize(plate)
    direction = direction if direction in DIR_RU else "entry"
    d = db.decide(plate, direction) if plate else None
    opened = gate.open_gate()
    if d and d["guest"] is not None and direction == "entry":
        db.guest_use(d["guest"]["id"])
    reason = f"открыто вручную: {uname(request)}"
    if d and d["flag"]:
        reason += "; повторный въезд без выезда"
    eid = db.add_event(plate, 1.0, direction, d, opened, source="manual", username=uname(request),
                       comment=comment, reason=reason)
    db.ex("UPDATE events SET allowed=1 WHERE id=?", (eid,))  # пропустил человек - проезд разрешён
    log_action(request, "ручное открытие шлагбаума",
               f"{DIR_RU[direction]}; номер {plate or '—'}" + (f"; {comment}" if comment else ""))
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
        cid = _cat_id(f.get("id", ""))
        db.category_save(cid, f["name"], f.get("color", "#2563eb"), days,
                         f.get("time_from", "00:00"), f.get("time_to", "23:59"), bool(f.get("exit_any_time")))
        log_action(request, "категория " + ("изменена" if cid else "создана"),
                   f"{f['name']}: дни {days}, {f.get('time_from')}–{f.get('time_to')}")
    return go("/categories", "saved")


@app.post("/categories/{cid}/delete")
def categories_delete(request: Request, cid: int):
    c = next((r for r in db.categories() if r["id"] == cid), None)
    db.category_delete(cid)
    log_action(request, "категория удалена", c["name"] if c else str(cid))
    return go("/categories", "deleted")


# ---------------- белый список ----------------
@app.get("/whitelist", response_class=HTMLResponse)
def whitelist(request: Request, add: str = "", edit: int = 0, cat: str = ""):
    return templates.TemplateResponse(request, "whitelist.html", {
        "page": "whitelist", "rows": db.whitelist_all(cat), "prefill": add, "cat": cat,
        "cur": db.whitelist_get(edit) if edit else None, "categories": db.categories()})


@app.post("/whitelist/save")
def whitelist_save(request: Request, plate: str = Form(...), owner: str = Form(""), phone: str = Form(""),
                   note: str = Form(""), valid_until: str = Form(""), category_id: str = Form(""), id: str = Form("")):
    if db.normalize(plate):
        if id:
            old = db.whitelist_get(int(id))
            db.whitelist_update(int(id), plate, owner, phone, note, valid_until, _cat_id(category_id))
            log_action(request, "белый список: изменён", f"{old['plate'] if old else ''} → {db.normalize(plate)}, {owner}")
        else:
            db.whitelist_add(plate, owner, phone, note, valid_until, category_id=_cat_id(category_id))
            log_action(request, "белый список: добавлен", f"{db.normalize(plate)}, {owner}")
    return go("/whitelist", "saved")


@app.post("/whitelist/{pid}/delete")
def whitelist_delete(request: Request, pid: int):
    r = db.whitelist_get(pid)
    db.whitelist_delete(pid)
    log_action(request, "белый список: удалён", f"{r['plate']}, {r['owner']}" if r else str(pid))
    return go("/whitelist", "deleted")


@app.post("/whitelist/{pid}/toggle")
def whitelist_toggle(request: Request, pid: int):
    r = db.whitelist_get(pid)
    db.whitelist_toggle(pid)
    if r:
        log_action(request, "белый список: " + ("отключён" if r["active"] else "включён"), r["plate"])
    return go("/whitelist", "toggled")


@app.post("/whitelist/import")
async def whitelist_import(request: Request, file: UploadFile):
    """Excel: Номер | Владелец | Телефон | Примечание | Действует до | Категория (1-я строка - заголовок)."""
    wb = load_workbook(io.BytesIO(await file.read()), read_only=True)
    n = 0
    for row in list(wb.active.iter_rows(values_only=True))[1:]:
        if not row or not row[0]:
            continue
        vals = [("" if v is None else (v.strftime("%Y-%m-%d") if isinstance(v, (date, datetime)) else str(v)))
                for v in row] + [""] * 6
        cat = db.category_by_name(vals[5])
        db.whitelist_add(vals[0], vals[1], vals[2], vals[3], vals[4][:10], category_id=cat["id"] if cat else None)
        n += 1
    log_action(request, "белый список: импорт Excel", f"{file.filename}: {n} строк")
    return go("/whitelist", "imported")


@app.get("/whitelist/export")
def whitelist_export():
    rows = [[r["plate"], r["owner"], r["phone"], r["note"], r["valid_until"], r["cat_name"] or "",
             "да" if r["active"] else "нет"] for r in db.whitelist_all()]
    return _xlsx("белый_список", ["Номер", "Владелец", "Телефон", "Примечание", "Действует до", "Категория",
                                  "Активен"], rows)


# ---------------- гостевые пропуска ----------------
@app.get("/guests", response_class=HTMLResponse)
def guests(request: Request, show: str = "active", add: str = ""):
    t = datetime.now()
    presets = [("2 часа", t + timedelta(hours=2)), ("До 18:00", t.replace(hour=18, minute=0, second=0)),
               ("До конца дня", t.replace(hour=23, minute=59, second=0)), ("Сутки", t + timedelta(days=1)),
               ("Неделя", t + timedelta(days=7))]
    presets = [(n, v.strftime("%Y-%m-%dT%H:%M")) for n, v in presets if v > t]
    return templates.TemplateResponse(request, "guests.html", {
        "page": "guests", "rows": db.guests_list(show), "show": show, "prefill": add,
        "now": t.strftime("%Y-%m-%dT%H:%M"), "presets": presets})


@app.post("/guests/create")
def guests_create(request: Request, plate: str = Form(...), name: str = Form(""), note: str = Form(""),
                  valid_from: str = Form(""), valid_to: str = Form(...), max_entries: int = Form(1)):
    plate = db.normalize(plate)
    vf = (valid_from or datetime.now().strftime("%Y-%m-%dT%H:%M")).replace("T", " ")
    vt = valid_to.replace("T", " ")
    vf, vt = (vf + ":00")[:19], (vt + ":59")[:19]
    if not plate or vt <= vf:
        return go("/guests", "saved")
    db.guest_create(plate, name, note, vf, vt, max(0, max_entries), uname(request))
    log_action(request, "гостевой пропуск выдан",
               f"{plate} {name}: {vf[:16]} – {vt[:16]}, въездов: {max_entries or 'без ограничения'}")
    return go("/guests", "created")


@app.post("/guests/{gid}/revoke")
def guests_revoke(request: Request, gid: int):
    g = db.guest_get(gid)
    db.guest_revoke(gid)
    log_action(request, "гостевой пропуск отозван", f"{g['plate']} {g['name']}" if g else str(gid))
    return go("/guests", "revoked")


# ---------------- журнал ----------------
JOURNAL_PER_PAGE = 100


@app.get("/journal", response_class=HTMLResponse)
def journal(request: Request, date_from: str = "", date_to: str = "", plate: str = "",
            allowed: str = "", direction: str = "", category: str = "", page: int = 1):
    date_from, date_to = _period(date_from, date_to)
    filt = (date_from, date_to, plate, allowed, direction, category)
    pg = _pager(page, JOURNAL_PER_PAGE, db.events_count(*filt))
    rows = db.events(*filt, limit=JOURNAL_PER_PAGE, offset=(pg["page"] - 1) * JOURNAL_PER_PAGE)
    f = {"date_from": date_from, "date_to": date_to, "plate": plate, "allowed": allowed,
         "direction": direction, "category": category}
    return templates.TemplateResponse(request, "journal.html", {
        "page": "journal", "rows": rows, "f": f, "categories": db.categories(), "pg": pg,
        "periods": periods(date_from, date_to), "rest": _qs(f, skip=("date_from", "date_to")),
        "qs": _qs(f), "back": f"/journal?{_qs({**f, 'page': pg['page']})}", "ds": db.dataset_stats(),
        "is_admin": auth.is_admin(user_of(request))})


def _back(back: str, eid: int, msg: str):
    back = back if back.startswith("/journal") else "/journal"
    return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg={msg}#e{eid}", status_code=303)


@app.post("/journal/{eid}/correct")
def journal_correct(request: Request, eid: int, plate: str = Form(...), back: str = Form("/journal")):
    e = db.event_get(eid)
    db.correct_event(eid, plate)
    if e:
        new = db.normalize(plate)
        log_action(request, "журнал: номер " + ("подтверждён" if new == e["plate"] else "исправлен"),
                   f"#{eid} {e['ts']}: {e['plate'] or '—'}" + (f" → {new}" if new != e["plate"] else ""))
    return _back(back, eid, "corrected")


@app.post("/journal/{eid}/edit")
def journal_edit(request: Request, eid: int, comment: str = Form(""), direction: str = Form(""),
                 back: str = Form("/journal")):
    e = db.event_get(eid)
    if e:
        changes = []
        if comment.strip() != (e["comment"] or ""):
            db.event_comment(eid, comment)
            changes.append(f"комментарий: «{comment.strip()}»")
        if direction in DIR_RU and direction != e["direction"]:
            db.event_set_direction(eid, direction)
            changes.append(f"направление: {DIR_RU[e['direction']]} → {DIR_RU[direction]}")
        if changes:
            log_action(request, "журнал: изменена запись", f"#{eid} {e['ts']} {e['plate'] or '—'}: " + "; ".join(changes))
    return _back(back, eid, "comment")


@app.post("/journal/{eid}/delete")
def journal_delete(request: Request, eid: int, back: str = Form("/journal")):
    if not auth.is_admin(user_of(request)):
        return JSONResponse({"ok": False, "error": "удалять записи может только администратор"}, status_code=403)
    e = db.event_delete(eid)
    if e:
        if e["snapshot"]:
            for suffix in (".jpg", "_plate.jpg"):
                (config.SNAP_DIR / f"{e['snapshot']}{suffix}").unlink(missing_ok=True)
        log_action(request, "журнал: запись удалена",
                   f"#{eid} {e['ts']} {e['plate'] or '—'} {DIR_RU.get(e['direction'], '')} ({e['camera'] or e['source']})")
    back = back if back.startswith("/journal") else "/journal"
    return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg=deleted", status_code=303)


@app.get("/journal/export")
def journal_export(date_from: str = "", date_to: str = "", plate: str = "", allowed: str = "",
                   direction: str = "", category: str = ""):
    date_from, date_to = _period(date_from, date_to)
    rows = [[r["ts"], r["plate"], r["raw_plate"] if r["raw_plate"] != r["plate"] else "",
             DIR_RU.get(r["direction"], r["direction"]), r["category"], r["owner"],
             "разрешён" if r["allowed"] else "отказ", r["reason"], FLAGS.get(r["flag"], r["flag"]),
             "да" if r["gate_opened"] else "нет", r["camera"] or r["source"], r["username"], r["comment"],
             r["confidence"]]
            for r in db.events(date_from, date_to, plate, allowed, direction, category, limit=100000)]
    return _xlsx(f"журнал_{date_from}_{date_to}",
                 ["Время", "Номер", "Прочитано камерой", "Направление", "Категория", "Владелец", "Доступ",
                  "Причина", "Отметка", "Шлагбаум открыт", "Камера", "Открыл", "Комментарий", "Уверенность"], rows)


@app.get("/dataset/export")
def dataset_export(request: Request):
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
    log_action(request, "выгрузка набора для обучения", f"{n} фото")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=dataset_{date.today()}.zip"})


# ---------------- отчёты ----------------
@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, date_from: str = "", date_to: str = ""):
    date_from, date_to = _period(date_from, date_to)
    by_day = db.report_by_day(date_from, date_to)
    by_hour = {r["hour"]: r["cnt"] for r in db.report_by_hour(date_from, date_to)}
    totals = {k: sum(r[k] or 0 for r in by_day) for k in ("entries", "exits", "unknown", "denied", "opened", "flagged")}
    return templates.TemplateResponse(request, "reports.html", {
        "page": "reports", "date_from": date_from, "date_to": date_to, "periods": periods(date_from, date_to),
        "by_day": by_day, "by_hour": [by_hour.get(h, 0) for h in range(24)],
        "by_cat": db.report_by_category(date_from, date_to), "denied": db.report_denied(date_from, date_to),
        "flagged": db.report_flagged(date_from, date_to),
        "top": db.report_top(date_from, date_to), "visits": db.visits(date_from, date_to)[:500],
        "inside": db.inside_now(), "totals": totals})


@app.get("/reports/export")
def reports_export(date_from: str = "", date_to: str = ""):
    date_from, date_to = _period(date_from, date_to)
    wb = Workbook()
    ws = wb.active
    ws.title = "По дням"
    ws.append(["День", "Въездов", "Выездов", "Разных машин", "Неизвестных", "Отказов", "Открытий", "Подозрительных"])
    for r in db.report_by_day(date_from, date_to):
        ws.append([r["day"], r["entries"], r["exits"], r["unique_cars"], r["unknown"], r["denied"], r["opened"],
                   r["flagged"]])
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
    ws = wb.create_sheet("Подозрительные")
    ws.append(["Время", "Номер", "Направление", "Владелец", "Отметка", "Причина"])
    for r in db.report_flagged(date_from, date_to, limit=100000):
        ws.append([r["ts"], r["plate"], DIR_RU.get(r["direction"]), r["owner"], FLAGS.get(r["flag"], r["flag"]),
                   r["reason"]])
    ws = wb.create_sheet("Сейчас на территории")
    ws.append(["Номер", "Владелец", "Категория", "Въехал"])
    for r in db.inside_now():
        ws.append([r["plate"], r["owner"], r["category"], r["ts"]])
    return _wb_response(wb, f"отчёт_{date_from}_{date_to}")


# ---------------- настройки и обслуживание (админ) ----------------
@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings", "cameras": hub.cameras,
        "s": {"MIN_OCR_CONF": config.MIN_OCR_CONF, "CONFIRM_HITS": config.CONFIRM_HITS,
              "COOLDOWN_SEC": config.COOLDOWN_SEC, "WHITELIST_MAX_DISTANCE": config.WHITELIST_MAX_DISTANCE,
              "ANTIPASSBACK": config.ANTIPASSBACK, "SNAP_KEEP_DAYS": config.SNAP_KEEP_DAYS,
              "BACKUP_KEEP": config.BACKUP_KEEP},
        "disk": maintenance.disk_info(), "snaps": maintenance.snaps_info(), "backups": maintenance.backups_list(),
        "mstate": maintenance.state, "backup_dir": str(config.BACKUP_DIR), "backup_hour": config.BACKUP_HOUR,
        "telegram": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)})


@app.post("/settings/camera/{cid}")
def settings_camera(request: Request, cid: int, roi: str = Form(""), scale: float = Form(1.0),
                    direction: str = Form("auto"), name: str = Form("")):
    cam = hub.camera(cid)
    if cam:
        scale = max(1.0, min(3.0, scale))
        direction = direction if direction in ("auto", "entry", "exit") else "auto"
        cam.cfg.update(roi=roi, scale=scale, direction=direction, name=name.strip() or cam.name)
        config.set_env(f"CAMERA{cid}_ROI", roi)
        config.set_env(f"CAMERA{cid}_SCALE", scale)
        config.set_env(f"CAMERA{cid}_DIRECTION", direction)
        config.set_env(f"CAMERA{cid}_NAME", cam.name)
        log_action(request, "настройки камеры", f"{cam.name}: зона {roi or 'весь кадр'}, ×{scale}, {direction}")
    return go("/settings", "settings")


@app.post("/settings/recognition")
def settings_recognition(request: Request, MIN_OCR_CONF: float = Form(...), CONFIRM_HITS: int = Form(...),
                         COOLDOWN_SEC: float = Form(...), WHITELIST_MAX_DISTANCE: int = Form(...),
                         ANTIPASSBACK: str = Form("warn")):
    values = {"MIN_OCR_CONF": max(0.0, min(1.0, MIN_OCR_CONF)), "CONFIRM_HITS": max(1, min(10, CONFIRM_HITS)),
              "COOLDOWN_SEC": max(5.0, COOLDOWN_SEC), "WHITELIST_MAX_DISTANCE": max(0, min(2, WHITELIST_MAX_DISTANCE)),
              "ANTIPASSBACK": ANTIPASSBACK if ANTIPASSBACK in ("off", "warn", "deny") else "warn"}
    for k, v in values.items():
        setattr(config, k, v)
        config.set_env(k, v)
    log_action(request, "настройки распознавания", ", ".join(f"{k}={v}" for k, v in values.items()))
    return go("/settings", "settings")


@app.post("/settings/maintenance")
def settings_maintenance(request: Request, SNAP_KEEP_DAYS: int = Form(...), BACKUP_KEEP: int = Form(...)):
    values = {"SNAP_KEEP_DAYS": max(0, SNAP_KEEP_DAYS), "BACKUP_KEEP": max(1, min(365, BACKUP_KEEP))}
    for k, v in values.items():
        setattr(config, k, v)
        config.set_env(k, v)
    log_action(request, "настройки обслуживания", ", ".join(f"{k}={v}" for k, v in values.items()))
    return go("/settings#maintenance", "settings")


@app.post("/maintenance/backup")
def maintenance_backup(request: Request):
    try:
        maintenance.backup_now(uname(request))
    except Exception:  # noqa: BLE001
        pass
    return go("/settings#maintenance", "backup")


@app.post("/maintenance/cleanup")
def maintenance_cleanup(request: Request):
    maintenance.cleanup_snaps(uname(request))
    return go("/settings#maintenance", "cleanup")


@app.get("/backups/{name}")
def backup_download(request: Request, name: str):
    f = (config.BACKUP_DIR / name).resolve()
    if f.parent != config.BACKUP_DIR.resolve() or not name.startswith("gate_") or not f.exists():
        return Response(status_code=404)
    log_action(request, "скачана резервная копия", name)
    return FileResponse(f, filename=name)


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
