"""SQLite: пользователи, аудит, категории, белый список, гостевые пропуска, журнал событий."""
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from . import config

_lock = threading.RLock()
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
# WAL быстрее, но не работает на папках, подключённых в Docker Desktop с Windows (нет общей памяти).
# В контейнере задаётся DB_JOURNAL=DELETE.
_conn.execute(f"PRAGMA journal_mode={os.environ.get('DB_JOURNAL', 'WAL')}")
_conn.execute("PRAGMA busy_timeout=5000")

_conn.executescript(
    """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT DEFAULT '#2563eb',
    days TEXT DEFAULT '1111111',       -- пн..вс, 1 = въезд разрешён
    time_from TEXT DEFAULT '00:00',
    time_to TEXT DEFAULT '23:59',
    exit_any_time INTEGER DEFAULT 1    -- выезд в любое время
);
CREATE TABLE IF NOT EXISTS plates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate TEXT NOT NULL UNIQUE,
    owner TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    note TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    valid_until TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    plate TEXT NOT NULL,
    confidence REAL,
    direction TEXT NOT NULL,          -- entry / exit
    allowed INTEGER NOT NULL,         -- 1 = доступ разрешён
    gate_opened INTEGER NOT NULL,
    matched_plate TEXT DEFAULT '',
    owner TEXT DEFAULT '',
    snapshot TEXT DEFAULT '',
    source TEXT DEFAULT 'camera'      -- camera / manual / vehicle (проезд без номера)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_plate ON events(plate);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'guard',  -- admin / guard
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT NOT NULL,          -- 'система' для автоматических событий
    action TEXT NOT NULL,
    details TEXT DEFAULT '',
    ip TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS guest_passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate TEXT NOT NULL,
    name TEXT DEFAULT '',
    note TEXT DEFAULT '',
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    max_entries INTEGER DEFAULT 1,    -- 0 = без ограничения
    used_entries INTEGER DEFAULT 0,
    revoked INTEGER DEFAULT 0,
    created_by TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guest_plate ON guest_passes(plate);
"""
)


def _add_column(table: str, col: str, ddl: str) -> None:
    cols = [r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


_add_column("plates", "category_id", "INTEGER")
_add_column("events", "category", "TEXT DEFAULT ''")
_add_column("events", "reason", "TEXT DEFAULT ''")
_add_column("events", "raw_plate", "TEXT DEFAULT ''")   # что прочитала камера до исправления
_add_column("events", "verified", "INTEGER DEFAULT 0")  # номер проверен человеком
_add_column("events", "camera", "TEXT DEFAULT ''")
_add_column("events", "comment", "TEXT DEFAULT ''")
_add_column("events", "suggest", "TEXT DEFAULT ''")     # вероятный номер из белого списка
_add_column("events", "flag", "TEXT DEFAULT ''")        # antipassback / ...
_add_column("events", "username", "TEXT DEFAULT ''")    # кто открыл вручную
_add_column("events", "guest_id", "INTEGER")

if not _conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
    _conn.executemany(
        "INSERT INTO categories(name, color, days, time_from, time_to, exit_any_time) VALUES(?,?,?,?,?,?)",
        [("Сотрудники", "#2563eb", "1111110", "07:00", "20:00", 1),
         ("Техника", "#15803d", "1111111", "00:00", "23:59", 1),
         ("Поставщики", "#c2410c", "1111100", "08:00", "18:00", 1)],
    )
_conn.commit()

TS_FMT = "%Y-%m-%d %H:%M:%S"
DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def now() -> str:
    return datetime.now().strftime(TS_FMT)


def q(sql: str, args=()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, args).fetchall()


def q1(sql: str, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args=()) -> int:
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def backup_to(path: str) -> None:
    """Консистентная копия базы (работает и во время записи)."""
    with _lock:
        dst = sqlite3.connect(path)
        try:
            _conn.backup(dst)
        finally:
            dst.close()


# ---------- утилиты ----------

def normalize(plate: str) -> str:
    # кириллица, похожая на латиницу, -> латиница; убираем пробелы и дефисы
    table = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")
    return "".join(ch for ch in (plate or "").upper().translate(table) if ch.isalnum())


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------- аудит ----------

def audit(username: str, action: str, details: str = "", ip: str = "") -> None:
    ex("INSERT INTO audit(ts, username, action, details, ip) VALUES(?,?,?,?,?)",
       (now(), username or "?", action, details, ip))


def audit_list(date_from="", date_to="", username="", text="", limit=100, offset=0):
    sql, args = " FROM audit WHERE 1=1", []
    if date_from:
        sql += " AND ts >= ?"; args.append(date_from + " 00:00:00")
    if date_to:
        sql += " AND ts <= ?"; args.append(date_to + " 23:59:59")
    if username:
        sql += " AND username = ?"; args.append(username)
    if text:
        sql += " AND (action LIKE ? OR details LIKE ?)"; args += [f"%{text}%"] * 2
    total = q("SELECT COUNT(*) n" + sql, args)[0]["n"]
    rows = q("SELECT *" + sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
    return rows, total


# ---------- пользователи и сессии ----------

def users():
    return q("SELECT * FROM users ORDER BY role, username")


def user_by_name(username: str):
    return q1("SELECT * FROM users WHERE lower(username)=lower(?)", (username.strip(),))


def user_get(uid: int):
    return q1("SELECT * FROM users WHERE id=?", (uid,))


def user_create(username, password_hash, role="guard", full_name="") -> int:
    return ex("INSERT INTO users(username, full_name, password_hash, role, created_at) VALUES(?,?,?,?,?)",
              (username.strip(), full_name.strip(), password_hash, role, now()))


def user_update(uid, full_name=None, role=None, active=None, password_hash=None) -> None:
    for col, val in (("full_name", full_name), ("role", role), ("active", active), ("password_hash", password_hash)):
        if val is not None:
            ex(f"UPDATE users SET {col}=? WHERE id=?", (val, uid))
    if active == 0 or password_hash is not None:
        ex("DELETE FROM sessions WHERE user_id=?", (uid,))  # выкинуть из системы


def user_delete(uid: int) -> None:
    ex("DELETE FROM sessions WHERE user_id=?", (uid,))
    ex("DELETE FROM users WHERE id=?", (uid,))


def admins_count() -> int:
    return q("SELECT COUNT(*) n FROM users WHERE role='admin' AND active=1")[0]["n"]


def session_create(token, user_id, days, ip) -> None:
    exp = (datetime.now() + timedelta(days=days)).strftime(TS_FMT)
    ex("INSERT INTO sessions(token, user_id, created_at, expires_at, ip) VALUES(?,?,?,?,?)",
       (token, user_id, now(), exp, ip))
    ex("UPDATE users SET last_login=? WHERE id=?", (now(), user_id))
    ex("DELETE FROM sessions WHERE expires_at < ?", (now(),))


def session_user(token: str):
    if not token:
        return None
    return q1("""SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                 WHERE s.token=? AND s.expires_at > ? AND u.active=1""", (token, now()))


def session_delete(token: str) -> None:
    ex("DELETE FROM sessions WHERE token=?", (token,))


# ---------- категории ----------

def categories() -> list[sqlite3.Row]:
    return q("""SELECT c.*, (SELECT COUNT(*) FROM plates p WHERE p.category_id = c.id) cars
                FROM categories c ORDER BY c.name""")


def category_save(cid, name, color, days, time_from, time_to, exit_any_time) -> None:
    args = (name.strip(), color, days, time_from or "00:00", time_to or "23:59", int(exit_any_time))
    if cid:
        ex("UPDATE categories SET name=?, color=?, days=?, time_from=?, time_to=?, exit_any_time=? WHERE id=?",
           (*args, cid))
    else:
        ex("INSERT INTO categories(name, color, days, time_from, time_to, exit_any_time) VALUES(?,?,?,?,?,?)", args)


def category_delete(cid: int) -> None:
    ex("UPDATE plates SET category_id=NULL WHERE category_id=?", (cid,))
    ex("DELETE FROM categories WHERE id=?", (cid,))


def category_by_name(name: str):
    return q1("SELECT * FROM categories WHERE lower(name)=lower(?)", ((name or "").strip(),))


def schedule_text(c) -> str:
    days = c["days"]
    if days == "1111111":
        d = "ежедневно"
    else:
        on = [DAYS_RU[i] for i in range(7) if days[i] == "1"]
        d = ", ".join(on) if on else "никогда"
        idx = [i for i in range(7) if days[i] == "1"]
        if len(idx) > 2 and idx == list(range(idx[0], idx[-1] + 1)):
            d = f"{DAYS_RU[idx[0]]}–{DAYS_RU[idx[-1]]}"
    t = "круглосуточно" if (c["time_from"], c["time_to"]) == ("00:00", "23:59") else f"{c['time_from']}–{c['time_to']}"
    return f"{d}, {t}"


def schedule_ok(c, at: datetime) -> bool:
    days = c["days"] or "1111111"
    if days[at.weekday()] != "1":
        return False
    t = at.strftime("%H:%M")
    f, to = c["time_from"] or "00:00", c["time_to"] or "23:59"
    return f <= t <= to if f <= to else (t >= f or t <= to)  # ночная смена: 22:00–06:00


# ---------- белый список ----------

_PLATE_SQL = """SELECT p.*, c.name cat_name, c.color cat_color, c.days, c.time_from, c.time_to, c.exit_any_time
                FROM plates p LEFT JOIN categories c ON c.id = p.category_id"""


def whitelist_all(category_id: str = "") -> list[sqlite3.Row]:
    if category_id == "none":
        return q(_PLATE_SQL + " WHERE p.category_id IS NULL ORDER BY p.plate")
    if category_id:
        return q(_PLATE_SQL + " WHERE p.category_id=? ORDER BY p.plate", (int(category_id),))
    return q(_PLATE_SQL + " ORDER BY p.plate")


def whitelist_get(pid: int):
    return q1(_PLATE_SQL + " WHERE p.id=?", (pid,))


def whitelist_add(plate, owner="", phone="", note="", valid_until="", active=1, category_id=None) -> None:
    ex(
        """INSERT INTO plates(plate, owner, phone, note, valid_until, active, category_id, created_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(plate) DO UPDATE SET owner=excluded.owner, phone=excluded.phone,
             note=excluded.note, valid_until=excluded.valid_until, active=excluded.active,
             category_id=excluded.category_id""",
        (normalize(plate), owner, phone, note, valid_until, active, category_id or None, now()),
    )


def whitelist_update(pid, plate, owner, phone, note, valid_until, category_id) -> None:
    ex("""UPDATE plates SET plate=?, owner=?, phone=?, note=?, valid_until=?, category_id=? WHERE id=?""",
       (normalize(plate), owner, phone, note, valid_until, category_id or None, pid))


def whitelist_delete(pid: int) -> None:
    ex("DELETE FROM plates WHERE id=?", (pid,))


def whitelist_toggle(pid: int) -> None:
    ex("UPDATE plates SET active = 1 - active WHERE id=?", (pid,))


def whitelist_find(plate: str, max_distance: int | None = None):
    """Ближайший номер из списка (с учётом допуска ошибок), независимо от статуса."""
    if not plate:
        return None
    exact = q1(_PLATE_SQL + " WHERE p.plate=?", (plate,))
    if exact:
        return exact
    md = config.WHITELIST_MAX_DISTANCE if max_distance is None else max_distance
    if md <= 0:
        return None
    best, best_d = None, 99
    for r in q(_PLATE_SQL):
        d = levenshtein(plate, r["plate"])
        if d < best_d:
            best, best_d = r, d
    return best if best_d <= md else None


def suggest_plate(readings: list[str], max_distance: int = 3):
    """Самый похожий номер из белого списка и гостевых пропусков для набора неточных чтений."""
    candidates = [(r["plate"], r["owner"]) for r in q("SELECT plate, owner FROM plates WHERE active=1")]
    candidates += [(g["plate"], f"гость: {g['name']}") for g in guests_active()]
    best, best_d = None, 99
    for text in {normalize(t) for t in readings if t}:
        for plate, owner in candidates:
            d = levenshtein(text, plate)
            if d < best_d:
                best, best_d = (plate, owner), d
    return best if best and best_d <= max_distance else None


# ---------- гостевые пропуска ----------

def guest_create(plate, name, note, valid_from, valid_to, max_entries, created_by) -> int:
    return ex("""INSERT INTO guest_passes(plate, name, note, valid_from, valid_to, max_entries, created_by, created_at)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (normalize(plate), name.strip(), note.strip(), valid_from, valid_to, int(max_entries), created_by, now()))


def guest_get(gid: int):
    return q1("SELECT * FROM guest_passes WHERE id=?", (gid,))


def guest_revoke(gid: int) -> None:
    ex("UPDATE guest_passes SET revoked=1 WHERE id=?", (gid,))


def guests_active(at: str | None = None):
    at = at or now()
    return q("""SELECT * FROM guest_passes WHERE revoked=0 AND valid_from<=? AND valid_to>=?
                AND (max_entries=0 OR used_entries<max_entries) ORDER BY valid_to""", (at, at))


def guests_list(show: str = "active", limit=200):
    n = now()
    if show == "active":
        return guests_active(n)
    return q("SELECT * FROM guest_passes ORDER BY id DESC LIMIT ?", (limit,))


def guest_status(g) -> str:
    n = now()
    if g["revoked"]:
        return "отозван"
    if g["valid_to"] < n:
        return "истёк"
    if g["valid_from"] > n:
        return "ещё не начался"
    if g["max_entries"] and g["used_entries"] >= g["max_entries"]:
        return "использован"
    return "активен"


def guest_find(plate: str, at: datetime, any_status: bool = False):
    """Действующий гостевой пропуск на номер (точно или с допуском ошибок, как у белого списка)."""
    ts = at.strftime(TS_FMT)
    rows = q("SELECT * FROM guest_passes WHERE revoked=0 AND valid_from<=? AND valid_to>=? ORDER BY valid_to DESC",
             (ts, ts))
    md = config.WHITELIST_MAX_DISTANCE
    for g in rows:
        if levenshtein(plate, g["plate"]) <= md:
            if any_status or not g["max_entries"] or g["used_entries"] < g["max_entries"]:
                return g
    return None


def guest_use(gid: int) -> None:
    ex("UPDATE guest_passes SET used_entries = used_entries + 1 WHERE id=?", (gid,))


# ---------- решение о допуске ----------

def decide(plate: str, direction: str, at: datetime | None = None) -> dict:
    """Решение о допуске. Порядок: белый список -> гостевой пропуск -> отказ. Плюс анти-passback."""
    at = at or datetime.now()
    d = {"match": None, "guest": None, "allowed": False, "reason": "", "category": "", "owner": "",
         "matched_plate": "", "flag": ""}
    m = whitelist_find(plate)
    if m is not None:
        d.update(match=m, matched_plate=m["plate"], owner=m["owner"], category=m["cat_name"] or "")
        if not m["active"]:
            d["reason"] = "пропуск отключён"
        elif m["valid_until"] and m["valid_until"] < at.strftime("%Y-%m-%d"):
            d["reason"] = f"пропуск истёк {m['valid_until']}"
        elif m["category_id"] and m["cat_name"] and not (direction == "exit" and m["exit_any_time"]) \
                and not schedule_ok(m, at):
            d["reason"] = f"вне расписания ({m['cat_name']}: {schedule_text(m)})"
        else:
            d["allowed"] = True
    if not d["allowed"]:
        g = guest_find(plate, at, any_status=(direction == "exit"))
        if g is not None:
            d.update(guest=g, allowed=True, reason=f"гостевой пропуск: {g['name'] or g['plate']}",
                     category="Гость", owner=g["name"], matched_plate=g["plate"])
        elif m is None:
            d["reason"] = "нет в белом списке"
    # анти-passback: повторный въезд без выезда
    if direction == "entry" and config.ANTIPASSBACK != "off":
        last = last_event_for(plate)
        if last and last["direction"] == "entry" and last["allowed"]:
            age = at - datetime.strptime(last["ts"], TS_FMT)
            if timedelta(0) <= age < timedelta(hours=config.AUTO_RESET_HOURS):
                d["flag"] = "antipassback"
                note = f"повторный въезд без выезда (въехал {last['ts'][11:16]})"
                if config.ANTIPASSBACK == "deny" and d["allowed"]:
                    d["allowed"] = False
                    d["reason"] = note
                else:
                    d["reason"] = f"{d['reason']}; {note}" if d["reason"] else note
    return d


# ---------- события ----------

def add_event(plate, confidence, direction, decision: dict | None, gate_opened, snapshot="", source="camera",
              camera="", ts=None, raw_plate=None, suggest="", username="", comment="", reason=None) -> int:
    d = decision or {}
    g = d.get("guest")
    return ex(
        """INSERT INTO events(ts, plate, confidence, direction, allowed, gate_opened, matched_plate, owner,
                              category, reason, snapshot, source, camera, raw_plate, suggest, flag, username,
                              comment, guest_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts or now(), plate, confidence, direction, int(bool(d.get("allowed"))), int(bool(gate_opened)),
         d.get("matched_plate", ""), d.get("owner", ""), d.get("category", ""),
         d.get("reason", "") if reason is None else reason, snapshot, source, camera,
         plate if raw_plate is None else raw_plate, suggest, d.get("flag", ""), username, comment,
         g["id"] if g else None),
    )


def event_get(eid: int):
    return q1("SELECT * FROM events WHERE id=?", (eid,))


def event_delete(eid: int):
    e = event_get(eid)
    ex("DELETE FROM events WHERE id=?", (eid,))
    return e


def event_comment(eid: int, comment: str) -> None:
    ex("UPDATE events SET comment=? WHERE id=?", (comment.strip()[:500], eid))


def event_set_direction(eid: int, direction: str) -> None:
    if direction in ("entry", "exit"):
        ex("UPDATE events SET direction=? WHERE id=?", (direction, eid))


def correct_event(eid: int, new_plate: str) -> None:
    """Охранник исправил/подтвердил номер. Решение о проезде (allowed, gate_opened) не меняется,
    т.к. это то, что произошло на самом деле; обновляется только кто это был."""
    e = event_get(eid)
    new_plate = normalize(new_plate)
    if not e or not new_plate:
        return
    at = datetime.strptime(e["ts"], TS_FMT)
    m = whitelist_find(new_plate)
    g = None if m else guest_find(new_plate, at, any_status=True)
    owner = m["owner"] if m else (g["name"] if g else "")
    category = (m["cat_name"] or "") if m else ("Гость" if g else "")
    matched = m["plate"] if m else (g["plate"] if g else "")
    reason = e["reason"]
    if new_plate != e["plate"]:
        note = f"номер исправлен вручную (камера: {e['raw_plate'] or e['plate'] or '—'})"
        if "исправлен" not in (reason or ""):
            reason = f"{reason}; {note}" if reason else note
    ex("""UPDATE events SET plate=?, matched_plate=?, owner=?, category=?, reason=?, verified=1,
                            raw_plate=CASE WHEN raw_plate='' THEN ? ELSE raw_plate END
          WHERE id=?""",
       (new_plate, matched, owner, category, reason, e["plate"], eid))


def dataset_rows():
    return q("""SELECT id, plate, raw_plate, snapshot FROM events
                WHERE verified=1 AND snapshot != '' AND plate != '' AND source='camera' ORDER BY id""")


def dataset_stats():
    r = q("""SELECT SUM(verified=1 AND snapshot!='' AND source='camera') verified,
                    SUM(verified=1 AND snapshot!='' AND source='camera' AND raw_plate!='' AND raw_plate!=plate) corrected,
                    SUM(snapshot!='' AND source='camera') total FROM events""")[0]
    return {k: r[k] or 0 for k in ("verified", "corrected", "total")}


def last_event_for(plate: str):
    if not plate:
        return None
    return q1("SELECT * FROM events WHERE plate=? ORDER BY id DESC LIMIT 1", (plate,))


def guess_direction(plate: str) -> str:
    last = last_event_for(plate)
    if last and last["direction"] == "entry":
        age = datetime.now() - datetime.strptime(last["ts"], TS_FMT)
        if age < timedelta(hours=config.AUTO_RESET_HOURS):
            return "exit"
    return "entry"


def _events_where(date_from="", date_to="", plate="", allowed="", direction="", category=""):
    sql, args = " FROM events WHERE 1=1", []
    if date_from:
        sql += " AND ts >= ?"; args.append(date_from + " 00:00:00")
    if date_to:
        sql += " AND ts <= ?"; args.append(date_to + " 23:59:59")
    if plate:
        sql += " AND (plate LIKE ? OR raw_plate LIKE ? OR suggest LIKE ?)"; args += [f"%{normalize(plate)}%"] * 3
    if allowed in ("0", "1"):
        sql += " AND allowed = ?"; args.append(int(allowed))
    elif allowed == "unknown":
        sql += " AND matched_plate = ''"
    elif allowed == "unverified":
        sql += " AND verified = 0 AND snapshot != ''"
    elif allowed == "noplate":
        sql += " AND source = 'vehicle' AND verified = 0"
    elif allowed == "flag":
        sql += " AND flag != ''"
    elif allowed == "comment":
        sql += " AND comment != ''"
    if direction in ("entry", "exit"):
        sql += " AND direction = ?"; args.append(direction)
    if category == "none":
        sql += " AND category = ''"
    elif category:
        sql += " AND category = ?"; args.append(category)
    return sql, args


def events(date_from="", date_to="", plate="", allowed="", direction="", category="", limit=1000, offset=0):
    sql, args = _events_where(date_from, date_to, plate, allowed, direction, category)
    return q("SELECT *" + sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))


def events_count(date_from="", date_to="", plate="", allowed="", direction="", category="") -> int:
    sql, args = _events_where(date_from, date_to, plate, allowed, direction, category)
    return q("SELECT COUNT(*) n" + sql, args)[0]["n"]


def inside_now():
    """Машины, у которых последнее событие - въезд (за последние AUTO_RESET_HOURS)."""
    since = (datetime.now() - timedelta(hours=config.AUTO_RESET_HOURS)).strftime(TS_FMT)
    return q(
        """SELECT e.* FROM events e
           JOIN (SELECT plate, MAX(id) mid FROM events WHERE plate != '' GROUP BY plate) t
             ON e.id = t.mid
           WHERE e.direction='entry' AND e.ts >= ? ORDER BY e.ts DESC""",
        (since,),
    )


def visits(date_from: str, date_to: str):
    """Пары въезд -> выезд с длительностью пребывания."""
    rows = q(
        "SELECT * FROM events WHERE plate != '' AND ts BETWEEN ? AND ? ORDER BY plate, ts",
        (date_from + " 00:00:00", date_to + " 23:59:59"),
    )
    result, open_entry = [], {}
    for r in rows:
        if r["direction"] == "entry":
            if r["plate"] in open_entry:
                result.append(_visit(open_entry.pop(r["plate"]), None))
            open_entry[r["plate"]] = r
        else:
            result.append(_visit(open_entry.pop(r["plate"], None), r))
    for e in open_entry.values():
        result.append(_visit(e, None))
    result.sort(key=lambda v: v["start"] or v["end"], reverse=True)
    return result


def _visit(entry, exit_):
    start = entry["ts"] if entry else ""
    end = exit_["ts"] if exit_ else ""
    dur = ""
    if start and end:
        sec = int((datetime.strptime(end, TS_FMT) - datetime.strptime(start, TS_FMT)).total_seconds())
        dur = f"{sec // 3600}:{sec % 3600 // 60:02d}"
    src = entry or exit_
    return {"plate": src["plate"], "owner": src["owner"], "category": src["category"],
            "allowed": src["allowed"], "start": start, "end": end, "duration": dur}


def _range(date_from, date_to):
    return date_from + " 00:00:00", date_to + " 23:59:59"


def report_by_day(date_from: str, date_to: str):
    return q(
        """SELECT substr(ts,1,10) day,
                  SUM(direction='entry') entries,
                  SUM(direction='exit') exits,
                  COUNT(DISTINCT NULLIF(plate,'')) unique_cars,
                  SUM(matched_plate='') unknown,
                  SUM(allowed=0) denied,
                  SUM(gate_opened) opened,
                  SUM(flag!='') flagged
           FROM events WHERE ts BETWEEN ? AND ?
           GROUP BY day ORDER BY day DESC""",
        _range(date_from, date_to),
    )


def report_by_category(date_from: str, date_to: str):
    return q(
        """SELECT CASE WHEN category='' THEN '— без категории / неизвестные' ELSE category END cat,
                  SUM(direction='entry') entries, SUM(direction='exit') exits,
                  COUNT(DISTINCT NULLIF(plate,'')) unique_cars, SUM(allowed=0) denied
           FROM events WHERE ts BETWEEN ? AND ? GROUP BY cat ORDER BY entries DESC""",
        _range(date_from, date_to),
    )


def report_denied(date_from: str, date_to: str, limit=200):
    return q("""SELECT * FROM events WHERE allowed=0 AND ts BETWEEN ? AND ? ORDER BY id DESC LIMIT ?""",
             (*_range(date_from, date_to), limit))


def report_flagged(date_from: str, date_to: str, limit=200):
    return q("""SELECT * FROM events WHERE flag!='' AND ts BETWEEN ? AND ? ORDER BY id DESC LIMIT ?""",
             (*_range(date_from, date_to), limit))


def report_by_hour(date_from: str, date_to: str):
    return q(
        """SELECT CAST(substr(ts,12,2) AS INTEGER) hour, COUNT(*) cnt
           FROM events WHERE ts BETWEEN ? AND ? GROUP BY hour ORDER BY hour""",
        _range(date_from, date_to),
    )


def report_top(date_from: str, date_to: str, limit=20):
    return q(
        """SELECT plate, MAX(owner) owner, MAX(category) category, MAX(matched_plate != '') known,
                  COUNT(*) cnt, MAX(ts) last_ts
           FROM events WHERE ts BETWEEN ? AND ? AND plate != ''
           GROUP BY plate ORDER BY cnt DESC LIMIT ?""",
        (*_range(date_from, date_to), limit),
    )
