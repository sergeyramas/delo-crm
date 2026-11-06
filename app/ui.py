"""Экраны списка, воронки, карточки и ленты. Почту наружу не шлёт."""

from __future__ import annotations

import hmac
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.domain import (
    BUCKETS,
    DOC_CLASS,
    FEED_FILTERS,
    KANBAN,
    STAGES,
    STATUSES,
    fmt_dt,
    now_iso,
)
from app.logic import (
    ApiError,
    add_note,
    add_task,
    apply_patch,
    file_path,
    kpis,
    move_task,
    save_upload,
    view_event,
    view_package_row,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def stage_href(stage: str, status: str) -> str:
    params = {}
    if stage:
        params["stage"] = stage
    if status:
        params["status"] = status
    return "/?" + urlencode(params) if params else "/"


templates.env.globals["stage_href"] = stage_href

ERRS = {
    "csrf": "Сессия формы устарела. Обновите страницу и повторите.",
    "otkaz": "Для отказа нужен комментарий.",
    "status": "Неизвестный статус.",
    "stage": "Неизвестная стадия.",
    "file": "Не удалось принять файл.",
    "empty": "Пустая заметка.",
    "task": "Не удалось записать задачу.",
    "fields": "Проверьте поля формы.",
}


def _eq(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def check_csrf(request: Request, got) -> None:
    expected = getattr(request.state, "csrf", "")
    if not got or not expected or not _eq(str(got), expected):
        raise ApiError(403, "csrf")


def latest_id(db) -> int | None:
    row = db.one("SELECT id FROM packages ORDER BY updated_at DESC, id DESC LIMIT 1")
    return int(row["id"]) if row else None


def base_ctx(request: Request, active: str, current_id: int | None = None) -> dict:
    db = request.app.state.db
    if current_id is None:
        current_id = latest_id(db)
    return {
        "request": request,
        "active": active,
        "who": request.app.state.settings.display_name,
        "csrf": request.state.csrf,
        "current_id": current_id,
        "err": ERRS.get(request.query_params.get("err", ""), ""),
        "stages": STAGES,
        "statuses": STATUSES,
    }


def _rows(db) -> list[dict]:
    raw = db.q(
        """
        SELECT p.*, c.name AS client_name, c.contact_name AS client_contact,
               c.email AS client_email, c.phone AS client_phone, c.note AS client_note
        FROM packages p
        LEFT JOIN clients c ON c.id = p.client_id
        ORDER BY p.updated_at DESC, p.id DESC
        """
    )
    return [view_package_row(row) for row in raw]


def _one(db, key: str) -> dict:
    rows = db.q(
        """
        SELECT p.*, c.name AS client_name, c.contact_name AS client_contact,
               c.email AS client_email, c.phone AS client_phone, c.note AS client_note
        FROM packages p
        LEFT JOIN clients c ON c.id = p.client_id
        WHERE p.id = ? OR p.number = ?
        """,
        (int(key) if str(key).isdigit() else -1, str(key)),
    )
    if not rows:
        raise ApiError(404, "Пакет не найден.")
    return view_package_row(rows[0])


def _events(db, package_id: int) -> list[dict]:
    rows = db.q(
        "SELECT * FROM events WHERE package_id = ? ORDER BY created_at, id",
        (package_id,),
    )
    return [view_event(row) for row in rows]


def _docs(db, package_id: int) -> list[dict]:
    rows = db.q("SELECT * FROM documents WHERE package_id = ? ORDER BY id", (package_id,))
    for row in rows:
        row["css"] = DOC_CLASS.get(row["state_label"], "st-podtverzhden")
    return rows


def _tasks(db) -> list[dict]:
    rows = db.q(
        """
        SELECT t.*, p.number, p.stage
        FROM tasks t
        JOIN packages p ON p.id = t.package_id
        ORDER BY t.id
        """
    )
    for row in rows:
        row["stage_class"] = f"st-{row['stage']}"
        row["when"] = row["due_label"]
    return rows


def _redirect(path: str, err: str | None = None) -> RedirectResponse:
    if err:
        join = "&" if "?" in path else "?"
        path = f"{path}{join}err={err}"
    return RedirectResponse(path, status_code=303)


@router.get("/")
def page_list(request: Request):
    db = request.app.state.db
    stage = request.query_params.get("stage", "")
    status = request.query_params.get("status", "")
    rows = _rows(db)
    counts = Counter(row["stage"] for row in rows)
    shown = rows
    if stage:
        shown = [row for row in shown if row["stage"] == stage]
    if status:
        shown = [row for row in shown if row["status"] == status]
    ctx = base_ctx(request, "list")
    ctx.update({
        "rows": shown,
        "all_count": len(rows),
        "counts": counts,
        "kpis": kpis(rows),
        "stage": stage,
        "status": status,
        "today": fmt_dt(now_iso())[:5],
    })
    return templates.TemplateResponse(request, "list.html", ctx)


@router.get("/kanban")
def page_kanban(request: Request):
    db = request.app.state.db
    rows = _rows(db)
    columns = []
    for column in KANBAN:
        cards = [row for row in rows if row["stage"] in column["stages"]]
        cards.sort(key=lambda row: (row["kanban_pos"], row["id"]))
        columns.append({**column, "cards": cards, "count": len(cards)})
    refused = [row for row in rows if row["stage"] == "otkaz" or row["status"] == "otkaz"]
    tasks = _tasks(db)
    buckets = []
    for bucket in BUCKETS:
        items = [task for task in tasks if task["bucket"] == bucket["code"]]
        buckets.append({**bucket, "tasks": items, "count": len(items)})
    ctx = base_ctx(request, "kanban")
    ctx.update({
        "columns": columns,
        "refused": refused,
        "buckets": buckets,
        "bucket_defs": BUCKETS,
        "packages": rows,
        "today": fmt_dt(now_iso())[:5],
    })
    return templates.TemplateResponse(request, "kanban.html", ctx)


@router.get("/packages")
def page_empty(request: Request):
    ctx = base_ctx(request, "card", current_id=None)
    return templates.TemplateResponse(request, "missing.html", ctx)


@router.get("/packages/{key}")
def page_card(request: Request, key: str):
    db = request.app.state.db
    try:
        package = _one(db, key)
    except ApiError:
        ctx = base_ctx(request, "card")
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)
    events = _events(db, package["id"])
    ctx = base_ctx(request, "card", current_id=package["id"])
    ctx.update({
        "p": package,
        "events": events[-8:],
        "event_count": len(events),
        "docs": _docs(db, package["id"]),
    })
    return templates.TemplateResponse(request, "card.html", ctx)


@router.get("/packages/{key}/feed")
def page_feed(request: Request, key: str):
    db = request.app.state.db
    try:
        package = _one(db, key)
    except ApiError:
        ctx = base_ctx(request, "feed")
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)
    kind = request.query_params.get("kind", "all") or "all"
    events = _events(db, package["id"])
    counts = Counter(event["group"] for event in events)
    if kind == "agent":
        visible = [event for event in events if event["group"] in {"agent", "yes"}]
    elif kind == "all":
        visible = events
    else:
        visible = [event for event in events if event["group"] == kind]
    filters = []
    for item in FEED_FILTERS:
        code = item["code"]
        if code == "file" and counts.get("file", 0) == 0 and kind != "file":
            continue
        if code == "all":
            count = len(events)
        elif code == "agent":
            count = counts.get("agent", 0) + counts.get("yes", 0)
        else:
            count = counts.get(code, 0)
        filters.append({**item, "count": count, "on": kind == code})
    ctx = base_ctx(request, "feed", current_id=package["id"])
    ctx.update({"p": package, "events": visible, "filters": filters, "kind": kind})
    return templates.TemplateResponse(request, "feed.html", ctx)


def _form_last(form, name: str) -> str:
    values = form.getlist(name) if hasattr(form, "getlist") else [form.get(name)]
    values = [v for v in values if v is not None]
    return str(values[-1]) if values else ""


@router.post("/packages/{key}/advance")
async def advance(request: Request, key: str):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        package = _one(request.app.state.db, key)
    except ApiError as exc:
        return _redirect(f"/packages/{key}", "csrf" if exc.message == "csrf" else "stage")
    from app.domain import next_stage

    nxt = next_stage(package["stage"])
    if not nxt:
        return _redirect(f"/packages/{package['id']}")
    try:
        apply_patch(
            request.app.state.db,
            request.app.state.settings,
            str(package["id"]),
            {"stage": nxt, "actor": request.app.state.settings.display_name},
        )
    except ApiError:
        return _redirect(f"/packages/{package['id']}", "stage")
    return _redirect(f"/packages/{package['id']}")


@router.post("/packages/{key}/update")
async def update_form(request: Request, key: str):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        package = _one(request.app.state.db, key)
    except ApiError as exc:
        return _redirect(f"/packages/{key}", "csrf" if exc.message == "csrf" else "fields")
    payload = {
        "route_from": _form_last(form, "route_from"),
        "route_to": _form_last(form, "route_to"),
        "cargo": _form_last(form, "cargo"),
        "weight": _form_last(form, "weight"),
        "pallets": _form_last(form, "pallets"),
        "load_date": _form_last(form, "load_date"),
        "rate_rub": _form_last(form, "rate_rub"),
        "carrier_rate_rub": _form_last(form, "carrier_rate_rub"),
        "carrier_name": _form_last(form, "carrier_name"),
        "carrier_email": _form_last(form, "carrier_email"),
        "carrier_contract": _form_last(form, "carrier_contract"),
        "driver_name": _form_last(form, "driver_name"),
        "driver_channel": _form_last(form, "driver_channel"),
        "plate": _form_last(form, "plate"),
        "vehicle": _form_last(form, "vehicle"),
        "gate": _form_last(form, "gate"),
        "flags": _form_last(form, "flags"),
        "decision": _form_last(form, "decision"),
        "list_note": _form_last(form, "list_note"),
        "source_detail": _form_last(form, "source_detail"),
        "hint": _form_last(form, "hint"),
        "client_name": _form_last(form, "client_name"),
        "client_contact": _form_last(form, "client_contact"),
        "client_email": _form_last(form, "client_email"),
        "client_phone": _form_last(form, "client_phone"),
        "client_note": _form_last(form, "client_note"),
        "signed_client": _form_last(form, "signed_client"),
        "signed_carrier": _form_last(form, "signed_carrier"),
        "actor": request.app.state.settings.display_name,
    }
    stage = _form_last(form, "stage")
    status = _form_last(form, "status")
    if stage and stage != package["stage"]:
        payload["stage"] = stage
    if status and status != package["status"]:
        payload["status"] = status
    comment = _form_last(form, "refusal_comment")
    if comment:
        payload["refusal_comment"] = comment
        payload["comment"] = comment
    try:
        apply_patch(request.app.state.db, request.app.state.settings, str(package["id"]), payload)
    except ApiError as exc:
        code = "otkaz" if "отказ" in exc.message.lower() else "fields"
        return _redirect(f"/packages/{package['id']}", code)
    return _redirect(f"/packages/{package['id']}")


@router.post("/packages/{key}/note")
async def note_form(request: Request, key: str):
    form = await request.form()
    back = _form_last(form, "back") or f"/packages/{key}"
    try:
        check_csrf(request, form.get("csrf"))
        package = _one(request.app.state.db, key)
        add_note(
            request.app.state.db,
            str(package["id"]),
            "note",
            _form_last(form, "text"),
            request.app.state.settings.display_name,
            _form_last(form, "title") or "Заметка",
            {"group": "agent"},
        )
    except ApiError as exc:
        code = "csrf" if exc.message == "csrf" else "empty"
        return _redirect(back if back.startswith("/packages/") else f"/packages/{key}", code)
    if not back.startswith("/packages/"):
        back = f"/packages/{package['id']}"
    return _redirect(back)


@router.post("/packages/{key}/file")
async def file_form(request: Request, key: str):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        package = _one(request.app.state.db, key)
        upload = form.get("file")
        blob = await upload.read() if upload is not None and getattr(upload, "filename", "") else b""
        if not blob:
            raise ApiError(422, "file")
        save_upload(
            request.app.state.db,
            str(package["id"]),
            getattr(upload, "filename", "") or "file",
            blob,
            _form_last(form, "title"),
            _form_last(form, "note"),
            request.app.state.settings.display_name,
        )
    except ApiError as exc:
        code = "csrf" if exc.message == "csrf" else "file"
        return _redirect(f"/packages/{key}", code)
    return _redirect(f"/packages/{package['id']}")


@router.post("/tasks")
async def task_form(request: Request):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        add_task(
            request.app.state.db,
            _form_last(form, "package_id"),
            _form_last(form, "title"),
            _form_last(form, "bucket") or "today",
            _form_last(form, "due_label"),
            _form_last(form, "assignee"),
        )
    except ApiError as exc:
        code = "csrf" if exc.message == "csrf" else "task"
        return _redirect("/kanban", code)
    return _redirect("/kanban")


@router.post("/tasks/{task_id}/bucket")
async def task_bucket(request: Request, task_id: int):
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        move_task(request.app.state.db, task_id, _form_last(form, "bucket"))
    except ApiError as exc:
        code = "csrf" if exc.message == "csrf" else "task"
        return _redirect("/kanban", code)
    return _redirect("/kanban")


@router.get("/files/{file_id}")
def download(request: Request, file_id: int):
    try:
        path, name = file_path(request.app.state.db, file_id)
    except ApiError:
        ctx = base_ctx(request, "card")
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)
    return FileResponse(path, filename=name, content_disposition_type="attachment")


def list_query(stage: str, status: str, **extra) -> str:
    params = {}
    if stage:
        params["stage"] = stage
    if status:
        params["status"] = status
    params.update(extra)
    return "/?" + urlencode(params) if params else "/"
