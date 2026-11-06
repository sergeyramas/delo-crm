"""Карточки заказчика, перевозчика и водителя.

Ключ внутри одного kind (client | carrier | driver), по порядку:
1. ИНН — 10 или 12 цифр;
2. иначе телефон (РФ: 8XXXXXXXXXX → 7XXXXXXXXXX, короче 10 цифр ключом не считается);
3. иначе email;
4. иначе точное имя, если более сильного ключа во входе нет.

Один и тот же телефон у заказчика и у водителя — две карточки: kind разный.
Если телефон уже занят другой карточкой того же kind, он на неё не переносится.
Пустые поля входящего заявку не затирают. Форма сделки затирает то, что прислала.
"""

from __future__ import annotations

from app.config import Settings
from app.db import Database
from app.domain import now_iso, stage_info
from app.logic import ApiError, clean
from app.messengers import check_phone, deliver_telegram

KINDS = ("client", "carrier", "driver")
KIND_LABEL = {"client": "Заказчик", "carrier": "Перевозчик", "driver": "Водитель"}
MS_LABEL = {"yes": "есть", "no": "нет", "unknown": "неизвестно"}

TG_ACTOR = "Ассистент Андрея"
TG_SIGN_FIRST = "Привет, это ИИ-ассистент Сергея."
TG_SIGN_NEXT = "ИИ-ассистент Сергея на связи."

_DEAL_COLUMN = {
    "client": "client_id",
    "carrier": "carrier_party_id",
    "driver": "driver_party_id",
}

_LIMITS = {
    "name": 300,
    "contact_name": 200,
    "company": 300,
    "phone": 80,
    "email": 200,
    "inn": 20,
    "note": 2000,
    "vehicle": 80,
    "plate": 40,
    "license_no": 40,
    "passport_no": 40,
    "channel": 120,
    "contract": 120,
}


def phone_key(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) < 10:
        return ""
    return digits


def inn_key(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) in {10, 12}:
        return digits
    return ""


def email_key(raw: str) -> str:
    text = (raw or "").strip().lower()
    if "@" not in text or any(ch.isspace() for ch in text):
        return ""
    return text[:200]


def whatsapp_href(key: str) -> str:
    if not key or not key.isdigit():
        return ""
    return "https://wa.me/" + key


def _status(value: str) -> str:
    if value in MS_LABEL:
        return value
    return "unknown"


def public_party(row: dict) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "kind_label": KIND_LABEL.get(row["kind"], row["kind"]),
        "name": row["name"],
        "contact_name": row["contact_name"],
        "company": row["company"],
        "phone": row["phone"],
        "email": row["email"],
        "inn": row["inn"],
        "note": row["note"],
        "vehicle": row["vehicle"],
        "plate": row["plate"],
        "license_no": row["license_no"],
        "passport_no": row["passport_no"],
        "channel": row["channel"],
        "contract": row["contract"],
        "tg_status": _status(row["tg_status"]),
        "wa_status": _status(row["wa_status"]),
        "max_status": _status(row["max_status"]),
        "messengers_checked_at": row["messengers_checked_at"],
        "gps_status": row.get("gps_status") or "off",
    }


def _find(conn, kind: str, column: str, key: str):
    if column not in {"inn_key", "phone_key", "email_key"}:
        raise ApiError(500, "Некорректный поиск карточки.")
    row = conn.execute(
        f"SELECT * FROM parties WHERE kind = ? AND {column} = ? ORDER BY id LIMIT 1",
        (kind, key),
    ).fetchone()
    return dict(row) if row else None


def _taken(conn, kind: str, column: str, key: str, except_id: int) -> bool:
    if not key:
        return False
    row = conn.execute(
        f"SELECT id FROM parties WHERE kind = ? AND {column} = ? AND id <> ? LIMIT 1",
        (kind, key, except_id),
    ).fetchone()
    return row is not None


def _name_match(conn, kind: str, name: str):
    row = conn.execute(
        "SELECT * FROM parties WHERE kind = ? AND name = ? COLLATE NOCASE ORDER BY id LIMIT 1",
        (kind, name),
    ).fetchone()
    return dict(row) if row else None


def _apply_messengers(conn, settings: Settings, party_id: int, key: str, moment: str) -> None:
    marks = check_phone(settings, key)
    conn.execute(
        """UPDATE parties
           SET tg_status = ?, wa_status = ?, max_status = ?, messengers_checked_at = ?
           WHERE id = ?""",
        (marks["tg"], marks["wa"], marks["max"], moment, party_id),
    )


def _mirror_client(conn, party: dict, moment: str) -> None:
    if party["kind"] != "client":
        return
    row = conn.execute("SELECT id FROM clients WHERE id = ?", (party["id"],)).fetchone()
    values = (
        party["name"] or party["email"] or "Без названия",
        party["contact_name"],
        party["email"],
        party["phone"],
        party["note"],
    )
    if row is None:
        conn.execute(
            """INSERT INTO clients (id, name, contact_name, email, phone, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (party["id"], *values, party["created_at"] or moment),
        )
    else:
        conn.execute(
            "UPDATE clients SET name = ?, contact_name = ?, email = ?, phone = ?, note = ? WHERE id = ?",
            (*values, party["id"]),
        )


def _load(conn, party_id: int, kind: str | None = None) -> dict | None:
    if kind:
        row = conn.execute("SELECT * FROM parties WHERE id = ? AND kind = ?", (party_id, kind)).fetchone()
    else:
        row = conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    return dict(row) if row else None


def upsert_party(conn, settings: Settings, kind: str, fields: dict, moment: str, linked_id: int | None = None) -> int | None:
    if kind not in KINDS:
        raise ApiError(422, "Неизвестный тип карточки.")
    cleaned = {}
    for key, limit in _LIMITS.items():
        if key in fields and fields[key] is not None:
            cleaned[key] = clean(fields[key], limit)
    incoming_inn = inn_key(cleaned["inn"]) if cleaned.get("inn") else ""
    incoming_phone = phone_key(cleaned["phone"]) if cleaned.get("phone") else ""
    incoming_email = email_key(cleaned["email"]) if cleaned.get("email") else ""
    incoming_name = cleaned.get("name") or ""
    has_data = bool(
        incoming_inn
        or incoming_phone
        or incoming_email
        or incoming_name
        or any(cleaned.get(key) for key in ("contact_name", "company", "note", "vehicle", "plate", "license_no", "passport_no", "channel", "contract"))
    )
    found = None
    if incoming_inn:
        found = _find(conn, kind, "inn_key", incoming_inn)
    elif incoming_phone:
        found = _find(conn, kind, "phone_key", incoming_phone)
    elif incoming_email:
        found = _find(conn, kind, "email_key", incoming_email)
    elif incoming_name and not linked_id:
        found = _name_match(conn, kind, incoming_name)
    row = found
    if row is None and linked_id:
        row = _load(conn, linked_id, kind)
    if row is None:
        if not has_data:
            return None
        blank = {key: "" for key in _LIMITS}
        blank.update(cleaned)
        if not blank["name"]:
            blank["name"] = blank["company"] or blank["plate"] or blank["phone"] or blank["email"] or KIND_LABEL[kind]
        if not blank["company"] and kind in {"client", "carrier"}:
            blank["company"] = blank["name"]
        pk = phone_key(blank["phone"])
        ik = inn_key(blank["inn"])
        ek = email_key(blank["email"])
        if pk and _taken(conn, kind, "phone_key", pk, 0):
            blank["phone"] = ""
            pk = ""
        if ik and _taken(conn, kind, "inn_key", ik, 0):
            blank["inn"] = ""
            ik = ""
        if ek and _taken(conn, kind, "email_key", ek, 0):
            blank["email"] = ""
            ek = ""
        cur = conn.execute(
            """INSERT INTO parties (
                kind, name, contact_name, company, phone, phone_key, email, email_key, inn, inn_key, note,
                vehicle, plate, license_no, passport_no, channel, contract,
                tg_status, wa_status, max_status, messengers_checked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', 'unknown', 'unknown', '', ?, ?)""",
            (
                kind,
                blank["name"],
                blank["contact_name"],
                blank["company"],
                blank["phone"],
                pk,
                blank["email"],
                ek,
                blank["inn"],
                ik,
                blank["note"],
                blank["vehicle"],
                blank["plate"],
                blank["license_no"],
                blank["passport_no"],
                blank["channel"],
                blank["contract"],
                moment,
                moment,
            ),
        )
        party_id = int(cur.lastrowid)
        _apply_messengers(conn, settings, party_id, pk, moment)
        saved = _load(conn, party_id)
        _mirror_client(conn, saved, moment)
        return party_id
    assign = dict(cleaned)
    party_id = int(row["id"])
    if "phone" in assign:
        pk = phone_key(assign["phone"])
        if pk and pk != row["phone_key"] and _taken(conn, kind, "phone_key", pk, party_id):
            assign.pop("phone", None)
        else:
            assign["phone_key"] = pk
    if "email" in assign:
        ek = email_key(assign["email"])
        if ek and ek != row["email_key"] and _taken(conn, kind, "email_key", ek, party_id):
            assign.pop("email", None)
        else:
            assign["email_key"] = ek
    if "inn" in assign:
        ik = inn_key(assign["inn"])
        if ik and ik != row["inn_key"] and _taken(conn, kind, "inn_key", ik, party_id):
            assign.pop("inn", None)
        else:
            assign["inn_key"] = ik
    if not assign:
        return party_id
    allowed = set(_LIMITS) | {"phone_key", "email_key", "inn_key"}
    assign = {column: value for column, value in assign.items() if column in allowed}
    if not assign:
        return party_id
    sets = [f"{column} = ?" for column in assign]
    conn.execute(
        f"UPDATE parties SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
        (*assign.values(), moment, party_id),
    )
    updated = _load(conn, party_id)
    if updated["phone_key"] != row["phone_key"] or not updated["messengers_checked_at"]:
        _apply_messengers(conn, settings, party_id, updated["phone_key"], moment)
        updated = _load(conn, party_id)
    _mirror_client(conn, updated, moment)
    return party_id


def _put(target: dict, column: str, value: str | None) -> None:
    if value is None:
        return
    target[column] = value


def _inbound_value(payload: dict, key: str, limit: int, fallback: str = "") -> str | None:
    if key in payload and payload[key] not in (None, ""):
        return clean(payload.get(key), limit)
    if fallback:
        return clean(fallback, limit)
    return None


def sync_package_parties(
    conn,
    settings: Settings,
    package: dict | None,
    payload: dict,
    moment: str,
    *,
    mode: str,
    from_name: str = "",
    from_email: str = "",
    client_hint: str = "",
) -> dict:
    """Возвращает поля packages, которые надо записать вместе со сделкой."""
    linked_client = package.get("client_id") if package else None
    linked_carrier = package.get("carrier_party_id") if package else None
    linked_driver = package.get("driver_party_id") if package else None
    updates: dict = {}

    if mode == "inbound":
        client = {}
        _put(client, "name", _inbound_value(payload, "client_name", 300, client_hint or from_name))
        contact_fallback = from_name if (client_hint or payload.get("client_name")) and from_name else ""
        _put(client, "contact_name", _inbound_value(payload, "client_contact", 200, contact_fallback))
        _put(client, "email", _inbound_value(payload, "client_email", 200, from_email))
        _put(client, "phone", _inbound_value(payload, "client_phone", 80, str(payload.get("phone") or "")))
        _put(client, "inn", _inbound_value(payload, "client_inn", 20, str(payload.get("inn") or "")))
        _put(client, "company", _inbound_value(payload, "client_company", 300, client_hint))
        _put(client, "note", _inbound_value(payload, "client_note", 2000))
        carrier = {}
        _put(carrier, "name", _inbound_value(payload, "carrier_name", 200))
        _put(carrier, "email", _inbound_value(payload, "carrier_email", 200))
        _put(carrier, "phone", _inbound_value(payload, "carrier_phone", 80))
        _put(carrier, "inn", _inbound_value(payload, "carrier_inn", 20))
        _put(carrier, "company", _inbound_value(payload, "carrier_company", 300))
        _put(carrier, "contract", _inbound_value(payload, "carrier_contract", 120))
        driver = {}
        _put(driver, "name", _inbound_value(payload, "driver_name", 120))
        _put(driver, "phone", _inbound_value(payload, "driver_phone", 80))
        _put(driver, "email", _inbound_value(payload, "driver_email", 200))
        _put(driver, "vehicle", _inbound_value(payload, "vehicle", 80))
        _put(driver, "plate", _inbound_value(payload, "plate", 40))
        _put(driver, "license_no", _inbound_value(payload, "license_no", 40) or _inbound_value(payload, "driver_license", 40))
        _put(driver, "passport_no", _inbound_value(payload, "passport_no", 40) or _inbound_value(payload, "driver_passport", 40))
        _put(driver, "channel", _inbound_value(payload, "driver_channel", 120))
    elif mode == "patch":
        client = _slice(payload, {
            "name": "client_name",
            "contact_name": "client_contact",
            "email": "client_email",
            "phone": "client_phone",
            "inn": "client_inn",
            "company": "client_company",
            "note": "client_note",
        })
        carrier = _slice(payload, {
            "name": "carrier_name",
            "email": "carrier_email",
            "phone": "carrier_phone",
            "inn": "carrier_inn",
            "company": "carrier_company",
            "contract": "carrier_contract",
        })
        driver = _slice(payload, {
            "name": "driver_name",
            "phone": "driver_phone",
            "email": "driver_email",
            "vehicle": "vehicle",
            "plate": "plate",
            "license_no": "license_no",
            "passport_no": "passport_no",
            "channel": "driver_channel",
        })
        if "license_no" not in driver and "driver_license" in payload:
            driver["license_no"] = clean(payload.get("driver_license"), 40)
        if "passport_no" not in driver and "driver_passport" in payload:
            driver["passport_no"] = clean(payload.get("driver_passport"), 40)
    else:
        raise ApiError(500, "Неизвестный режим карточки.")

    client_id = upsert_party(conn, settings, "client", client, moment, linked_client) if client or linked_client and mode == "patch" else None
    if mode == "inbound" and not client:
        client_id = None
    if client_id:
        updates["client_id"] = client_id

    if carrier or (mode == "patch" and linked_carrier and _any_key(payload, ("carrier_name", "carrier_email", "carrier_phone", "carrier_inn", "carrier_company", "carrier_contract"))):
        carrier_id = upsert_party(conn, settings, "carrier", carrier, moment, linked_carrier)
        if carrier_id:
            saved = _load(conn, carrier_id)
            updates["carrier_party_id"] = carrier_id
            updates["carrier_name"] = saved["name"]
            updates["carrier_email"] = saved["email"]
            updates["carrier_contract"] = saved["contract"]
    if driver or (mode == "patch" and linked_driver and _any_key(payload, ("driver_name", "driver_phone", "driver_email", "vehicle", "plate", "license_no", "passport_no", "driver_license", "driver_passport", "driver_channel"))):
        driver_id = upsert_party(conn, settings, "driver", driver, moment, linked_driver)
        if driver_id:
            saved = _load(conn, driver_id)
            updates["driver_party_id"] = driver_id
            updates["driver_name"] = saved["name"]
            updates["driver_channel"] = saved["channel"]
            updates["plate"] = saved["plate"]
            updates["vehicle"] = saved["vehicle"]
    return updates


def _slice(payload: dict, mapping: dict[str, str]) -> dict:
    out = {}
    for column, key in mapping.items():
        if key in payload and payload[key] is not None:
            out[column] = clean(payload.get(key), _LIMITS[column])
    return out


def _any_key(payload: dict, keys: tuple[str, ...]) -> bool:
    return any(key in payload and payload[key] is not None for key in keys)


def public_by_id(conn, party_id: int | None) -> dict | None:
    if not party_id:
        return None
    row = _load(conn, int(party_id))
    if row is None:
        return None
    return public_party(row)


def load_party(conn, party_id: int) -> dict:
    row = _load(conn, party_id)
    if row is None:
        raise ApiError(404, "Карточка не найдена.")
    return row


def party_deals(conn, party: dict, limit: int = 50) -> tuple[list[dict], int]:
    from app.domain import PARTY_DEAL_CAP

    column = _DEAL_COLUMN[party["kind"]]
    size = limit if 1 <= limit <= PARTY_DEAL_CAP else PARTY_DEAL_CAP
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM packages WHERE {column} = ?",
        (party["id"],),
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT id, number, route_from, route_to, stage, status, load_date
            FROM packages WHERE {column} = ? ORDER BY updated_at DESC, id DESC
            LIMIT ?""",
        (party["id"], size),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["stage_label"] = stage_info(item["stage"])["label"]
        item["stage_class"] = f"st-{item['stage']}" if item["stage"] != "otkaz" else "st-otkaz"
        left = item["route_from"] or "—"
        right = item["route_to"] or "—"
        item["route_label"] = f"{left} → {right}"
        out.append(item)
    return out, int(total)


def party_messages(conn, party_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM outbound_messages WHERE party_id = ? ORDER BY id DESC LIMIT 20",
        (party_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def telegram_signature(conn, party_id: int) -> str:
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM outbound_messages
           WHERE party_id = ? AND channel = 'tg' AND status IN ('local', 'sent')""",
        (party_id,),
    ).fetchone()
    return TG_SIGN_FIRST if int(row["n"]) == 0 else TG_SIGN_NEXT


def recheck_party(db: Database, settings: Settings, party_id: int) -> None:
    moment = now_iso()
    with db.tx() as conn:
        row = _load(conn, party_id)
        if row is None:
            raise ApiError(404, "Карточка не найдена.")
        _apply_messengers(conn, settings, party_id, row["phone_key"], moment)


def record_outbound(db: Database, settings: Settings, party_id: int, channel: str, text: str, operator: str, confirmed: bool) -> dict:
    if channel not in {"tg", "max"}:
        raise ApiError(422, "Неизвестный канал.")
    if not confirmed:
        raise ApiError(403, "confirm")
    text = clean(text, 4000)
    if not text:
        raise ApiError(422, "Пустой текст.")
    moment = now_iso()
    with db.tx() as conn:
        party = _load(conn, party_id)
        if party is None:
            raise ApiError(404, "Карточка не найдена.")
        if channel == "tg":
            signature = telegram_signature(conn, party_id)
            body = f"{signature}\n\n{text}"
            ok, detail = deliver_telegram(settings, party["phone_key"], body)
            status = "local" if ok else "not_sent"
            actor = TG_ACTOR
        else:
            signature = ""
            body = text
            detail = "MAX не подключён. Черновик сохранён в CRM, наружу не ушло."
            status = "not_sent"
            actor = operator or "CRM"
        cur = conn.execute(
            """INSERT INTO outbound_messages
               (party_id, channel, body, signature, actor, operator, status, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (party_id, channel, body, signature, actor, clean(operator, 200), status, detail, moment),
        )
        saved = conn.execute("SELECT * FROM outbound_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(saved)


def backfill_conn(conn, settings: Settings, moment: str | None = None) -> None:
    moment = moment or now_iso()
    clients = conn.execute("SELECT * FROM clients").fetchall()
    for client in clients:
        if conn.execute("SELECT 1 FROM parties WHERE id = ?", (client["id"],)).fetchone():
            continue
        phone = client["phone"] or ""
        email = client["email"] or ""
        conn.execute(
            """INSERT INTO parties (
                id, kind, name, contact_name, company, phone, phone_key, email, email_key, note,
                created_at, updated_at
            ) VALUES (?, 'client', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                client["id"],
                client["name"],
                client["contact_name"],
                client["name"],
                phone,
                phone_key(phone),
                email,
                email_key(email),
                client["note"],
                client["created_at"],
                moment,
            ),
        )
    carriers = conn.execute(
        """SELECT * FROM packages
           WHERE TRIM(carrier_name) != '' AND (carrier_party_id IS NULL OR carrier_party_id = 0)"""
    ).fetchall()
    for package in carriers:
        party_id = upsert_party(
            conn,
            settings,
            "carrier",
            {
                "name": package["carrier_name"],
                "email": package["carrier_email"],
                "contract": package["carrier_contract"],
            },
            moment,
        )
        if party_id:
            conn.execute("UPDATE packages SET carrier_party_id = ? WHERE id = ?", (party_id, package["id"]))
    drivers = conn.execute(
        """SELECT * FROM packages
           WHERE (TRIM(driver_name) != '' OR TRIM(plate) != '' OR TRIM(vehicle) != '')
             AND (driver_party_id IS NULL OR driver_party_id = 0)"""
    ).fetchall()
    for package in drivers:
        party_id = upsert_party(
            conn,
            settings,
            "driver",
            {
                "name": package["driver_name"],
                "plate": package["plate"],
                "vehicle": package["vehicle"],
                "channel": package["driver_channel"],
            },
            moment,
        )
        if party_id:
            conn.execute("UPDATE packages SET driver_party_id = ? WHERE id = ?", (party_id, package["id"]))


def backfill(db: Database, settings: Settings) -> None:
    with db.tx() as conn:
        backfill_conn(conn, settings)
