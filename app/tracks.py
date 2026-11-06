"""Трекер документов по получателям на карточке сделки.

Три строки: заказчик, перевозчик, водитель. Если перевозчик и водитель — один человек,
строки перевозчика и водителя сливаются в одну.
Писем и MAX отсюда нет: статус ставится вручную или подтягивается из уже лежащих
полей пакета (стадия, otpravleno, подписи, а также mirror_status и scan_ok в PATCH).
"""

from __future__ import annotations

from app.domain import STAGE_ORDER, fmt_dt
from app.logic import ApiError, clean
from app.parties import phone_key

RECIPIENTS = ("client", "carrier", "driver", "carrier_driver")
STATUSES = ("prepared", "sent", "received", "signed")
STATUS_LABEL = {
    "": "не отмечено",
    "prepared": "подготовлено",
    "sent": "отправлено",
    "received": "получено",
    "signed": "подписано",
}
RANK = {"": 0, "prepared": 1, "sent": 2, "received": 3, "signed": 4}

MIRROR_STATUS = {
    "prepared": "prepared",
    "podgotovleno": "prepared",
    "sent": "sent",
    "otpravleno": "sent",
    "received": "received",
    "polucheno": "received",
    "signed": "signed",
    "podpisano": "signed",
    "scan_ok": "signed",
}

_SPECS = {
    "client": {
        "title": "Заказчик",
        "flow": "Карточка перевозчика и сканы → подписанная заявка",
        "channel": "email",
    },
    "carrier": {
        "title": "Перевозчик",
        "flow": "Зеркальная заявка → подпись перевозчика",
        "channel": "email",
    },
    "driver": {
        "title": "Водитель",
        "flow": "Заявка и данные рейса в MAX",
        "channel": "max",
    },
    "carrier_driver": {
        "title": "Перевозчик и водитель",
        "flow": "Зеркальная заявка → подпись; данные рейса в MAX. Один человек, строка общая.",
        "channel": "email, MAX",
    },
}


def _stage_at_least(stage: str, target: str) -> bool:
    if stage not in STAGE_ORDER or target not in STAGE_ORDER:
        return False
    return STAGE_ORDER.index(stage) >= STAGE_ORDER.index(target)


def _same_records(carrier: dict, driver: dict) -> bool:
    if int(driver.get("user_is_carrier") or 0):
        return True
    left = phone_key(carrier.get("phone") or "")
    right = phone_key(driver.get("phone") or "")
    if left and left == right:
        return True
    cname = (carrier.get("name") or "").strip().casefold()
    dname = (driver.get("name") or "").strip().casefold()
    return bool(cname and cname == dname)


def same_person(conn, package: dict) -> bool:
    carrier_id = package.get("carrier_id")
    driver_id = package.get("driver_id")
    if carrier_id and driver_id:
        carrier = conn.execute("SELECT * FROM carriers WHERE id = ?", (carrier_id,)).fetchone()
        driver = conn.execute("SELECT * FROM drivers WHERE id = ?", (driver_id,)).fetchone()
        if carrier and driver and _same_records(dict(carrier), dict(driver)):
            return True
        if carrier and driver:
            return False
    cname = (package.get("carrier_name") or "").strip().casefold()
    dname = (package.get("driver_name") or "").strip().casefold()
    return bool(cname and cname == dname)


def _recipient_codes(merged: bool) -> tuple[str, ...]:
    if merged:
        return ("client", "carrier_driver")
    return ("client", "carrier", "driver")


def _rows(conn, package_id: int) -> dict[str, dict]:
    found = {}
    for row in conn.execute(
        "SELECT * FROM package_doc_tracks WHERE package_id = ?",
        (package_id,),
    ):
        found[row["recipient"]] = dict(row)
    return found


def _ensure(conn, package_id: int, code: str, moment: str) -> dict:
    spec = _SPECS[code]
    conn.execute(
        """INSERT OR IGNORE INTO package_doc_tracks
           (package_id, recipient, status, channel, status_at, updated_at)
           VALUES (?, ?, '', ?, '', ?)""",
        (package_id, code, spec["channel"], moment),
    )
    row = conn.execute(
        "SELECT * FROM package_doc_tracks WHERE package_id = ? AND recipient = ?",
        (package_id, code),
    ).fetchone()
    return dict(row)


def _write(conn, package_id: int, code: str, status: str, channel: str, moment: str) -> None:
    conn.execute(
        """UPDATE package_doc_tracks
           SET status = ?, channel = ?, status_at = ?, updated_at = ?
           WHERE package_id = ? AND recipient = ?""",
        (status, channel, moment, moment, package_id, code),
    )


def bump(conn, package_id: int, code: str, status: str, channel: str, moment: str, *, force: bool = False) -> None:
    if code not in _SPECS or status not in STATUSES:
        return
    row = _ensure(conn, package_id, code, moment)
    current = row["status"] or ""
    if not force and RANK.get(current, 0) >= RANK[status]:
        return
    use_channel = (channel or row["channel"] or _SPECS[code]["channel"])[:40]
    if code == "carrier_driver" and not force:
        use_channel = row["channel"] or _SPECS[code]["channel"]
    _write(conn, package_id, code, status, use_channel, moment)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "ok"}
    return False


def _carrier_code(merged: bool) -> str:
    return "carrier_driver" if merged else "carrier"


def sync_from_package(conn, package: dict, moment: str, hints: dict | None = None) -> None:
    """Подтянуть трек из статуса пакета. Вниз по лестнице сам не опускает."""
    hints = hints or {}
    merged = same_person(conn, package)
    for code in _recipient_codes(merged):
        _ensure(conn, package["id"], code, moment)
    stage = package.get("stage") or ""
    status = package.get("status") or ""
    carrier_code = _carrier_code(merged)
    driver_code = "carrier_driver" if merged else "driver"
    if _stage_at_least(stage, "docs_sent") or status == "otpravleno":
        bump(conn, package["id"], "client", "sent", "email", moment)
    if int(package.get("signed_client") or 0):
        bump(conn, package["id"], "client", "signed", "email", moment)
    if int(package.get("signed_carrier") or 0):
        bump(conn, package["id"], carrier_code, "signed", "email", moment)
    if _stage_at_least(stage, "v_puti"):
        bump(conn, package["id"], driver_code, "sent", "max", moment)
    mirror = clean(hints.get("mirror_status"), 40).lower()
    if mirror in MIRROR_STATUS:
        bump(conn, package["id"], carrier_code, MIRROR_STATUS[mirror], "email", moment)
    if _truthy(hints.get("scan_ok")):
        bump(conn, package["id"], carrier_code, "signed", "email", moment)
    if _truthy(hints.get("kartochka_ready")):
        bump(conn, package["id"], "client", "prepared", "email", moment)


def set_track(conn, package: dict, recipient: str, status: str, channel: str, moment: str) -> None:
    merged = same_person(conn, package)
    allowed = _recipient_codes(merged)
    if recipient not in allowed:
        raise ApiError(422, "Такой строки трека на этой сделке нет.")
    if status not in STATUSES:
        raise ApiError(422, "Статус: подготовлено, отправлено, получено или подписано.")
    channel = clean(channel, 40) or _SPECS[recipient]["channel"]
    bump(conn, package["id"], recipient, status, channel, moment, force=True)


def track_view(db, package: dict) -> dict:
    with db.tx() as conn:
        merged = same_person(conn, package)
        stored = _rows(conn, package["id"])
    codes = _recipient_codes(merged)
    rows = []
    for code in codes:
        spec = _SPECS[code]
        row = stored.get(code)
        if row is None and code == "carrier_driver":
            row = _best(stored.get("carrier"), stored.get("driver"))
        status = (row or {}).get("status") or ""
        channel = (row or {}).get("channel") or spec["channel"]
        moment = (row or {}).get("status_at") or ""
        steps = []
        for item_status in STATUSES:
            steps.append({
                "code": item_status,
                "label": STATUS_LABEL[item_status],
                "on": item_status == status,
            })
        rows.append({
            "recipient": code,
            "title": spec["title"],
            "flow": spec["flow"],
            "status": status,
            "status_label": STATUS_LABEL[status],
            "channel": channel,
            "when": fmt_dt(moment) if moment else "",
            "steps": steps,
        })
    return {"merged": merged, "rows": rows}


def _best(left: dict | None, right: dict | None) -> dict | None:
    if left is None:
        return right
    if right is None:
        return left
    if RANK.get(right.get("status") or "", 0) > RANK.get(left.get("status") or "", 0):
        return right
    return left
