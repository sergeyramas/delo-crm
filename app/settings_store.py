"""Чтение и запись настроек. Опубликованное, черновики и история — в SQLite."""

from __future__ import annotations

import difflib
from typing import Any

from app.domain import now_iso
from app.settings_catalog import BY_KEY, SettingKey, default_text, display_text
from app.settings_validate import ValidationError, validate_value


def _spec(key: str) -> SettingKey:
    item = BY_KEY.get(key)
    if item is None:
        raise ValidationError(f"Неизвестный ключ настройки: {key}.")
    return item


def version(db) -> int:
    row = db.one("SELECT version FROM settings_version WHERE id = 1")
    return int(row["version"]) if row else 0


def published(db, key: str | None = None) -> dict[str, dict] | dict | None:
    if key is None:
        rows = db.q("SELECT * FROM settings")
        return {row["key"]: row for row in rows}
    row = db.one("SELECT * FROM settings WHERE key = ?", (key,))
    return row


def draft(db, key: str | None = None) -> dict[str, dict] | dict | None:
    if key is None:
        rows = db.q("SELECT * FROM settings_drafts")
        return {row["key"]: row for row in rows}
    return db.one("SELECT * FROM settings_drafts WHERE key = ?", (key,))


def history(db, key: str) -> list[dict]:
    rows = db.q(
        "SELECT * FROM settings_history WHERE key = ? ORDER BY id DESC",
        (key,),
    )
    for row in rows:
        row["diff"] = _diff(
            display_text(BY_KEY[key], row["old_value"]) if row["old_value"] else "",
            display_text(BY_KEY[key], row["new_value"]) if row["new_value"] else "",
        )
    return rows


def _diff(old: str, new: str) -> str:
    if old == new:
        return ""
    lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile="было",
        tofile="стало",
        lineterm="",
    )
    return "\n".join(lines)


def state(db, key: str) -> dict[str, Any]:
    item = _spec(key)
    pub = published(db, key)
    drf = draft(db, key)
    effective = pub["value"] if pub else default_text(item)
    return {
        "key": item.key,
        "title": item.title,
        "type": item.type,
        "section": item.section,
        "placeholders": item.placeholders,
        "max_len": item.max_len,
        "published": pub,
        "draft": drf,
        "current": effective,
        "current_display": display_text(item, effective),
        "draft_display": display_text(item, drf["value"]) if drf else "",
        "default_display": display_text(item, default_text(item)),
        "changed": bool(pub) and pub["value"] != default_text(item),
        "history": history(db, key),
    }


def save_draft(db, key: str, raw: str | None, who: str) -> str:
    item = _spec(key)
    value = validate_value(item, raw)
    moment = now_iso()
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO settings_drafts (key, value, updated_by, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_by = excluded.updated_by,
                   updated_at = excluded.updated_at""",
            (key, value, who, moment),
        )
    return value


def _next_version(conn) -> int:
    conn.execute("INSERT OR IGNORE INTO settings_version (id, version) VALUES (1, 0)")
    conn.execute("UPDATE settings_version SET version = version + 1 WHERE id = 1")
    row = conn.execute("SELECT version FROM settings_version WHERE id = 1").fetchone()
    return int(row["version"])


def publish(db, key: str, raw: str | None, who: str, comment: str = "") -> dict:
    item = _spec(key)
    with db.tx() as conn:
        source = raw
        if source is None:
            drf = conn.execute(
                "SELECT value FROM settings_drafts WHERE key = ?", (key,)
            ).fetchone()
            if drf:
                source = drf["value"]
            else:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
                source = row["value"] if row else default_text(item)
        value = validate_value(item, source)
        old_row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        old_value = old_row["value"] if old_row else ""
        number = _next_version(conn)
        moment = now_iso()
        conn.execute(
            """INSERT INTO settings (key, type, value, version, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   type = excluded.type,
                   value = excluded.value,
                   version = excluded.version,
                   updated_by = excluded.updated_by,
                   updated_at = excluded.updated_at""",
            (key, item.type, value, number, who, moment),
        )
        conn.execute("DELETE FROM settings_drafts WHERE key = ?", (key,))
        conn.execute(
            """INSERT INTO settings_history
               (key, version, old_value, new_value, who, at, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, number, old_value, value, who, moment, comment.strip()),
        )
    return {"key": key, "version": number, "value": value}


def reset(db, key: str, who: str, comment: str = "") -> dict:
    item = _spec(key)
    with db.tx() as conn:
        old_row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        old_value = old_row["value"] if old_row else ""
        number = _next_version(conn)
        moment = now_iso()
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.execute("DELETE FROM settings_drafts WHERE key = ?", (key,))
        conn.execute(
            """INSERT INTO settings_history
               (key, version, old_value, new_value, who, at, comment)
               VALUES (?, ?, ?, '', ?, ?, ?)""",
            (key, number, old_value, who, moment, comment.strip() or "Сброс к умолчанию"),
        )
    return {"key": key, "version": number, "default": default_text(item)}


def rollback(db, key: str, history_id: int, who: str, comment: str = "") -> dict:
    _spec(key)
    row = db.one("SELECT * FROM settings_history WHERE id = ?", (history_id,))
    if row is None or row["key"] != key:
        raise ValidationError("Версия не найдена.")
    note = comment.strip() or f"Откат к версии {row['version']}"
    if not row["new_value"]:
        return reset(db, key, who, note)
    return publish(db, key, row["new_value"], who, note)


def snapshot(db) -> dict[str, Any]:
    rows = db.q("SELECT key, value FROM settings ORDER BY key")
    return {
        "version": version(db),
        "generated_at": now_iso(),
        "values": {row["key"]: row["value"] for row in rows},
    }


def sections() -> list[dict]:
    from app.settings_catalog import SECTIONS

    out = []
    for code, title in SECTIONS:
        out.append({
            "code": code,
            "title": title,
            "cards": [item.key for item in BY_KEY.values() if item.section == code],
        })
    return out
