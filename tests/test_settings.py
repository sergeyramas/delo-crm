"""Настройки: каталог, проверка, черновики, история, snapshot, API и доступ."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.settings_catalog import BY_KEY, KEYS, default_text, render_placeholders, render_preview
from app.settings_constants import PROTECTED, PROTECTED_NAMES, SENDER, SIGNER
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
from app.settings_validate import (
    EMPTY,
    FORBIDDEN_WORD,
    LINK_NOT_ALLOWED,
    MISSING_SIGNER,
    REQUISITES_NOT_ALLOWED,
    TOO_LONG,
    UNKNOWN_PLACEHOLDER,
    ValidationError,
    validate_value,
)
from tests.conftest import SECRET

SIGNER_KEYS = ("mail.client.body", "mail.client.template_md", "mail.mirror.template_md")


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


def test_catalog_has_no_editable_signer_or_from():
    assert "mail.from_name" not in BY_KEY
    assert "mail.client.signature" not in BY_KEY
    for key in SIGNER_KEYS:
        item = BY_KEY[key]
        assert "подписант" in item.placeholders, key
        assert set(PROTECTED_NAMES) <= set(item.placeholders), key
        assert "{подписант}" in default_text(item), key
    for item in KEYS:
        text = default_text(item)
        assert "@" not in text, item.key
        assert "http://" not in text and "https://" not in text and "www." not in text, item.key


def test_all_catalog_defaults_pass_validation():
    for item in KEYS:
        assert validate_value(item, default_text(item))


def test_validation_codes_and_neutral_messages():
    with pytest.raises(ValidationError) as unknown:
        validate_value("mail.client.body", "Привет, {nope}")
    assert unknown.value.code == UNKNOWN_PLACEHOLDER
    assert "неизвестная подстановка" in str(unknown.value)

    with pytest.raises(ValidationError) as unknown_md:
        validate_value("mail.client.template_md", "Маршрут {{unknown}}")
    assert unknown_md.value.code == UNKNOWN_PLACEHOLDER

    for broken in ("Привет, {route", "Привет, route}"):
        with pytest.raises(ValidationError) as exc:
            validate_value("mail.client.body", broken)
        assert exc.value.code == UNKNOWN_PLACEHOLDER

    for word in ("Моргунов", "моргунов", "МОРГУНОВУ", "моргуновой", "Моргуновым"):
        with pytest.raises(ValidationError) as exc:
            validate_value("group.checking", word)
        assert exc.value.code == FORBIDDEN_WORD
        assert "моргунов" not in str(exc.value).lower()
        assert str(exc.value) == "текст не принят"

    for value in (
        "ссылка https://example.test/a",
        "ссылка http://example.test",
        "сайт www.example.test",
        "почта user@example.test",
    ):
        with pytest.raises(ValidationError) as exc:
            validate_value("group.checking", value)
        assert exc.value.code == LINK_NOT_ALLOWED
        assert str(exc.value) == "текст не принят"
        assert "http" not in str(exc.value).lower()
        assert "www" not in str(exc.value).lower()

    for digits in ("123456789", "1234567890", "123456789012", "12345678901234567890"):
        with pytest.raises(ValidationError) as exc:
            validate_value("group.checking", f"номер {digits}")
        assert exc.value.code == REQUISITES_NOT_ALLOWED
        assert str(exc.value) == "текст не принят"
        assert "инн" not in str(exc.value).lower()
        assert "бик" not in str(exc.value).lower()

    for key in SIGNER_KEYS:
        with pytest.raises(ValidationError) as exc:
            validate_value(key, "Текст без подписи")
        assert exc.value.code == MISSING_SIGNER, key
        assert "подписант" in str(exc.value)

    with pytest.raises(ValidationError) as too_long:
        validate_value("group.checking", "x" * 301)
    assert too_long.value.code == TOO_LONG

    for empty in ("   ", "", None):
        with pytest.raises(ValidationError) as exc:
            validate_value("group.checking", empty)
        assert exc.value.code == EMPTY

    with pytest.raises(ValidationError) as label_long:
        validate_value("buttons.labels", '{"a": "' + "я" * 33 + '"}')
    assert label_long.value.code == TOO_LONG
    with pytest.raises(ValidationError, match="не должны повторяться"):
        validate_value("buttons.labels", '{"a": "Ок", "b": "Ок"}')
    with pytest.raises(ValidationError, match="объект JSON"):
        validate_value("group.reasons", "[]")

    assert validate_value("mail.client.body", "Добрый день, {file_name}\n{подписант}") == (
        "Добрый день, {file_name}\n{подписант}"
    )
    assert validate_value("group.checking", "⏳ Проверяю…") == "⏳ Проверяю…"


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

    publish(db, "group.checking", "⏳ Проверяю…", "тестер", "")
    one = client.get("/s/settings/snapshot", headers={"X-Inbound-Secret": SECRET}).json()
    assert one["version"] == 1
    assert list(one["values"]) == ["group.checking"]
    assert "mail.client.body" not in one["values"]

    publish(db, "group.already_sent", "✅ Уже отправлено", "тестер", "")
    two = client.get("/s/settings/snapshot", headers={"X-Inbound-Secret": SECRET}).json()
    assert two["version"] == 2
    assert two["version"] > one["version"]
    assert set(two["values"]) == {"group.checking", "group.already_sent"}


def test_snapshot_ignores_legacy_protected_keys(client):
    db = client.app.state.db
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO settings (key, type, value, version, updated_by, updated_at)
               VALUES ('mail.from_name', 'text', 'старое значение', 1, 'тестер', '2026-11-12T10:00:00')"""
        )
        conn.execute(
            """INSERT INTO settings (key, type, value, version, updated_by, updated_at)
               VALUES ('mail.client.signature', 'text', 'старое значение', 1, 'тестер', '2026-11-12T10:00:00')"""
        )
    body = snapshot(db)
    assert "mail.from_name" not in body["values"]
    assert "mail.client.signature" not in body["values"]


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


def test_page_shows_protected_values_read_only(client):
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Защищённые подстановки" in page.text
    assert SIGNER in page.text
    assert "mail.from_name" not in page.text
    assert "mail.client.signature" not in page.text
    assert 'name="value"' in page.text


def test_preview_uses_synthetic_deal_and_protected_values(client):
    template = default_text(BY_KEY["mail.client.template_md"])
    preview = render_preview(BY_KEY["mail.client.template_md"], template)
    assert "Нижний Новгород — Москва" in preview
    assert "Д-2026-0001" in preview
    assert "{{" not in preview and "}}" not in preview
    assert date.today().strftime("%d.%m.%Y") in preview
    assert SIGNER in preview
    assert SENDER in preview
    assert "{подписант}" not in preview
    assert "{отправитель}" not in preview

    body = render_preview(BY_KEY["mail.client.body"], default_text(BY_KEY["mail.client.body"]))
    assert "Д-2026-0001-zayavka.pdf" in body
    assert "{file_name}" not in body
    assert SIGNER in body

    assert render_placeholders("{подписант}", PROTECTED) == SIGNER
    assert render_placeholders("{реквизиты}", PROTECTED) == ""
    assert render_placeholders("{неизвестно}", PROTECTED) == "{неизвестно}"
    assert render_placeholders("{подписант.__class__}", PROTECTED) == "{подписант.__class__}"
    assert render_placeholders("{0}", PROTECTED) == "{0}"


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

    forbidden = client.post(
        "/settings/group.checking/publish",
        data={"csrf": token, "value": "Моргунов"},
        follow_redirects=True,
    )
    assert forbidden.status_code == 200
    assert "текст не принят" in forbidden.text
    assert "Моргунов" not in forbidden.text


def test_settings_write_api_validates_and_protects_protected_keys(client):
    headers = {"X-Inbound-Secret": SECRET}
    assert client.post("/s/settings/group.checking", json={"value": "⏳ Проверяю…"}).status_code == 401

    ok = client.post(
        "/s/settings/group.checking",
        headers=headers,
        json={"value": "⏳ Проверяю API", "actor": "воркер", "comment": "через API"},
    )
    assert ok.status_code == 200
    assert snapshot(client.app.state.db)["values"]["group.checking"] == "⏳ Проверяю API"

    drafted = client.post(
        "/s/settings/group.already_sent/draft",
        headers=headers,
        json={"value": "✅ Черновик API"},
    )
    assert drafted.status_code == 200
    assert draft(client.app.state.db, "group.already_sent")["value"] == "✅ Черновик API"
    assert "group.already_sent" not in snapshot(client.app.state.db)["values"]

    link = client.post(
        "/s/settings/group.checking/publish",
        headers=headers,
        json={"value": "ссылка https://example.test"},
    )
    assert link.status_code == 422
    assert link.json()["code"] == LINK_NOT_ALLOWED
    assert link.json()["message"] == "текст не принят"

    banned = client.post(
        "/s/settings/group.checking",
        headers=headers,
        json={"value": "Моргунову"},
    )
    assert banned.status_code == 422
    assert banned.json()["code"] == FORBIDDEN_WORD
    assert banned.json()["message"] == "текст не принят"
    assert "моргунов" not in banned.text.lower()

    no_signer = client.post(
        "/s/settings/mail.client.body",
        headers=headers,
        json={"value": "Текст без подписи"},
    )
    assert no_signer.status_code == 422
    assert no_signer.json()["code"] == MISSING_SIGNER

    for key in ("mail.from_name", "mail.client.signature"):
        denied_key = client.post(
            f"/s/settings/{key}",
            headers=headers,
            json={"value": f"{SIGNER}"},
        )
        assert denied_key.status_code == 422, key
        assert denied_key.json()["code"] == "unknown_key"
        assert key not in snapshot(client.app.state.db)["values"]
