"""Настройки: каталог, проверка, черновики, история, snapshot и доступ."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.settings_catalog import BY_KEY, default_text, render_preview
from app.settings_store import (
    draft,
    history,
    publish,
    published,
    reset,
    rollback,
    save_draft,
    snapshot,
    version,
)
from app.settings_validate import ValidationError, validate_value
from tests.conftest import SECRET


def test_migration_creates_settings_tables(client):
    db = client.app.state.db
    expected = {
        "settings": {"key", "type", "value", "version", "updated_by", "updated_at"},
        "settings_drafts": {"key", "value", "updated_by", "updated_at"},
        "settings_history": {"id", "key", "version", "old_value", "new_value", "who", "at", "comment"},
        "settings_version": {"id", "version"},
    }
    for table, columns in expected.items():
        got = {row["name"] for row in db.q(f"PRAGMA table_info({table})")}
        assert columns <= got, table
    assert version(db) == 0


def test_validation_rules():
    with pytest.raises(ValidationError, match="неизвестная подстановка"):
        validate_value("mail.client.body", "Привет, {nope}")
    with pytest.raises(ValidationError, match="неизвестная подстановка"):
        validate_value("mail.client.template_md", "Маршрут {{unknown}}")
    with pytest.raises(ValidationError, match="Непарные скобки"):
        validate_value("mail.client.body", "Привет, {route")
    with pytest.raises(ValidationError, match="Непарные скобки"):
        validate_value("mail.client.body", "Привет, route}")
    with pytest.raises(ValidationError, match="ИП Кртин"):
        validate_value("mail.from_name", "Перевозчик 21")
    with pytest.raises(ValidationError, match="ИП Кртин"):
        validate_value("mail.client.signature", "С уважением, кто-то")
    for value in ("Моргунов", "моргунов", "МОРГУНОВУ"):
        with pytest.raises(ValidationError, match="Моргунов"):
            validate_value("group.checking", value)
    with pytest.raises(ValidationError, match="Пустое значение"):
        validate_value("group.checking", "   ")
    with pytest.raises(ValidationError, match="Слишком длинно"):
        validate_value("mail.from_name", "ИП Кртин " + "x" * 300)
    with pytest.raises(ValidationError, match="64 байт"):
        validate_value("buttons.labels", '{"a": "' + "я" * 33 + '"}')
    with pytest.raises(ValidationError, match="не должны повторяться"):
        validate_value("buttons.labels", '{"a": "Ок", "b": "Ок"}')
    with pytest.raises(ValidationError, match="объект JSON"):
        validate_value("group.reasons", "[]")
    with pytest.raises(ValidationError, match="Пустое значение"):
        validate_value("group.reasons", "{}")
    assert validate_value("mail.client.body", "Добрый день, {file_name}") == "Добрый день, {file_name}"
    assert validate_value("mail.from_name", "ИП Кртин А.С. («Перевозчик 21»)").startswith("ИП Кртин")


def test_draft_publish_history_rollback_reset(client):
    db = client.app.state.db
    key = "group.checking"
    start = version(db)

    save_draft(db, key, "⏳ Проверяю, это черновик", "тестер")
    assert draft(db, key)["value"] == "⏳ Проверяю, это черновик"

    publish(db, key, None, "тестер", "первая публикация")
    assert draft(db, key) is None
    assert published(db, key)["value"] == "⏳ Проверяю, это черновик"
    assert version(db) == start + 1
    first = history(db, key)[0]
    assert first["old_value"] == ""
    assert first["comment"] == "первая публикация"
    assert "⏳ Проверяю, это черновик" in snapshot(db)["values"][key]

    publish(db, key, "⚠ НЕ отправлено: ", "тестер", "вторая")
    assert version(db) == start + 2
    assert snapshot(db)["values"][key] == "⚠ НЕ отправлено: "

    rolled = rollback(db, key, first["id"], "тестер", "")
    assert rolled["value"] == "⏳ Проверяю, это черновик"
    assert published(db, key)["value"] == "⏳ Проверяю, это черновик"
    assert version(db) == start + 3
    assert history(db, key)[0]["comment"] == f"Откат к версии {first['version']}"
    assert history(db, key)[0]["diff"]

    reset(db, key, "тестер", "")
    assert published(db, key) is None
    assert version(db) == start + 4
    assert key not in snapshot(db)["values"]
    assert history(db, key)[0]["comment"] == "Сброс к умолчанию"
    assert history(db, key)[0]["new_value"] == ""


def test_snapshot_api_only_published_and_version_grows(client):
    denied = client.get("/s/settings/snapshot")
    assert denied.status_code == 401
    wrong = client.get("/s/settings/snapshot", headers={"X-Inbound-Secret": "wrong-secret-value!!"})
    assert wrong.status_code == 401

    db = client.app.state.db
    empty = client.get("/s/settings/snapshot", headers={"X-Inbound-Secret": SECRET})
    assert empty.status_code == 200
    body = empty.json()
    assert body["version"] == 0
    assert body["values"] == {}
    assert body["generated_at"]

    publish(db, "mail.from_name", "ИП Кртин А.С. («Перевозчик 21»)", "тестер", "")
    one = client.get("/s/settings/snapshot", headers={"X-Inbound-Secret": SECRET}).json()
    assert one["version"] == 1
    assert list(one["values"]) == ["mail.from_name"]
    assert "mail.client.body" not in one["values"]

    publish(db, "group.already_sent", "✅ Уже отправлено", "тестер", "")
    two = client.get("/s/settings/snapshot", headers={"X-Inbound-Secret": SECRET}).json()
    assert two["version"] == 2
    assert two["version"] > one["version"]
    assert set(two["values"]) == {"mail.from_name", "group.already_sent"}


def test_settings_page_is_admin_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "basic")
    monkeypatch.setenv("NOTIFY_TG", "0")
    monkeypatch.setenv("MESSENGER_CHECK", "off")
    monkeypatch.setenv("DELO_USER_SERGEY_PASSWORD", "test-sergey-password")
    monkeypatch.delenv("DELO_USER_ANDREY_PASSWORD", raising=False)
    monkeypatch.delenv("UI_USER", raising=False)
    monkeypatch.delenv("UI_PASSWORD", raising=False)
    from app.auth import hash_password
    from app.main import create_app

    local = TestClient(create_app())
    moment = "2026-11-12T10:00:00"
    with local.app.state.db.tx() as conn:
        conn.execute(
            """INSERT INTO users (login, display_name, role_code, password_hash, active, created_at)
               VALUES ('smena', 'Смена', 'dispatcher', ?, 1, ?)""",
            (hash_password("test-smena-password"), moment),
        )
        conn.execute(
            """INSERT INTO users (login, display_name, role_code, password_hash, active, created_at)
               VALUES ('operator', 'Оператор', 'operator', ?, 1, ?)""",
            (hash_password("test-operator-password"), moment),
        )

    admin = local.get("/settings", auth=("sergey", "test-sergey-password"))
    assert admin.status_code == 200
    assert "Настройки диспетчерской" in admin.text
    assert "Письмо заказчику" in admin.text
    assert "Заявка перевозчику" in admin.text
    assert "Посты в группе" in admin.text
    assert "Кнопки" in admin.text
    assert "Стадии и нюансы" in admin.text

    for login, password in (("smena", "test-smena-password"), ("operator", "test-operator-password")):
        denied = local.get("/settings", auth=(login, password))
        assert denied.status_code == 403, login
        home = local.get("/", auth=(login, password))
        assert 'href="/settings"' not in home.text

    home = local.get("/", auth=("sergey", "test-sergey-password"))
    assert 'href="/settings"' in home.text


def test_preview_uses_synthetic_deal(client):
    template = default_text(BY_KEY["mail.client.template_md"])
    preview = render_preview(BY_KEY["mail.client.template_md"], template)
    assert "Нижний Новгород — Москва" in preview
    assert "Д-2026-0001" in preview
    assert "{{" not in preview and "}}" not in preview
    assert date.today().strftime("%d.%m.%Y") in preview
    body = render_preview(BY_KEY["mail.client.body"], default_text(BY_KEY["mail.client.body"]))
    assert "Д-2026-0001-zayavka.pdf" in body
    assert "{file_name}" not in body


def test_page_posts_draft_and_publish(client):
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Предпросмотр на синтетическом деле" in page.text
    assert "mail.client.body" in page.text
    token = client.cookies["delo_csrf"]

    saved = client.post(
        "/settings/group.checking/draft",
        data={"csrf": token, "value": "⏳ Черновик из формы"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert draft(client.app.state.db, "group.checking")["value"] == "⏳ Черновик из формы"

    published_response = client.post(
        "/settings/group.checking/publish",
        data={"csrf": token, "value": "⏳ Черновик из формы", "comment": "из формы"},
        follow_redirects=False,
    )
    assert published_response.status_code == 303
    assert published(client.app.state.db, "group.checking")["value"] == "⏳ Черновик из формы"
    assert history(client.app.state.db, "group.checking")[0]["comment"] == "из формы"

    bad = client.post(
        "/settings/mail.client.body/publish",
        data={"csrf": token, "value": "Текст с {nope}"},
        follow_redirects=True,
    )
    assert bad.status_code == 200
    assert "неизвестная подстановка" in bad.text
