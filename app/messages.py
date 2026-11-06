"""Переписка: хуки и лента.

v1(A) только читает и записывает то, что уже пришло. send() сокет не открывает
и статус в базе на «отправлено» сам не ставит. Токены каналов сюда не входят.
"""

from __future__ import annotations

import json
import re
import sqlite3

from app.db import Database
from app.domain import (
    LIST_PAGE_MAX,
    MESSAGE_CAP,
    MESSAGE_CHANNELS,
    MESSAGE_DIRECTIONS,
    MESSAGE_STATUSES,
    fmt_dt,
    loads,
    now_iso,
)
from app.logic import ApiError, clean, fetch_package, opt_int

CHANNELS = {item["code"] for item in MESSAGE_CHANNELS}
DIRECTIONS = {item["code"] for item in MESSAGE_DIRECTIONS}
STATUSES = set(MESSAGE_STATUSES)
CHANNEL_BY = {item["code"]: item for item in MESSAGE_CHANNELS}
DIRECTION_BY = {item["code"]: item for item in MESSAGE_DIRECTIONS}

_MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$")

_LIST_SQL = """
    SELECT m.id, m.package_id, m.party_id, m.channel, m.direction, m.status,
           m.subject, m.body_text, m.external_id, m.thread_key,
           m.sent_at, m.delivered_at, m.received_at, m.author_user,
           m.meta_json, m.created_at,
           COALESCE(p.number, '') AS package_number,
           COALESCE(p.route_from, '') AS route_from,
           COALESCE(p.route_to, '') AS route_to,
           COALESCE(y.name, '') AS party_name
    FROM messages m
    LEFT JOIN packages p ON p.id = m.package_id
    LEFT JOIN parties y ON y.id = m.party_id
"""

_ONE_SQL = """
    SELECT m.id, m.package_id, m.party_id, m.channel, m.direction, m.status,
           m.subject, m.body_text, m.body_html, m.external_id, m.thread_key,
           m.sent_at, m.delivered_at, m.received_at, m.author_user,
           m.meta_json, m.created_at,
           COALESCE(p.number, '') AS package_number,
           COALESCE(p.route_from, '') AS route_from,
           COALESCE(p.route_to, '') AS route_to,
           COALESCE(y.name, '') AS party_name
    FROM messages m
    LEFT JOIN packages p ON p.id = m.package_id
    LEFT JOIN parties y ON y.id = m.party_id
"""


class ChannelAdapter:
    """Нормализация входящего и заглушка отправки. Сеть не вызывается."""

    channel = ""

    def text_of(self, payload: dict) -> str:
        return clean(payload.get("text") or payload.get("body_text") or "", 8000)

    def html_of(self, payload: dict) -> str:
        return clean(payload.get("html") or payload.get("body_html") or "", 16000)

    def normalize(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ApiError(422, "Нужен объект JSON.")
        text = self.text_of(payload)
        html = self.html_of(payload)
        subject = clean(payload.get("subject") or "", 300)
        if self.channel == "email":
            if not text and not html and not subject:
                raise ApiError(422, "Пустое сообщение.")
        elif not text:
            raise ApiError(422, "Пустое сообщение.")
        direction = clean(payload.get("direction") or "in", 8)
        if direction not in DIRECTIONS:
            raise ApiError(422, "Неизвестное направление.")
        status = clean(payload.get("status") or "", 16)
        if not status:
            status = "received" if direction == "in" else "queued"
        if status not in STATUSES:
            raise ApiError(422, "Неизвестный статус сообщения.")
        party_raw = payload.get("party_id")
        party_id = None
        if party_raw not in (None, "", 0, "0"):
            party_id = opt_int(party_raw, "party_id") or None
        package_key = clean(payload.get("package") or payload.get("number") or "", 40)
        if not package_key and payload.get("package_id") not in (None, ""):
            package_key = clean(payload.get("package_id"), 40)
        return {
            "channel": self.channel,
            "direction": direction,
            "status": status,
            "subject": subject,
            "body_text": text,
            "body_html": html,
            "external_id": clean(payload.get("external_id") or "", 200),
            "thread_key": clean(payload.get("thread_key") or payload.get("thread") or "", 200),
            "sent_at": _moment(payload.get("sent_at"), "sent_at"),
            "delivered_at": _moment(payload.get("delivered_at"), "delivered_at"),
            "received_at": _moment(payload.get("received_at"), "received_at"),
            "author_user": clean(
                payload.get("author_user") or payload.get("author") or payload.get("from") or "",
                200,
            ),
            "meta": _meta(payload.get("meta")),
            "party_id": party_id,
            "package_key": package_key,
        }

    def send(self, message: dict) -> tuple[str, str]:
        """Живой отправки нет. message в лог не пишется, сокет не открывается."""
        del message
        return "queued", "Адаптер-заглушка: наружу не отправлено, сеть не вызывалась."


class EmailAdapter(ChannelAdapter):
    channel = "email"

    def text_of(self, payload: dict) -> str:
        return clean(payload.get("text") or payload.get("body_text") or payload.get("body") or "", 8000)


class TelegramAdapter(ChannelAdapter):
    channel = "tg"

    def text_of(self, payload: dict) -> str:
        return clean(payload.get("text") or payload.get("message") or payload.get("body_text") or "", 8000)


class MaxAdapter(TelegramAdapter):
    channel = "max"


_ADAPTERS = {
    "email": EmailAdapter,
    "tg": TelegramAdapter,
    "max": MaxAdapter,
}


def adapter_for(channel: str) -> ChannelAdapter:
    found = _ADAPTERS.get(channel)
    if found is None:
        raise ApiError(404, "Неизвестный канал.")
    return found()


def _moment(value, name: str) -> str:
    text = clean(value, 40)
    if not text:
        return ""
    if not _MOMENT.fullmatch(text):
        raise ApiError(422, f"Поле {name} — дата ISO.")
    return text.replace(" ", "T")


def _meta(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ApiError(422, "Поле meta — объект.")
    out: dict = {}
    for key, value in list(raw.items())[:20]:
        name = clean(key, 40)
        if not name or name == "secret":
            continue
        if isinstance(value, bool):
            out[name] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            out[name] = value
        elif isinstance(value, str):
            out[name] = clean(value, 200)
        else:
            continue
    blob = json.dumps(out, ensure_ascii=False)
    if len(blob) > 2000:
        raise ApiError(422, "Поле meta слишком большое.")
    return out


def _at(row: dict) -> str:
    return row.get("sent_at") or row.get("received_at") or row.get("created_at") or ""


def public_message(row: dict, *, with_html: bool = False) -> dict:
    meta = loads(row.get("meta_json") or "", {})
    if not isinstance(meta, dict):
        meta = {}
    item = {
        "id": row["id"],
        "package_id": row["package_id"],
        "package_number": row.get("package_number") or "",
        "party_id": row["party_id"],
        "channel": row["channel"],
        "direction": row["direction"],
        "status": row["status"],
        "subject": row["subject"],
        "body_text": row["body_text"],
        "external_id": row["external_id"],
        "thread_key": row["thread_key"],
        "sent_at": row["sent_at"],
        "delivered_at": row["delivered_at"],
        "received_at": row["received_at"],
        "author_user": row["author_user"],
        "meta": meta,
        "created_at": row["created_at"],
    }
    if with_html:
        item["body_html"] = row.get("body_html") or ""
    return item


def present(row: dict) -> dict:
    item = public_message(row)
    channel = CHANNEL_BY.get(row["channel"], {})
    direction = DIRECTION_BY.get(row["direction"], {})
    left = row.get("route_from") or ""
    right = row.get("route_to") or ""
    item.update({
        "channel_label": channel.get("label", row["channel"]),
        "direction_label": direction.get("item", row["direction"]),
        "status_label": MESSAGE_STATUSES.get(row["status"], row["status"]),
        "css": channel.get("css", "st-podtverzhden"),
        "unbound": row["package_id"] is None,
        "when": fmt_dt(_at(row)),
        "route_label": f"{left} → {right}" if left or right else "",
        "party_name": row.get("party_name") or "",
    })
    return item


def _window(limit: int, offset: int, cap: int) -> tuple[int, int]:
    try:
        size = int(limit)
        skip = int(offset)
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "limit и offset должны быть целыми.") from exc
    if size < 1:
        size = min(50, cap)
    if size > cap:
        size = cap
    if skip < 0:
        skip = 0
    if skip > 100_000:
        skip = 100_000
    return size, skip


def _where(
    channel: str = "",
    direction: str = "",
    package_id: int | None = None,
    unbound: bool = False,
) -> tuple[str, list]:
    parts: list[str] = []
    args: list = []
    if channel:
        parts.append("m.channel = ?")
        args.append(channel)
    if direction:
        parts.append("m.direction = ?")
        args.append(direction)
    if unbound:
        parts.append("m.package_id IS NULL")
    elif package_id is not None:
        parts.append("m.package_id = ?")
        args.append(package_id)
    if not parts:
        return "", args
    return " WHERE " + " AND ".join(parts), args


def _order_at() -> str:
    return "COALESCE(NULLIF(m.sent_at, ''), NULLIF(m.received_at, ''), m.created_at)"


def store_message(conn, spec: dict) -> int:
    """Прямая запись уже разобранного сообщения. Хуки идут через accept_inbound."""
    if spec.get("channel") not in CHANNELS:
        raise ApiError(422, "Неизвестный канал.")
    if spec.get("direction") not in DIRECTIONS:
        raise ApiError(422, "Неизвестное направление.")
    if spec.get("status") not in STATUSES:
        raise ApiError(422, "Неизвестный статус сообщения.")
    if not spec.get("created_at"):
        raise ApiError(422, "Нет времени записи.")
    return _insert(conn, spec)


def _insert(conn, spec: dict) -> int:
    cur = conn.execute(
        """INSERT INTO messages (
            package_id, party_id, channel, direction, status, subject, body_text, body_html,
            external_id, thread_key, sent_at, delivered_at, received_at, author_user,
            meta_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            spec.get("package_id"),
            spec.get("party_id"),
            spec["channel"],
            spec["direction"],
            spec["status"],
            spec.get("subject") or "",
            spec.get("body_text") or "",
            spec.get("body_html") or "",
            spec.get("external_id") or "",
            spec.get("thread_key") or "",
            spec.get("sent_at") or "",
            spec.get("delivered_at") or "",
            spec.get("received_at") or "",
            spec.get("author_user") or "",
            json.dumps(spec.get("meta") or {}, ensure_ascii=False),
            spec["created_at"],
        ),
    )
    return int(cur.lastrowid)


def _one(conn, message_id: int) -> dict:
    row = conn.execute(_ONE_SQL + " WHERE m.id = ?", (message_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Сообщение не найдено.")
    return dict(row)


def _existing(conn, channel: str, external_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id FROM messages WHERE channel = ? AND external_id = ?",
        (channel, external_id),
    ).fetchone()
    if row is None:
        return None
    return _one(conn, int(row["id"]))


def accept_inbound(db: Database, channel: str, payload: dict) -> dict:
    spec = adapter_for(channel).normalize(payload)
    with db.tx() as conn:
        if spec["external_id"]:
            found = _existing(conn, spec["channel"], spec["external_id"])
            if found is not None:
                item = public_message(found, with_html=True)
                item["created"] = False
                return item
        party_id = spec["party_id"]
        if party_id:
            known = conn.execute("SELECT 1 FROM parties WHERE id = ?", (party_id,)).fetchone()
            if known is None:
                raise ApiError(422, "Карточка контрагента не найдена.")
        package_id = None
        if spec["package_key"]:
            try:
                package_id = fetch_package(conn, spec["package_key"])["id"]
            except ApiError as exc:
                raise ApiError(422, "Заявка не найдена.") from exc
        spec["package_id"] = package_id
        spec["created_at"] = now_iso()
        try:
            new_id = _insert(conn, spec)
        except sqlite3.IntegrityError:
            if spec["external_id"]:
                found = _existing(conn, spec["channel"], spec["external_id"])
                if found is not None:
                    item = public_message(found, with_html=True)
                    item["created"] = False
                    return item
            raise ApiError(409, "Сообщение не записано.") from None
        saved = _one(conn, new_id)
    item = public_message(saved, with_html=True)
    item["created"] = True
    return item


def list_messages(
    db: Database,
    *,
    channel: str = "",
    direction: str = "",
    package: str = "",
    unbound: bool = False,
    limit: int = 50,
    offset: int = 0,
    page_max: int = LIST_PAGE_MAX,
) -> dict:
    channel = clean(channel, 16)
    direction = clean(direction, 8)
    package = clean(package, 40)
    if channel and channel not in CHANNELS:
        raise ApiError(422, "Неизвестный канал.")
    if direction and direction not in DIRECTIONS:
        raise ApiError(422, "Неизвестное направление.")
    package_id = None
    if package and not unbound:
        row = _package_row(db, package)
        if row is None:
            raise ApiError(422, "Заявка не найдена.")
        package_id = int(row["id"])
    size, skip = _window(limit, offset, page_max)
    clause, args = _where(channel, direction, package_id, unbound)
    total = db.one(f"SELECT COUNT(*) AS n FROM messages m{clause}", tuple(args))["n"]
    rows = db.q(
        _LIST_SQL + clause + f" ORDER BY {_order_at()} DESC, m.id DESC LIMIT ? OFFSET ?",
        tuple(args) + (size, skip),
    )
    return {
        "messages": [public_message(row) for row in rows],
        "total": int(total),
        "limit": size,
        "offset": skip,
    }


def get_message(db: Database, message_id: int) -> dict:
    row = db.one(_ONE_SQL + " WHERE m.id = ?", (message_id,))
    if row is None:
        raise ApiError(404, "Сообщение не найдено.")
    return public_message(row, with_html=True)


def bind_message(db: Database, message_id: int, package_key: str, actor: str = "") -> dict:
    key = clean(package_key, 40)
    if not key:
        raise ApiError(422, "Укажите заявку.")
    moment = now_iso()
    with db.tx() as conn:
        current = conn.execute("SELECT id, meta_json FROM messages WHERE id = ?", (message_id,)).fetchone()
        if current is None:
            raise ApiError(404, "Сообщение не найдено.")
        package = fetch_package(conn, key)
        meta = loads(current["meta_json"], {})
        if not isinstance(meta, dict):
            meta = {}
        who = clean(actor, 200)
        if who:
            meta["bound_by"] = who
        meta["bound_at"] = moment
        conn.execute(
            "UPDATE messages SET package_id = ?, meta_json = ? WHERE id = ?",
            (package["id"], json.dumps(meta, ensure_ascii=False), message_id),
        )
        saved = _one(conn, message_id)
    return public_message(saved, with_html=True)


def count_for_package(db: Database, package_id: int) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM messages WHERE package_id = ?", (package_id,))
    return int(row["n"]) if row else 0


def _package_row(db: Database, key: str) -> dict | None:
    return db.one(
        "SELECT id, number FROM packages WHERE id = ? OR number = ?",
        (int(key) if str(key).isdigit() else -1, str(key)),
    )


def _count(db: Database, channel: str, direction: str, package_id: int | None, unbound: bool) -> int:
    clause, args = _where(channel, direction, package_id, unbound)
    row = db.one(f"SELECT COUNT(*) AS n FROM messages m{clause}", tuple(args))
    return int(row["n"])


def messages_of_package(db: Database, package_id: int, limit: int = MESSAGE_CAP) -> tuple[list[dict], int]:
    total = count_for_package(db, package_id)
    size, _skip = _window(limit, 0, MESSAGE_CAP)
    rows = db.q(
        _LIST_SQL + f" WHERE m.package_id = ? ORDER BY {_order_at()} DESC, m.id DESC LIMIT ?",
        (package_id, size),
    )
    rows.reverse()
    return [present(row) for row in rows], total


def feed_state(db: Database, channel: str, direction: str, package: str, unbound: bool) -> dict:
    channel = clean(channel, 16)
    direction = clean(direction, 8)
    typed = clean(package, 40)
    notes: list[str] = []
    if channel and channel not in CHANNELS:
        notes.append("Неизвестный канал. Показаны все.")
        channel = ""
    if direction and direction not in DIRECTIONS:
        notes.append("Неизвестное направление. Показаны все.")
        direction = ""
    package_id = None
    package_number = ""
    unknown = False
    if unbound:
        typed_filter = ""
    else:
        typed_filter = typed
    if typed_filter:
        row = _package_row(db, typed_filter)
        if row is None:
            unknown = True
            notes.append("Такой заявки нет. Проверьте номер, например Д-2026-0001.")
        else:
            package_id = int(row["id"])
            package_number = row["number"]
    if unknown:
        rows: list[dict] = []
        total = 0
    else:
        clause, args = _where(channel, direction, package_id, unbound)
        total = _count(db, channel, direction, package_id, unbound)
        rows = db.q(
            _LIST_SQL + clause + f" ORDER BY {_order_at()} DESC, m.id DESC LIMIT ?",
            tuple(args) + (MESSAGE_CAP,),
        )
    facet_package = None if unknown else package_id
    facet_unbound = False if unknown else unbound
    channel_counts = {
        item["code"]: _count(db, item["code"], direction, facet_package, facet_unbound)
        for item in MESSAGE_CHANNELS
    }
    channel_counts["all"] = _count(db, "", direction, facet_package, facet_unbound)
    direction_counts = {
        item["code"]: _count(db, channel, item["code"], facet_package, facet_unbound)
        for item in MESSAGE_DIRECTIONS
    }
    direction_counts["all"] = _count(db, channel, "", facet_package, facet_unbound)
    loose = _count(db, channel, direction, None, True)
    return {
        "messages": [present(row) for row in rows],
        "total": total,
        "truncated": total > len(rows),
        "cap": MESSAGE_CAP,
        "channel": channel,
        "direction": direction,
        "package": package_number,
        "package_query": "" if unbound else (package_number or typed),
        "unbound": unbound and not unknown,
        "unknown_package": unknown,
        "notes": notes,
        "channel_counts": channel_counts,
        "direction_counts": direction_counts,
        "unbound_count": loose,
    }
