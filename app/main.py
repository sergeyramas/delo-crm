"""ATI Delivery CRM — пакеты документов за nginx, только 127.0.0.1."""

from __future__ import annotations

import base64
import hmac
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.config import Settings
from app.db import Database
from app.logic import ApiError
from app.ui import router as ui_router

ROOT = Path(__file__).resolve().parents[1]
OPEN_PATHS = {"/health", "/robots.txt"}


def _eq(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _basic_ok(header: str, user: str, password: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
    except Exception:
        return False
    got_user, sep, got_password = raw.partition(":")
    if not sep:
        return False
    return _eq(got_user, user) and _eq(got_password, password)


def create_app() -> FastAPI:
    settings = Settings.from_env()
    db = Database(settings)
    db.init()
    app = FastAPI(title="ATI Delivery CRM", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db

    @app.middleware("http")
    async def guard(request: Request, call_next):
        token = request.cookies.get("delo_csrf") or secrets.token_urlsafe(24)
        request.state.csrf = token
        path = request.url.path
        if path not in OPEN_PATHS and not path.startswith("/s/"):
            mode = settings.auth_mode
            if mode == "basic":
                if not _basic_ok(request.headers.get("authorization", ""), settings.ui_user, settings.ui_password):
                    return Response(
                        "Нужен вход.",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="ATI Delivery CRM"'},
                        media_type="text/plain; charset=utf-8",
                    )
            elif mode == "proxy" and settings.ui_gate_secret:
                if not _eq(request.headers.get("x-delo-gate", ""), settings.ui_gate_secret):
                    return PlainTextResponse("Нужен вход через nginx.", status_code=401)
        response = await call_next(request)
        if request.cookies.get("delo_csrf") != token:
            response.set_cookie("delo_csrf", token, httponly=True, samesite="lax", path="/")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    @app.exception_handler(ApiError)
    async def on_api_error(_request: Request, exc: ApiError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.get("/health")
    def health():
        db.q("SELECT 1")
        return {"ok": True, "service": "delo-crm"}

    @app.get("/robots.txt")
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    app.include_router(api_router)
    app.include_router(ui_router)
    app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
    return app


_app = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
