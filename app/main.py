"""ATI Delivery CRM — пакеты документов за nginx, только 127.0.0.1."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import inbound_ok, router as api_router
from app.auth import authenticate, sync_users
from app.carrier_pages import router as carrier_pages_router
from app.config import Settings
from app.db import Database
from app.logic import ApiError
from app.parties import backfill
from app.settings_ui import router as settings_router
from app.ui import router as ui_router

ROOT = Path(__file__).resolve().parents[1]
OPEN_PATHS = {"/health", "/robots.txt"}
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "0.0.0"


def _eq(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def create_app() -> FastAPI:
    settings = Settings.from_env()
    db = Database(settings)
    db.init()
    backfill(db, settings)
    sync_users(db)
    app = FastAPI(title="ATI Delivery CRM", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db

    def _deny_ui(request: Request) -> Response | None:
        mode = settings.auth_mode
        if mode == "basic":
            user = authenticate(db, settings, request.headers.get("authorization", ""))
            if not user:
                return Response(
                    "Нужен вход.",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="ATI Delivery CRM"'},
                    media_type="text/plain; charset=utf-8",
                )
            request.state.user = user
        elif mode == "proxy" and settings.ui_gate_secret:
            if not _eq(request.headers.get("x-delo-gate", ""), settings.ui_gate_secret):
                return PlainTextResponse("Нужен вход через nginx.", status_code=401)
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        token = request.cookies.get("delo_csrf") or secrets.token_urlsafe(24)
        request.state.csrf = token
        request.state.user = None
        path = request.url.path
        # /api/* — тот же вход, что у экранов, либо секрет шлюза (воркер на 127.0.0.1).
        if path.startswith("/api/"):
            if not inbound_ok(request):
                denied = _deny_ui(request)
                if denied is not None:
                    return denied
        elif path not in OPEN_PATHS and not path.startswith("/s/"):
            denied = _deny_ui(request)
            if denied is not None:
                return denied
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
        return {"ok": True, "service": "delo-crm", "version": VERSION}

    @app.get("/robots.txt")
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    app.include_router(api_router)
    app.include_router(carrier_pages_router)
    app.include_router(settings_router)
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
