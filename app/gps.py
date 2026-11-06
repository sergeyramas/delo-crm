"""Заготовка GPS «АТИ Водитель».

Сеть не открывается: ни ATI, ни MAX, ни Telegram, ни почта.
Кнопка «Отслеживать» только пишет факт опроса заглушки.
«Отправить инструкцию» кладёт черновик в gps_drafts и ставит статус «приглашён».
Боевой флаг отправки здесь не читается и не включается.

Координаты на экране: Сергей, Андрей и диспетчер этого рейса.
Точки: поле expires_at. Пустое, пока рейс не завершён
(стадия не dostavlen / originaly / otkaz). После завершения — 30 суток.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.db import Database
from app.domain import fmt_dt, now_iso
from app.logic import ApiError, clean
from app.parties import phone_key

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "app" / "static" / "gps"
PDF_NAME = "driver-instruction.pdf"
TEXT_NAME = "driver-message.txt"
PDF_HREF = "/static/gps/" + PDF_NAME
ATTACHMENT = "gps/" + PDF_NAME

# 30 дней после рейса. Пока рейс идёт, expires_at пустой и срок не тикает.
RETENTION_DAYS = 30
FINISHED_STAGES = {"dostavlen", "originaly", "otkaz"}

STATUS_OFF = "off"
STATUS_INVITED = "invited"
STATUS_TRACKING = "tracking"
STATUSES = (STATUS_OFF, STATUS_INVITED, STATUS_TRACKING)
STATUS_LABEL = {
    STATUS_OFF: "не подключён",
    STATUS_INVITED: "приглашён",
    STATUS_TRACKING: "отслеживается",
}
STATUS_BADGE = {
    STATUS_OFF: "gps-off",
    STATUS_INVITED: "gps-invited",
    STATUS_TRACKING: "gps-tracking",
}
_RANK = {STATUS_OFF: 0, STATUS_INVITED: 1, STATUS_TRACKING: 2}

# Логины, которым координаты видны всегда. Диспетчер рейса — отдельно, по полю сделки.
COORD_LOGINS = frozenset({"sergey", "andrey"})

USERS_ACTIVITY = "POST /v1/driver/public/deal/users_activity"
MAP_ROUTE = "GET /v1.2/orders/carrier/map_route/by_deal"
STUB_REASON = "доступа к ATI API нет, сеть не открывалась"

DRAFT_DETAIL = "Черновик. MAX, Telegram и почта не вызывались."


def status_label(code: str) -> str:
    return STATUS_LABEL.get(code or STATUS_OFF, STATUS_LABEL[STATUS_OFF])


def higher_status(left: str, right: str) -> str:
    a = left if left in _RANK else STATUS_OFF
    b = right if right in _RANK else STATUS_OFF
    return a if _RANK[a] >= _RANK[b] else b


def expires_after_trip(stage: str, finished_at: str) -> str:
    """Пустая строка, пока рейс не завершён. Иначе finished_at + 30 суток."""
    if stage not in FINISHED_STAGES or not (finished_at or "").strip():
        return ""
    try:
        moment = datetime.fromisoformat(finished_at.replace("Z", ""))
    except ValueError:
        return ""
    return (moment + timedelta(days=RETENTION_DAYS)).replace(microsecond=0).isoformat(timespec="seconds")


def point_visible(expires_at: str, now: str | None = None) -> bool:
    if not (expires_at or "").strip():
        return True
    return expires_at > (now or now_iso())


def users_activity(deal_id, phone: str) -> dict:
    """Заглушка POST /v1/driver/public/deal/users_activity. HTTP нет, last_location пустой."""
    del deal_id, phone
    return {
        "endpoint": USERS_ACTIVITY,
        "called": False,
        "reason": STUB_REASON,
        "has_tracker": None,
        "last_location": None,
    }


def map_route(deal_id) -> dict:
    """Заглушка GET /v1.2/orders/carrier/map_route/by_deal. Трасса пустая, HTTP нет."""
    del deal_id
    return {
        "endpoint": MAP_ROUTE,
        "called": False,
        "reason": STUB_REASON,
        "traces": [],
    }


def can_see_coordinates(user: dict | None, packages: list[dict]) -> bool:
    """Сергей и Андрей — всегда. Диспетчер — если он диспетчер рейса.

    Пустой dispatcher_login значит, что смена на рейс ещё не названа:
    роль «диспетчер» тогда видит заглушку. Оператор — нет.
    Без пользователя (AUTH_MODE=off) экран локальный, живых точек всё равно нет.
    """
    if user is None:
        return True
    login = (user.get("login") or "").strip().lower()
    if login in COORD_LOGINS:
        return True
    if (user.get("role_code") or "").strip().lower() != "dispatcher":
        return False
    assigned = {
        (row.get("dispatcher_login") or "").strip().lower()
        for row in packages
        if (row.get("dispatcher_login") or "").strip()
    }
    if not assigned:
        return True
    return login in assigned


def instruction_text() -> str:
    path = ASSET_DIR / TEXT_NAME
    if not path.is_file():
        raise ApiError(500, "Нет текста инструкции.")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ApiError(500, "Текст инструкции пустой.")
    return text + "\n\nВложение: " + PDF_NAME + ". Черновик, наружу не отправлено."


def pdf_path() -> Path:
    path = ASSET_DIR / PDF_NAME
    if not path.is_file():
        raise ApiError(500, "Нет PDF инструкции.")
    return path


def _channels(email: str, hint: str) -> list[dict]:
    items = [
        {"code": "max", "label": "MAX", "available": True},
        {"code": "tg", "label": "Telegram", "available": True},
        {"code": "email", "label": "Почта водителя", "available": bool((email or "").strip())},
    ]
    low = (hint or "").lower()
    prefer = "max"
    if "telegram" in low or low.strip() in {"tg", "telegram"}:
        prefer = "tg"
    elif "почт" in low or "mail" in low or "@" in low:
        prefer = "email" if items[2]["available"] else "max"
    for item in items:
        item["selected"] = item["available"] and item["code"] == prefer
    if not any(item["selected"] for item in items):
        items[0]["selected"] = True
    return items


def _drafts(db: Database, *, package_id: int | None, party_id: int | None, driver_id: int | None) -> list[dict]:
    clauses = []
    args: list = []
    if package_id:
        clauses.append("package_id = ?")
        args.append(package_id)
    if party_id:
        clauses.append("party_id = ?")
        args.append(party_id)
    if driver_id:
        clauses.append("driver_id = ?")
        args.append(driver_id)
    if not clauses:
        return []
    rows = db.q(
        f"SELECT * FROM gps_drafts WHERE {' OR '.join(clauses)} ORDER BY id DESC LIMIT 8",
        tuple(args),
    )
    out = []
    for row in rows:
        out.append({
            "id": row["id"],
            "channel": row["channel"],
            "status": row["status"],
            "created_at": fmt_dt(row["created_at"]),
            "detail": row["detail"],
            "attachment": row["attachment"],
        })
    return out


def _packages_for(db: Database, *, package_id: int | None, party_id: int | None, driver_id: int | None) -> list[dict]:
    if package_id:
        row = db.one("SELECT * FROM packages WHERE id = ?", (package_id,))
        return [row] if row else []
    if party_id:
        return db.q("SELECT * FROM packages WHERE driver_party_id = ?", (party_id,))
    if driver_id:
        return db.q("SELECT * FROM packages WHERE driver_id = ?", (driver_id,))
    return []


def _latest_point(db: Database, packages: list[dict], party_id: int | None, driver_id: int | None) -> dict | None:
    clauses = []
    args: list = []
    ids = [int(row["id"]) for row in packages]
    if ids:
        marks = ",".join("?" for _ in ids)
        clauses.append(f"package_id IN ({marks})")
        args.extend(ids)
    if party_id:
        clauses.append("party_id = ?")
        args.append(party_id)
    if driver_id:
        clauses.append("driver_id = ?")
        args.append(driver_id)
    if not clauses:
        return None
    rows = db.q(
        f"SELECT * FROM gps_points WHERE {' OR '.join(clauses)} ORDER BY id DESC LIMIT 20",
        tuple(args),
    )
    now = now_iso()
    for row in rows:
        if point_visible(row["expires_at"], now):
            return row
    return None


def _status_bundle(db: Database, *, package: dict | None, party: dict | None, driver: dict | None) -> str:
    code = STATUS_OFF
    if package:
        code = higher_status(code, package.get("gps_status") or STATUS_OFF)
    if party:
        code = higher_status(code, party.get("gps_status") or STATUS_OFF)
    if driver:
        code = higher_status(code, driver.get("gps_status") or STATUS_OFF)
    if package is None and party:
        for row in db.q("SELECT gps_status FROM packages WHERE driver_party_id = ?", (party["id"],)):
            code = higher_status(code, row.get("gps_status") or STATUS_OFF)
    if package is None and driver:
        for row in db.q("SELECT gps_status FROM packages WHERE driver_id = ?", (driver["id"],)):
            code = higher_status(code, row.get("gps_status") or STATUS_OFF)
    return code


def _panel(
    db: Database,
    user: dict | None,
    *,
    package: dict | None,
    party: dict | None,
    driver: dict | None,
    email: str,
    hint: str,
    phone: str,
    ready: bool,
    anchor: str,
    track_action: str,
    instruct_action: str,
    back: str,
) -> dict:
    packages = _packages_for(
        db,
        package_id=package["id"] if package else None,
        party_id=party["id"] if party and not package else None,
        driver_id=driver["id"] if driver and not package and not party else None,
    )
    if package and package not in packages:
        packages = [package]
    see = can_see_coordinates(user, packages)
    code = _status_bundle(db, package=package, party=party, driver=driver)
    point = _latest_point(
        db,
        packages,
        party["id"] if party else None,
        driver["id"] if driver else None,
    )
    if not see:
        map_text = "Координаты скрыты. Их видят Сергей, Андрей и диспетчер этого рейса."
        point_line = ""
    elif point is None:
        map_text = "Последней точки нет. Здесь будет карта."
        point_line = ""
    else:
        map_text = "Точка есть. Карта — заглушка, тайлы не грузятся."
        point_line = f"{point['lat']}, {point['lon']} · {fmt_dt(point['recorded_at'])}"
    return {
        "ready": ready,
        "anchor": anchor,
        "status": code,
        "status_label": status_label(code),
        "badge": STATUS_BADGE.get(code, "gps-off"),
        "see": see,
        "map_text": map_text,
        "point_line": point_line,
        "stub_line": f"{USERS_ACTIVITY} и {MAP_ROUTE} не вызывались: доступа к ATI нет.",
        "access_note": "Координаты видят Сергей, Андрей и диспетчер этого рейса.",
        "retention_note": "Точки хранятся 30 дней после рейса, затем скрываются.",
        "channels": _channels(email, hint),
        "phone": phone,
        "pdf_href": PDF_HREF,
        "track_action": track_action,
        "instruct_action": instruct_action,
        "back": back,
        "package_id": package["id"] if package else None,
        "next": "",
        "drafts": _drafts(
            db,
            package_id=package["id"] if package else None,
            party_id=party["id"] if party else None,
            driver_id=driver["id"] if driver else None,
        ),
    }


def panel_package(db: Database, user: dict | None, package: dict, back: str = "") -> dict:
    party = None
    driver = None
    if package.get("driver_party_id"):
        party = db.one("SELECT * FROM parties WHERE id = ? AND kind = 'driver'", (package["driver_party_id"],))
    if package.get("driver_id"):
        driver = db.one("SELECT * FROM drivers WHERE id = ?", (package["driver_id"],))
    email = (party or {}).get("email") or ""
    phone = (party or {}).get("phone") or (driver or {}).get("phone") or ""
    hint = package.get("driver_channel") or ""
    ready = bool(party or driver or (package.get("driver_name") or "").strip())
    key = package["id"]
    view = _panel(
        db,
        user,
        package=package,
        party=party,
        driver=driver,
        email=email,
        hint=hint,
        phone=phone,
        ready=ready,
        anchor=f"deal-{key}",
        track_action=f"/packages/{key}/gps/track",
        instruct_action=f"/packages/{key}/gps/instruction",
        back=back,
    )
    view["next"] = f"/packages/{key}#gps"
    view["package_id"] = None
    return view


def panel_party(db: Database, user: dict | None, party: dict, back: str = "") -> dict:
    view = _panel(
        db,
        user,
        package=None,
        party=party,
        driver=None,
        email=party.get("email") or "",
        hint=party.get("channel") or "",
        phone=party.get("phone") or "",
        ready=True,
        anchor=f"party-{party['id']}",
        track_action=f"/parties/{party['id']}/gps/track",
        instruct_action=f"/parties/{party['id']}/gps/instruction",
        back=back,
    )
    deal = int(back) if str(back).isdigit() else None
    if deal:
        row = db.one("SELECT id, driver_party_id FROM packages WHERE id = ?", (deal,))
        if not row or row.get("driver_party_id") != party["id"]:
            deal = None
    view["package_id"] = deal
    suffix = f"?from={back}" if str(back).isdigit() else ""
    view["next"] = f"/parties/{party['id']}{suffix}#gps"
    return view


def panel_driver(db: Database, user: dict | None, driver: dict, carrier_id: int) -> dict:
    hint = "max"
    view = _panel(
        db,
        user,
        package=None,
        party=None,
        driver=driver,
        email="",
        hint=hint,
        phone=driver.get("phone") or "",
        ready=True,
        anchor=f"driver-{driver['id']}",
        track_action=f"/drivers/{driver['id']}/gps/track",
        instruct_action=f"/drivers/{driver['id']}/gps/instruction",
        back=str(carrier_id),
    )
    view["package_id"] = None
    view["next"] = f"/carriers/{carrier_id}/drivers/{driver['id']}#gps"
    return view


def _raise_status(conn, table: str, row_id: int, moment: str) -> None:
    if table not in {"parties", "drivers", "packages"}:
        raise ApiError(500, "Некорректная таблица GPS.")
    row = conn.execute(f"SELECT gps_status FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return
    current = row["gps_status"] or STATUS_OFF
    if _RANK.get(current, 0) >= _RANK[STATUS_INVITED]:
        return
    conn.execute(
        f"UPDATE {table} SET gps_status = ?, gps_invited_at = ? WHERE id = ?",
        (STATUS_INVITED, moment, row_id),
    )


def _touch_linked(conn, *, package_id: int | None, party_id: int | None, driver_id: int | None, phone: str, moment: str) -> None:
    if party_id:
        _raise_status(conn, "parties", party_id, moment)
        for row in conn.execute("SELECT id FROM packages WHERE driver_party_id = ?", (party_id,)):
            _raise_status(conn, "packages", int(row["id"]), moment)
    if driver_id:
        _raise_status(conn, "drivers", driver_id, moment)
        for row in conn.execute("SELECT id FROM packages WHERE driver_id = ?", (driver_id,)):
            _raise_status(conn, "packages", int(row["id"]), moment)
    if package_id:
        _raise_status(conn, "packages", package_id, moment)
        pack = conn.execute("SELECT driver_party_id, driver_id FROM packages WHERE id = ?", (package_id,)).fetchone()
        if pack and pack["driver_party_id"]:
            _raise_status(conn, "parties", int(pack["driver_party_id"]), moment)
        if pack and pack["driver_id"]:
            _raise_status(conn, "drivers", int(pack["driver_id"]), moment)
    key = phone_key(phone)
    if key:
        for row in conn.execute("SELECT id, phone FROM drivers"):
            if phone_key(row["phone"]) == key:
                _raise_status(conn, "drivers", int(row["id"]), moment)
        for row in conn.execute("SELECT id, phone FROM parties WHERE kind = 'driver'"):
            if phone_key(row["phone"]) == key:
                _raise_status(conn, "parties", int(row["id"]), moment)


def _insert_draft(conn, *, package_id, party_id, driver_id, channel: str, body: str, actor: str, moment: str) -> dict:
    if channel not in {"max", "tg", "email"}:
        raise ApiError(422, "Канал: MAX, Telegram или почта водителя.")
    cur = conn.execute(
        """INSERT INTO gps_drafts
           (package_id, party_id, driver_id, channel, body, attachment, status, actor, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)""",
        (
            package_id,
            party_id,
            driver_id,
            channel,
            body,
            ATTACHMENT,
            clean(actor, 200),
            DRAFT_DETAIL,
            moment,
        ),
    )
    saved = conn.execute("SELECT * FROM gps_drafts WHERE id = ?", (cur.lastrowid,)).fetchone()
    if saved["status"] != "draft":
        raise ApiError(500, "Черновик GPS записался не как draft.")
    return dict(saved)


def _load_contacts(conn, package_id, party_id, driver_id) -> tuple[str, str, str]:
    email = ""
    phone = ""
    hint = ""
    if party_id:
        row = conn.execute("SELECT email, phone, channel, kind FROM parties WHERE id = ?", (party_id,)).fetchone()
        if row is None or row["kind"] != "driver":
            raise ApiError(404, "Карточка водителя не найдена.")
        email, phone, hint = row["email"], row["phone"], row["channel"]
    if driver_id:
        row = conn.execute("SELECT phone FROM drivers WHERE id = ?", (driver_id,)).fetchone()
        if row is None:
            raise ApiError(404, "Водитель не найден.")
        phone = phone or row["phone"]
        hint = hint or "max"
    if package_id:
        row = conn.execute(
            "SELECT driver_name, driver_channel, driver_party_id, driver_id FROM packages WHERE id = ?",
            (package_id,),
        ).fetchone()
        if row is None:
            raise ApiError(404, "Пакет не найден.")
        hint = hint or row["driver_channel"]
        if not party_id and row["driver_party_id"]:
            party = conn.execute("SELECT email, phone FROM parties WHERE id = ?", (row["driver_party_id"],)).fetchone()
            if party:
                email = email or party["email"]
                phone = phone or party["phone"]
        if not phone and row["driver_id"]:
            drv = conn.execute("SELECT phone FROM drivers WHERE id = ?", (row["driver_id"],)).fetchone()
            if drv:
                phone = drv["phone"]
        if not (row["driver_name"] or "").strip() and not row["driver_party_id"] and not row["driver_id"] and not party_id and not driver_id:
            raise ApiError(422, "gps_driver")
    return email or "", phone or "", hint or ""


def send_instruction(
    db: Database,
    *,
    channel: str,
    actor: str,
    package_id: int | None = None,
    party_id: int | None = None,
    driver_id: int | None = None,
) -> dict:
    """Только черновик. Адаптеры каналов и сокеты не вызываются."""
    channel = (channel or "").strip().lower()
    if channel not in {"max", "tg", "email"}:
        raise ApiError(422, "gps_channel")
    body = instruction_text()
    pdf_path()
    moment = now_iso()
    with db.tx() as conn:
        email, phone, _hint = _load_contacts(conn, package_id, party_id, driver_id)
        if channel == "email" and not email.strip():
            raise ApiError(422, "gps_email")
        if package_id and not party_id:
            pack = conn.execute("SELECT driver_party_id, driver_id FROM packages WHERE id = ?", (package_id,)).fetchone()
            if pack:
                party_id = int(pack["driver_party_id"]) if pack["driver_party_id"] else None
                driver_id = driver_id or (int(pack["driver_id"]) if pack["driver_id"] else None)
        saved = _insert_draft(
            conn,
            package_id=package_id,
            party_id=party_id,
            driver_id=driver_id,
            channel=channel,
            body=body,
            actor=actor,
            moment=moment,
        )
        _touch_linked(conn, package_id=package_id, party_id=party_id, driver_id=driver_id, phone=phone, moment=moment)
    return saved


def _remember_poll(conn, endpoint: str, package_id, party_id, driver_id, actor: str, moment: str) -> None:
    conn.execute(
        """INSERT INTO gps_polls
           (package_id, party_id, driver_id, endpoint, called, reason, actor, created_at)
           VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
        (package_id, party_id, driver_id, endpoint, STUB_REASON, clean(actor, 200), moment),
    )


def refresh_expiry(conn, package_id: int | None) -> None:
    if not package_id:
        return
    pack = conn.execute("SELECT stage, updated_at FROM packages WHERE id = ?", (package_id,)).fetchone()
    if pack is None:
        return
    expiry = expires_after_trip(pack["stage"], pack["updated_at"])
    if not expiry:
        return
    conn.execute(
        """UPDATE gps_points
           SET expires_at = ?
           WHERE package_id = ? AND expires_at = ''""",
        (expiry, package_id),
    )


def track(
    db: Database,
    *,
    actor: str,
    package_id: int | None = None,
    party_id: int | None = None,
    driver_id: int | None = None,
) -> list[dict]:
    """Опрос заглушки. last_location не выдумывается, HTTP нет, статус GPS не меняется."""
    moment = now_iso()
    with db.tx() as conn:
        phone = ""
        deal = package_id
        if package_id or party_id or driver_id:
            _email, phone, _hint = _load_contacts(conn, package_id, party_id, driver_id)
        activity = users_activity(deal, phone)
        route = map_route(deal)
        if activity["called"] or route["called"] or activity["last_location"] or route["traces"]:
            raise ApiError(500, "Заглушка ATI не должна звонить в сеть или подставлять точку.")
        _remember_poll(conn, activity["endpoint"], package_id, party_id, driver_id, actor, moment)
        _remember_poll(conn, route["endpoint"], package_id, party_id, driver_id, actor, moment)
        refresh_expiry(conn, package_id)
        if party_id and not package_id:
            for row in conn.execute("SELECT id FROM packages WHERE driver_party_id = ?", (party_id,)):
                refresh_expiry(conn, int(row["id"]))
        if driver_id and not package_id:
            for row in conn.execute("SELECT id FROM packages WHERE driver_id = ?", (driver_id,)):
                refresh_expiry(conn, int(row["id"]))
    return [activity, route]


def store_point(
    db: Database,
    *,
    lat: float,
    lon: float,
    recorded_at: str,
    package_id: int | None = None,
    party_id: int | None = None,
    driver_id: int | None = None,
    source: str = "manual",
) -> dict:
    """Запись точки в модель. Экран «Отслеживать» её не вызывает: ATI молчит."""
    moment = now_iso()
    with db.tx() as conn:
        stage = ""
        finished = ""
        if package_id:
            pack = conn.execute("SELECT stage, updated_at FROM packages WHERE id = ?", (package_id,)).fetchone()
            if pack:
                stage = pack["stage"]
                finished = pack["updated_at"]
        expiry = expires_after_trip(stage, finished)
        cur = conn.execute(
            """INSERT INTO gps_points
               (package_id, party_id, driver_id, lat, lon, recorded_at, source, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (package_id, party_id, driver_id, lat, lon, recorded_at, clean(source, 40) or "manual", expiry, moment),
        )
        row = conn.execute("SELECT * FROM gps_points WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)
