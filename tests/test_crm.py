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
    monkeypatch.setenv("MESSENGER_CHECK", "off")
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    monkeypatch.delenv("UI_PASSWORD", raising=False)
    monkeypatch.delenv("UI_USER", raising=False)
    monkeypatch.delenv("UI_GATE_SECRET", raising=False)
    monkeypatch.delenv("MESSENGER_TG_TOKEN", raising=False)
    monkeypatch.delenv("MESSENGER_WA_TOKEN", raising=False)
    monkeypatch.delenv("MESSENGER_MAX_TOKEN", raising=False)
    monkeypatch.delenv("DELO_USER_SERGEY_PASSWORD", raising=False)
    monkeypatch.delenv("DELO_USER_ANDREY_PASSWORD", raising=False)


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
    assert "Карточка заказчика" in card.text
    assert "Карточка водителя" in card.text
    client_party = local.app.state.db.one("SELECT id FROM parties WHERE inn_key = ?", ("7701123456",))
    assert client_party
    profile = local.get(f"/parties/{client_party['id']}")
    assert profile.status_code == 200
    assert "7701123456" in profile.text
    assert "Д-2026-0001" in profile.text
    assert "Написать" in profile.text
    driver = local.app.state.db.one(
        "SELECT id, license_no, passport_no FROM parties WHERE kind = 'driver' AND name = ?",
        ("Орлов К.",),
    )
    assert driver["license_no"] == "77 00 123456"
    assert driver["passport_no"] == "4500 000000"
    driver_page = local.get(f"/parties/{driver['id']}")
    assert "Водительское удостоверение" in driver_page.text
    assert "А123ВС 777" in driver_page.text
    feed = local.get("/packages/Д-2026-0001/feed")
    assert "Бот ATI" in feed.text
    assert "Письма" in feed.text


def test_env_example_and_no_smtp():
    text = (ROOT / ".env.example").read_text()
    secret_keys = {
        "INBOUND_SECRET",
        "UI_PASSWORD",
        "UI_GATE_SECRET",
        "TG_BOT_TOKEN",
        "TG_CHAT_ID",
        "UI_USER",
        "MESSENGER_TG_TOKEN",
        "MESSENGER_WA_TOKEN",
        "MESSENGER_MAX_TOKEN",
        "DELO_USER_SERGEY_PASSWORD",
        "DELO_USER_ANDREY_PASSWORD",
    }
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


def test_upsert_links_client_carrier_driver(client):
    first = _post(client, {
        "source": "manual",
        "text": "заявка 1",
        "client_hint": "ООО Ромашка",
        "client_contact": "Иванов Иван",
        "client_phone": "+7 (903) 111-22-33",
        "client_email": "client@example.ru",
        "client_inn": "7701234567",
        "carrier_name": "ИП Перевоз",
        "carrier_phone": "+7 903 111-22-44",
        "carrier_inn": "770200000033",
        "driver_name": "Петров Пётр",
        "driver_phone": "+7 (903) 111-22-33",
        "vehicle": "тент",
        "plate": "А000АА 77",
        "driver_license": "77 01 000001",
        "driver_passport": "4500 100000",
    })
    assert first.status_code == 200, first.text
    first_body = first.json()
    second = _post(client, {
        "source": "manual",
        "text": "заявка 2",
        "client_hint": "ООО Ромашка новое",
        "client_phone": "8 903 111-22-33",
    })
    assert second.status_code == 200, second.text
    db = client.app.state.db
    clients = db.q("SELECT * FROM parties WHERE kind = 'client' ORDER BY id")
    assert len(clients) == 1
    assert clients[0]["inn_key"] == "7701234567"
    assert clients[0]["phone_key"] == "79031112233"
    assert clients[0]["name"] == "ООО Ромашка новое"
    assert clients[0]["email_key"] == "client@example.ru"
    drivers = db.q("SELECT * FROM parties WHERE kind = 'driver'")
    carriers = db.q("SELECT * FROM parties WHERE kind = 'carrier'")
    assert len(drivers) == 1
    assert len(carriers) == 1
    assert drivers[0]["phone_key"] == clients[0]["phone_key"]
    assert drivers[0]["id"] != clients[0]["id"]
    assert drivers[0]["license_no"] == "77 01 000001"
    assert drivers[0]["passport_no"] == "4500 100000"
    assert carriers[0]["inn_key"] == "770200000033"
    profile = client.get(f"/parties/{clients[0]['id']}")
    assert profile.status_code == 200
    assert first_body["number"] in profile.text
    assert second.json()["number"] in profile.text
    assert "7701234567" in profile.text
    card = client.get(f"/packages/{first_body['id']}")
    assert f"/parties/{clients[0]['id']}" in card.text
    assert "Карточка водителя" in card.text
    assert "Карточка перевозчика" in card.text
    other = _post(client, {
        "source": "manual",
        "text": "чужой телефон",
        "client_hint": "ООО Другое",
        "client_phone": "+7 903 000-00-02",
        "client_inn": "7701234500",
    }).json()
    third = _post(client, {
        "source": "manual",
        "text": "тот же инн",
        "client_hint": "ООО Ромашка снова",
        "client_inn": "7701234567",
        "client_phone": "+7 903 000-00-02",
    }).json()
    again = db.q("SELECT id, phone_key, name FROM parties WHERE kind = 'client' ORDER BY id")
    assert len(again) == 2
    linked = db.one("SELECT client_id FROM packages WHERE id = ?", (third["id"],))
    assert linked["client_id"] == clients[0]["id"]
    kept = db.one("SELECT phone_key FROM parties WHERE id = ?", (clients[0]["id"],))
    assert kept["phone_key"] == "79031112233"
    other_row = db.one("SELECT client_id FROM packages WHERE number = ?", (other["number"],))
    assert other_row["client_id"] != clients[0]["id"]


def test_messenger_write_is_gated(tmp_path, monkeypatch):
    calls = []

    def boom(*_args, **_kwargs):
        calls.append("net")
        raise AssertionError("сеть")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("NOTIFY_TG", "0")
    monkeypatch.setenv("MESSENGER_CHECK", "mock")
    from app.main import create_app

    local = TestClient(create_app())
    created = local.post(
        "/s/inbound/doc",
        json={
            "source": "manual",
            "text": "для мессенджера",
            "client_hint": "Клиент Мессенджер",
            "client_phone": "+7 903 000-00-10",
        },
        headers={"X-Inbound-Secret": SECRET},
    ).json()
    party = local.app.state.db.one("SELECT * FROM parties WHERE kind = 'client'")
    assert party["tg_status"] == "yes"
    assert party["wa_status"] == "no"
    assert party["max_status"] == "yes"
    page = local.get(f"/parties/{party['id']}?from={created['id']}")
    assert "Telegram: есть" in page.text
    assert "WhatsApp: нет" in page.text
    assert "MAX: есть" in page.text
    assert "Написать" in page.text
    assert "https://wa.me/79030000010" in page.text
    token = local.cookies["delo_csrf"]
    denied = local.post(
        f"/parties/{party['id']}/write",
        data={"csrf": token, "channel": "tg", "text": "По загрузке", "from": str(created["id"])},
        follow_redirects=True,
    )
    assert denied.status_code == 200
    assert "не ушло" in denied.text
    assert local.app.state.db.one("SELECT COUNT(*) AS n FROM outbound_messages")["n"] == 0
    sent = local.post(
        f"/parties/{party['id']}/write",
        data={
            "csrf": token,
            "channel": "tg",
            "text": "По загрузке",
            "confirm": "yes",
            "from": str(created["id"]),
        },
        follow_redirects=True,
    )
    assert sent.status_code == 200
    assert "Ассистент Андрея" in sent.text
    row = local.app.state.db.one("SELECT * FROM outbound_messages")
    assert row["status"] == "local"
    assert row["actor"] == "Ассистент Андрея"
    assert row["body"].startswith("Привет, это ИИ-ассистент Сергея.")
    assert "По загрузке" in row["body"]
    assert "mock" in row["detail"]
    local.post(
        f"/parties/{party['id']}/write",
        data={"csrf": token, "channel": "tg", "text": "Второе", "confirm": "yes"},
        follow_redirects=True,
    )
    bodies = local.app.state.db.q("SELECT body FROM outbound_messages ORDER BY id")
    assert bodies[1]["body"].startswith("ИИ-ассистент Сергея на связи.")
    draft = local.post(
        f"/parties/{party['id']}/write",
        data={"csrf": token, "channel": "max", "text": "черновик max", "confirm": "yes"},
        follow_redirects=True,
    )
    assert draft.status_code == 200
    max_row = local.app.state.db.one(
        "SELECT status, detail FROM outbound_messages WHERE channel = 'max'"
    )
    assert max_row["status"] == "not_sent"
    assert "не ушло" in max_row["detail"]
    assert calls == []


def test_live_messenger_stays_offline(tmp_path, monkeypatch):
    calls = []

    def boom(*_args, **_kwargs):
        calls.append("net")
        raise AssertionError("сеть")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("NOTIFY_TG", "0")
    monkeypatch.setenv("MESSENGER_CHECK", "live")
    monkeypatch.setenv("MESSENGER_TG_TOKEN", "live-token-must-not-leak")
    from app.main import create_app

    local = TestClient(create_app())
    local.post(
        "/s/inbound/doc",
        json={"source": "manual", "text": "live", "client_hint": "Живой", "client_phone": "+7 903 000-00-10"},
        headers={"X-Inbound-Secret": SECRET},
    )
    party = local.app.state.db.one("SELECT * FROM parties WHERE kind = 'client'")
    assert party["tg_status"] == "unknown"
    token = local.cookies["delo_csrf"]
    page = local.get(f"/parties/{party['id']}")
    assert "live-token-must-not-leak" not in page.text
    local.post(
        f"/parties/{party['id']}/messengers",
        data={"csrf": token},
        follow_redirects=True,
    )
    local.post(
        f"/parties/{party['id']}/write",
        data={"csrf": token, "channel": "tg", "text": "не уходи", "confirm": "yes"},
        follow_redirects=True,
    )
    saved = local.app.state.db.one("SELECT status, detail, body FROM outbound_messages")
    assert saved["status"] == "not_sent"
    assert "live-token-must-not-leak" not in saved["detail"]
    assert saved["body"].startswith("Привет, это ИИ-ассистент Сергея.")
    assert calls == []


def test_sergey_and_andrey_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "basic")
    monkeypatch.setenv("NOTIFY_TG", "0")
    monkeypatch.setenv("DELO_USER_SERGEY_PASSWORD", "test-sergey-password")
    monkeypatch.setenv("DELO_USER_ANDREY_PASSWORD", "test-andrey-password")
    monkeypatch.delenv("UI_USER", raising=False)
    monkeypatch.delenv("UI_PASSWORD", raising=False)
    from app.main import create_app

    local = TestClient(create_app())
    assert local.get("/").status_code == 401
    sergey = local.get("/", auth=("sergey", "test-sergey-password"))
    assert sergey.status_code == 200
    assert "Сергей" in sergey.text
    assert "Диспетчер" in sergey.text
    andrey = local.get("/", auth=("andrey", "test-andrey-password"))
    assert andrey.status_code == 200
    assert "Андрей (логист)" in andrey.text
    assert "Оператор" in andrey.text
    assert local.get("/", auth=("sergey", "wrong-password")).status_code == 401
    rows = local.app.state.db.q("SELECT login, role_code, password_hash FROM users ORDER BY login")
    blob = str(rows)
    assert "test-sergey-password" not in blob
    assert "test-andrey-password" not in blob
    by_login = {row["login"]: row for row in rows}
    assert by_login["sergey"]["role_code"] == "dispatcher"
    assert by_login["andrey"]["role_code"] == "operator"
    assert by_login["sergey"]["password_hash"].startswith("pbkdf2$")
    assert by_login["andrey"]["password_hash"].startswith("pbkdf2$")
    readme = (ROOT / "README.md").read_text()
    assert "/root/ati/secrets/delo-crm-auth.env" in readme
    assert "test-sergey-password" not in readme
    assert "test-andrey-password" not in readme


def test_list_pages_do_not_load_everything(client):
    for index in range(3):
        created = _post(client, {"source": "manual", "text": f"пакет {index}", "subject": f"s{index}"})
        assert created.status_code == 200
    first = client.get("/s/packages?limit=2&offset=0", headers={"X-Inbound-Secret": SECRET})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert body["total"] == 3
    assert len(body["packages"]) == 2
    second = client.get("/s/packages?limit=2&offset=2", headers={"X-Inbound-Secret": SECRET})
    assert len(second.json()["packages"]) == 1
    capped = client.get("/s/packages?limit=10000", headers={"X-Inbound-Secret": SECRET})
    assert capped.json()["limit"] == 100
    page = client.get("/")
    assert "Показаны 1-3 из 3" in page.text
    serve = (ROOT / "bin" / "serve").read_text()
    assert "workers=1" in serve
    assert "MemoryMax=512M" in (ROOT / "deploy" / "delo-crm.service").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "1.8G" in readme or "1.8" in readme
    assert "LIMIT_CONCURRENCY" in readme
    assert "выгрузка" in readme.lower() or "HTTPS-выгрузка" in readme


def test_validation(client):
    assert _post(client, {"source": "site", "text": "x"}).status_code == 422
    assert _post(client, {"source": "manual"}).status_code == 422
    assert client.get("/s/packages?status=nope", headers={"X-Inbound-Secret": SECRET}).status_code == 422
    assert client.get("/s/packages/999", headers={"X-Inbound-Secret": SECRET}).status_code == 404
