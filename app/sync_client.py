"""Клиент шлюза CRM для воркера Hermes.

Файл копируется на Hermes целиком. Зависимостей кроме стандартной библиотеки нет.
Секрет только из окружения или из аргумента вызывающего кода, не из argv и не из репозитория.

DELO_CRM_URL — база, без слэша в конце. На Ядре надёжно http://127.0.0.1:8091.
DELO_CRM_INBOUND_SECRET — та же строка, что INBOUND_SECRET у CRM.
DELO_CRM_CONNECT — необязательно. IP, куда открыть TCP, если URL — живое имя
(SNI остаётся из URL). На Ядре через docker-nginx: 127.0.0.1, это порт на хосте.
Внутри контейнера /s/ пускает мост 172.18.0.0/16, не 127.0.0.1 контейнера.
Для прямого http://127.0.0.1:8091 переменную не задавать.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
from urllib.parse import quote, urlparse


class SyncError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def crm_url() -> str:
    url = os.environ.get("DELO_CRM_URL", "").strip().rstrip("/")
    if not url:
        raise SyncError(0, "Нет DELO_CRM_URL.")
    return url


def crm_secret() -> str:
    secret = os.environ.get("DELO_CRM_INBOUND_SECRET", "").strip() or os.environ.get("INBOUND_SECRET", "").strip()
    if len(secret) < 16:
        raise SyncError(0, "Секрет шлюза не задан или короче 16 символов.")
    return secret


def crm_connect() -> str:
    return os.environ.get("DELO_CRM_CONNECT", "").strip()


def _hide(text: str, secret: str) -> str:
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text[:400]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TCP на connect_host, SNI и проверка сертификата — по имени из URL."""

    def __init__(self, host: str, port: int, connect_host: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._connect_host = connect_host

    def connect(self) -> None:
        sock = socket.create_connection((self._connect_host, self.port), self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def request_json(
    method: str,
    path: str,
    payload: dict | None,
    *,
    base_url: str,
    secret: str,
    timeout: float = 60,
    connect_host: str = "",
) -> tuple[int, dict]:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SyncError(0, "URL CRM должен начинаться с http:// или https://.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    full_path = (parsed.path.rstrip("/") + (path if path.startswith("/") else "/" + path)) or "/"
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "X-Inbound-Secret": secret,
        "Host": parsed.netloc,
    }
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(body))
    tcp_host = connect_host.strip() or parsed.hostname
    try:
        if parsed.scheme == "https":
            conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
                parsed.hostname, port, tcp_host, timeout, ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(tcp_host, port, timeout=timeout)
        conn.request(method.upper(), full_path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        conn.close()
    except OSError as exc:
        raise SyncError(0, f"Нет связи с CRM: {exc.__class__.__name__}") from exc
    if not raw:
        return status, {}
    try:
        parsed_body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, {"error": _hide(raw.decode("utf-8", errors="replace"), secret)}
    if not isinstance(parsed_body, dict):
        return status, {"error": "Ответ CRM не объект."}
    if isinstance(parsed_body.get("error"), str):
        parsed_body["error"] = _hide(parsed_body["error"], secret)
    return status, parsed_body


def _call(method: str, path: str, payload: dict | None, *, base_url: str | None, secret: str | None, timeout: float, connect_host: str | None) -> dict:
    status, body = request_json(
        method,
        path,
        payload,
        base_url=base_url or crm_url(),
        secret=secret or crm_secret(),
        timeout=timeout,
        connect_host=connect_host if connect_host is not None else crm_connect(),
    )
    if status >= 400 or status == 0:
        message = body.get("error") if isinstance(body.get("error"), str) else f"HTTP {status}"
        raise SyncError(status, message)
    return body


def post_inbound(
    payload: dict,
    *,
    base_url: str | None = None,
    secret: str | None = None,
    timeout: float = 60,
    connect_host: str | None = None,
) -> dict:
    return _call("POST", "/s/inbound/doc", payload, base_url=base_url, secret=secret, timeout=timeout, connect_host=connect_host)


def patch_package(
    number: str,
    payload: dict,
    *,
    base_url: str | None = None,
    secret: str | None = None,
    timeout: float = 60,
    connect_host: str | None = None,
) -> dict:
    path = "/s/packages/" + quote(number, safe="")
    return _call("PATCH", path, payload, base_url=base_url, secret=secret, timeout=timeout, connect_host=connect_host)


def post_event(
    number: str,
    payload: dict,
    *,
    base_url: str | None = None,
    secret: str | None = None,
    timeout: float = 60,
    connect_host: str | None = None,
) -> dict:
    path = "/s/packages/" + quote(number, safe="") + "/events"
    return _call("POST", path, payload, base_url=base_url, secret=secret, timeout=timeout, connect_host=connect_host)
