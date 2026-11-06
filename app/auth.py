"""Учётки экрана. Пароль в базу только как хэш, в лог не пишется.

Боевые значения — в /root/ati/secrets/delo-crm-auth.env (mode 600).
В репозитории их нет. Пустой пароль оставляет строку пользователя, но войти нельзя.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any

from app.db import Database
from app.domain import now_iso

ROLES = {
    "dispatcher": "Диспетчер",
    "operator": "Оператор",
}

# Логин фиксирован. Пароль только из окружения, сюда не подставляется.
ACCOUNTS = (
    {
        "login": "sergey",
        "display_name": "Сергей",
        "password_env": "DELO_USER_SERGEY_PASSWORD",
        "role_env": "DELO_USER_SERGEY_ROLE",
        "default_role": "dispatcher",
    },
    {
        "login": "andrey",
        "display_name": "Андрей (логист)",
        "password_env": "DELO_USER_ANDREY_PASSWORD",
        "role_env": "DELO_USER_ANDREY_ROLE",
        "default_role": "operator",
    },
)

PBKDF2_ROUNDS = 200_000
# Basic auth приходит с каждым запросом, в том числе за CSS.
# Держим несколько уже проверенных пар, чтобы не считать pbkdf2 заново.
# Пароль целиком не храним. Больше восьми записей — словарь сбрасывается.
_HITS: dict[tuple[str, str], bytes] = {}
_HITS_MAX = 8


def role_label(code: str) -> str:
    return ROLES.get(code, code or "оператор")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, rounds, salt_hex, digest_hex = stored.split("$", 3)
        if kind != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        )
        got = digest.hex()
    except (ValueError, TypeError):
        return False
    if len(got) != len(digest_hex):
        return False
    return hmac.compare_digest(got, digest_hex)


def _role(raw: str, default: str) -> str:
    value = (raw or "").strip().lower()
    if value in ROLES:
        return value
    return default


def sync_users(db: Database) -> None:
    """Сергей и Андрей есть в таблице всегда. Хэш пишется, только если пароль задан в env."""
    moment = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO roles (code, label) VALUES ('dispatcher', 'Диспетчер'), ('operator', 'Оператор')"
        )
        for account in ACCOUNTS:
            role = _role(os.environ.get(account["role_env"], ""), account["default_role"])
            password = os.environ.get(account["password_env"], "")
            existing = conn.execute(
                "SELECT id, password_hash FROM users WHERE login = ?",
                (account["login"],),
            ).fetchone()
            if password:
                digest = hash_password(password)
            elif existing:
                digest = existing["password_hash"]
            else:
                digest = ""
            if existing:
                conn.execute(
                    "UPDATE users SET display_name = ?, role_code = ?, password_hash = ?, active = 1 WHERE id = ?",
                    (account["display_name"], role, digest, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO users (login, display_name, role_code, password_hash, active, created_at)
                       VALUES (?, ?, ?, ?, 1, ?)""",
                    (account["login"], account["display_name"], role, digest, moment),
                )


def _eq(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _basic(header: str) -> tuple[str, str] | None:
    if not header.startswith("Basic "):
        return None
    import base64

    try:
        raw = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
    except Exception:
        return None
    login, sep, password = raw.partition(":")
    if not sep:
        return None
    return login, password


def _password_ok(login: str, password: str, stored: str) -> bool:
    key = (login, stored)
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    if _HITS.get(key) == digest:
        return True
    if not verify_password(password, stored):
        return False
    if len(_HITS) >= _HITS_MAX:
        _HITS.clear()
    _HITS[key] = digest
    return True


def authenticate(db: Database, settings, header: str) -> dict[str, Any] | None:
    parsed = _basic(header)
    if not parsed:
        return None
    login, password = parsed
    row = db.one(
        "SELECT * FROM users WHERE login = ? AND active = 1",
        (login,),
    )
    if row and row["password_hash"]:
        if _password_ok(login, password, row["password_hash"]):
            return dict(row)
        return None
    if settings.ui_user and settings.ui_password and _eq(login, settings.ui_user) and _eq(password, settings.ui_password):
        if row:
            return dict(row)
        return {
            "login": settings.ui_user,
            "display_name": settings.display_name,
            "role_code": "operator",
            "password_hash": "",
        }
    return None
