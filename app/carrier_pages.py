"""Экраны раздела «Перевозчики», связка в заявке, переговоры и трекер."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.carriers import (
    add_negotiation,
    assemble_package,
    assign_package,
    build_for,
    carrier_detail,
    create_carrier,
    create_driver,
    create_vehicle,
    list_clients,
    list_drivers,
    list_vehicles,
    save_client_file,
    delete_carrier,
    delete_driver,
    delete_vehicle,
    fleet_file_path,
    list_carriers,
    list_negotiations,
    load_expeditor,
    open_package,
    save_expeditor,
    save_fleet_file,
    unlink_stored,
    update_carrier,
    update_driver,
    update_vehicle,
)
from app.domain import fmt_dt, now_iso
from app.logic import ApiError
from app.ui import (
    _form_last,
    _one,
    _read_upload,
    _redirect,
    actor_name,
    base_ctx,
    check_csrf,
    require_admin,
    require_writer,
    templates,
)

router = APIRouter()


def _fail(exc: ApiError, path: str, fallback: str):
    if exc.message == "csrf":
        return _redirect(path, "csrf")
    if exc.status == 403 or exc.message == "role":
        return _redirect(path, "role")
    text = exc.message or ""
    if exc.status == 413 or "PDF" in text or "файл" in text.lower() or "Файл" in text:
        return _redirect(path, "scan")
    return _redirect(path, fallback)


def _user(request: Request):
    return getattr(request.state, "user", None)


def safe_next(value: str) -> str:
    raw = (value or "").strip()
    if not raw.startswith("/") or raw.startswith("//") or "\\" in raw or "://" in raw:
        return ""
    path = raw.split("?", 1)[0].split("#", 1)[0]
    if path in {"/carriers", "/vehicles", "/drivers", "/clients"}:
        return path
    for prefix in ("/packages/", "/carriers/"):
        if path.startswith(prefix) and path.removeprefix(prefix).isdigit():
            return path
    return ""


async def _maybe_file(form, name: str) -> tuple[str, bytes]:
    upload = form.get(name)
    if upload is None or not getattr(upload, "filename", ""):
        return "", b""
    return getattr(upload, "filename", "") or "file", await _read_upload(upload)


def _after(conn, form, kind: str, new_id: int, carrier_id: int | None, moment: str) -> str:
    from app.carriers import attach_newcomer

    nxt = safe_next(_form_last(form, "next"))
    if nxt.startswith("/packages/"):
        package = open_package(conn, nxt.rsplit("/", 1)[-1])
        attach_newcomer(conn, package, kind, new_id, moment)
        return nxt + "?note=saved#haul"
    if nxt.startswith("/carriers/") and kind in {"vehicle", "driver"}:
        tab = "drivers" if kind == "driver" else "vehicles"
        return f"{nxt}?tab={tab}&note=saved"
    if kind == "carrier":
        return f"/carriers/{new_id}?note=saved"
    if kind == "vehicle":
        return f"/carriers/{carrier_id}?tab=vehicles&note=saved"
    if kind == "driver":
        return f"/carriers/{carrier_id}?tab=drivers&note=saved"
    return "/clients?note=saved"


def _opt(form, name: str):
    raw = _form_last(form, name).strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ApiError(422, "Некорректный идентификатор.")
    return int(raw)


@router.get("/carriers")
def page_carriers(request: Request):
    query = request.query_params.get("q", "")
    with request.app.state.db.tx() as conn:
        rows = list_carriers(conn, query, limit=50)
        expeditor = load_expeditor(conn)
    ctx = base_ctx(request, "carriers")
    ctx.update({"rows": rows, "q": query, "expeditor": expeditor})
    return templates.TemplateResponse(request, "carriers.html", ctx)


@router.get("/carriers/new")
def page_carrier_new(request: Request):
    ctx = base_ctx(request, "carriers")
    ctx.update({"carrier": None, "next": safe_next(request.query_params.get("next", ""))})
    return templates.TemplateResponse(request, "carrier_form.html", ctx)


@router.post("/carriers")
async def page_carrier_create(request: Request):
    form = await request.form()
    back = "/carriers/new"
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        filename, blob = await _maybe_file(form, "doc")
        moment = now_iso()
        with request.app.state.db.tx() as conn:
            saved = create_carrier(conn, _carrier_payload(form), moment)
            if blob:
                save_fleet_file(
                    conn, request.app.state.db.data_dir, saved["id"], "doc", filename, blob, moment,
                )
            target = _after(conn, form, "carrier", saved["id"], saved["id"], moment)
    except ApiError as exc:
        return _fail(exc, back, "carrier")
    return _redirect(target)


@router.get("/vehicles")
def page_vehicles(request: Request):
    query = request.query_params.get("q", "")
    with request.app.state.db.tx() as conn:
        rows = list_vehicles(conn, query)
    ctx = base_ctx(request, "carriers")
    ctx.update({"rows": rows, "q": query, "title": "Машины", "add_href": "/vehicles/new", "add_label": "+ Добавить машину"})
    return templates.TemplateResponse(request, "fleet_list.html", ctx)


@router.get("/vehicles/new")
def page_vehicle_new(request: Request):
    carrier_id = request.query_params.get("carrier_id", "")
    with request.app.state.db.tx() as conn:
        carriers = list_carriers(conn, "", limit=100)
    ctx = base_ctx(request, "carriers")
    ctx.update({
        "carriers": carriers,
        "carrier_id": int(carrier_id) if str(carrier_id).isdigit() else None,
        "next": safe_next(request.query_params.get("next", "")),
    })
    return templates.TemplateResponse(request, "vehicle_new.html", ctx)


@router.post("/vehicles")
async def page_vehicle_new_save(request: Request):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        carrier_id = _opt(form, "carrier_id")
        if not carrier_id:
            raise ApiError(422, "Выберите перевозчика.")
        filename, blob = await _maybe_file(form, "sts")
        moment = now_iso()
        with request.app.state.db.tx() as conn:
            saved = create_vehicle(conn, carrier_id, _vehicle_payload(form), moment)
            if blob:
                save_fleet_file(
                    conn, request.app.state.db.data_dir, carrier_id, "sts", filename, blob, moment,
                    vehicle_id=saved["id"],
                )
            target = _after(conn, form, "vehicle", saved["id"], carrier_id, moment)
    except ApiError as exc:
        return _fail(exc, "/vehicles/new", "fields")
    return _redirect(target)


@router.get("/drivers")
def page_drivers(request: Request):
    query = request.query_params.get("q", "")
    with request.app.state.db.tx() as conn:
        rows = list_drivers(conn, query)
    ctx = base_ctx(request, "carriers")
    ctx.update({"rows": rows, "q": query, "title": "Водители", "add_href": "/drivers/new", "add_label": "+ Добавить водителя"})
    return templates.TemplateResponse(request, "fleet_list.html", ctx)


@router.get("/drivers/new")
def page_driver_new(request: Request):
    carrier_id = request.query_params.get("carrier_id", "")
    with request.app.state.db.tx() as conn:
        carriers = list_carriers(conn, "", limit=100)
    ctx = base_ctx(request, "carriers")
    ctx.update({
        "carriers": carriers,
        "carrier_id": int(carrier_id) if str(carrier_id).isdigit() else None,
        "next": safe_next(request.query_params.get("next", "")),
    })
    return templates.TemplateResponse(request, "driver_new.html", ctx)


@router.post("/drivers")
async def page_driver_new_save(request: Request):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        carrier_id = _opt(form, "carrier_id")
        if not carrier_id:
            raise ApiError(422, "Выберите перевозчика.")
        passport_name, passport = await _maybe_file(form, "passport")
        license_name, license_blob = await _maybe_file(form, "license")
        moment = now_iso()
        with request.app.state.db.tx() as conn:
            saved = create_driver(conn, carrier_id, _driver_payload(form), moment)
            if passport:
                save_fleet_file(
                    conn, request.app.state.db.data_dir, carrier_id, "passport", passport_name, passport, moment,
                    driver_id=saved["id"],
                )
            if license_blob:
                save_fleet_file(
                    conn, request.app.state.db.data_dir, carrier_id, "license", license_name, license_blob, moment,
                    driver_id=saved["id"],
                )
            target = _after(conn, form, "driver", saved["id"], carrier_id, moment)
    except ApiError as exc:
        return _fail(exc, "/drivers/new", "fields")
    return _redirect(target)


@router.get("/clients")
def page_clients(request: Request):
    query = request.query_params.get("q", "")
    with request.app.state.db.tx() as conn:
        rows = list_clients(conn, query)
    ctx = base_ctx(request, "carriers")
    ctx.update({"rows": rows, "q": query})
    return templates.TemplateResponse(request, "clients.html", ctx)


@router.get("/clients/new")
def page_client_new(request: Request):
    ctx = base_ctx(request, "carriers")
    ctx.update({"next": safe_next(request.query_params.get("next", ""))})
    return templates.TemplateResponse(request, "client_form.html", ctx)


@router.post("/clients")
async def page_client_create(request: Request):
    from app.parties import upsert_party

    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        name = _form_last(form, "name").strip()
        if not name:
            raise ApiError(422, "Нужно наименование заказчика.")
        filename, blob = await _maybe_file(form, "doc")
        moment = now_iso()
        with request.app.state.db.tx() as conn:
            party_id = upsert_party(
                conn,
                request.app.state.settings,
                "client",
                {
                    "name": name,
                    "contact_name": _form_last(form, "contact_name"),
                    "company": _form_last(form, "company") or name,
                    "phone": _form_last(form, "phone"),
                    "email": _form_last(form, "email"),
                    "inn": _form_last(form, "inn"),
                    "note": _form_last(form, "note"),
                },
                moment,
            )
            if not party_id:
                raise ApiError(422, "Не удалось записать заказчика.")
            if blob:
                save_client_file(conn, request.app.state.db.data_dir, party_id, filename, blob, moment)
            target = _after(conn, form, "client", party_id, None, moment)
    except ApiError as exc:
        return _fail(exc, "/clients/new", "fields")
    return _redirect(target)


@router.post("/packages/{key}/client")
async def page_client_pick(request: Request, key: str):
    from app.carriers import attach_newcomer

    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        client_id = _opt(form, "client_id")
        if not client_id:
            raise ApiError(422, "Выберите заказчика.")
        package = _one(request.app.state.db, key)
        with request.app.state.db.tx() as conn:
            raw = open_package(conn, str(package["id"]))
            attach_newcomer(conn, raw, "client", client_id, now_iso())
    except ApiError as exc:
        return _fail(exc, f"/packages/{key}", "haul")
    return _redirect(f"/packages/{package['id']}?note=assigned#haul")


@router.get("/carriers/expeditor")
def page_expeditor(request: Request):
    with request.app.state.db.tx() as conn:
        expeditor = load_expeditor(conn)
    ctx = base_ctx(request, "carriers")
    ctx.update({"expeditor": expeditor})
    return templates.TemplateResponse(request, "expeditor.html", ctx)


@router.post("/carriers/expeditor")
async def page_expeditor_save(request: Request):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_admin(request)
        payload = {key: _form_last(form, key) for key in (
            "name", "inn", "ogrnip", "address", "bank", "account", "bik", "corr_account",
            "phone", "email", "dispatcher_name", "dispatcher_phone", "ati_code", "edo_id",
        )}
        with request.app.state.db.tx() as conn:
            save_expeditor(conn, payload, now_iso())
    except ApiError as exc:
        return _fail(exc, "/carriers/expeditor", "fields")
    return _redirect("/carriers/expeditor?note=saved")


@router.get("/carriers/{carrier_id}")
def page_carrier(request: Request, carrier_id: int):
    tab = request.query_params.get("tab", "vehicles")
    if tab not in {"vehicles", "drivers", "docs"}:
        tab = "vehicles"
    try:
        with request.app.state.db.tx() as conn:
            carrier = carrier_detail(conn, carrier_id)
    except ApiError:
        ctx = base_ctx(request, "carriers")
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)
    ctx = base_ctx(request, "carriers")
    ctx.update({"carrier": carrier, "tab": tab})
    return templates.TemplateResponse(request, "carrier.html", ctx)


@router.post("/carriers/{carrier_id}")
async def page_carrier_save(request: Request, carrier_id: int):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        with request.app.state.db.tx() as conn:
            update_carrier(conn, carrier_id, _carrier_payload(form), now_iso())
    except ApiError as exc:
        return _fail(exc, f"/carriers/{carrier_id}", "carrier")
    return _redirect(f"/carriers/{carrier_id}?note=saved")


@router.post("/carriers/{carrier_id}/delete")
async def page_carrier_delete(request: Request, carrier_id: int):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        with request.app.state.db.tx() as conn:
            names = delete_carrier(conn, request.app.state.db.data_dir, carrier_id)
        unlink_stored(request.app.state.db.data_dir, names)
    except ApiError as exc:
        return _fail(exc, f"/carriers/{carrier_id}", "carrier")
    return _redirect("/carriers?note=saved")


@router.post("/carriers/{carrier_id}/vehicles")
async def page_vehicle_create(request: Request, carrier_id: int):
    form = await request.form()
    back = f"/carriers/{carrier_id}?tab=vehicles"
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        with request.app.state.db.tx() as conn:
            create_vehicle(conn, carrier_id, _vehicle_payload(form), now_iso())
    except ApiError as exc:
        return _fail(exc, back, "fields")
    return _redirect(back + "&note=saved")


@router.post("/carriers/{carrier_id}/vehicles/{vehicle_id}")
async def page_vehicle_save(request: Request, carrier_id: int, vehicle_id: int):
    form = await request.form()
    back = f"/carriers/{carrier_id}?tab=vehicles"
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        with request.app.state.db.tx() as conn:
            update_vehicle(conn, carrier_id, vehicle_id, _vehicle_payload(form), now_iso())
    except ApiError as exc:
        return _fail(exc, back, "fields")
    return _redirect(back + "&note=saved")


@router.post("/carriers/{carrier_id}/vehicles/{vehicle_id}/delete")
async def page_vehicle_delete(request: Request, carrier_id: int, vehicle_id: int):
    form = await request.form()
    back = f"/carriers/{carrier_id}?tab=vehicles"
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        with request.app.state.db.tx() as conn:
            names = delete_vehicle(conn, request.app.state.db.data_dir, carrier_id, vehicle_id)
        unlink_stored(request.app.state.db.data_dir, names)
    except ApiError as exc:
        return _fail(exc, back, "fields")
    return _redirect(back + "&note=saved")


@router.post("/carriers/{carrier_id}/drivers")
async def page_driver_create(request: Request, carrier_id: int):
    form = await request.form()
    back = f"/carriers/{carrier_id}?tab=drivers"
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        with request.app.state.db.tx() as conn:
            create_driver(conn, carrier_id, _driver_payload(form), now_iso())
    except ApiError as exc:
        return _fail(exc, back, "fields")
    return _redirect(back + "&note=saved")


@router.post("/carriers/{carrier_id}/drivers/{driver_id}")
async def page_driver_save(request: Request, carrier_id: int, driver_id: int):
    form = await request.form()
    back = f"/carriers/{carrier_id}?tab=drivers"
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        with request.app.state.db.tx() as conn:
            update_driver(conn, carrier_id, driver_id, _driver_payload(form), now_iso())
    except ApiError as exc:
        return _fail(exc, back, "fields")
    return _redirect(back + "&note=saved")


@router.post("/carriers/{carrier_id}/drivers/{driver_id}/delete")
async def page_driver_delete(request: Request, carrier_id: int, driver_id: int):
    form = await request.form()
    back = f"/carriers/{carrier_id}?tab=drivers"
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        with request.app.state.db.tx() as conn:
            names = delete_driver(conn, request.app.state.db.data_dir, carrier_id, driver_id)
        unlink_stored(request.app.state.db.data_dir, names)
    except ApiError as exc:
        return _fail(exc, back, "fields")
    return _redirect(back + "&note=saved")


@router.post("/carriers/{carrier_id}/files")
async def page_fleet_upload(request: Request, carrier_id: int):
    form = await request.form()
    kind = _form_last(form, "kind") or "doc"
    tab = "docs" if kind == "doc" else ("vehicles" if kind == "sts" else "drivers")
    back = f"/carriers/{carrier_id}?tab={tab}"
    upload = form.get("file")
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        blob = await _read_upload(upload)
        with request.app.state.db.tx() as conn:
            save_fleet_file(
                conn,
                request.app.state.db.data_dir,
                carrier_id,
                kind,
                getattr(upload, "filename", "") or "",
                blob,
                now_iso(),
                vehicle_id=_opt(form, "vehicle_id"),
                driver_id=_opt(form, "driver_id"),
            )
    except ApiError as exc:
        return _fail(exc, back, "scan")
    return _redirect(back + "&note=saved")


@router.get("/carriers/{carrier_id}/kartochka.pdf")
def page_kartochka(request: Request, carrier_id: int, vehicle_id: int = 0, driver_id: int = 0):
    try:
        with request.app.state.db.tx() as conn:
            detail = carrier_detail(conn, carrier_id)
            vid = vehicle_id or (detail["vehicles"][0]["id"] if len(detail["vehicles"]) == 1 else 0)
            did = driver_id or (detail["drivers"][0]["id"] if len(detail["drivers"]) == 1 else 0)
            if not vid or not did:
                raise ApiError(422, "Выберите ТС и водителя.")
            blob = build_for(conn, carrier_id, vid, did, _user(request))
    except ApiError:
        return _redirect(f"/carriers/{carrier_id}", "gap")
    return Response(
        content=blob,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="kartochka-perevozchika.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/fleet-files/{file_id}")
def page_fleet_file(request: Request, file_id: int):
    try:
        path, name, media = fleet_file_path(request.app.state.db, file_id)
    except ApiError:
        ctx = base_ctx(request, "carriers")
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)
    from fastapi.responses import FileResponse

    return FileResponse(
        path,
        media_type=media,
        filename=name,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/packages/{key}/assign")
async def page_assign(request: Request, key: str):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        package = _one(request.app.state.db, key)
        with request.app.state.db.tx() as conn:
            raw = open_package(conn, str(package["id"]))
            assign_package(conn, raw, _opt(form, "carrier_id"), _opt(form, "vehicle_id"), _opt(form, "driver_id"), now_iso())
    except ApiError as exc:
        target = f"/packages/{key}"
        return _fail(exc, target, "haul")
    haul = _form_last(form, "haul")
    suffix = f"?haul={haul}&note=assigned" if haul else "?note=assigned"
    return _redirect(f"/packages/{package['id']}{suffix}#haul")


@router.post("/packages/{key}/assemble")
async def page_assemble(request: Request, key: str):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        package = _one(request.app.state.db, key)
        with request.app.state.db.tx() as conn:
            raw = open_package(conn, str(package["id"]))
            assemble_package(conn, request.app.state.db.data_dir, raw, _user(request), now_iso(), actor_name(request))
    except ApiError as exc:
        code = "gap" if exc.status == 422 else "haul"
        return _fail(exc, f"/packages/{key}", code)
    return _redirect(f"/packages/{package['id']}?note=assembled#haul")


@router.post("/packages/{key}/tracks")
async def page_track(request: Request, key: str):
    from app.tracks import set_track

    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        package = _one(request.app.state.db, key)
        with request.app.state.db.tx() as conn:
            raw = open_package(conn, str(package["id"]))
            set_track(conn, raw, _form_last(form, "recipient"), _form_last(form, "status"), _form_last(form, "channel"), now_iso())
    except ApiError as exc:
        return _fail(exc, f"/packages/{key}", "fields")
    return _redirect(f"/packages/{package['id']}?note=saved#doc-tracks")


@router.get("/packages/{key}/negotiations")
def page_negotiations(request: Request, key: str):
    try:
        package = _one(request.app.state.db, key)
    except ApiError:
        ctx = base_ctx(request, "card")
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)
    with request.app.state.db.tx() as conn:
        notes = list_negotiations(conn, package["id"])
    for note in notes:
        note["when"] = fmt_dt(note["created_at"])
    ctx = base_ctx(request, "card", current_id=package["id"])
    ctx.update({"p": package, "notes": notes})
    return templates.TemplateResponse(request, "negotiations.html", ctx)


@router.post("/packages/{key}/negotiations")
async def page_negotiations_add(request: Request, key: str):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        require_writer(request)
        package = _one(request.app.state.db, key)
        with request.app.state.db.tx() as conn:
            raw = open_package(conn, str(package["id"]))
            add_negotiation(conn, raw["id"], _form_last(form, "text"), actor_name(request), now_iso())
    except ApiError as exc:
        return _fail(exc, f"/packages/{key}/negotiations", "empty")
    return _redirect(f"/packages/{package['id']}/negotiations?note=nego")


def _carrier_payload(form) -> dict:
    return {
        "name": _form_last(form, "name"),
        "kind": _form_last(form, "kind"),
        "inn": _form_last(form, "inn"),
        "ogrn": _form_last(form, "ogrn"),
        "phone": _form_last(form, "phone"),
        "email": _form_last(form, "email"),
        "max_contact": _form_last(form, "max_contact"),
        "tg_contact": _form_last(form, "tg_contact"),
        "wa_contact": _form_last(form, "wa_contact"),
        "note": _form_last(form, "note"),
    }


def _vehicle_payload(form) -> dict:
    return {
        "make_model": _form_last(form, "make_model"),
        "body": _form_last(form, "body"),
        "volume": _form_last(form, "volume"),
        "loading": _form_last(form, "loading"),
        "plate": _form_last(form, "plate"),
        "trailer_plate": _form_last(form, "trailer_plate"),
        "vin": _form_last(form, "vin"),
        "sts_series": _form_last(form, "sts_series"),
        "sts_number": _form_last(form, "sts_number"),
    }


def _driver_payload(form) -> dict:
    return {
        "name": _form_last(form, "name"),
        "birth_date": _form_last(form, "birth_date"),
        "phone": _form_last(form, "phone"),
        "passport_series": _form_last(form, "passport_series"),
        "passport_number": _form_last(form, "passport_number"),
        "passport_issued_by": _form_last(form, "passport_issued_by"),
        "passport_issued_at": _form_last(form, "passport_issued_at"),
        "passport_dept_code": _form_last(form, "passport_dept_code"),
        "license_series": _form_last(form, "license_series"),
        "license_number": _form_last(form, "license_number"),
        "license_issued_at": _form_last(form, "license_issued_at"),
        "license_issued_by": _form_last(form, "license_issued_by"),
        "license_valid_until": _form_last(form, "license_valid_until"),
        "license_categories": _form_last(form, "license_categories"),
        "consent": _form_last(form, "consent"),
        "user_is_carrier": _form_last(form, "user_is_carrier"),
    }
