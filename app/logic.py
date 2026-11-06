"""Пакеты, нумерация, inbound. Исходящей почты здесь нет."""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.domain import (
    ADVANCE_LABEL,
    BUCKETS,
    EVENT_TYPES,
    STAGE_ORDER,
    STAGE_STATUS,
    STAGES,
    STATUSES,
    chain_classes,
    default_group,
    event_class,
    fmt_day,
    fmt_dt,
    loads,
    money,
    now_iso,
    stage_for_status,
    stage_info,
    status_for_stage,
    status_info,
)
from app.notify import maybe_notify

log = logging.getLogger("delo.logic")

MAX_FILE = 8 * 1024 * 1024
MAX_ATTACHMENTS = 10

TEXT_LIMITS = {
    "route_from": 120,
    "route_to": 120,
    "cargo": 300,
    "weight": 60,
    "pallets": 60,
    "source_detail": 500,
    "list_note": 300,
    "hint": 300,
    "board_title": 200,
    "board_note": 300,
    "meta_left": 120,
    "meta_right": 120,
    "subject": 500,
    "thread_id": 200,
    "carrier_name": 200,
    "carrier_email": 200,
    "carrier_contract": 120,
    "driver_name": 120,
    "driver_channel": 120,
    "plate": 40,
    "vehicle": 80,
    "gate": 300,
    "flags": 2000,
    "refusal_comment": 2000,
    "client_name": 300,
    "client_contact": 200,
    "client_email": 200,
    "client_phone": 80,
    "client_note": 2000,
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def clean(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        else:
            return ""
    text = value.replace("\x00", "").strip()
    return text[:limit]


def opt_int(value, name: str):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ApiError(422, f"Поле {name} должно быть целым.")
    if isinstance(value, int):
        number = value
    else:
        raw = str(value).strip().replace(" ", "").replace("₽", "").replace("\u00a0", "")
        if raw == "":
            return None
        if not re.fullmatch(r"\d+", raw):
            raise ApiError(422, f"Поле {name} должно быть целым.")
        number = int(raw)
    if number < 0 or number > 100_000_000:
        raise ApiError(422, f"Поле {name} вне диапазона.")
    return number


def parse_from(value: str) -> tuple[str, str]:
    value = clean(value, 300)
    match = re.search(r"([^<>\s]+@[^<>\s]+)", value)
    email = match.group(1) if match else ""
    name = re.sub(r"<[^>]*>", "", value).strip().strip('"')
    return name, email


def allocate_number(conn, year: int) -> tuple[int, str]:
    row = conn.execute("SELECT last_seq FROM year_counters WHERE year = ?", (year,)).fetchone()
    if row is None:
        seq = 1
        conn.execute("INSERT INTO year_counters(year, last_seq) VALUES (?, ?)", (year, seq))
    else:
        seq = int(row["last_seq"]) + 1
        conn.execute("UPDATE year_counters SET last_seq = ? WHERE year = ?", (seq, year))
    return seq, f"Д-{year}-{seq:04d}"


def touch_stage(raw: str, stage: str, moment: str) -> str:
    times = loads(raw, {})
    if not isinstance(times, dict):
        times = {}
    if stage not in times:
        times[stage] = moment
    return json.dumps(times, ensure_ascii=False)


def add_event(conn, package_id: int, event_type: str, text: str, actor: str, title: str, moment: str, attachments=None, meta=None) -> int:
    if event_type not in EVENT_TYPES:
        raise ApiError(422, "Неизвестный тип события.")
    cur = conn.execute(
        """INSERT INTO events (package_id, type, text, actor, title, attachments_json, meta_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            package_id,
            event_type,
            clean(text, 8000),
            clean(actor, 200),
            clean(title, 200),
            json.dumps(attachments or [], ensure_ascii=False),
            json.dumps(meta or {}, ensure_ascii=False),
            moment,
        ),
    )
    return int(cur.lastrowid)


def fetch_package(conn, key: str) -> dict:
    key = str(key).strip()
    if key.isdigit():
        row = conn.execute("SELECT * FROM packages WHERE id = ?", (int(key),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM packages WHERE number = ?", (key,)).fetchone()
    if row is None:
        raise ApiError(404, "Пакет не найден.")
    return dict(row)


def _client_public(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "contact_name": row["contact_name"],
        "email": row["email"],
        "phone": row["phone"],
        "note": row["note"],
    }


def _package_public(row: dict, client: dict | None) -> dict:
    return {
        "id": row["id"],
        "number": row["number"],
        "year": row["year"],
        "seq": row["seq"],
        "status": row["status"],
        "status_label": status_info(row["status"])["label"],
        "stage": row["stage"],
        "stage_label": stage_info(row["stage"])["label"],
        "route_from": row["route_from"],
        "route_to": row["route_to"],
        "cargo": row["cargo"],
        "weight": row["weight"],
        "pallets": row["pallets"],
        "rate_rub": row["rate_rub"],
        "carrier_rate_rub": row["carrier_rate_rub"],
        "load_date": row["load_date"],
        "source": row["source"],
        "source_detail": row["source_detail"],
        "subject": row["subject"],
        "thread_id": row["thread_id"],
        "carrier_name": row["carrier_name"],
        "carrier_email": row["carrier_email"],
        "carrier_contract": row["carrier_contract"],
        "driver_name": row["driver_name"],
        "driver_channel": row["driver_channel"],
        "plate": row["plate"],
        "vehicle": row["vehicle"],
        "gate": row["gate"],
        "flags": row["flags"],
        "decision": row["decision"],
        "refusal_comment": row["refusal_comment"],
        "signed_client": bool(row["signed_client"]),
        "signed_carrier": bool(row["signed_carrier"]),
        "client": _client_public(client),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _load_client(conn, client_id) -> dict | None:
    if not client_id:
        return None
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    return dict(row) if row else None


def detail(conn, key: str) -> dict:
    package = fetch_package(conn, key)
    client = _load_client(conn, package["client_id"])
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE package_id = ? ORDER BY created_at, id", (package["id"],)
    ).fetchall()]
    documents = [dict(r) for r in conn.execute(
        "SELECT * FROM documents WHERE package_id = ? ORDER BY id", (package["id"],)
    ).fetchall()]
    public_events = []
    for event in events:
        meta = loads(event["meta_json"], {})
        if not isinstance(meta, dict):
            meta = {}
        attachments = loads(event["attachments_json"], [])
        public_events.append({
            "id": event["id"],
            "type": event["type"],
            "title": event["title"],
            "text": event["text"],
            "actor": event["actor"],
            "attachments": attachments,
            "group": default_group(event["type"], meta),
            "created_at": event["created_at"],
        })
    return {
        "package": _package_public(package, client),
        "events": public_events,
        "documents": [
            {
                "id": doc["id"],
                "title": doc["title"],
                "note": doc["note"],
                "state_label": doc["state_label"],
                "file_id": doc["file_id"],
            }
            for doc in documents
        ],
    }


def list_packages(db: Database, status: str | None = None, stage: str | None = None) -> list[dict]:
    if status and status not in {s["code"] for s in STATUSES}:
        raise ApiError(422, "Неизвестный статус.")
    if stage and stage not in STAGE_STATUS:
        raise ApiError(422, "Неизвестная стадия.")
    sql = """
        SELECT p.*, c.name AS client_name
        FROM packages p
        LEFT JOIN clients c ON c.id = p.client_id
        WHERE 1 = 1
    """
    args: list = []
    if status:
        sql += " AND p.status = ?"
        args.append(status)
    if stage:
        sql += " AND p.stage = ?"
        args.append(stage)
    sql += " ORDER BY p.updated_at DESC, p.id DESC"
    rows = db.q(sql, tuple(args))
    out = []
    for row in rows:
        client = {"id": row["client_id"], "name": row.get("client_name") or "", "contact_name": "", "email": "", "phone": "", "note": ""} if row["client_id"] else None
        item = _package_public(row, client)
        item["client_name"] = row.get("client_name") or ""
        out.append(item)
    return out


def _upsert_client(conn, name: str, email: str, contact: str, phone: str, note: str, moment: str) -> int | None:
    name = clean(name, 300)
    email = clean(email, 200)
    if not name and email:
        name = email
    if not name:
        return None
    row = conn.execute("SELECT * FROM clients WHERE name = ? COLLATE NOCASE ORDER BY id LIMIT 1", (name,)).fetchone()
    if row is None:
        cur = conn.execute(
            """INSERT INTO clients (name, contact_name, email, phone, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, clean(contact, 200), email, clean(phone, 80), clean(note, 2000), moment),
        )
        return int(cur.lastrowid)
    updates = []
    values = []
    for column, value in (
        ("contact_name", clean(contact, 200)),
        ("email", email),
        ("phone", clean(phone, 80)),
        ("note", clean(note, 2000)),
    ):
        if value and not row[column]:
            updates.append(f"{column} = ?")
            values.append(value)
    if updates:
        values.append(row["id"])
        conn.execute(f"UPDATE clients SET {', '.join(updates)} WHERE id = ?", values)
    return int(row["id"])


def _save_bytes(db: Database, original: str, blob: bytes) -> str:
    if len(blob) > MAX_FILE:
        raise ApiError(413, "Файл больше 8 МБ.")
    if not blob:
        raise ApiError(422, "Пустой файл.")
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", Path(original).name).strip("._") or "file"
    safe = safe[:80]
    stored = f"{uuid.uuid4().hex}_{safe}"
    path = (db.data_dir / "files" / stored).resolve()
    root = (db.data_dir / "files").resolve()
    if root not in path.parents:
        raise ApiError(400, "Некорректное имя файла.")
    path.write_bytes(blob)
    return stored


def _store_attachment(db: Database, conn, package_id: int, item, moment: str, written: list[Path]) -> dict:
    if isinstance(item, str):
        item = {"name": item}
    if not isinstance(item, dict):
        raise ApiError(422, "Вложение должно быть объектом.")
    name = clean(item.get("name") or "file", 120) or "file"
    url = clean(item.get("url"), 1000)
    if url and not re.match(r"https?://", url):
        raise ApiError(422, "Ссылка вложения должна начинаться с http:// или https://.")
    raw_b64 = item.get("content_base64") or item.get("base64")
    if raw_b64:
        if not isinstance(raw_b64, str):
            raise ApiError(422, "content_base64 должен быть строкой.")
        try:
            blob = base64.b64decode(raw_b64, validate=True)
        except Exception as exc:
            raise ApiError(422, "Не удалось разобрать content_base64.") from exc
        stored = _save_bytes(db, name, blob)
        written.append(db.data_dir / "files" / stored)
        cur = conn.execute(
            "INSERT INTO files (package_id, stored_name, original_name, size, created_at) VALUES (?, ?, ?, ?, ?)",
            (package_id, stored, name, len(blob), moment),
        )
        file_id = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO documents (package_id, title, note, state_label, file_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (package_id, name, "из inbound", "Приложен", file_id, moment),
        )
        return {"name": name, "file_id": file_id}
    if url:
        return {"name": name, "url": url}
    return {"name": name}


def create_inbound(db: Database, settings: Settings, payload: dict) -> dict:
    source = clean(payload.get("source"), 20)
    if source not in {"mail", "tg", "manual"}:
        raise ApiError(422, "source должен быть mail, tg или manual.")
    sender = clean(payload.get("from"), 300)
    subject = clean(payload.get("subject"), 500)
    text = clean(payload.get("text"), 8000)
    client_hint = clean(payload.get("client_hint"), 300)
    thread_id = clean(payload.get("thread_id"), 200)
    attachments = payload.get("attachments") or []
    if not isinstance(attachments, list):
        raise ApiError(422, "attachments должен быть списком.")
    if len(attachments) > MAX_ATTACHMENTS:
        raise ApiError(422, "Слишком много вложений.")
    if not any([sender, subject, text, client_hint, attachments]):
        raise ApiError(422, "Пустой пакет.")
    from_name, from_email = parse_from(sender)
    moment = now_iso()
    year = int(moment[:4])
    written: list[Path] = []
    created = True
    try:
        with db.tx() as conn:
            existing = None
            if thread_id:
                row = conn.execute(
                    "SELECT * FROM packages WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                existing = dict(row) if row else None
            if existing:
                created = False
                package_id = existing["id"]
                number = existing["number"]
                status = existing["status"]
                fill = {}
                for key in ("route_from", "route_to", "cargo", "weight", "pallets", "load_date", "subject"):
                    value = clean(payload.get(key), TEXT_LIMITS.get(key, 300))
                    if key == "subject":
                        value = subject
                    if value and not existing[key]:
                        fill[key] = value
                if payload.get("rate_rub") not in (None, "") and existing["rate_rub"] is None:
                    fill["rate_rub"] = opt_int(payload.get("rate_rub"), "rate_rub")
                if not existing["client_id"] and (client_hint or from_name or from_email):
                    fill["client_id"] = _upsert_client(
                        conn, client_hint or from_name, from_email, from_name if client_hint else "", "", "", moment
                    )
                if fill:
                    sets = ", ".join(f"{k} = ?" for k in fill)
                    conn.execute(
                        f"UPDATE packages SET {sets}, updated_at = ? WHERE id = ?",
                        (*fill.values(), moment, package_id),
                    )
            else:
                seq, number = allocate_number(conn, year)
                client_id = _upsert_client(
                    conn,
                    client_hint or from_name,
                    from_email,
                    from_name if client_hint else "",
                    "",
                    "",
                    moment,
                )
                status = "polucheno"
                stage = "nahodka"
                cur = conn.execute(
                    """INSERT INTO packages (
                        number, year, seq, status, stage, client_id, route_from, route_to, cargo, weight, pallets,
                        rate_rub, load_date, source, source_detail, list_note, subject, thread_id, stage_times_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        number,
                        year,
                        seq,
                        status,
                        stage,
                        client_id,
                        clean(payload.get("route_from"), 120),
                        clean(payload.get("route_to"), 120),
                        clean(payload.get("cargo"), 300),
                        clean(payload.get("weight"), 60),
                        clean(payload.get("pallets"), 60),
                        opt_int(payload.get("rate_rub"), "rate_rub"),
                        _date_or_blank(payload.get("load_date")),
                        source,
                        clean(payload.get("source_detail"), 500),
                        clean(payload.get("list_note"), 300),
                        subject,
                        thread_id,
                        touch_stage("{}", stage, moment),
                        moment,
                        moment,
                    ),
                )
                package_id = int(cur.lastrowid)
            stored = []
            for item in attachments:
                meta = _store_attachment(db, conn, package_id, item, moment, written)
                stored.append(meta)
            event_type = {"mail": "mail_in", "tg": "tg", "manual": "note"}[source]
            title = {"mail": "Входящее письмо", "tg": "Telegram", "manual": "Ручной ввод"}[source]
            body = text or subject or "Пакет без текста."
            add_event(
                conn,
                package_id,
                event_type,
                body,
                sender or "inbound",
                title,
                moment,
                stored,
                {"group": "mail" if source == "mail" else ("tg" if source == "tg" else "agent"), "source": source},
            )
            if not existing:
                add_event(
                    conn,
                    package_id,
                    "status",
                    "Пакет получен. Статус: Получено.",
                    "CRM",
                    "Статус пакета",
                    moment,
                    None,
                    {"group": "agent", "status": "polucheno", "stage": "nahodka"},
                )
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    log.info("inbound number=%s source=%s created=%s", number, source, created)
    return {"id": package_id, "number": number, "status": status, "created": created}


def _date_or_blank(value) -> str:
    raw = clean(value, 20)
    if not raw:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ApiError(422, "load_date в формате ГГГГ-ММ-ДД.")
    return raw


def apply_patch(db: Database, settings: Settings, key: str, payload: dict) -> dict:
    moment = now_iso()
    notify_pair = None
    snapshot = None
    with db.tx() as conn:
        package = fetch_package(conn, key)
        old_status = package["status"]
        old_stage = package["stage"]
        updates: dict = {}
        for key_name, limit in TEXT_LIMITS.items():
            if key_name.startswith("client_"):
                continue
            if key_name in payload and payload[key_name] is not None:
                updates[key_name] = clean(payload.get(key_name), limit)
        if "load_date" in payload:
            updates["load_date"] = _date_or_blank(payload.get("load_date"))
        if "rate_rub" in payload:
            updates["rate_rub"] = opt_int(payload.get("rate_rub"), "rate_rub")
        if "carrier_rate_rub" in payload:
            updates["carrier_rate_rub"] = opt_int(payload.get("carrier_rate_rub"), "carrier_rate_rub")
        if "decision" in payload:
            decision = clean(payload.get("decision"), 20)
            if decision not in {"", "berem", "ne_berem"}:
                raise ApiError(422, "decision: berem, ne_berem или пусто.")
            updates["decision"] = decision
        for flag in ("signed_client", "signed_carrier"):
            if flag in payload:
                updates[flag] = 1 if str(payload.get(flag)).lower() in {"1", "true", "yes", "on"} else 0
        new_stage = old_stage
        new_status = old_status
        if payload.get("stage"):
            stage = clean(payload.get("stage"), 40)
            if stage not in STAGE_STATUS:
                raise ApiError(422, "Неизвестная стадия.")
            new_stage = stage
            new_status = status_for_stage(stage)
        if payload.get("status"):
            status = clean(payload.get("status"), 40)
            if status not in {s["code"] for s in STATUSES}:
                raise ApiError(422, "Неизвестный статус.")
            new_status = status
            new_stage = stage_for_status(status, new_stage if payload.get("stage") else old_stage)
            if payload.get("stage"):
                # Явно заданы оба — стадия главнее, статус с неё.
                new_stage = clean(payload.get("stage"), 40)
                new_status = status_for_stage(new_stage)
        comment = clean(payload.get("comment") or payload.get("text") or payload.get("refusal_comment"), 2000)
        if new_status == "otkaz" and old_status != "otkaz" and not (comment or package["refusal_comment"]):
            raise ApiError(422, "Для отказа нужен комментарий.")
        if comment and new_status == "otkaz":
            updates["refusal_comment"] = comment
        if new_stage == "podtverzhden" and not (updates.get("decision") or package["decision"]):
            updates["decision"] = "berem"
        if new_stage != old_stage or new_status != old_status:
            updates["stage"] = new_stage
            updates["status"] = new_status
            updates["stage_times_json"] = touch_stage(package["stage_times_json"], new_stage, moment)
        client_fields = {
            "name": clean(payload.get("client_name"), 300) if "client_name" in payload else None,
            "contact": clean(payload.get("client_contact"), 200) if "client_contact" in payload else None,
            "email": clean(payload.get("client_email"), 200) if "client_email" in payload else None,
            "phone": clean(payload.get("client_phone"), 80) if "client_phone" in payload else None,
            "note": clean(payload.get("client_note"), 2000) if "client_note" in payload else None,
        }
        if any(v is not None for v in client_fields.values()):
            client = _load_client(conn, package["client_id"])
            if client is None:
                cid = _upsert_client(
                    conn,
                    client_fields["name"] or "",
                    client_fields["email"] or "",
                    client_fields["contact"] or "",
                    client_fields["phone"] or "",
                    client_fields["note"] or "",
                    moment,
                )
                if cid:
                    updates["client_id"] = cid
            else:
                sets = []
                vals = []
                mapping = {
                    "name": "name",
                    "contact": "contact_name",
                    "email": "email",
                    "phone": "phone",
                    "note": "note",
                }
                for src, column in mapping.items():
                    if client_fields[src] is not None:
                        sets.append(f"{column} = ?")
                        vals.append(client_fields[src])
                if sets:
                    vals.append(client["id"])
                    conn.execute(f"UPDATE clients SET {', '.join(sets)} WHERE id = ?", vals)
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE packages SET {sets}, updated_at = ? WHERE id = ?",
                (*updates.values(), moment, package["id"]),
            )
        if new_status != old_status or new_stage != old_stage:
            bits = []
            if new_stage != old_stage:
                bits.append(f"стадия {stage_info(old_stage)['label']} → {stage_info(new_stage)['label']}")
            if new_status != old_status:
                bits.append(f"статус пакета {status_info(old_status)['label']} → {status_info(new_status)['label']}")
            if comment:
                bits.append(comment)
            add_event(
                conn,
                package["id"],
                "status",
                ". ".join(bits),
                clean(payload.get("actor"), 200) or "CRM",
                "Смена стадии",
                moment,
                None,
                {"group": "agent", "status": new_status, "stage": new_stage},
            )
            if new_status != old_status:
                notify_pair = (old_status, new_status)
        snapshot = detail(conn, str(package["id"]))
    if notify_pair and snapshot:
        maybe_notify(settings, snapshot["package"], notify_pair[0], notify_pair[1])
    return snapshot


def add_note(db: Database, key: str, event_type: str, text: str, actor: str, title: str, meta: dict | None = None, attachments=None) -> dict:
    if event_type not in EVENT_TYPES:
        raise ApiError(422, "Неизвестный тип события.")
    text = clean(text, 8000)
    if event_type in {"note", "mail_in", "mail_out", "tg"} and not text:
        raise ApiError(422, "Пустая заметка.")
    moment = now_iso()
    with db.tx() as conn:
        package = fetch_package(conn, key)
        event_id = add_event(conn, package["id"], event_type, text, actor or "CRM", title, moment, attachments, meta)
        conn.execute("UPDATE packages SET updated_at = ? WHERE id = ?", (moment, package["id"]))
    return {"id": event_id, "package_id": package["id"], "type": event_type}


def save_upload(db: Database, key: str, filename: str, blob: bytes, title: str, note: str, actor: str) -> dict:
    moment = now_iso()
    stored = _save_bytes(db, filename or "file", blob)
    path = db.data_dir / "files" / stored
    try:
        with db.tx() as conn:
            package = fetch_package(conn, key)
            cur = conn.execute(
                "INSERT INTO files (package_id, stored_name, original_name, size, created_at) VALUES (?, ?, ?, ?, ?)",
                (package["id"], stored, clean(filename, 120) or "file", len(blob), moment),
            )
            file_id = int(cur.lastrowid)
            doc_title = clean(title, 200) or clean(filename, 120) or "Файл"
            conn.execute(
                """INSERT INTO documents (package_id, title, note, state_label, file_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (package["id"], doc_title, clean(note, 500), "Приложен", file_id, moment),
            )
            add_event(
                conn,
                package["id"],
                "file",
                clean(note, 2000) or doc_title,
                actor or "CRM",
                doc_title,
                moment,
                [{"name": clean(filename, 120) or "file", "file_id": file_id}],
                {"group": "file"},
            )
            conn.execute("UPDATE packages SET updated_at = ? WHERE id = ?", (moment, package["id"]))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"file_id": file_id, "package_id": package["id"]}


def file_path(db: Database, file_id: int) -> tuple[Path, str]:
    row = db.one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not row:
        raise ApiError(404, "Файл не найден.")
    stored = row["stored_name"]
    if "/" in stored or "\\" in stored or stored.startswith("."):
        raise ApiError(404, "Файл не найден.")
    root = (db.data_dir / "files").resolve()
    path = (root / stored).resolve()
    if root != path.parent or not path.is_file():
        raise ApiError(404, "Файл не найден.")
    return path, row["original_name"]


def add_task(db: Database, package_key: str, title: str, bucket: str, due_label: str, assignee: str) -> dict:
    title = clean(title, 200)
    if not title:
        raise ApiError(422, "Пустая задача.")
    if bucket not in {b["code"] for b in BUCKETS}:
        raise ApiError(422, "Неизвестная колонка задач.")
    moment = now_iso()
    with db.tx() as conn:
        package = fetch_package(conn, package_key)
        cur = conn.execute(
            """INSERT INTO tasks (package_id, title, bucket, due_label, assignee, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (package["id"], title, bucket, clean(due_label, 80), clean(assignee, 120), moment),
        )
        return {"id": int(cur.lastrowid), "package_id": package["id"]}


def move_task(db: Database, task_id: int, bucket: str) -> None:
    if bucket not in {b["code"] for b in BUCKETS}:
        raise ApiError(422, "Неизвестная колонка задач.")
    with db.tx() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ApiError(404, "Задача не найдена.")
        conn.execute("UPDATE tasks SET bucket = ? WHERE id = ?", (bucket, task_id))


def _reached(stage: str) -> list[str]:
    if stage not in STAGE_ORDER:
        return []
    return STAGE_ORDER[: STAGE_ORDER.index(stage) + 1]


def view_package_row(row: dict) -> dict:
    info = stage_info(row["stage"])
    st = status_info(row["status"])
    times = loads(row.get("stage_times_json") or "{}", {})
    if not isinstance(times, dict):
        times = {}
    reached = set(_reached(row["stage"]))
    timeline = []
    for stage in STAGES:
        timeline.append({
            "code": stage["code"],
            "label": stage["label"],
            "class": f"st-{stage['code']}",
            "at": fmt_dt(times.get(stage["code"], "")) if times.get(stage["code"]) else "—",
            "reached": stage["code"] in reached or bool(times.get(stage["code"])),
        })
    parties_carrier = row["carrier_name"]
    parties_driver = row["driver_name"]
    if parties_carrier and parties_driver:
        who = f"{parties_carrier} / {parties_driver}"
    elif parties_carrier or parties_driver:
        who = parties_carrier or parties_driver
    else:
        who = "— / —"
    return {
        **row,
        "stage_label": info["label"],
        "stage_class": f"st-{info['code']}",
        "status_label": st["label"],
        "status_class": "st-otkaz" if row["status"] == "otkaz" else f"st-{STATUS_STAGE_CLASS.get(row['status'], 'podtverzhden')}",
        "chain": chain_classes(row["stage"]),
        "load_day": fmt_day(row["load_date"]),
        "rate_label": money(row["rate_rub"]),
        "carrier_rate_label": money(row["carrier_rate_rub"]) if row["carrier_rate_rub"] is not None else "",
        "who": who,
        "plate_label": row["plate"] or "—",
        "route_label": _route_label(row),
        "timeline": timeline,
        "advance_label": ADVANCE_LABEL.get(row["stage"]),
        "client_name": row.get("client_name") or "",
    }


STATUS_STAGE_CLASS = {
    "polucheno": "nahodka",
    "podgotovleno": "zayavka",
    "otpravleno": "docs_sent",
    "podpisano": "podpisany",
    "arhiv": "originaly",
    "otkaz": "otkaz",
}


def _route_label(row: dict) -> str:
    left = row["route_from"]
    right = row["route_to"]
    if left or right:
        return f"{left or '—'} → {right or '—'}"
    return "Маршрут не указан"


def view_event(event: dict) -> dict:
    meta = loads(event.get("meta_json") or "{}", {})
    if not isinstance(meta, dict):
        meta = {}
    group = default_group(event["type"], meta)
    attachments = loads(event.get("attachments_json") or "[]", [])
    title = event["title"] or event["actor"] or event["type"]
    return {
        **event,
        "group": group,
        "css": event_class(group, event["type"]),
        "when": fmt_dt(event["created_at"]),
        "heading": title,
        "attachments": attachments if isinstance(attachments, list) else [],
    }


def kpis(rows: list[dict]) -> dict:
    in_work = sum(1 for row in rows if row["status"] != "otkaz")
    waiting_yes = sum(1 for row in rows if (row.get("gate") or "").strip())
    waiting_sign = sum(1 for row in rows if row["stage"] in {"docs_sent", "podpisany"})
    total = sum(int(row["rate_rub"] or 0) for row in rows if row["status"] != "otkaz")
    return {
        "in_work": in_work,
        "waiting_yes": waiting_yes,
        "waiting_sign": waiting_sign,
        "rate_sum": money(total) if rows else money(0),
    }
