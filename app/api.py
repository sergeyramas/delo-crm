"""Шлюз /s/*. Секрет как inbox_site_secret у AlpStream, только для этого входа."""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.logic import ApiError, apply_patch, create_inbound, detail, list_packages

router = APIRouter()
MIN_SECRET = 16
MAX_BODY = 12 * 1024 * 1024


def _eq(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def inbound_ok(request: Request) -> bool:
    """Секрет шлюза только из заголовка. В query и в теле GET его нет."""
    settings = request.app.state.settings
    secret = settings.inbound_secret
    if len(secret) < MIN_SECRET:
        return False
    header = request.headers.get("x-inbound-secret", "")
    if not header or len(header) != len(secret):
        return False
    return _eq(header, secret)


def require_secret(request: Request, payload: dict | None = None) -> None:
    settings = request.app.state.settings
    if len(settings.inbound_secret) < MIN_SECRET:
        raise ApiError(503, "Шлюз не настроен.")
    header = request.headers.get("x-inbound-secret", "")
    field = ""
    if payload is not None and isinstance(payload.get("secret"), str):
        field = payload["secret"]
    got = header or field
    if not _eq(got, settings.inbound_secret):
        raise ApiError(401, "Неверный секрет.")
    if payload is not None:
        payload.pop("secret", None)


@router.post("/s/inbound/doc")
async def inbound(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(413, "Слишком большой запрос.")
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(422, "Не JSON.") from exc
    else:
        payload = {}
    if not isinstance(payload, dict):
        raise ApiError(422, "Нужен объект JSON.")
    require_secret(request, payload)
    return create_inbound(request.app.state.db, request.app.state.settings, payload)


@router.get("/s/packages")
def packages(
    request: Request,
    status: str | None = None,
    stage: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    require_secret(request)
    return list_packages(
        request.app.state.db,
        status=status or None,
        stage=stage or None,
        limit=limit,
        offset=offset,
    )


@router.get("/s/packages/{key}")
def package_one(request: Request, key: str):
    require_secret(request)
    with request.app.state.db.tx() as conn:
        return detail(conn, key)


@router.patch("/s/packages/{key}")
async def package_patch(request: Request, key: str):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(413, "Слишком большой запрос.")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(422, "Не JSON.") from exc
    if not isinstance(payload, dict):
        raise ApiError(422, "Нужен объект JSON.")
    require_secret(request, payload)
    return apply_patch(request.app.state.db, request.app.state.settings, key, payload)


@router.post("/s/packages/{key}/events")
async def package_event(request: Request, key: str):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(413, "Слишком большой запрос.")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(422, "Не JSON.") from exc
    if not isinstance(payload, dict):
        raise ApiError(422, "Нужен объект JSON.")
    require_secret(request, payload)
    from app.logic import add_note, save_upload
    import base64

    event_type = payload.get("type") or "note"
    attachments = payload.get("attachments") or []
    if attachments:
        # Файл из base64 кладём на диск и пишем событие file, если тип file.
        # Остальные типы сохраняют только имя и url, без скачивания ссылок.
        if event_type == "file" and len(attachments) == 1 and isinstance(attachments[0], dict) and (
            attachments[0].get("content_base64") or attachments[0].get("base64")
        ):
            item = attachments[0]
            raw_b64 = item.get("content_base64") or item.get("base64")
            try:
                blob = base64.b64decode(raw_b64, validate=True)
            except Exception as exc:
                raise ApiError(422, "Не удалось разобрать content_base64.") from exc
            saved = save_upload(
                request.app.state.db,
                key,
                item.get("name") or "file",
                blob,
                item.get("name") or "Файл",
                payload.get("text") or "",
                payload.get("actor") or "",
            )
            return saved
    return add_note(
        request.app.state.db,
        key,
        event_type,
        payload.get("text") or "",
        payload.get("actor") or "",
        payload.get("title") or "",
        {"group": payload.get("group")} if payload.get("group") else None,
        [a for a in attachments if isinstance(a, dict) and not a.get("content_base64") and not a.get("base64")],
    )


@router.get("/api/packages/{key}/client-attachments")
def client_attachments_list(request: Request, key: str):
    from app.scans import client_attachments

    return client_attachments(request.app.state.db, key)


@router.get("/api/packages/{key}/client-attachments/{kind}")
def client_attachments_file(request: Request, key: str, kind: str):
    from app.scans import open_client_attachment

    path, name, media = open_client_attachment(request.app.state.db, key, kind)
    return FileResponse(
        path,
        media_type=media,
        filename=name,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


async def _json(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(413, "Слишком большой запрос.")
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(422, "Не JSON.") from exc
    else:
        payload = {}
    if not isinstance(payload, dict):
        raise ApiError(422, "Нужен объект JSON.")
    require_secret(request, payload)
    return payload


def _pdf_response(blob: bytes):
    from fastapi.responses import Response

    return Response(
        content=blob,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="kartochka-perevozchika.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/s/carriers")
def carriers_list(request: Request, q: str = "", limit: int = 50):
    from app.carriers import list_carriers

    require_secret(request)
    with request.app.state.db.tx() as conn:
        return {"carriers": list_carriers(conn, q, limit)}


@router.post("/s/carriers")
async def carriers_create(request: Request):
    from app.carriers import create_carrier
    from app.domain import now_iso

    payload = await _json(request)
    with request.app.state.db.tx() as conn:
        return create_carrier(conn, payload, now_iso())


@router.get("/s/carriers/{carrier_id}")
def carriers_one(request: Request, carrier_id: int):
    from app.carriers import carrier_detail

    require_secret(request)
    with request.app.state.db.tx() as conn:
        return carrier_detail(conn, carrier_id)


@router.patch("/s/carriers/{carrier_id}")
async def carriers_patch(request: Request, carrier_id: int):
    from app.carriers import update_carrier
    from app.domain import now_iso

    payload = await _json(request)
    with request.app.state.db.tx() as conn:
        return update_carrier(conn, carrier_id, payload, now_iso())


@router.get("/s/carriers/{carrier_id}/vehicles")
def vehicles_list(request: Request, carrier_id: int):
    from app.carriers import carrier_detail

    require_secret(request)
    with request.app.state.db.tx() as conn:
        detail = carrier_detail(conn, carrier_id)
    return {"vehicles": detail["vehicles"]}


@router.post("/s/carriers/{carrier_id}/vehicles")
async def vehicles_create(request: Request, carrier_id: int):
    from app.carriers import create_vehicle
    from app.domain import now_iso

    payload = await _json(request)
    with request.app.state.db.tx() as conn:
        return create_vehicle(conn, carrier_id, payload, now_iso())


@router.patch("/s/carriers/{carrier_id}/vehicles/{vehicle_id}")
async def vehicles_patch(request: Request, carrier_id: int, vehicle_id: int):
    from app.carriers import update_vehicle
    from app.domain import now_iso

    payload = await _json(request)
    with request.app.state.db.tx() as conn:
        return update_vehicle(conn, carrier_id, vehicle_id, payload, now_iso())


@router.get("/s/carriers/{carrier_id}/drivers")
def drivers_list(request: Request, carrier_id: int):
    from app.carriers import carrier_detail

    require_secret(request)
    with request.app.state.db.tx() as conn:
        detail = carrier_detail(conn, carrier_id)
    return {"drivers": detail["drivers"]}


@router.post("/s/carriers/{carrier_id}/drivers")
async def drivers_create(request: Request, carrier_id: int):
    from app.carriers import create_driver
    from app.domain import now_iso

    payload = await _json(request)
    with request.app.state.db.tx() as conn:
        return create_driver(conn, carrier_id, payload, now_iso())


@router.patch("/s/carriers/{carrier_id}/drivers/{driver_id}")
async def drivers_patch(request: Request, carrier_id: int, driver_id: int):
    from app.carriers import update_driver
    from app.domain import now_iso

    payload = await _json(request)
    with request.app.state.db.tx() as conn:
        return update_driver(conn, carrier_id, driver_id, payload, now_iso())


@router.post("/s/carriers/{carrier_id}/files")
async def carriers_file(
    request: Request,
    carrier_id: int,
    kind: str = "doc",
    vehicle_id: int | None = None,
    driver_id: int | None = None,
):
    from app.carriers import save_fleet_file
    from app.domain import now_iso
    from app.scans import MAX_SCAN

    require_secret(request)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        raise ApiError(422, "Пустой файл.")
    blob = await upload.read()
    if len(blob) > MAX_SCAN:
        raise ApiError(413, "Файл больше 12 МБ.")
    file_kind = str(form.get("kind") or kind or "doc")
    vehicle = form.get("vehicle_id") or vehicle_id
    driver = form.get("driver_id") or driver_id
    with request.app.state.db.tx() as conn:
        return save_fleet_file(
            conn,
            request.app.state.db.data_dir,
            carrier_id,
            file_kind,
            getattr(upload, "filename", "") or "",
            blob,
            now_iso(),
            vehicle_id=int(vehicle) if vehicle else None,
            driver_id=int(driver) if driver else None,
        )


@router.post("/s/packages/{key}/assign")
async def package_assign(request: Request, key: str):
    from app.carriers import assign_package, open_package
    from app.domain import now_iso

    payload = await _json(request)

    def opt(name):
        if name not in payload:
            return None
        value = payload.get(name)
        if value in (None, ""):
            return None
        if isinstance(value, bool) or not str(value).strip().isdigit():
            raise ApiError(422, "Некорректный идентификатор.")
        return int(value)

    with request.app.state.db.tx() as conn:
        package = open_package(conn, key)
        return assign_package(conn, package, opt("carrier_id"), opt("vehicle_id"), opt("driver_id"), now_iso())


@router.post("/s/packages/{key}/kartochka.pdf")
async def package_kartochka(request: Request, key: str):
    from app.carriers import assemble_package, open_package
    from app.domain import now_iso

    raw = await request.body()
    payload = {}
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(422, "Не JSON.") from exc
        if not isinstance(payload, dict):
            raise ApiError(422, "Нужен объект JSON.")
    require_secret(request, payload if raw else None)
    if not raw:
        require_secret(request)
    actor = ""
    if isinstance(payload, dict):
        actor = str(payload.get("actor") or "")
    with request.app.state.db.tx() as conn:
        package = open_package(conn, key)
        saved = assemble_package(
            conn,
            request.app.state.db.data_dir,
            package,
            None,
            now_iso(),
            actor or "CRM",
        )
    return _pdf_response(saved["pdf"])


@router.get("/s/packages/{key}/negotiations")
def negotiations_list(request: Request, key: str):
    from app.carriers import list_negotiations, open_package

    require_secret(request)
    with request.app.state.db.tx() as conn:
        package = open_package(conn, key)
        return {"package_id": package["id"], "number": package["number"], "negotiations": list_negotiations(conn, package["id"])}


@router.post("/s/packages/{key}/negotiations")
async def negotiations_add(request: Request, key: str):
    from app.carriers import add_negotiation, open_package
    from app.domain import now_iso

    payload = await _json(request)
    with request.app.state.db.tx() as conn:
        package = open_package(conn, key)
        item = add_negotiation(conn, package["id"], payload.get("text") or "", payload.get("author") or "", now_iso())
    return item
