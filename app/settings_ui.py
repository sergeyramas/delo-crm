"""Страница «Настройки». Правка текстов диспетчерской, только роль admin."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.domain import fmt_dt
from app.logic import ApiError
from app.settings_catalog import BY_KEY, SECTIONS, render_preview
from app.settings_store import (
    publish,
    reset,
    rollback,
    save_draft,
    state as key_state,
)
from app.settings_validate import ValidationError
from app.ui import (
    ERRS,
    actor_name,
    base_ctx,
    check_csrf,
    require_admin,
    templates,
)

router = APIRouter()

NOTES = {
    "draft": "Черновик сохранён. Опубликованное значение не менялось.",
    "published": "Опубликовано. Дело увидит новое значение в следующем snapshot.",
    "reset": "Сброшено к умолчанию: ключ убран из snapshot, Дело возьмёт своё значение.",
    "rollback": "Откат записан как новая публикация.",
}


def _redirect(path: str, note: str = "", err: str = "") -> RedirectResponse:
    params = {}
    if note:
        params["note"] = note
    if err:
        params["err"] = err
    target = f"{path}?{urlencode(params)}" if params else path
    return RedirectResponse(target, status_code=303)


def _message(request: Request) -> tuple[str, str]:
    raw_err = request.query_params.get("err", "")
    if raw_err == "csrf":
        err = ERRS["csrf"]
    else:
        err = raw_err
    note = NOTES.get(request.query_params.get("note", ""), "")
    return err, note


def _card(db, item) -> dict:
    data = key_state(db, item.key)
    data["preview"] = (
        render_preview(item, data["current"])
        if item.type == "template"
        else data["current_display"]
    )
    for row in data["history"]:
        row["at_display"] = fmt_dt(row["at"])
    return data


@router.get("/settings")
def page_settings(request: Request):
    require_admin(request)
    db = request.app.state.db
    ctx = base_ctx(request, "settings")
    err, note = _message(request)
    ctx["err"] = err
    ctx["note"] = note
    ctx["sections"] = [
        {
            "code": code,
            "title": title,
            "cards": [_card(db, BY_KEY[item.key]) for item in BY_KEY.values() if item.section == code],
        }
        for code, title in SECTIONS
    ]
    return templates.TemplateResponse(request, "settings.html", ctx)


def _key_or_redirect(request: Request, key: str):
    item = BY_KEY.get(key)
    if item is None:
        return None, _redirect("/settings", err=f"Неизвестный ключ настройки: {key}")
    return item, None


@router.post("/settings/{key}/draft")
async def save_draft_form(request: Request, key: str):
    require_admin(request)
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        item, denied = _key_or_redirect(request, key)
        if denied is not None:
            return denied
        save_draft(
            request.app.state.db,
            item.key,
            _last(form, "value"),
            actor_name(request),
        )
    except ApiError as exc:
        return _redirect("/settings", err="csrf")
    except ValidationError as exc:
        return _redirect("/settings", err=str(exc))
    return _redirect("/settings", note="draft")


@router.post("/settings/{key}/publish")
async def publish_form(request: Request, key: str):
    require_admin(request)
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        item, denied = _key_or_redirect(request, key)
        if denied is not None:
            return denied
        raw = _last(form, "value") if form.get("value") is not None else None
        publish(
            request.app.state.db,
            item.key,
            raw,
            actor_name(request),
            _last(form, "comment"),
        )
    except ApiError as exc:
        return _redirect("/settings", err="csrf")
    except ValidationError as exc:
        return _redirect("/settings", err=str(exc))
    return _redirect("/settings", note="published")


@router.post("/settings/{key}/reset")
async def reset_form(request: Request, key: str):
    require_admin(request)
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        item, denied = _key_or_redirect(request, key)
        if denied is not None:
            return denied
        reset(
            request.app.state.db,
            item.key,
            actor_name(request),
            _last(form, "comment"),
        )
    except ApiError as exc:
        return _redirect("/settings", err="csrf")
    except ValidationError as exc:
        return _redirect("/settings", err=str(exc))
    return _redirect("/settings", note="reset")


@router.post("/settings/{key}/rollback/{history_id}")
async def rollback_form(request: Request, key: str, history_id: int):
    require_admin(request)
    form = await request.form()
    try:
        check_csrf(request, form.get("csrf"))
        item, denied = _key_or_redirect(request, key)
        if denied is not None:
            return denied
        rollback(
            request.app.state.db,
            item.key,
            history_id,
            actor_name(request),
            _last(form, "comment"),
        )
    except ApiError as exc:
        return _redirect("/settings", err="csrf")
    except ValidationError as exc:
        return _redirect("/settings", err=str(exc))
    return _redirect("/settings", note="rollback")


def _last(form, name: str) -> str:
    values = form.getlist(name) if hasattr(form, "getlist") else [form.get(name)]
    values = [value for value in values if value is not None]
    return str(values[-1]) if values else ""
