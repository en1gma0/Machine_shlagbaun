"""Вход в систему: пароли, сессии, роли, защита от подбора."""
import hashlib
import hmac
import secrets
import time

from . import db

COOKIE = "gate_session"
ROLES = {"admin": "Администратор", "guard": "Охранник"}

# Что разрешено охраннику. Всё остальное (белый список, категории, настройки, пользователи, аудит,
# резервные копии) - только администратору. Удаление записей журнала проверяется отдельно.
GUARD_EXACT = {"/", "/gate/open", "/account", "/logout", "/dataset/export"}
GUARD_SECTIONS = ("/api", "/video", "/snap", "/camera", "/journal", "/reports", "/guests")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2$200000${salt}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, AttributeError):
        return False


def password_problem(password: str) -> str:
    if len(password) < 6:
        return "Пароль должен быть не короче 6 символов"
    return ""


# ---------- защита от подбора: 5 ошибок с одного адреса -> пауза 2 минуты ----------
_fails: dict[str, list[float]] = {}


def login_blocked(ip: str) -> int:
    now = time.time()
    tries = [t for t in _fails.get(ip, []) if now - t < 120]
    _fails[ip] = tries
    return int(120 - (now - tries[0])) if len(tries) >= 5 else 0


def login_failed(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def login_ok(ip: str) -> None:
    _fails.pop(ip, None)


def can_access(user, path: str) -> bool:
    if user["role"] == "admin":
        return True
    return path in GUARD_EXACT or any(path == s or path.startswith(s + "/") for s in GUARD_SECTIONS)


def is_admin(user) -> bool:
    return bool(user) and user["role"] == "admin"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def needs_setup() -> bool:
    return not db.users()
