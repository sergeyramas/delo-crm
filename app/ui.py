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
    FEED_CAP,
    FEED_FILTERS,
    KANBAN,
    KANBAN_CAP,
    LIST_PAGE_SIZE,
    STAGES,
    STATUSES,
    TASK_CAP,
    fmt_dt,
    now_iso,
)
from app.auth import role_label
from app.logic import (
    ApiError,
    add_note,
    add_task,
    apply_patch,
    file_path,
    move_task,
    package_stats,
    save_upload,
    view_event,
    view_package_row,
)
from app.messengers import mode_note
from app.parties import (
    KIND_LABEL,
    MS_LABEL,
    TG_ACTOR,
    load_party,
    party_deals,
    party_messages,
    public_party,
    recheck_party,
    record_outbound,
    telegram_signature,
    whatsapp_href,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def stage_href(stage: str, status: str, page: int = 1) -> str:
    params = {}
    if stage:
        params["stage"] = stage
    if status:
        params["status"] = status
    if page > 1:
        params["page"] = str(page)
    return "/?" + urlencode(params) if params else "/"


def _page_num(raw: str) -> int:
    if not raw or not str(raw).isdigit():
        return 1
    page = int(raw)
    if page < 1:
        return 1
    if page > 10000:
        return 10000
    return page


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
    "confirm": "Отправка только после подтверждения. Сообщение не ушло.",
    "write": "Не удалось записать сообщение.",
    "party": "Карточка не найдена.",
}

WROTE = {
    "local": "Текст записан в CRM от имени «Ассистент Андрея». В Telegram он не уходил: это заглушка.",
    "not_sent": "Наружу ничего не ушло. Черновик лежит в этой карточке.",
    "checked": "Отметки мессенджеров обновлены.",
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


def actor_name(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user:
        return user["display_name"]
    return request.app.state.settings.display_name


def base_ctx(request: Request, active: str, current_id: int | None = None) -> dict:
    db = request.app.state.db
    if current_id is None:
        current_id = latest_id(db)
    user = getattr(request.state, "user", None)
    if user:
        who = f"{user['display_name']} · {role_label(user['role_code'])}"
    else:
        who = request.app.state.settings.display_name
    return {
        "request": request,
        "active": active,
        "who": who,
        "csrf": request.state.csrf,
        "current_id": current_id,
        "err": ERRS.get(request.query_params.get("err", ""), ""),
        "note": WROTE.get(request.query_params.get("note", ""), ""),
        "stages": STAGES,
        "statuses": STATUSES,
    }


PACKAGE_FROM = """
    FROM packages p
    LEFT JOIN parties c ON c.id = p.client_id AND c.kind = 'client'
    LEFT JOIN clients cl ON cl.id = p.client_id
    LEFT JOIN parties ca ON ca.id = p.carrier_party_id
    LEFT JOIN parties dr ON dr.id = p.driver_party_id
"""

PACKAGE_SELECT = f"""
    SELECT p.*,
           COALESCE(c.name, cl.name, '') AS client_name,
           COALESCE(c.contact_name, cl.contact_name, '') AS client_contact,
           COALESCE(c.email, cl.email, '') AS client_email,
           COALESCE(c.phone, cl.phone, '') AS client_phone,
           COALESCE(c.note, cl.note, '') AS client_note,
           COALESCE(c.inn, '') AS client_inn,
           COALESCE(c.company, '') AS client_company,
           c.id AS client_party_id,
           ca.phone AS carrier_phone,
           ca.inn AS carrier_inn,
           ca.company AS carrier_company,
           dr.phone AS driver_phone,
           dr.email AS driver_email,
           COALESCE(dr.license_no, '') AS license_no,
           COALESCE(dr.passport_no, '') AS passport_no
    {PACKAGE_FROM}
"""


def _rows(db, stage: str = "", status: str = "", limit: int = LIST_PAGE_SIZE, offset: int = 0) -> tuple[list[dict], int]:
    where = []
    args: list = []
    if stage:
        where.append("p.stage = ?")
        args.append(stage)
    if status:
        where.append("p.status = ?")
        args.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.one(f"SELECT COUNT(*) AS n FROM packages p{clause}", tuple(args))["n"]
    raw = db.q(
        PACKAGE_SELECT + clause + " ORDER BY p.updated_at DESC, p.id DESC LIMIT ? OFFSET ?",
        tuple(args) + (int(limit), int(offset)),
    )
    return [view_package_row(row) for row in raw], int(total)


def _one(db, key: str) -> dict:
    rows = db.q(
        PACKAGE_SELECT + " WHERE p.id = ? OR p.number = ?",
        (int(key) if str(key).isdigit() else -1, str(key)),
    )
    if not rows:
        raise ApiError(404, "Пакет не найден.")
    return view_package_row(rows[0])


def _events(db, package_id: int, limit: int = FEED_CAP) -> tuple[list[dict], int]:
    total = db.one("SELECT COUNT(*) AS n FROM events WHERE package_id = ?", (package_id,))["n"]
    rows = db.q(
        """
        SELECT * FROM (
            SELECT * FROM events WHERE package_id = ? ORDER BY id DESC LIMIT ?
        ) ORDER BY created_at, id
        """,
        (package_id, limit),
    )
    return [view_event(row) for row in rows], int(total)


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
        ORDER BY t.id DESC
        LIMIT ?
        """,
        (TASK_CAP,),
    )
    rows.sort(key=lambda row: row["id"])
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
    page = _page_num(request.query_params.get("page", ""))
    offset = (page - 1) * LIST_PAGE_SIZE
    rows, total = _rows(db, stage=stage, status=status, limit=LIST_PAGE_SIZE, offset=offset)
    pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    if page > pages:
        page = pages
        offset = (page - 1) * LIST_PAGE_SIZE
        rows, total = _rows(db, stage=stage, status=status, limit=LIST_PAGE_SIZE, offset=offset)
    stats = package_stats(db)
    ctx = base_ctx(request, "list")
    ctx.update({
        "rows": rows,
        "all_count": stats["all_count"],
        "counts": stats["counts"],
        "kpis": stats["kpis"],
        "stage": stage,
        "status": status,
        "today": fmt_dt(now_iso())[:5],
        "pager": {
            "page": page,
            "pages": pages,
            "start": offset + 1 if rows else 0,
            "stop": offset + len(rows),
            "total": total,
            "prev": stage_href(stage, status, page - 1) if page > 1 else "",
            "next": stage_href(stage, status, page + 1) if page < pages else "",
        },
    })
    return templates.TemplateResponse(request, "list.html", ctx)


@router.get("/kanban")
def page_kanban(request: Request):
    db = request.app.state.db
    rows, board_total = _rows(db, limit=KANBAN_CAP, offset=0)
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
        "board_truncated": board_total > len(rows),
        "board_total": board_total,
        "board_cap": KANBAN_CAP,
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
    events, event_count = _events(db, package["id"])
    ctx = base_ctx(request, "card", current_id=package["id"])
    ctx.update({
        "p": package,
        "events": events[-8:],
        "event_count": event_count,
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
    events, event_total = _events(db, package["id"])
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
    ctx.update({
        "p": package,
        "events": visible,
        "filters": filters,
        "kind": kind,
        "feed_truncated": event_total > len(events),
        "feed_cap": FEED_CAP,
    })
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
            {"stage": nxt, "actor": actor_name(request)},
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
        "client_inn": _form_last(form, "client_inn"),
        "client_company": _form_last(form, "client_company"),
        "client_note": _form_last(form, "client_note"),
        "carrier_phone": _form_last(form, "carrier_phone"),
        "carrier_inn": _form_last(form, "carrier_inn"),
        "driver_phone": _form_last(form, "driver_phone"),
        "driver_email": _form_last(form, "driver_email"),
        "license_no": _form_last(form, "license_no"),
        "passport_no": _form_last(form, "passport_no"),
        "signed_client": _form_last(form, "signed_client"),
        "signed_carrier": _form_last(form, "signed_carrier"),
        "actor": actor_name(request),
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
            actor_name(request),
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
            actor_name(request),
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


def _party_or_404(request: Request, party_id: int):
    try:
        with request.app.state.db.tx() as conn:
            row = load_party(conn, party_id)
            deals, deal_total = party_deals(conn, row)
            messages = party_messages(conn, party_id)
            signature = telegram_signature(conn, party_id) if row else ""
    except ApiError:
        return None
    return row, deals, deal_total, messages, signature


@router.get("/parties/{party_id}")
def page_party(request: Request, party_id: int):
    loaded = _party_or_404(request, party_id)
    if loaded is None:
        ctx = base_ctx(request, "card")
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)
    row, deals, deal_total, messages, _signature = loaded
    party = public_party(row)
    back = request.query_params.get("from", "")
    back_id = int(back) if back.isdigit() else None
    ctx = base_ctx(request, "card", current_id=back_id)
    ctx.update({
        "party": party,
        "kind_label": KIND_LABEL.get(party["kind"], "Карточка"),
        "deals": deals,
        "deal_total": deal_total,
        "messages": messages,
        "ms": MS_LABEL,
        "wa_href": whatsapp_href(row["phone_key"]),
        "messenger_note": mode_note(request.app.state.settings),
        "back_id": back_id,
        "tg_actor": TG_ACTOR,
    })
    return templates.TemplateResponse(request, "party.html", ctx)


@router.post("/parties/{party_id}/messengers")
async def party_recheck(request: Request, party_id: int):
    form = await request.form()
    back = _form_last(form, "from")
    suffix = f"?from={back}" if back.isdigit() else ""
    try:
        check_csrf(request, form.get("csrf"))
        recheck_party(request.app.state.db, request.app.state.settings, party_id)
    except ApiError as exc:
        code = "csrf" if exc.message == "csrf" else "party"
        return _redirect(f"/parties/{party_id}{suffix}", code)
    join = "&" if suffix else "?"
    return _redirect(f"/parties/{party_id}{suffix}{join}note=checked")


@router.get("/parties/{party_id}/write")
def page_write(request: Request, party_id: int):
    channel = request.query_params.get("channel", "tg")
    if channel not in {"tg", "max"}:
        return _redirect(f"/parties/{party_id}", "write")
    loaded = _party_or_404(request, party_id)
    if loaded is None:
        ctx = base_ctx(request, "card")
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)
    row, _deals, _deal_total, _messages, signature = loaded
    back = request.query_params.get("from", "")
    back_id = int(back) if back.isdigit() else None
    ctx = base_ctx(request, "card", current_id=back_id)
    ctx.update({
        "party": public_party(row),
        "channel": channel,
        "signature": signature if channel == "tg" else "",
        "tg_actor": TG_ACTOR,
        "back_id": back_id,
        "messenger_note": mode_note(request.app.state.settings),
    })
    return templates.TemplateResponse(request, "write.html", ctx)


@router.post("/parties/{party_id}/write")
async def party_write(request: Request, party_id: int):
    form = await request.form()
    channel = _form_last(form, "channel") or "tg"
    back = _form_last(form, "from")
    suffix = f"?from={back}" if back.isdigit() else ""
    try:
        check_csrf(request, form.get("csrf"))
        saved = record_outbound(
            request.app.state.db,
            request.app.state.settings,
            party_id,
            channel,
            _form_last(form, "text"),
            actor_name(request),
            _form_last(form, "confirm") == "yes",
        )
    except ApiError as exc:
        if exc.message == "csrf":
            code = "csrf"
        elif exc.message == "confirm":
            code = "confirm"
        else:
            code = "write"
        target = f"/parties/{party_id}/write?channel={channel}"
        if back.isdigit():
            target += f"&from={back}"
        return _redirect(target, code)
    join = "&" if suffix else "?"
    note = "local" if saved["status"] == "local" else "not_sent"
    return _redirect(f"/parties/{party_id}{suffix}{join}note={note}")


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
