import pytest
from fastapi.testclient import TestClient

from tests.conftest import SECRET
from tests.test_crm import _post


def _hook(client, channel, payload, secret=SECRET):
    headers = {"X-Inbound-Secret": secret} if secret is not None else {}
    return client.post(f"/s/hooks/{channel}", json=payload, headers=headers)


def _package(client, text="заявка для переписки"):
    created = _post(client, {"source": "manual", "text": text, "client_hint": "ООО Поле"})
    assert created.status_code == 200, created.text
    return created.json()


def test_hooks_need_secret_and_do_not_send(client, monkeypatch):
    calls = []

    def boom(*_args, **_kwargs):
        calls.append("net")
        raise AssertionError("сеть")

    def no_send(self, message):
        del self, message
        raise AssertionError("send")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setattr("app.messages.EmailAdapter.send", no_send)
    monkeypatch.setattr("app.messages.TelegramAdapter.send", no_send)
    monkeypatch.setattr("app.messages.MaxAdapter.send", no_send)
    assert _hook(client, "email", {"text": "без секрета"}, secret=None).status_code == 401
    assert _hook(client, "email", {"text": "чужой"}, secret="wrong-secret-value!!").status_code == 401
    assert client.post("/s/hooks/email?secret=" + SECRET, json={"text": "query"}).status_code == 401
    saved = _hook(client, "email", {
        "external_id": "mail-1",
        "direction": "in",
        "from": "Иванова <docs@example.ru>",
        "subject": "Скан",
        "text": "Подписанный скан",
        "html": "<p>скан</p>",
        "secret": SECRET,
        "meta": {"secret": "не класть", "box": "inbox"},
    })
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["created"] is True
    assert body["channel"] == "email"
    assert body["status"] == "received"
    assert body["package_id"] is None
    assert body["body_html"] == "<p>скан</p>"
    assert body["meta"] == {"box": "inbox"}
    assert SECRET not in saved.text
    assert calls == []
    row = client.app.state.db.one("SELECT meta_json, body_text FROM messages WHERE id = ?", (body["id"],))
    assert "secret" not in row["meta_json"]
    assert SECRET not in row["body_text"]


def test_idempotent_by_channel_and_external_id(client):
    first = _hook(client, "tg", {"external_id": "tg-1", "message": "первый текст"})
    assert first.status_code == 200, first.text
    second = _hook(client, "tg", {"external_id": "tg-1", "message": "другой текст"})
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["body_text"] == "первый текст"
    other = _hook(client, "max", {"external_id": "tg-1", "text": "тот же ключ, другой канал"})
    assert other.json()["created"] is True
    assert other.json()["id"] != first.json()["id"]
    blank_a = _hook(client, "max", {"text": "без ключа один"})
    blank_b = _hook(client, "max", {"text": "без ключа два"})
    assert blank_a.json()["created"] is True
    assert blank_b.json()["created"] is True
    assert blank_a.json()["id"] != blank_b.json()["id"]
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 4


def test_hook_validation(client):
    package = _package(client)
    linked = _hook(client, "email", {
        "external_id": "mail-pkg",
        "subject": "К заявке",
        "text": "текст",
        "package": package["number"],
        "direction": "out",
        "status": "sent",
    })
    assert linked.status_code == 200, linked.text
    assert linked.json()["package_id"] == package["id"]
    assert linked.json()["package_number"] == package["number"]
    assert linked.json()["status"] == "sent"
    assert _hook(client, "email", {"text": "нет такой", "package": "Д-1999-0001"}).status_code == 422
    assert _hook(client, "tg", {"subject": "без текста"}).status_code == 422
    assert _hook(client, "email", {"subject": "только тема"}).status_code == 200
    assert _hook(client, "sms", {"text": "нет канала"}).status_code == 404
    assert _hook(client, "email", {"text": "плохое направление", "direction": "side"}).status_code == 422
    listed = client.get("/s/messages", headers={"X-Inbound-Secret": SECRET})
    assert listed.status_code == 200, listed.text
    assert "body_html" not in listed.json()["messages"][0]
    one = client.get(f"/s/messages/{linked.json()['id']}", headers={"X-Inbound-Secret": SECRET})
    assert one.status_code == 200
    assert one.json()["body_html"] == ""
    assert client.get("/s/messages/9999", headers={"X-Inbound-Secret": SECRET}).status_code == 404
    filtered = client.get(
        "/s/messages",
        params={"channel": "email", "direction": "out", "package": package["number"]},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["messages"][0]["subject"] == "К заявке"
    assert client.get("/s/messages", params={"channel": "sms"}, headers={"X-Inbound-Secret": SECRET}).status_code == 422
    loose = client.get("/s/messages", params={"unbound": 1}, headers={"X-Inbound-Secret": SECRET})
    assert loose.json()["total"] >= 1
    assert all(item["package_id"] is None for item in loose.json()["messages"])


def test_bind_and_delete_package_unlinks(client):
    package = _package(client)
    saved = _hook(client, "email", {"external_id": "loose-1", "text": "ещё без заявки", "subject": "Без номера"})
    mid = saved.json()["id"]
    missing = client.post(
        f"/s/messages/{mid}/bind",
        json={"package": "Д-1999-0099"},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert missing.status_code == 404
    bound = client.post(
        f"/s/messages/{mid}/bind",
        json={"package": package["number"], "actor": "Андрей"},
        headers={"X-Inbound-Secret": SECRET},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["package_id"] == package["id"]
    assert bound.json()["meta"]["bound_by"] == "Андрей"
    with client.app.state.db.tx() as conn:
        conn.execute("DELETE FROM packages WHERE id = ?", (package["id"],))
    left = client.app.state.db.one("SELECT package_id FROM messages WHERE id = ?", (mid,))
    assert left["package_id"] is None


@pytest.mark.parametrize("channel", ["email", "tg", "max"])
def test_adapter_send_is_stub(channel):
    from app.messages import adapter_for

    status, detail = adapter_for(channel).send({"body_text": "секретный текст"})
    assert status == "queued"
    assert "сеть не вызывалась" in detail
    assert "секретный текст" not in detail


def test_ui_feed_tab_and_manual_bind(client):
    package = _package(client, "сделка для вкладки")
    _hook(client, "email", {
        "external_id": "ui-linked",
        "package": package["number"],
        "subject": "Письмо по сделке",
        "text": "Текст на вкладке",
        "html": "<b>жирный</b>",
    })
    _hook(client, "max", {
        "external_id": "ui-loose",
        "text": "Сообщение без заявки <script>alert(1)</script>",
        "from": "водитель",
    })
    home = client.get("/")
    assert 'href="/messages"' in home.text
    assert "Переписка" in home.text
    feed = client.get("/messages")
    assert feed.status_code == 200
    assert "Общая лента" in feed.text
    assert "Письмо по сделке" in feed.text
    assert "не привязано" in feed.text
    assert "Привязать" in feed.text
    assert "Открыть сделку" in feed.text
    assert "&lt;script&gt;" in feed.text
    assert "<script>alert" not in feed.text
    assert "&lt;b&gt;" not in feed.text
    loose = client.get("/messages?unbound=1")
    assert "Сообщение без заявки" in loose.text
    assert "Письмо по сделке" not in loose.text
    mail = client.get("/messages?channel=email")
    assert "Письмо по сделке" in mail.text
    assert "Сообщение без заявки" not in mail.text
    by_number = client.get(f"/messages?package={package['number']}")
    assert "Письмо по сделке" in by_number.text
    assert "Сообщение без заявки" not in by_number.text
    missing = client.get("/messages?package=Д-1999-0001")
    assert "Такой заявки нет" in missing.text
    card = client.get(f"/packages/{package['id']}")
    assert "Переписка · 1" in card.text
    tab = client.get(f"/packages/{package['number']}/messages")
    assert tab.status_code == 200
    assert "Текст на вкладке" in tab.text
    assert "Сообщение без заявки" not in tab.text
    assert "сверху старые" in tab.text
    assert '<button class="btn" type="submit" disabled>Отправить</button>' in tab.text
    assert "форма заложена и выключена" in tab.text
    assert "<b>жирный</b>" not in tab.text
    token = client.cookies["delo_csrf"]
    mid = client.app.state.db.one(
        "SELECT id FROM messages WHERE external_id = ?",
        ("ui-loose",),
    )["id"]
    denied = client.post(
        f"/messages/{mid}/bind",
        data={"csrf": "nope", "package": package["number"], "back": "/messages?unbound=1"},
        follow_redirects=False,
    )
    assert denied.status_code == 303
    assert "err=csrf" in denied.headers["location"]
    bound = client.post(
        f"/messages/{mid}/bind",
        data={"csrf": token, "package": package["number"], "back": "/messages?unbound=1"},
        follow_redirects=True,
    )
    assert bound.status_code == 200, bound.text
    assert "Сообщение привязано к заявке" in bound.text
    assert "Сообщение без заявки" in bound.text
    assert f"/packages/{package['id']}/messages" in str(bound.url)
    again = client.get("/messages?unbound=1")
    assert "Сообщение без заявки" not in again.text
    bad = client.post(
        f"/messages/{mid}/bind",
        data={"csrf": token, "package": "нет-такой", "back": "/messages"},
        follow_redirects=True,
    )
    assert "Заявка не найдена" in bad.text


def test_seed_messages(env, monkeypatch):
    import sys

    from app.main import create_app
    from app.seed import main

    monkeypatch.setattr(sys, "argv", ["app.seed"])
    assert main() == 0
    monkeypatch.setattr(sys, "argv", ["app.seed", "--force"])
    assert main() == 0
    local = TestClient(create_app())
    rows = local.app.state.db.q(
        "SELECT channel, package_id, external_id, party_id FROM messages ORDER BY id"
    )
    assert len(rows) == 8
    assert sum(1 for row in rows if row["package_id"] is None) == 2
    linked = local.app.state.db.one(
        "SELECT party_id FROM messages WHERE external_id = ?",
        ("seed-tg-0001-in",),
    )
    assert linked["party_id"]
    page = local.get("/messages")
    assert "не привязано" in page.text
    assert "Заявка на Казань, без номера" in page.text
    assert page.text.index("Водитель написал в MAX") < page.text.index("Заявка на Казань, без номера")
    tab = local.get("/packages/Д-2026-0001/messages")
    assert tab.status_code == 200
    assert tab.text.index("Заявка Д-2026-0001 Москва") < tab.text.index("Подписанный скан")
    assert tab.text.index("Подписанный скан") < tab.text.index("Загрузился, выехал")
    assert "Отправить" in tab.text
    card = local.get("/packages/Д-2026-0001")
    assert "Переписка · 4" in card.text
    assert "нет СТС" in card.text
