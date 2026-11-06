"""Раздел «Перевозчики»: экспедитор, перевозчик, ТС, водитель, переговоры.

Это не карточки parties. Заявка по-прежнему заводит parties (заказчик / перевозчик / водитель)
и сканы СТС, прав и паспорта на party водителя. Здесь отдельные таблицы: у ТС свой скан СТС,
у водителя — паспорт и ВУ. Пакет ссылается на них полями carrier_id, vehicle_id, driver_id
и не подменяет carrier_party_id / driver_party_id.
Почта и MAX из этого модуля не уходят.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.domain import now_iso
from app.kartochka import render_kartochka
from app.logic import ApiError, clean, fetch_package
from app.parties import phone_key
from app.scans import display_name, resolve_under_files, sniff

CARRIER_KINDS = ("self_employed", "ip", "ooo")
KIND_LABEL = {
    "self_employed": "Самозанятый",
    "ip": "ИП",
    "ooo": "ООО",
}
SCAN_STUB = b"%PDF-1.4\n% STUB-SCAN-NOT-IN-KARTOCHKA\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

EXPEDITOR_FIELDS = (
    "name",
    "inn",
    "ogrnip",
    "address",
    "bank",
    "account",
    "bik",
    "corr_account",
    "phone",
    "email",
    "dispatcher_name",
    "dispatcher_phone",
    "ati_code",
    "edo_id",
)
_EXPEDITOR_LIMIT = {
    "name": 300,
    "inn": 20,
    "ogrnip": 20,
    "address": 500,
    "bank": 300,
    "account": 40,
    "bik": 20,
    "corr_account": 40,
    "phone": 80,
    "email": 200,
    "dispatcher_name": 200,
    "dispatcher_phone": 80,
    "ati_code": 40,
    "edo_id": 80,
}

SAMPLE_EXPEDITOR = {
    "name": "ИП Кртин Андрей Станиславович",
    "inn": "212909112476",
    "ogrnip": "315213000004340",
    "address": "428003, Чувашская Республика, г. Чебоксары, Чебоксарский проспект, д. 45, корп. 2, кв. 36",
    "bank": "Кировское отделение № 8612 ПАО Сбербанк",
    "account": "40802810975000001318",
    "bik": "043304609",
    "corr_account": "30101810500000000609",
    "phone": "8-987-760-97-09",
    "email": "1828201@mail.ru",
    "dispatcher_name": "Андрей",
    "dispatcher_phone": "8-987-760-97-09",
    "ati_code": "8152462",
    "edo_id": "2AE0206731C-CCD3-448C-B721-68B0544F7902",
}

SAMPLE_CARRIER = {
    "name": "Доронин Артём Олегович",
    "kind": "self_employed",
    "inn": "524506946364",
    "ogrn": "",
    "phone": "+7 986 769-59-42",
    "email": "",
    "note": "Образец карточки перевозчика",
}

SAMPLE_VEHICLE = {
    "make_model": "2834DE (марка и модель по СТС), грузовой бортовой, тентованный, распашные ворота, 2012 г., категория B (N1), цвет белый",
    "body": "тент",
    "volume": "19,4 м³",
    "loading": "задняя, боковая, верхняя",
    "plate": "Х 822 ВЕ 252",
    "trailer_plate": "",
    "vin": "XU42834DEC0003017",
    "sts_series": "99 99",
    "sts_number": "116320",
}

SAMPLE_DRIVER = {
    "name": "Доронин Артём Олегович",
    "birth_date": "1993-08-20",
    "phone": "+7 986 769-59-42",
    "passport_series": "22 13",
    "passport_number": "088797",
    "passport_issued_by": "Отделением УФМС России по Нижегородской области в Богородском р-не",
    "passport_issued_at": "2013-09-21",
    "passport_dept_code": "520-012",
    "license_series": "99 26",
    "license_number": "723115",
    "license_issued_at": "2021-08-26",
    "license_issued_by": "ГИБДД 5216",
    "license_valid_until": "2031-08-26",
    "license_categories": "B, B1, C, C1, M",
    "consent": 1,
    "user_is_carrier": 1,
}


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _kind(value: str) -> str:
    kind = clean(value, 20).lower()
    if kind not in CARRIER_KINDS:
        raise ApiError(422, "Тип перевозчика: self_employed, ip или ooo.")
    return kind


def _inn(value: str) -> str:
    digits = _digits(clean(value, 20))
    if len(digits) not in {10, 12}:
        raise ApiError(422, "ИНН — 10 или 12 цифр.")
    return digits


def _ogrn(kind: str, value: str) -> str:
    digits = _digits(clean(value, 20))
    if kind == "self_employed":
        return digits
    if kind == "ip" and len(digits) != 15:
        raise ApiError(422, "Для ИП нужен ОГРНИП из 15 цифр.")
    if kind == "ooo" and len(digits) != 13:
        raise ApiError(422, "Для ООО нужен ОГРН из 13 цифр.")
    return digits


def _phone(value: str) -> str:
    phone = clean(value, 80)
    if len(_digits(phone)) < 10:
        raise ApiError(422, "Нужен телефон.")
    return phone


def _email(value: str) -> str:
    email = clean(value, 200)
    if email and ("@" not in email or any(ch.isspace() for ch in email)):
        raise ApiError(422, "Проверьте e-mail.")
    return email


def _date(value: str, label: str) -> str:
    raw = clean(value, 10)
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-" and raw[:4].isdigit() and raw[5:7].isdigit() and raw[8:10].isdigit():
        return raw
    if len(raw) == 10 and raw[2] == "." and raw[5] == "." and raw[:2].isdigit() and raw[3:5].isdigit() and raw[6:].isdigit():
        return f"{raw[6:10]}-{raw[3:5]}-{raw[0:2]}"
    raise ApiError(422, f"{label} в формате ГГГГ-ММ-ДД.")


def _need(value: str, limit: int, label: str) -> str:
    text = clean(value, limit)
    if not text:
        raise ApiError(422, f"Нужно поле «{label}».")
    return text


def _flag(value) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return 1 if int(value) == 1 else 0
    return 1 if str(value or "").strip().lower() in {"1", "true", "yes", "on"} else 0


def _opt_id(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ApiError(422, "Некорректный идентификатор.")
    raw = str(value).strip()
    if not raw.isdigit():
        raise ApiError(422, "Некорректный идентификатор.")
    number = int(raw)
    if number < 1:
        raise ApiError(422, "Некорректный идентификатор.")
    return number


def public_expeditor(row: dict | None) -> dict:
    row = row or {}
    return {key: row.get(key) or "" for key in EXPEDITOR_FIELDS} | {"id": 1}


def public_carrier(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "kind_label": KIND_LABEL.get(row["kind"], row["kind"]),
        "inn": row["inn"],
        "ogrn": row["ogrn"],
        "phone": row["phone"],
        "email": row["email"],
        "max_contact": row["max_contact"],
        "tg_contact": row["tg_contact"],
        "wa_contact": row["wa_contact"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def public_vehicle(row: dict, sts: dict | None = None) -> dict:
    return {
        "id": row["id"],
        "carrier_id": row["carrier_id"],
        "make_model": row["make_model"],
        "body": row["body"],
        "volume": row["volume"],
        "loading": row["loading"],
        "plate": row["plate"],
        "trailer_plate": row["trailer_plate"],
        "vin": row["vin"],
        "sts_series": row["sts_series"],
        "sts_number": row["sts_number"],
        "sts_file": _public_file(sts),
    }


def public_driver(row: dict, passport: dict | None = None, license_file: dict | None = None) -> dict:
    return {
        "id": row["id"],
        "carrier_id": row["carrier_id"],
        "name": row["name"],
        "birth_date": row["birth_date"],
        "phone": row["phone"],
        "passport_series": row["passport_series"],
        "passport_number": row["passport_number"],
        "passport_issued_by": row["passport_issued_by"],
        "passport_issued_at": row["passport_issued_at"],
        "passport_dept_code": row["passport_dept_code"],
        "license_series": row["license_series"],
        "license_number": row["license_number"],
        "license_issued_at": row["license_issued_at"],
        "license_issued_by": row["license_issued_by"],
        "license_valid_until": row["license_valid_until"],
        "license_categories": row["license_categories"],
        "consent": bool(row["consent"]),
        "consent_at": row["consent_at"],
        "user_is_carrier": bool(row["user_is_carrier"]),
        "passport_file": _public_file(passport),
        "license_file": _public_file(license_file),
    }


def _public_file(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "file_kind": row["file_kind"],
        "original_name": row["original_name"],
        "size": row["size"],
        "content_type": row["content_type"],
        "created_at": row["created_at"],
    }


def load_expeditor(conn) -> dict:
    row = conn.execute("SELECT * FROM expeditor WHERE id = 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO expeditor (id, updated_at) VALUES (1, '')")
        row = conn.execute("SELECT * FROM expeditor WHERE id = 1").fetchone()
    return dict(row)


def save_expeditor(conn, payload: dict, moment: str) -> dict:
    current = load_expeditor(conn)
    values = {}
    for key, limit in _EXPEDITOR_LIMIT.items():
        if key in payload and payload[key] is not None:
            values[key] = clean(payload.get(key), limit)
    if not values:
        return public_expeditor(current)
    sets = ", ".join(f"{key} = ?" for key in values)
    conn.execute(
        f"UPDATE expeditor SET {sets}, updated_at = ? WHERE id = 1",
        (*values.values(), moment),
    )
    return public_expeditor(load_expeditor(conn))


def _carrier_values(payload: dict, partial: bool) -> dict:
    if partial:
        values = {}
        if "name" in payload and payload["name"] is not None:
            values["name"] = _need(payload.get("name"), 300, "Наименование")
        if "kind" in payload and payload["kind"] is not None:
            values["kind"] = _kind(payload.get("kind"))
        if "inn" in payload and payload["inn"] is not None:
            values["inn"] = _inn(payload.get("inn"))
        if "phone" in payload and payload["phone"] is not None:
            values["phone"] = _phone(payload.get("phone"))
        if "ogrn" in payload and payload["ogrn"] is not None:
            values["ogrn"] = clean(payload.get("ogrn"), 20)
        for key, limit in (
            ("email", 200),
            ("max_contact", 80),
            ("tg_contact", 80),
            ("wa_contact", 80),
            ("note", 2000),
        ):
            if key in payload and payload[key] is not None:
                values[key] = _email(payload.get(key)) if key == "email" else clean(payload.get(key), limit)
        return values
    kind = _kind(payload.get("kind"))
    return {
        "name": _need(payload.get("name"), 300, "Наименование"),
        "kind": kind,
        "inn": _inn(payload.get("inn")),
        "ogrn": _ogrn(kind, payload.get("ogrn")),
        "phone": _phone(payload.get("phone")),
        "email": _email(payload.get("email")),
        "max_contact": clean(payload.get("max_contact"), 80),
        "tg_contact": clean(payload.get("tg_contact"), 80),
        "wa_contact": clean(payload.get("wa_contact"), 80),
        "note": clean(payload.get("note"), 2000),
    }


def _finish_carrier_rules(values: dict, current: dict | None) -> None:
    kind = values.get("kind") or (current or {}).get("kind") or ""
    ogrn = values["ogrn"] if "ogrn" in values else (current or {}).get("ogrn") or ""
    if kind and kind != "self_employed":
        _ogrn(kind, ogrn)


def list_carriers(conn, query: str = "", limit: int = 50) -> list[dict]:
    limit = 50 if not 1 <= int(limit) <= 100 else int(limit)
    text = clean(query, 80)
    if not text:
        rows = conn.execute(
            "SELECT * FROM carriers ORDER BY updated_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        digits = _digits(text)
        phone_like = f"%{digits}%" if digits else ""
        rows = conn.execute(
            """SELECT * FROM carriers
               WHERE name LIKE ? COLLATE NOCASE
                  OR inn LIKE ?
                  OR phone LIKE ?
                  OR (? != '' AND REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, ' ', ''), '-', ''), '+', ''), '(', ''), ')', '') LIKE ?)
               ORDER BY updated_at DESC, id DESC
               LIMIT ?""",
            (f"%{text}%", f"%{text}%", f"%{text}%", phone_like, phone_like, limit),
        ).fetchall()
    out = []
    for row in rows:
        item = public_carrier(dict(row))
        item["vehicle_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM vehicles WHERE carrier_id = ?",
            (item["id"],),
        ).fetchone()["n"]
        item["driver_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM drivers WHERE carrier_id = ?",
            (item["id"],),
        ).fetchone()["n"]
        out.append(item)
    return out


def _load_carrier(conn, carrier_id: int) -> dict:
    row = conn.execute("SELECT * FROM carriers WHERE id = ?", (carrier_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Перевозчик не найден.")
    return dict(row)


def create_carrier(conn, payload: dict, moment: str) -> dict:
    values = _carrier_values(payload, partial=False)
    cur = conn.execute(
        """INSERT INTO carriers (
            name, kind, inn, ogrn, phone, email, max_contact, tg_contact, wa_contact, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            values["name"], values["kind"], values["inn"], values["ogrn"], values["phone"], values["email"],
            values["max_contact"], values["tg_contact"], values["wa_contact"], values["note"], moment, moment,
        ),
    )
    return carrier_detail(conn, int(cur.lastrowid))


def update_carrier(conn, carrier_id: int, payload: dict, moment: str) -> dict:
    current = _load_carrier(conn, carrier_id)
    values = _carrier_values(payload, partial=True)
    _finish_carrier_rules(values, current)
    if not values:
        return carrier_detail(conn, carrier_id)
    sets = ", ".join(f"{key} = ?" for key in values)
    conn.execute(
        f"UPDATE carriers SET {sets}, updated_at = ? WHERE id = ?",
        (*values.values(), moment, carrier_id),
    )
    return carrier_detail(conn, carrier_id)


def _files_for(conn, owner_kind: str, owner_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM fleet_files
           WHERE owner_kind = ? AND owner_id = ?
           ORDER BY id""",
        (owner_kind, owner_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _one_file(files: list[dict], kind: str) -> dict | None:
    for row in files:
        if row["file_kind"] == kind:
            return row
    return None


def carrier_detail(conn, carrier_id: int) -> dict:
    carrier = public_carrier(_load_carrier(conn, carrier_id))
    vehicles = []
    for row in conn.execute(
        "SELECT * FROM vehicles WHERE carrier_id = ? ORDER BY id",
        (carrier_id,),
    ):
        files = _files_for(conn, "vehicle", row["id"])
        vehicles.append(public_vehicle(dict(row), _one_file(files, "sts")))
    drivers = []
    for row in conn.execute(
        "SELECT * FROM drivers WHERE carrier_id = ? ORDER BY id",
        (carrier_id,),
    ):
        files = _files_for(conn, "driver", row["id"])
        drivers.append(public_driver(dict(row), _one_file(files, "passport"), _one_file(files, "license")))
    docs = [_public_file(row) for row in _files_for(conn, "carrier", carrier_id) if row["file_kind"] == "doc"]
    carrier["vehicles"] = vehicles
    carrier["drivers"] = drivers
    carrier["files"] = docs
    return carrier


def _vehicle_values(payload: dict) -> dict:
    return {
        "make_model": _need(payload.get("make_model"), 500, "Марка, модель"),
        "body": _need(payload.get("body"), 80, "Кузов"),
        "volume": _need(payload.get("volume"), 80, "Объём"),
        "loading": _need(payload.get("loading"), 200, "Способ погрузки"),
        "plate": _need(payload.get("plate"), 40, "Госномер"),
        "trailer_plate": clean(payload.get("trailer_plate"), 40),
        "vin": _need(payload.get("vin"), 40, "VIN"),
        "sts_series": _need(payload.get("sts_series"), 20, "Серия СТС"),
        "sts_number": _need(payload.get("sts_number"), 20, "Номер СТС"),
    }


def create_vehicle(conn, carrier_id: int, payload: dict, moment: str) -> dict:
    _load_carrier(conn, carrier_id)
    values = _vehicle_values(payload)
    cur = conn.execute(
        """INSERT INTO vehicles (
            carrier_id, make_model, body, volume, loading, plate, trailer_plate, vin, sts_series, sts_number,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            carrier_id, values["make_model"], values["body"], values["volume"], values["loading"],
            values["plate"], values["trailer_plate"], values["vin"], values["sts_series"], values["sts_number"],
            moment, moment,
        ),
    )
    conn.execute("UPDATE carriers SET updated_at = ? WHERE id = ?", (moment, carrier_id))
    return _vehicle_public(conn, int(cur.lastrowid))


def update_vehicle(conn, carrier_id: int, vehicle_id: int, payload: dict, moment: str) -> dict:
    row = _own_vehicle(conn, carrier_id, vehicle_id)
    values = _vehicle_values({**row, **{key: payload[key] for key in payload if payload[key] is not None}})
    sets = ", ".join(f"{key} = ?" for key in values)
    conn.execute(
        f"UPDATE vehicles SET {sets}, updated_at = ? WHERE id = ?",
        (*values.values(), moment, vehicle_id),
    )
    conn.execute("UPDATE carriers SET updated_at = ? WHERE id = ?", (moment, carrier_id))
    return _vehicle_public(conn, vehicle_id)


def _own_vehicle(conn, carrier_id: int, vehicle_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM vehicles WHERE id = ? AND carrier_id = ?",
        (vehicle_id, carrier_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "ТС не найдено у этого перевозчика.")
    return dict(row)


def _vehicle_public(conn, vehicle_id: int) -> dict:
    row = conn.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
    files = _files_for(conn, "vehicle", vehicle_id)
    return public_vehicle(dict(row), _one_file(files, "sts"))


def _driver_values(payload: dict, moment: str) -> dict:
    consent = _flag(payload.get("consent"))
    if not consent:
        raise ApiError(422, "Нужно согласие водителя на передачу данных.")
    return {
        "name": _need(payload.get("name"), 200, "ФИО"),
        "birth_date": _date(payload.get("birth_date"), "Дата рождения"),
        "phone": _phone(payload.get("phone")),
        "passport_series": _need(payload.get("passport_series"), 20, "Серия паспорта"),
        "passport_number": _need(payload.get("passport_number"), 20, "Номер паспорта"),
        "passport_issued_by": _need(payload.get("passport_issued_by"), 300, "Кем выдан паспорт"),
        "passport_issued_at": _date(payload.get("passport_issued_at"), "Дата выдачи паспорта"),
        "passport_dept_code": _need(payload.get("passport_dept_code"), 20, "Код подразделения"),
        "license_series": _need(payload.get("license_series"), 20, "Серия ВУ"),
        "license_number": _need(payload.get("license_number"), 20, "Номер ВУ"),
        "license_issued_at": _date(payload.get("license_issued_at"), "Дата выдачи ВУ"),
        "license_issued_by": _need(payload.get("license_issued_by"), 120, "Кем выдано ВУ"),
        "license_valid_until": _date(payload.get("license_valid_until"), "ВУ действует до"),
        "license_categories": _need(payload.get("license_categories"), 80, "Категории ВУ"),
        "consent": 1,
        "consent_at": moment,
        "user_is_carrier": _flag(payload.get("user_is_carrier")),
    }


def create_driver(conn, carrier_id: int, payload: dict, moment: str) -> dict:
    _load_carrier(conn, carrier_id)
    values = _driver_values(payload, moment)
    cur = conn.execute(
        """INSERT INTO drivers (
            carrier_id, name, birth_date, phone, passport_series, passport_number, passport_issued_by,
            passport_issued_at, passport_dept_code, license_series, license_number, license_issued_at,
            license_issued_by, license_valid_until, license_categories, consent, consent_at, user_is_carrier,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            carrier_id, values["name"], values["birth_date"], values["phone"], values["passport_series"],
            values["passport_number"], values["passport_issued_by"], values["passport_issued_at"],
            values["passport_dept_code"], values["license_series"], values["license_number"],
            values["license_issued_at"], values["license_issued_by"], values["license_valid_until"],
            values["license_categories"], values["consent"], values["consent_at"], values["user_is_carrier"],
            moment, moment,
        ),
    )
    conn.execute("UPDATE carriers SET updated_at = ? WHERE id = ?", (moment, carrier_id))
    return _driver_public(conn, int(cur.lastrowid))


def update_driver(conn, carrier_id: int, driver_id: int, payload: dict, moment: str) -> dict:
    row = _own_driver(conn, carrier_id, driver_id)
    merged = {**row, **{key: payload[key] for key in payload if payload[key] is not None}}
    if "consent" not in payload:
        merged["consent"] = row["consent"]
    if "user_is_carrier" not in payload:
        merged["user_is_carrier"] = row["user_is_carrier"]
    values = _driver_values(merged, row["consent_at"] or moment)
    sets = ", ".join(f"{key} = ?" for key in values)
    conn.execute(
        f"UPDATE drivers SET {sets}, updated_at = ? WHERE id = ?",
        (*values.values(), moment, driver_id),
    )
    conn.execute("UPDATE carriers SET updated_at = ? WHERE id = ?", (moment, carrier_id))
    return _driver_public(conn, driver_id)


def _own_driver(conn, carrier_id: int, driver_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM drivers WHERE id = ? AND carrier_id = ?",
        (driver_id, carrier_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "Водитель не найден у этого перевозчика.")
    return dict(row)


def _driver_public(conn, driver_id: int) -> dict:
    row = conn.execute("SELECT * FROM drivers WHERE id = ?", (driver_id,)).fetchone()
    files = _files_for(conn, "driver", driver_id)
    return public_driver(dict(row), _one_file(files, "passport"), _one_file(files, "license"))


def delete_carrier(conn, data_dir: Path, carrier_id: int) -> list[str]:
    _load_carrier(conn, carrier_id)
    stored = _stored_for_carrier(conn, carrier_id)
    conn.execute(
        """UPDATE packages
           SET carrier_id = NULL, vehicle_id = NULL, driver_id = NULL
           WHERE carrier_id = ?""",
        (carrier_id,),
    )
    _delete_file_rows(conn, stored)
    conn.execute("DELETE FROM carriers WHERE id = ?", (carrier_id,))
    return stored


def delete_vehicle(conn, data_dir: Path, carrier_id: int, vehicle_id: int) -> list[str]:
    _own_vehicle(conn, carrier_id, vehicle_id)
    stored = [row["stored_name"] for row in _files_for(conn, "vehicle", vehicle_id)]
    conn.execute("UPDATE packages SET vehicle_id = NULL WHERE vehicle_id = ?", (vehicle_id,))
    _delete_file_rows(conn, stored)
    conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    return stored


def delete_driver(conn, data_dir: Path, carrier_id: int, driver_id: int) -> list[str]:
    _own_driver(conn, carrier_id, driver_id)
    stored = [row["stored_name"] for row in _files_for(conn, "driver", driver_id)]
    conn.execute("UPDATE packages SET driver_id = NULL WHERE driver_id = ?", (driver_id,))
    _delete_file_rows(conn, stored)
    conn.execute("DELETE FROM drivers WHERE id = ?", (driver_id,))
    return stored


def _stored_for_carrier(conn, carrier_id: int) -> list[str]:
    names = [row["stored_name"] for row in _files_for(conn, "carrier", carrier_id)]
    vehicles = conn.execute("SELECT id FROM vehicles WHERE carrier_id = ?", (carrier_id,)).fetchall()
    drivers = conn.execute("SELECT id FROM drivers WHERE carrier_id = ?", (carrier_id,)).fetchall()
    for row in vehicles:
        names.extend(item["stored_name"] for item in _files_for(conn, "vehicle", row["id"]))
    for row in drivers:
        names.extend(item["stored_name"] for item in _files_for(conn, "driver", row["id"]))
    return names


def _delete_file_rows(conn, stored_names: list[str]) -> None:
    for name in stored_names:
        conn.execute("DELETE FROM fleet_files WHERE stored_name = ?", (name,))


def unlink_stored(data_dir: Path, names: list[str]) -> None:
    for name in names:
        try:
            resolve_under_files(data_dir, name).unlink(missing_ok=True)
        except ApiError:
            continue


def save_fleet_file(
    conn,
    data_dir: Path,
    carrier_id: int,
    file_kind: str,
    filename: str,
    blob: bytes,
    moment: str,
    vehicle_id: int | None = None,
    driver_id: int | None = None,
) -> dict:
    file_kind = clean(file_kind, 20).lower() or "doc"
    if file_kind not in {"doc", "sts", "passport", "license"}:
        raise ApiError(422, "Неизвестный тип файла.")
    ext, content_type = sniff(filename, blob)
    original = display_name(filename, ext)
    if file_kind == "sts":
        vehicle = _own_vehicle(conn, carrier_id, int(vehicle_id or 0))
        owner_kind, owner_id = "vehicle", vehicle["id"]
    elif file_kind in {"passport", "license"}:
        driver = _own_driver(conn, carrier_id, int(driver_id or 0))
        owner_kind, owner_id = "driver", driver["id"]
    else:
        _load_carrier(conn, carrier_id)
        owner_kind, owner_id = "carrier", carrier_id
    stored = f"fleet/{owner_kind}/{owner_id}/{file_kind}_{uuid.uuid4().hex}.{ext}"
    path = data_dir / "files" / stored
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    old = ""
    try:
        if file_kind == "doc":
            conn.execute(
                """INSERT INTO fleet_files (
                    owner_kind, owner_id, file_kind, stored_name, original_name, content_type, size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (owner_kind, owner_id, file_kind, stored, original, content_type, len(blob), moment),
            )
        else:
            current = conn.execute(
                """SELECT stored_name FROM fleet_files
                   WHERE owner_kind = ? AND owner_id = ? AND file_kind = ?""",
                (owner_kind, owner_id, file_kind),
            ).fetchone()
            if current:
                old = current["stored_name"]
                conn.execute(
                    """UPDATE fleet_files
                       SET stored_name = ?, original_name = ?, content_type = ?, size = ?, created_at = ?
                       WHERE owner_kind = ? AND owner_id = ? AND file_kind = ?""",
                    (stored, original, content_type, len(blob), moment, owner_kind, owner_id, file_kind),
                )
            else:
                conn.execute(
                    """INSERT INTO fleet_files (
                        owner_kind, owner_id, file_kind, stored_name, original_name, content_type, size, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (owner_kind, owner_id, file_kind, stored, original, content_type, len(blob), moment),
                )
        conn.execute("UPDATE carriers SET updated_at = ? WHERE id = ?", (moment, carrier_id))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if old and old != stored:
        unlink_stored(data_dir, [old])
    saved = conn.execute(
        "SELECT * FROM fleet_files WHERE stored_name = ?",
        (stored,),
    ).fetchone()
    return _public_file(dict(saved))


def fleet_file_path(db, file_id: int) -> tuple[Path, str, str]:
    row = db.one("SELECT * FROM fleet_files WHERE id = ?", (file_id,))
    if not row:
        raise ApiError(404, "Файл не найден.")
    path = resolve_under_files(db.data_dir, row["stored_name"])
    return path, row["original_name"], row["content_type"]


def readiness(expeditor: dict, carrier: dict | None, vehicle: dict | None, driver: dict | None) -> dict:
    gaps = []
    for key, label in (
        ("name", "нет наименования экспедитора"),
        ("inn", "нет ИНН экспедитора"),
        ("ogrnip", "нет ОГРНИП экспедитора"),
        ("address", "нет адреса экспедитора"),
        ("bank", "нет банка экспедитора"),
        ("account", "нет расчётного счёта экспедитора"),
        ("bik", "нет БИК экспедитора"),
        ("corr_account", "нет корсчёта экспедитора"),
        ("phone", "нет телефона экспедитора"),
        ("email", "нет e-mail экспедитора"),
        ("dispatcher_name", "нет диспетчера по рейсу"),
        ("ati_code", "нет кода ATI.SU"),
        ("edo_id", "нет ЭДО экспедитора"),
    ):
        if not (expeditor.get(key) or "").strip():
            gaps.append(label)
    if carrier is None:
        gaps.append("не выбран перевозчик")
    else:
        if not carrier.get("name"):
            gaps.append("нет наименования перевозчика")
        if carrier.get("kind") not in CARRIER_KINDS:
            gaps.append("нет типа перевозчика")
        if not carrier.get("inn"):
            gaps.append("нет ИНН перевозчика")
        if carrier.get("kind") != "self_employed" and not carrier.get("ogrn"):
            gaps.append("нет ОГРН перевозчика")
        if not carrier.get("phone"):
            gaps.append("нет телефона перевозчика")
    if vehicle is None:
        gaps.append("не выбрано ТС")
    else:
        for key, label in (
            ("make_model", "нет марки и модели"),
            ("body", "нет кузова"),
            ("volume", "нет объёма"),
            ("loading", "нет способа погрузки"),
            ("plate", "нет госномера"),
            ("vin", "нет VIN"),
            ("sts_series", "нет серии СТС"),
            ("sts_number", "нет номера СТС"),
        ):
            if not (vehicle.get(key) or "").strip():
                gaps.append(label)
        if not vehicle.get("sts_file"):
            gaps.append("нет скана СТС")
    if driver is None:
        gaps.append("не выбран водитель")
    else:
        for key, label in (
            ("name", "нет ФИО водителя"),
            ("birth_date", "нет даты рождения"),
            ("phone", "нет телефона водителя"),
            ("passport_series", "нет серии паспорта"),
            ("passport_number", "нет номера паспорта"),
            ("passport_issued_by", "нет кем выдан паспорт"),
            ("passport_issued_at", "нет даты выдачи паспорта"),
            ("passport_dept_code", "нет кода подразделения"),
            ("license_series", "нет серии ВУ"),
            ("license_number", "нет номера ВУ"),
            ("license_issued_at", "нет даты выдачи ВУ"),
            ("license_issued_by", "нет кем выдано ВУ"),
            ("license_valid_until", "нет срока ВУ"),
            ("license_categories", "нет категорий ВУ"),
        ):
            if not (driver.get(key) or "").strip():
                gaps.append(label)
        if not driver.get("consent"):
            gaps.append("нет согласия водителя")
        if not driver.get("passport_file"):
            gaps.append("нет скана паспорта")
        if not driver.get("license_file"):
            gaps.append("нет скана ВУ")
    return {"ready": not gaps, "gaps": gaps}


def dispatcher_line(expeditor: dict, user: dict | None) -> str:
    name = (expeditor.get("dispatcher_name") or "").strip()
    phone = (expeditor.get("dispatcher_phone") or expeditor.get("phone") or "").strip()
    if user and (user.get("display_name") or "").strip():
        name = user["display_name"].strip()
    if name and phone:
        return f"{name}, {phone}"
    return name or phone


def _bundle(conn, carrier_id: int, vehicle_id: int, driver_id: int) -> tuple[dict, dict, dict]:
    detail = carrier_detail(conn, carrier_id)
    vehicle = next((row for row in detail["vehicles"] if row["id"] == vehicle_id), None)
    driver = next((row for row in detail["drivers"] if row["id"] == driver_id), None)
    if vehicle is None:
        raise ApiError(422, "ТС не относится к выбранному перевозчику.")
    if driver is None:
        raise ApiError(422, "Водитель не относится к выбранному перевозчику.")
    return detail, vehicle, driver


def kartochka_path(number: str) -> str:
    if "/" in number or "\\" in number or number.startswith("."):
        raise ApiError(422, "Некорректный номер пакета.")
    return f"packages/{number}/kartochka-perevozchika.pdf"


def build_for(conn, carrier_id: int, vehicle_id: int, driver_id: int, user: dict | None) -> bytes:
    expeditor = load_expeditor(conn)
    carrier, vehicle, driver = _bundle(conn, carrier_id, vehicle_id, driver_id)
    return render_kartochka(expeditor, carrier, vehicle, driver, dispatcher_line(expeditor, user))


def assign_package(conn, package: dict, carrier_id: int | None, vehicle_id: int | None, driver_id: int | None, moment: str) -> dict:
    carrier = vehicle = driver = None
    if carrier_id:
        carrier = carrier_detail(conn, carrier_id)
        if vehicle_id:
            vehicle = next((row for row in carrier["vehicles"] if row["id"] == vehicle_id), None)
            if vehicle is None:
                raise ApiError(422, "ТС не относится к выбранному перевозчику.")
        if driver_id:
            driver = next((row for row in carrier["drivers"] if row["id"] == driver_id), None)
            if driver is None:
                raise ApiError(422, "Водитель не относится к выбранному перевозчику.")
    elif vehicle_id or driver_id:
        raise ApiError(422, "Сначала выберите перевозчика.")
    carrier_name = carrier["name"] if carrier else ""
    carrier_email = carrier["email"] if carrier else ""
    driver_name = driver["name"] if driver else ""
    plate = vehicle["plate"] if vehicle else ""
    vehicle_label = (vehicle["make_model"] if vehicle else "")[:80]
    conn.execute(
        """UPDATE packages
           SET carrier_id = ?, vehicle_id = ?, driver_id = ?,
               carrier_name = ?, carrier_email = ?, driver_name = ?, plate = ?, vehicle = ?,
               updated_at = ?
           WHERE id = ?""",
        (
            carrier_id, vehicle_id, driver_id,
            carrier_name, carrier_email, driver_name, plate, vehicle_label,
            moment, package["id"],
        ),
    )
    fresh = dict(conn.execute("SELECT * FROM packages WHERE id = ?", (package["id"],)).fetchone())
    state = readiness(load_expeditor(conn), carrier, vehicle, driver)
    return {
        "id": fresh["id"],
        "number": fresh["number"],
        "carrier_id": fresh["carrier_id"],
        "vehicle_id": fresh["vehicle_id"],
        "driver_id": fresh["driver_id"],
        "ready": state["ready"],
        "gaps": state["gaps"],
    }


def store_kartochka(conn, data_dir: Path, package: dict, blob: bytes, moment: str, actor: str) -> dict:
    stored = kartochka_path(package["number"])
    path = data_dir / "files" / stored
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    original = "kartochka-perevozchika.pdf"
    row = conn.execute(
        "SELECT id FROM files WHERE package_id = ? AND stored_name = ?",
        (package["id"], stored),
    ).fetchone()
    if row:
        file_id = int(row["id"])
        conn.execute(
            "UPDATE files SET size = ?, created_at = ? WHERE id = ?",
            (len(blob), moment, file_id),
        )
    else:
        cur = conn.execute(
            """INSERT INTO files (package_id, stored_name, original_name, size, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (package["id"], stored, original, len(blob), moment),
        )
        file_id = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO documents (package_id, title, note, state_label, file_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                package["id"],
                "Карточка перевозчика",
                "PDF для грузоотправителя. Сканы отдельно, наружу не отправлено.",
                "Приложен",
                file_id,
                moment,
            ),
        )
    from app.logic import add_event

    add_event(
        conn,
        package["id"],
        "file",
        "Собран пакет заказчику: карточка перевозчика. Наружу не отправлено.",
        actor or "CRM",
        "Карточка перевозчика",
        moment,
        [{"name": original, "file_id": file_id}],
        {"group": "file"},
    )
    conn.execute("UPDATE packages SET updated_at = ? WHERE id = ?", (moment, package["id"]))
    from app.tracks import sync_from_package

    fresh = dict(conn.execute("SELECT * FROM packages WHERE id = ?", (package["id"],)).fetchone())
    sync_from_package(conn, fresh, moment, {"kartochka_ready": True})
    return {"file_id": file_id, "stored_name": stored, "size": len(blob), "package_id": package["id"]}


def assemble_package(conn, data_dir: Path, package: dict, user: dict | None, moment: str, actor: str) -> dict:
    if not package.get("carrier_id") or not package.get("vehicle_id") or not package.get("driver_id"):
        raise ApiError(422, "Сначала выберите перевозчика, ТС и водителя.")
    blob = build_for(conn, package["carrier_id"], package["vehicle_id"], package["driver_id"], user)
    saved = store_kartochka(conn, data_dir, package, blob, moment, actor)
    saved["pdf"] = blob
    return saved


def list_negotiations(conn, package_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM negotiations WHERE package_id = ? ORDER BY id",
        (package_id,),
    ).fetchall()
    return [
        {"id": row["id"], "author": row["author"], "text": row["body"], "created_at": row["created_at"]}
        for row in rows
    ]


def add_negotiation(conn, package_id: int, text: str, author: str, moment: str) -> dict:
    body = clean(text, 4000)
    if not body:
        raise ApiError(422, "Пустая заметка переговоров.")
    cur = conn.execute(
        """INSERT INTO negotiations (package_id, author, body, created_at)
           VALUES (?, ?, ?, ?)""",
        (package_id, clean(author, 200) or "CRM", body, moment),
    )
    conn.execute("UPDATE packages SET updated_at = ? WHERE id = ?", (moment, package_id))
    row = conn.execute("SELECT * FROM negotiations WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"id": row["id"], "author": row["author"], "text": row["body"], "created_at": row["created_at"]}


def haul_view(db, package: dict, query: str) -> dict:
    with db.tx() as conn:
        expeditor = load_expeditor(conn)
        carriers = list_carriers(conn, query, limit=8 if not clean(query, 80) else 20)
        selected = None
        vehicle = None
        driver = None
        if package.get("carrier_id"):
            try:
                selected = carrier_detail(conn, int(package["carrier_id"]))
            except ApiError:
                selected = None
        if selected:
            vehicle = next((row for row in selected["vehicles"] if row["id"] == package.get("vehicle_id")), None)
            driver = next((row for row in selected["drivers"] if row["id"] == package.get("driver_id")), None)
            if selected["id"] not in {item["id"] for item in carriers}:
                brief = {key: selected[key] for key in ("id", "name", "kind", "kind_label", "inn", "phone")}
                brief["vehicle_count"] = len(selected["vehicles"])
                brief["driver_count"] = len(selected["drivers"])
                carriers = [brief, *carriers]
        state = readiness(expeditor, selected, vehicle, driver)
        stored = conn.execute(
            "SELECT id FROM files WHERE package_id = ? AND stored_name = ?",
            (package["id"], kartochka_path(package["number"])),
        ).fetchone()
    same = False
    if selected and driver:
        same = bool(driver["user_is_carrier"]) or (
            phone_key(selected["phone"]) and phone_key(selected["phone"]) == phone_key(driver["phone"])
        ) or selected["name"].strip().casefold() == driver["name"].strip().casefold()
    return {
        "q": query or "",
        "carriers": [
            {**item, "selected": bool(selected and item["id"] == selected["id"])}
            for item in carriers
        ],
        "carrier": selected,
        "vehicle": vehicle,
        "driver": driver,
        "vehicles": [
            {**row, "selected": bool(vehicle and row["id"] == vehicle["id"])}
            for row in (selected["vehicles"] if selected else [])
        ],
        "drivers": [
            {**row, "selected": bool(driver and row["id"] == driver["id"])}
            for row in (selected["drivers"] if selected else [])
        ],
        "same_person": same,
        "ready": state["ready"],
        "gaps": state["gaps"],
        "kartochka_file_id": int(stored["id"]) if stored else None,
    }


def open_package(conn, key: str) -> dict:
    return fetch_package(conn, key)


def seed_doronin(conn, data_dir: Path, moment: str | None = None) -> dict:
    """Экспедитор из блока 1 образца и Доронин с ТС, водителем и тремя сканами-заглушками."""
    moment = moment or now_iso()
    save_expeditor(conn, SAMPLE_EXPEDITOR, moment)
    found = conn.execute(
        "SELECT id FROM carriers WHERE inn = ?",
        (SAMPLE_CARRIER["inn"],),
    ).fetchone()
    if found:
        carrier_id = int(found["id"])
        vehicle = conn.execute(
            "SELECT id FROM vehicles WHERE carrier_id = ? ORDER BY id LIMIT 1",
            (carrier_id,),
        ).fetchone()
        driver = conn.execute(
            "SELECT id FROM drivers WHERE carrier_id = ? ORDER BY id LIMIT 1",
            (carrier_id,),
        ).fetchone()
        return {
            "carrier_id": carrier_id,
            "vehicle_id": int(vehicle["id"]) if vehicle else None,
            "driver_id": int(driver["id"]) if driver else None,
        }
    created = create_carrier(conn, SAMPLE_CARRIER, moment)
    vehicle = create_vehicle(conn, created["id"], SAMPLE_VEHICLE, moment)
    driver = create_driver(conn, created["id"], SAMPLE_DRIVER, moment)
    for kind, owner in (("sts", "vehicle"), ("passport", "driver"), ("license", "driver")):
        save_fleet_file(
            conn,
            data_dir,
            created["id"],
            kind,
            f"{kind}.pdf",
            SCAN_STUB,
            moment,
            vehicle_id=vehicle["id"] if owner == "vehicle" else None,
            driver_id=driver["id"] if owner == "driver" else None,
        )
    return {"carrier_id": created["id"], "vehicle_id": vehicle["id"], "driver_id": driver["id"]}


def seed_package_link(conn, number: str, ids: dict, moment: str) -> None:
    row = conn.execute("SELECT * FROM packages WHERE number = ?", (number,)).fetchone()
    if row is None or not ids.get("carrier_id"):
        return
    assign_package(conn, dict(row), ids["carrier_id"], ids.get("vehicle_id"), ids.get("driver_id"), moment)


def seed_negotiations(conn, number: str, notes: list[tuple[str, str, str]]) -> None:
    row = conn.execute("SELECT id FROM packages WHERE number = ?", (number,)).fetchone()
    if row is None:
        return
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM negotiations WHERE package_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    if existing:
        return
    for moment, author, text in notes:
        add_negotiation(conn, int(row["id"]), text, author, moment)
