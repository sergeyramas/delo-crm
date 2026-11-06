import base64
import json
import re
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.sync_client import SyncError, patch_package, post_inbound, request_json
from tools.import_reestr import (
    build_row,
    carrier_from_komu,
    collect_attachments,
    load_csv,
    main,
    registry_target,
    run_import,
    split_route,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-inbound-secret-value"
CSV = """skvoznoj_n;data;klient;marshrut;gruz;stavka;status;email_klient;email_perevozchik;komu;kommentarij
Д-2026-0007;05.11.2026;ООО «Пример»;Москва → Казань;тент;68 000;otpravleno;docs@example.ru;mail@example.ru;;водитель Орлов К. А123ВС 777
Д-2026-0002;2026-11-06;АО «Склад»;Тула → Рязань;доски;12000;polucheno;a@example.ru;;ИП Петров А.А.;везёт доски
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("NOTIFY_TG", "0")
    monkeypatch.setenv("MESSENGER_CHECK", "off")


@pytest.fixture()
def client(env):
    from app.main import create_app

    return TestClient(create_app())


class ClientTransport:
    def __init__(self, client):
        self.client = client

    def __call__(self, method, path, payload):
        headers = {"X-Inbound-Secret": SECRET}
        method = method.upper()
        if method == "GET":
            response = self.client.get(path, headers=headers)
        elif method == "POST":
            response = self.client.post(path, json=payload, headers=headers)
        elif method == "PATCH":
            response = self.client.patch(path, json=payload, headers=headers)
        else:
            raise AssertionError(method)
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {"error": response.text[:200]}
        return response.status_code, body


def test_row_maps_route_carrier_plate_and_stage():
    payload, patch = build_row({
        "skvoznoj_n": "7",
        "data": "05.11.2026",
        "klient": "ООО «Пример»",
        "marshrut": "Москва → Казань",
        "gruz": "тент",
        "stavka": "68 000",
        "status": "otpravleno",
        "email_klient": "Иванова <docs@example.ru>",
        "email_perevozchik": "",
        "komu": "",
        "kommentarij": "водитель Орлов К. А123ВС 777",
    })
    assert payload["number"] == "Д-2026-0007"
    assert payload["source"] == "manual"
    assert payload["idempotent"] is True
    assert payload["route_from"] == "Москва"
    assert payload["route_to"] == "Казань"
    assert payload["cargo"] == "тент"
    assert payload["rate_rub"] == 68000
    assert payload["load_date"] == "2026-11-05"
    assert payload["client_name"] == "ООО «Пример»"
    assert payload["client_email"] == "docs@example.ru"
    assert payload["carrier_name"] == "Перевозчик 21"
    assert payload["driver_name"] == "Орлов К"
    assert payload["plate"] == "А123ВС 777"
    assert patch["status"] == "otpravleno"
    assert patch["stage"] == "docs_sent"
    assert registry_target("otpravleno") == ("docs_sent", "otpravleno")
    assert registry_target("Отправлено") == ("docs_sent", "otpravleno")
    assert registry_target("v_puti") == ("v_puti", "podpisano")
    assert registry_target("") is None
    assert split_route("Тула - Рязань") == ("Тула", "Рязань")
    assert carrier_from_komu("Камалюков И.И.") == "Перевозчик 21"
    assert carrier_from_komu("ИП Кртин") == "Перевозчик 21"
    assert carrier_from_komu("ИП Петров А.А., договор") == "ИП Петров А.А."
    own, own_patch = build_row({
        "skvoznoj_n": "Д-2026-0002",
        "data": "2026-11-06",
        "klient": "АО «Склад»",
        "marshrut": "Тула → Рязань",
        "gruz": "доски",
        "stavka": "12000",
        "status": "polucheno",
        "komu": "ИП Петров А.А.",
        "kommentarij": "",
    })
    assert own["carrier_name"] == "ИП Петров А.А."
    assert own_patch == {"status": "polucheno", "stage": "nahodka", "actor": "reestr"}


def test_import_dry_run_prints_counters_and_skips_network(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise AssertionError("сеть")

    monkeypatch.setattr("tools.import_reestr.request_json", boom)
    csv_path = tmp_path / "reestr.csv"
    csv_path.write_text(CSV + "Д-2026-0003;2026-11-06;АО «Нет»;Тула → Рязань;доски;10;неизвестно;;;;\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Д-2026-0007-zayavka.pdf").write_bytes(b"%PDF-1.4 demo")
    (docs / "заметка.txt").write_text("мимо", encoding="utf-8")
    code = main([
        str(csv_path),
        "--dry-run",
        "--base-url",
        "http://127.0.0.1:9",
        "--docs-dir",
        str(docs),
    ])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.strip() == "created=2 updated=0 attached=1 errors=1"
    assert "dry-run: запросы не отправлялись" in captured.err
    assert "неизвестный статус" in captured.err.lower() or "Неизвестный статус" in captured.err
    assert SECRET not in captured.out + captured.err


def test_import_is_idempotent_against_api(client, tmp_path, capsys):
    csv_path = tmp_path / "reestr.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Д-2026-0007-zayavka.pdf").write_bytes(b"%PDF-1.4 demo")
    plans, errors = load_csv(csv_path)
    attachments = collect_attachments(str(docs), [])
    assert errors == []
    assert [item.number for item in plans] == ["Д-2026-0007", "Д-2026-0002"]
    transport = ClientTransport(client)
    first = run_import(plans, attachments, transport, dry_run=False)
    assert first.line() == "created=2 updated=0 attached=1 errors=0"
    card = client.get("/s/packages/Д-2026-0007", headers={"X-Inbound-Secret": SECRET})
    assert card.status_code == 200, card.text
    package = card.json()["package"]
    assert package["status"] == "otpravleno"
    assert package["stage"] == "docs_sent"
    assert package["route_from"] == "Москва"
    assert package["route_to"] == "Казань"
    assert package["carrier_name"] == "Перевозчик 21"
    assert package["driver_name"] == "Орлов К"
    assert package["plate"] == "А123ВС 777"
    assert package["rate_rub"] == 68000
    second_pkg = client.get("/s/packages/Д-2026-0002", headers={"X-Inbound-Secret": SECRET}).json()["package"]
    assert second_pkg["carrier_name"] == "ИП Петров А.А."
    assert second_pkg["stage"] == "nahodka"
    second = run_import(plans, attachments, transport, dry_run=False)
    assert second.line() == "created=0 updated=2 attached=0 errors=0"
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM packages")["n"] == 2
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM files")["n"] == 1
    notes = [
        event for event in card.json()["events"] if event["type"] == "note"
    ]
    again = client.get("/s/packages/Д-2026-0007", headers={"X-Inbound-Secret": SECRET}).json()
    notes_again = [event for event in again["events"] if event["type"] == "note"]
    assert len(notes_again) == len(notes) == 1
    year = date.today().year
    nxt = client.post(
        "/s/inbound/doc",
        json={"source": "manual", "text": "живая заявка"},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert nxt.status_code == 200, nxt.text
    assert nxt.json()["number"] == f"Д-{year}-0008" if year == 2026 else nxt.json()["number"].endswith("0001")
    if year == 2026:
        counter = client.app.state.db.one("SELECT last_seq FROM year_counters WHERE year = 2026")
        assert counter["last_seq"] == 8
    assert capsys.readouterr().out == ""


def test_attach_flag_and_oversize(tmp_path, monkeypatch):
    folder = tmp_path / "one"
    folder.mkdir()
    target = folder / "scan.pdf"
    target.write_bytes(b"12345")
    monkeypatch.setattr("tools.import_reestr.MAX_FILE", 4)
    items = collect_attachments(None, [f"Д-2026-0007={target}"])
    assert items[0].error
    assert "8 МБ" in items[0].error
    monkeypatch.setattr("tools.import_reestr.MAX_FILE", 8 * 1024 * 1024)
    ok = collect_attachments(str(folder), [])
    assert ok == []
    named = folder / "Д-2026-0007-scan.pdf"
    target.rename(named)
    ok = collect_attachments(str(folder), [f"D-2026-0007={named}"])
    assert len(ok) == 1
    assert ok[0].number == "Д-2026-0007"
    assert ok[0].error == ""


def test_sync_client_hides_secret_and_patches_number():
    secret = "s" * 24
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def _body(self):
            size = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(size).decode() or "{}")

        def _send(self, status, payload):
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            seen["secret"] = self.headers.get("X-Inbound-Secret")
            body = self._body()
            if body.get("bad"):
                self._send(401, {"error": f"секрет {secret} не подошёл"})
                return
            seen["path"] = self.path
            self._send(200, {"id": 1, "number": "Д-2026-0001", "created": True, "status": "polucheno"})

        def do_PATCH(self):
            seen["patch_path"] = self.path
            seen["patch"] = self._body()
            self._send(200, {"package": {"status": "otpravleno", "stage": "docs_sent"}})

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(SyncError) as caught:
            post_inbound({"source": "manual", "text": "x", "bad": True}, base_url=base, secret=secret)
        assert secret not in caught.value.message
        assert "***" in caught.value.message
        created = post_inbound({"source": "manual", "text": "заявка"}, base_url=base, secret=secret)
        assert created["number"] == "Д-2026-0001"
        assert seen["path"] == "/s/inbound/doc"
        assert seen["secret"] == secret
        patched = patch_package("Д-2026-0001", {"status": "otpravleno", "actor": "worker"}, base_url=base, secret=secret)
        assert patched["package"]["stage"] == "docs_sent"
        assert seen["patch_path"] == "/s/packages/" + quote("Д-2026-0001", safe="")
        status, body = request_json("POST", "/s/inbound/doc", {"source": "manual"}, base_url=base, secret=secret)
        assert status == 200
        assert body["created"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_docs_point_at_live_host_without_secrets():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "WORKER-CRM-SYNC.md").read_text(encoding="utf-8")
    assert "https://crm.77-91-113-64.sslip.io" in readme
    assert "bin/import_reestr" in readme
    assert "--connect 127.0.0.1" in readme
    assert "--secret-file /root/ati/secrets/delo-crm.env" in readme
    assert "DELO_CRM_URL" in doc
    assert "DELO_CRM_INBOUND_SECRET" in doc
    assert "X-Inbound-Secret" in doc
    assert "docs_sent" in doc
    blob = readme + "\n" + doc
    assert not re.search(r"DELO_CRM_INBOUND_SECRET=\S", blob)
    assert not re.search(r"INBOUND_SECRET=\S", blob)
    assert SECRET not in blob
    raw = base64.b64encode(b"not-a-secret").decode()
    assert raw not in blob
