"""SQLite: категории, белый список, журнал событий."""
import sqlite3
import threading
from datetime import datetime, timedelta

from . import config

_lock = threading.RLock()
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")

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
    source TEXT DEFAULT 'camera'      -- camera / manual
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_plate ON events(plate);
"""
)


def _add_column(table: str, col: str, ddl: str) -> None:
    cols = [r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


_add_column("plates", "category_id", "INTEGER")
_add_column("events", "category", "TEXT DEFAULT ''")
_add_column("events", "reason", "TEXT DEFAULT ''")
_add_column("events", "raw_plate", "TEXT DEFAULT ''")  # что прочитала камера до исправления
_add_column("events", "verified", "INTEGER DEFAULT 0")  # номер проверен человеком
_add_column("events", "camera", "TEXT DEFAULT ''")

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


def ex(sql: str, args=()) -> int:
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


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
    rows = q("SELECT * FROM categories WHERE lower(name)=lower(?)", ((name or "").strip(),))
    return rows[0] if rows else None


def schedule_text(c) -> str:
    days = c["days"]
    if days == "1111111":
        d = "ежедневно"
    else:
        on = [DAYS_RU[i] for i in range(7) if days[i] == "1"]
        d = ", ".join(on) if on else "никогда"
        # компактно: Пн–Пт
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
    rows = q(_PLATE_SQL + " WHERE p.id=?", (pid,))
    return rows[0] if rows else None


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


def whitelist_find(plate: str):
    """Ближайший номер из списка (с учётом WHITELIST_MAX_DISTANCE), независимо от статуса."""
    if not plate:
        return None
    exact = q(_PLATE_SQL + " WHERE p.plate=?", (plate,))
    if exact:
        return exact[0]
    if config.WHITELIST_MAX_DISTANCE <= 0:
        return None
    best, best_d = None, 99
    for r in q(_PLATE_SQL):
        d = levenshtein(plate, r["plate"])
        if d < best_d:
            best, best_d = r, d
    return best if best_d <= config.WHITELIST_MAX_DISTANCE else None


def decide(plate: str, direction: str, at: datetime | None = None):
    """Решение о допуске: (строка белого списка или None, разрешено, причина отказа)."""
    at = at or datetime.now()
    m = whitelist_find(plate)
    if m is None:
        return None, False, "нет в белом списке"
    if not m["active"]:
        return m, False, "пропуск отключён"
    if m["valid_until"] and m["valid_until"] < at.strftime("%Y-%m-%d"):
        return m, False, f"пропуск истёк {m['valid_until']}"
    if m["category_id"] and m["cat_name"]:
        if direction == "exit" and m["exit_any_time"]:
            return m, True, ""
        if not schedule_ok(m, at):
            return m, False, f"вне расписания ({m['cat_name']}: {schedule_text(m)})"
    return m, True, ""


# ---------- события ----------

def add_event(plate, confidence, direction, allowed, gate_opened, match=None, reason="",
              snapshot="", source="camera", camera="", ts=None) -> int:
    return ex(
        """INSERT INTO events(ts, plate, confidence, direction, allowed, gate_opened, matched_plate,
                              owner, category, reason, snapshot, source, camera, raw_plate)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts or now(), plate, confidence, direction, int(allowed), int(gate_opened),
         match["plate"] if match else "", match["owner"] if match else "",
         (match["cat_name"] or "") if match else "", reason, snapshot, source, camera, plate),
    )


def event_get(eid: int):
    rows = q("SELECT * FROM events WHERE id=?", (eid,))
    return rows[0] if rows else None


def correct_event(eid: int, new_plate: str) -> None:
    """Охранник исправил/подтвердил номер. Решение о проезде (allowed, gate_opened) не меняется,
    т.к. это то, что произошло на самом деле; обновляется только кто это был."""
    e = event_get(eid)
    if not e:
        return
    new_plate = normalize(new_plate)
    if not new_plate:
        return
    m = whitelist_find(new_plate)
    reason = e["reason"]
    if new_plate != e["plate"]:
        note = f"номер исправлен вручную (камера: {e['raw_plate'] or e['plate']})"
        reason = f"{reason}; {note}" if reason and "исправлен" not in reason else (reason if "исправлен" in reason else note)
    ex("""UPDATE events SET plate=?, matched_plate=?, owner=?, category=?, reason=?, verified=1,
                            raw_plate=CASE WHEN raw_plate='' THEN ? ELSE raw_plate END
          WHERE id=?""",
       (new_plate, m["plate"] if m else "", m["owner"] if m else "", (m["cat_name"] or "") if m else "",
        reason, e["plate"], eid))


def dataset_rows():
    return q("""SELECT id, plate, raw_plate, snapshot FROM events
                WHERE verified=1 AND snapshot != '' AND plate != '' ORDER BY id""")


def dataset_stats():
    r = q("""SELECT SUM(verified=1 AND snapshot!='') verified,
                    SUM(verified=1 AND snapshot!='' AND raw_plate!='' AND raw_plate!=plate) corrected,
                    SUM(snapshot!='') total FROM events""")[0]
    return {k: r[k] or 0 for k in ("verified", "corrected", "total")}


def last_event_for(plate: str):
    rows = q("SELECT * FROM events WHERE plate=? ORDER BY id DESC LIMIT 1", (plate,))
    return rows[0] if rows else None


def guess_direction(plate: str) -> str:
    last = last_event_for(plate)
    if last and last["direction"] == "entry":
        age = datetime.now() - datetime.strptime(last["ts"], TS_FMT)
        if age < timedelta(hours=config.AUTO_RESET_HOURS):
            return "exit"
    return "entry"


def events(date_from="", date_to="", plate="", allowed="", direction="", category="", limit=1000):
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if date_from:
        sql += " AND ts >= ?"; args.append(date_from + " 00:00:00")
    if date_to:
        sql += " AND ts <= ?"; args.append(date_to + " 23:59:59")
    if plate:
        sql += " AND (plate LIKE ? OR raw_plate LIKE ?)"; args += [f"%{normalize(plate)}%"] * 2
    if allowed in ("0", "1"):
        sql += " AND allowed = ?"; args.append(int(allowed))
    if allowed == "unknown":
        sql += " AND matched_plate = ''"
    if allowed == "unverified":
        sql += " AND verified = 0 AND snapshot != ''"
    if direction in ("entry", "exit"):
        sql += " AND direction = ?"; args.append(direction)
    if category == "none":
        sql += " AND category = ''"
    elif category:
        sql += " AND category = ?"; args.append(category)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    return q(sql, args)


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
                  COUNT(DISTINCT plate) unique_cars,
                  SUM(matched_plate='') unknown,
                  SUM(allowed=0) denied,
                  SUM(gate_opened) opened
           FROM events WHERE ts BETWEEN ? AND ?
           GROUP BY day ORDER BY day DESC""",
        _range(date_from, date_to),
    )


def report_by_category(date_from: str, date_to: str):
    return q(
        """SELECT CASE WHEN category='' THEN '— без категории / неизвестные' ELSE category END cat,
                  SUM(direction='entry') entries, SUM(direction='exit') exits,
                  COUNT(DISTINCT plate) unique_cars, SUM(allowed=0) denied
           FROM events WHERE ts BETWEEN ? AND ? GROUP BY cat ORDER BY entries DESC""",
        _range(date_from, date_to),
    )


def report_denied(date_from: str, date_to: str, limit=200):
    return q("""SELECT * FROM events WHERE allowed=0 AND ts BETWEEN ? AND ? ORDER BY id DESC LIMIT ?""",
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
