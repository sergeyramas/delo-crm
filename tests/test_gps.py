"""GPS-заготовка: черновик инструкции и заглушка ATI, без сети."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import SECRET


def _driver(client: TestClient) -> tuple[dict, dict]:
    created = client.post(
        "/s/inbound/doc",
        json={
            "source": "manual",
            "text": "рейс на Казань",
            "client_hint": "ООО «Северный Хлеб»",
            "driver_name": "Орлов К.",
            "driver_phone": "+7 903 555-00-10",
            "driver_email": "orlov.k@example.ru",
            "driver_channel": "MAX",
            "route_from": "Москва",
            "route_to": "Казань",
        },
        headers={"X-Inbound-Secret": SECRET},
    )
    assert created.status_code == 200
    package = created.json()
    party = client.app.state.db.one("SELECT * FROM parties WHERE kind = 'driver'")
    assert party["gps_status"] == "off"
    assert package["id"]
    return package, party


def test_instruction_is_draft_and_invites(client, monkeypatch):
    calls = []

    def boom(*_args, **_kwargs):
        calls.append("net")
        raise AssertionError("сеть")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    package, party = _driver(client)
    page = client.get(f"/parties/{party['id']}?from={package['id']}")
    assert page.status_code == 200
    assert "не подключён" in page.text
    assert "Отслеживать" in page.text
    assert "Отправить инструкцию для отслеживания" in page.text
    assert "/static/gps/driver-instruction.pdf" in page.text
    pdf = client.get("/static/gps/driver-instruction.pdf")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    token = client.cookies["delo_csrf"]
    tracked = client.post(
        f"/parties/{party['id']}/gps/track",
        data={"csrf": token, "next": f"/parties/{party['id']}?from={package['id']}#gps"},
        follow_redirects=True,
    )
    assert tracked.status_code == 200
    assert "не вызывались" in tracked.text
    assert "не подключён" in tracked.text
    polls = client.app.state.db.q("SELECT called, endpoint FROM gps_polls ORDER BY id")
    assert [row["called"] for row in polls] == [0, 0]
    assert any("users_activity" in row["endpoint"] for row in polls)
    assert any("map_route/by_deal" in row["endpoint"] for row in polls)
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM gps_points")["n"] == 0
    sent = client.post(
        f"/parties/{party['id']}/gps/instruction",
        data={
            "csrf": token,
            "channel": "max",
            "package_id": str(package["id"]),
            "next": f"/parties/{party['id']}?from={package['id']}#gps",
        },
        follow_redirects=True,
    )
    assert sent.status_code == 200
    assert "приглашён" in sent.text
    assert "Черновик" in sent.text
    draft = client.app.state.db.one("SELECT * FROM gps_drafts")
    assert draft["status"] == "draft"
    assert draft["channel"] == "max"
    assert draft["attachment"] == "gps/driver-instruction.pdf"
    assert "play.google.com/store/apps/details?id=su.ati.driver" in draft["body"]
    assert "не ушло" in draft["detail"] or "не вызывались" in draft["detail"]
    assert client.app.state.db.one("SELECT gps_status FROM parties WHERE id = ?", (party["id"],))["gps_status"] == "invited"
    assert client.app.state.db.one("SELECT gps_status FROM packages WHERE id = ?", (package["id"],))["gps_status"] == "invited"
    deal = client.get(f"/packages/{package['id']}")
    assert "приглашён" in deal.text
    assert "Отслеживать" in deal.text
    assert "Отправить инструкцию для отслеживания" in deal.text
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM outbound_messages")["n"] == 0
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 0
    assert calls == []


def test_deal_instruction_and_email_channel(client):
    package, party = _driver(client)
    token = client.cookies["delo_csrf"]
    client.get(f"/packages/{package['id']}")
    sent = client.post(
        f"/packages/{package['id']}/gps/instruction",
        data={"csrf": token, "channel": "email", "next": f"/packages/{package['id']}#gps"},
        follow_redirects=True,
    )
    assert sent.status_code == 200
    assert "приглашён" in sent.text
    draft = client.app.state.db.one("SELECT channel, status FROM gps_drafts")
    assert draft["channel"] == "email"
    assert draft["status"] == "draft"
    party_page = client.get(f"/parties/{party['id']}")
    assert "приглашён" in party_page.text


def test_coordinates_follow_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "basic")
    monkeypatch.setenv("NOTIFY_TG", "0")
    monkeypatch.setenv("MESSENGER_CHECK", "off")
    monkeypatch.setenv("DELO_USER_SERGEY_PASSWORD", "test-sergey-password")
    monkeypatch.setenv("DELO_USER_ANDREY_PASSWORD", "test-andrey-password")
    from app.auth import hash_password
    from app.gps import store_point
    from app.main import create_app

    local = TestClient(create_app())
    created = local.post(
        "/s/inbound/doc",
        json={
            "source": "manual",
            "text": "рейс",
            "client_hint": "Клиент",
            "driver_name": "Орлов К.",
            "driver_phone": "+7 903 555-00-11",
        },
        headers={"X-Inbound-Secret": SECRET},
        auth=("sergey", "test-sergey-password"),
    ).json()
    db = local.app.state.db
    moment = "2026-11-06T12:00:00"
    with db.tx() as conn:
        conn.execute("UPDATE packages SET dispatcher_login = 'andrey' WHERE id = ?", (created["id"],))
        conn.execute(
            """INSERT INTO users (login, display_name, role_code, password_hash, active, created_at)
               VALUES ('smena', 'Смена', 'dispatcher', ?, 1, ?)""",
            (hash_password("test-smena-password"), moment),
        )
        conn.execute(
            """INSERT INTO users (login, display_name, role_code, password_hash, active, created_at)
               VALUES ('clerk', 'Оператор', 'operator', ?, 1, ?)""",
            (hash_password("test-clerk-password"), moment),
        )
    store_point(db, lat=48.12345, lon=37.54321, recorded_at=moment, package_id=created["id"])
    hidden = local.get(f"/packages/{created['id']}", auth=("clerk", "test-clerk-password"))
    assert hidden.status_code == 200
    assert "48.12345" not in hidden.text
    assert "Координаты скрыты" in hidden.text
    other = local.get(f"/packages/{created['id']}", auth=("smena", "test-smena-password"))
    assert "48.12345" not in other.text
    seen = local.get(f"/packages/{created['id']}", auth=("andrey", "test-andrey-password"))
    assert "48.12345" in seen.text
    boss = local.get(f"/packages/{created['id']}", auth=("sergey", "test-sergey-password"))
    assert "37.54321" in boss.text


def test_points_expire_thirty_days_after_trip(client):
    from datetime import datetime, timedelta

    from app.gps import expires_after_trip, refresh_expiry, store_point

    package, _party = _driver(client)
    db = client.app.state.db
    with db.tx() as conn:
        conn.execute(
            "UPDATE packages SET stage = 'v_puti', updated_at = '2026-11-06T12:00:00' WHERE id = ?",
            (package["id"],),
        )
    point = store_point(
        db,
        lat=55.75,
        lon=37.62,
        recorded_at="2026-11-06T12:00:00",
        package_id=package["id"],
    )
    assert point["expires_at"] == ""
    assert expires_after_trip("v_puti", "2026-11-06T12:00:00") == ""
    finished = "2026-11-08T18:00:00"
    with db.tx() as conn:
        conn.execute(
            "UPDATE packages SET stage = 'dostavlen', updated_at = ? WHERE id = ?",
            (finished, package["id"]),
        )
        refresh_expiry(conn, package["id"])
    saved = db.one("SELECT expires_at FROM gps_points WHERE id = ?", (point["id"],))
    expected = (datetime.fromisoformat(finished) + timedelta(days=30)).isoformat(timespec="seconds")
    assert saved["expires_at"] == expected


def test_fleet_driver_card_has_buttons(client):
    db = client.app.state.db
    moment = "2026-11-06T12:00:00"
    with db.tx() as conn:
        cur = conn.execute(
            """INSERT INTO carriers (name, kind, inn, phone, email, created_at, updated_at)
               VALUES ('ИП Тест', 'ip', '770200000099', '+7 900 111-22-33', '', ?, ?)""",
            (moment, moment),
        )
        carrier_id = int(cur.lastrowid)
        cur = conn.execute(
            """INSERT INTO drivers (carrier_id, name, phone, created_at, updated_at)
               VALUES (?, 'Тестов В.', '+7 900 111-22-44', ?, ?)""",
            (carrier_id, moment, moment),
        )
        driver_id = int(cur.lastrowid)
    page = client.get(f"/carriers/{carrier_id}/drivers/{driver_id}")
    assert page.status_code == 200
    assert "Отслеживать" in page.text
    assert "Отправить инструкцию для отслеживания" in page.text
    assert "не подключён" in page.text
    token = client.cookies["delo_csrf"]
    card = f"/carriers/{carrier_id}/drivers/{driver_id}#gps"
    denied = client.post(
        f"/drivers/{driver_id}/gps/instruction",
        data={"csrf": token, "channel": "email", "next": card},
        follow_redirects=True,
    )
    assert denied.status_code == 200
    assert "нет почты" in denied.text
    assert db.one("SELECT gps_status FROM drivers WHERE id = ?", (driver_id,))["gps_status"] == "off"
    assert db.one("SELECT COUNT(*) AS n FROM gps_drafts")["n"] == 0
    ok = client.post(
        f"/drivers/{driver_id}/gps/instruction",
        data={"csrf": token, "channel": "tg", "next": card},
        follow_redirects=True,
    )
    assert "приглашён" in ok.text
    assert db.one("SELECT status, channel FROM gps_drafts")["status"] == "draft"


@pytest.mark.parametrize("stage", ["dostavlen", "originaly", "otkaz"])
def test_finished_stage_sets_expiry(stage):
    from app.gps import expires_after_trip

    got = expires_after_trip(stage, "2026-11-01T00:00:00")
    assert got == "2026-12-01T00:00:00"
