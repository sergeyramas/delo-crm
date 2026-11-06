import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-inbound-secret-value"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("NOTIFY_TG", "0")
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    monkeypatch.delenv("UI_PASSWORD", raising=False)
    monkeypatch.delenv("UI_USER", raising=False)
    monkeypatch.delenv("UI_GATE_SECRET", raising=False)


@pytest.fixture()
def client(env):
    from app.main import create_app

    return TestClient(create_app())


def _post(client, payload, secret=SECRET):
    headers = {"X-Inbound-Secret": secret} if secret is not None else {}
    return client.post("/s/inbound/doc", json=payload, headers=headers)


def test_inbound_first_number_is_year_0001(client):
    year = date.today().year
    response = _post(client, {
        "source": "mail",
        "from": "Иванова <docs@example.ru>",
        "subject": "Заявка Москва Казань",
        "text": "Просьба подписать заявку",
        "client_hint": "ООО «Северный Хлеб»",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["number"] == f"Д-{year}-0001"
    assert body["status"] == "polucheno"
    assert body["created"] is True
    if year == 2026:
        assert body["number"] == "Д-2026-0001"
    card = client.get(f"/s/packages/{body['id']}", headers={"X-Inbound-Secret": SECRET})
    assert card.status_code == 200
    detail = card.json()
    assert detail["package"]["status"] == "polucheno"
    assert detail["package"]["stage"] == "nahodka"
    assert detail["package"]["client"]["name"] == "ООО «Северный Хлеб»"
    assert any(event["type"] == "mail_in" for event in detail["events"])
    page = client.get(f"/packages/{body['id']}")
    assert page.status_code == 200
    assert body["number"] in page.text
    assert "polucheno" in page.text
    assert "ATI Delivery" in page.text
    assert "Получено" in page.text
    listing = client.get("/")
    assert listing.status_code == 200
    assert "Список сделок" in listing.text
    assert body["number"] in listing.text


def test_second_number_and_secret(client):
    year = date.today().year
    assert _post(client, {"source": "manual", "text": "первый"}).json()["number"].endswith("0001")
    second = _post(client, {"source": "tg", "text": "второй", "from": "Андрей"})
    assert second.json()["number"] == f"Д-{year}-0002"
    assert _post(client, {"source": "manual", "text": "нет секрета"}, secret=None).status_code == 401
    assert _post(client, {"source": "manual", "text": "чужой"}, secret="wrong-secret-value!!").status_code == 401
    leaked = client.post("/s/inbound/doc", json={"source": "manual", "text": "через поле", "secret": SECRET})
    assert leaked.status_code == 200
    detail = client.get(f"/s/packages/{leaked.json()['id']}", headers={"X-Inbound-Secret": SECRET}).json()
    blob = str(detail)
    assert SECRET not in blob
    assert client.post(f"/s/inbound/doc?secret={SECRET}", json={"source": "manual", "text": "query"}).status_code == 401


def test_gateway_off_without_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", "")
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("NOTIFY_TG", "0")
    from app.main import create_app

    bare = TestClient(create_app())
    response = bare.post("/s/inbound/doc", json={"source": "manual", "text": "x"}, headers={"X-Inbound-Secret": SECRET})
    assert response.status_code == 503
    assert bare.get("/").status_code == 200


def test_thread_does_not_reuse_or_renumber(client):
    first = _post(client, {"source": "mail", "text": "письмо", "thread_id": "thr-1", "subject": "Тема"}).json()
    again = _post(client, {"source": "mail", "text": "ответ", "thread_id": "thr-1"}).json()
    assert again["id"] == first["id"]
    assert again["number"] == first["number"]
    assert again["created"] is False
    assert again["status"] == "polucheno"
    other = _post(client, {"source": "mail", "text": "другое", "thread_id": "thr-2"}).json()
    assert other["number"] != first["number"]


def test_patch_filter_and_otkaz(client, monkeypatch):
    calls = []

    def boom(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("сеть")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    created = _post(client, {"source": "manual", "text": "пакет", "route_from": "Москва", "route_to": "Казань"}).json()
    patched = client.patch(
        f"/s/packages/{created['id']}",
        json={"status": "podgotovleno", "rate_rub": 68000},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()["package"]
    assert body["status"] == "podgotovleno"
    assert body["stage"] == "zayavka"
    assert body["rate_rub"] == 68000
    assert calls == []
    listed = client.get("/s/packages?status=polucheno", headers={"X-Inbound-Secret": SECRET})
    assert listed.status_code == 200
    assert listed.json()["packages"] == []
    ready = client.get("/s/packages?status=podgotovleno", headers={"X-Inbound-Secret": SECRET})
    assert ready.json()["packages"][0]["number"] == created["number"]
    refused = client.patch(
        f"/s/packages/{created['number']}",
        json={"status": "otkaz"},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert refused.status_code == 422
    ok = client.patch(
        f"/s/packages/{created['number']}",
        json={"status": "otkaz", "comment": "не берём, срок"},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert ok.status_code == 200
    assert ok.json()["package"]["status"] == "otkaz"
    assert calls == []


def test_note_file_and_ui_advance(client):
    created = _post(client, {"source": "manual", "text": "старт", "client_hint": "Клиент"}).json()
    note = client.post(
        f"/s/packages/{created['id']}/events",
        json={"type": "note", "text": "проверили флаги", "actor": "Андрей", "title": "Заметка"},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert note.status_code == 200, note.text
    raw = base64.b64encode(b"%PDF-1.4 demo").decode()
    uploaded = client.post(
        f"/s/packages/{created['id']}/events",
        json={"type": "file", "text": "скан", "attachments": [{"name": "zayavka.pdf", "content_base64": raw}]},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert uploaded.status_code == 200, uploaded.text
    page = client.get(f"/packages/{created['id']}")
    token = client.cookies["delo_csrf"]
    moved = client.post(f"/packages/{created['id']}/advance", data={"csrf": token}, follow_redirects=True)
    assert moved.status_code == 200
    assert "На одобрении" in moved.text
    assert "polucheno" in moved.text
    download = client.get(f"/files/{uploaded.json()['file_id']}")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")
    bad = client.get("/files/99999")
    assert bad.status_code == 404


def test_parallel_numbers_have_no_gaps(client):
    from app.logic import create_inbound

    db = client.app.state.db
    settings = client.app.state.settings

    def one(i):
        return create_inbound(db, settings, {"source": "manual", "text": f"пакет {i}", "subject": f"s{i}"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, range(20)))
    numbers = [row["number"] for row in rows]
    assert len(set(numbers)) == 20
    seqs = sorted(int(number.rsplit("-", 1)[1]) for number in numbers)
    assert seqs == list(range(1, 21))


def test_notify_flag(tmp_path, monkeypatch):
    calls = []

    class Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open(request, timeout=0):
        calls.append(request.full_url)
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("NOTIFY_TG", "1")
    monkeypatch.setenv("TG_BOT_TOKEN", "token-for-test")
    monkeypatch.setenv("TG_CHAT_ID", "1")
    from app.main import create_app

    app = create_app()
    local = TestClient(app)
    created = local.post(
        "/s/inbound/doc",
        json={"source": "manual", "text": "для уведомления"},
        headers={"X-Inbound-Secret": SECRET},
    ).json()
    assert calls == []
    local.patch(
        f"/s/packages/{created['id']}",
        json={"status": "podgotovleno"},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert len(calls) == 1
    assert "api.telegram.org" in calls[0]
    assert "sendMessage" in calls[0]


def test_basic_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "basic")
    monkeypatch.setenv("UI_USER", "andrey")
    monkeypatch.setenv("UI_PASSWORD", "test-pass")
    monkeypatch.setenv("NOTIFY_TG", "0")
    from app.main import create_app

    local = TestClient(create_app())
    assert local.get("/").status_code == 401
    assert local.get("/health").status_code == 200
    opened = local.get("/", auth=("andrey", "test-pass"))
    assert opened.status_code == 200
    inbound = local.post(
        "/s/inbound/doc",
        json={"source": "manual", "text": "мимо basic"},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert inbound.status_code == 200


def test_seed_matches_mock_counts(env):
    from app.main import create_app
    from app.seed import main

    assert main() == 0
    assert main() == 1
    local = TestClient(create_app())
    page = local.get("/")
    assert page.status_code == 200
    assert "Д-2026-0001" in page.text
    assert "Водитель в пути" in page.text
    assert "314 500 ₽" in page.text
    assert page.text.count("нужно «да»") >= 1
    kanban = local.get("/kanban")
    assert "Находки" in kanban.text
    assert "Задачи на день" in kanban.text
    card = local.get("/packages/Д-2026-0001")
    assert "ООО «Северный Хлеб»" in card.text
    assert "Статус пакета: Подписано · podpisano" in card.text
    feed = local.get("/packages/Д-2026-0001/feed")
    assert "Бот ATI" in feed.text
    assert "Письма" in feed.text


def test_env_example_and_no_smtp():
    text = (ROOT / ".env.example").read_text()
    secret_keys = {"INBOUND_SECRET", "UI_PASSWORD", "UI_GATE_SECRET", "TG_BOT_TOKEN", "TG_CHAT_ID", "UI_USER"}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key in secret_keys:
            assert value.strip() == ""
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text()
        assert "smtplib" not in source
        assert "SMTP(" not in source


def test_validation(client):
    assert _post(client, {"source": "site", "text": "x"}).status_code == 422
    assert _post(client, {"source": "manual"}).status_code == 422
    assert client.get("/s/packages?status=nope", headers={"X-Inbound-Secret": SECRET}).status_code == 422
    assert client.get("/s/packages/999", headers={"X-Inbound-Secret": SECRET}).status_code == 404
