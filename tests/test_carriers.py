"""Реестр перевозчиков, PDF по образцу, переговоры и трекер. Почты нет."""

import re

from fastapi.testclient import TestClient
from pypdf import PdfReader
from io import BytesIO

from app.carriers import SAMPLE_CARRIER, SAMPLE_DRIVER, SAMPLE_EXPEDITOR, SAMPLE_VEHICLE, seed_doronin
from tests.conftest import SECRET
from tests.test_crm import _post

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
HEADERS = {"X-Inbound-Secret": SECRET}


def _doronin(client):
    with client.app.state.db.tx() as conn:
        return seed_doronin(conn, client.app.state.db.data_dir, "2026-11-06T12:00:00")


def _package(client):
    created = _post(client, {
        "source": "manual",
        "text": "заявка под связку",
        "client_hint": "ООО Поле",
        "client_email": "go@example.ru",
        "route_from": "Москва",
        "route_to": "Казань",
        "cargo": "тент",
        "weight": "3 т",
    })
    assert created.status_code == 200, created.text
    return created.json()


def test_doronin_seed_matches_sample_fields(client):
    ids = _doronin(client)
    carrier = client.get(f"/s/carriers/{ids['carrier_id']}", headers=HEADERS)
    assert carrier.status_code == 200, carrier.text
    body = carrier.json()
    assert body["name"] == SAMPLE_CARRIER["name"]
    assert body["kind"] == "self_employed"
    assert body["inn"] == SAMPLE_CARRIER["inn"]
    assert body["phone"] == SAMPLE_CARRIER["phone"]
    vehicle = body["vehicles"][0]
    assert vehicle["vin"] == SAMPLE_VEHICLE["vin"]
    assert vehicle["plate"] == SAMPLE_VEHICLE["plate"]
    assert vehicle["sts_series"] == "99 99"
    assert vehicle["sts_number"] == "116320"
    assert vehicle["sts_file"]["original_name"] == "sts.pdf"
    driver = body["drivers"][0]
    assert driver["name"] == SAMPLE_DRIVER["name"]
    assert driver["birth_date"] == "1993-08-20"
    assert driver["passport_number"] == "088797"
    assert driver["license_number"] == "723115"
    assert driver["consent"] is True
    assert driver["user_is_carrier"] is True
    assert driver["passport_file"]
    assert driver["license_file"]
    with client.app.state.db.tx() as conn:
        expeditor = conn.execute("SELECT * FROM expeditor WHERE id = 1").fetchone()
    assert expeditor["name"] == SAMPLE_EXPEDITOR["name"]
    assert expeditor["edo_id"] == SAMPLE_EXPEDITOR["edo_id"]
    page = client.get("/carriers?q=524506946364")
    assert "Доронин" in page.text
    phone = client.get("/carriers?q=986+769-59-42")
    assert "Доронин" in phone.text
    missing = client.get("/carriers?q=нет-такого")
    assert "Никого не найдено" in missing.text


def test_api_crud_files_assign_and_pdf(client):
    ids = _doronin(client)
    package = _package(client)
    listed = client.get("/api/packages/" + package["number"] + "/client-attachments")
    assert listed.json()["missing"] == ["sts", "license", "passport"]
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM party_scans")["n"] == 0

    renamed = client.patch(
        f"/s/carriers/{ids['carrier_id']}",
        headers=HEADERS,
        json={"note": "уточнение"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["note"] == "уточнение"

    uploaded = client.post(
        f"/s/carriers/{ids['carrier_id']}/files",
        headers=HEADERS,
        data={"kind": "doc"},
        files={"file": ("dogovor.pdf", PDF, "application/pdf")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["file_kind"] == "doc"

    assigned = client.post(
        f"/s/packages/{package['number']}/assign",
        headers=HEADERS,
        json={
            "carrier_id": ids["carrier_id"],
            "vehicle_id": ids["vehicle_id"],
            "driver_id": ids["driver_id"],
        },
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["ready"] is True
    assert assigned.json()["gaps"] == []

    pdf = client.post(f"/s/packages/{package['id']}/kartochka.pdf", headers=HEADERS, json={"actor": "тест"})
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert b"STUB-SCAN-NOT-IN-KARTOCHKA" not in pdf.content
    text = PdfReader(BytesIO(pdf.content)).pages[0].extract_text() or ""
    for needle in (
        "Карточка перевозчика для грузоотправителя",
        "ИП Кртин Андрей Станиславович",
        "212909112476 / 315213000004340",
        "428003, Чувашская Республика",
        "Кировское отделение № 8612 ПАО Сбербанк",
        "40802810975000001318, БИК 043304609, к/с 30101810500000000609",
        "8-987-760-97-09",
        "1828201@mail.ru",
        "Андрей, 8-987-760-97-09",
        "8152462",
        "2AE0206731C-CCD3-448C-B721-68B0544F7902",
        "ПЕРЕВОЗЧИК (САМОЗАНЯТЫЙ)",
        "Доронин Артём Олегович (самозанятый)",
        "524506946364",
        "+7 986 769-59-42",
        "2834DE (марка и модель по СТС)",
        "тент; объём 19,4 м³",
        "задняя, боковая, верхняя",
        "Х 822 ВЕ 252 / нет",
        "XU42834DEC0003017",
        "99 99 116320",
        "Доронин Артём Олегович, 20.08.1993 г.р.",
        "22 13 088797",
        "520-012",
        "99 26 723115",
        "категории B, B1, C, C1, M",
        "Данные водителя передаются с его согласия только для оформления пропуска и заявки.",
    ):
        assert needle in text, needle
    row = client.app.state.db.one(
        "SELECT stored_name FROM files WHERE package_id = ? ORDER BY id DESC LIMIT 1",
        (package["id"],),
    )
    assert row["stored_name"] == f"packages/{package['number']}/kartochka-perevozchika.pdf"
    path = client.app.state.db.data_dir / "files" / row["stored_name"]
    assert path.is_file()
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM outbound_messages")["n"] == 0

    deal = client.get(f"/packages/{package['id']}")
    assert "Кто везёт" in deal.text
    assert "Доронин Артём Олегович" in deal.text
    assert "Готово: поля карточки" in deal.text
    assert "Перевозчик и водитель совпали" in deal.text
    assert "Документы по получателям" in deal.text
    assert "Собрать пакет заказчику" in deal.text


def test_negotiations_stay_and_tracker_hooks(client):
    package = _package(client)
    first = client.post(
        f"/s/packages/{package['id']}/negotiations",
        headers=HEADERS,
        json={"text": "Окна 10:00–16:00", "author": "Сергей"},
    )
    second = client.post(
        f"/s/packages/{package['id']}/negotiations",
        headers=HEADERS,
        json={"text": "Ставка без НДС", "author": "Андрей"},
    )
    assert first.status_code == 200 and second.status_code == 200
    again = client.get(f"/s/packages/{package['id']}/negotiations", headers=HEADERS)
    notes = again.json()["negotiations"]
    assert [item["text"] for item in notes] == ["Окна 10:00–16:00", "Ставка без НДС"]
    page = client.get(f"/packages/{package['id']}/negotiations")
    assert "Окна 10:00–16:00" in page.text
    assert "Ставка без НДС" in page.text
    assert "Сергей" in page.text

    sent = client.patch(
        f"/s/packages/{package['id']}",
        headers=HEADERS,
        json={"status": "otpravleno", "actor": "worker"},
    )
    assert sent.status_code == 200, sent.text
    card = client.get(f"/packages/{package['id']}")
    assert "отправлено" in card.text
    client_row = client.app.state.db.one(
        "SELECT status, channel FROM package_doc_tracks WHERE package_id = ? AND recipient = 'client'",
        (package["id"],),
    )
    assert client_row["status"] == "sent"
    assert client_row["channel"] == "email"

    mirrored = client.patch(
        f"/s/packages/{package['id']}",
        headers=HEADERS,
        json={"mirror_status": "sent", "actor": "worker"},
    )
    assert mirrored.status_code == 200, mirrored.text
    carrier_row = client.app.state.db.one(
        "SELECT status, channel FROM package_doc_tracks WHERE package_id = ? AND recipient = 'carrier'",
        (package["id"],),
    )
    assert carrier_row["status"] == "sent"
    scanned = client.patch(
        f"/s/packages/{package['id']}",
        headers=HEADERS,
        json={"scan_ok": True, "signed_carrier": 1, "actor": "worker"},
    )
    assert scanned.status_code == 200, scanned.text
    carrier_row = client.app.state.db.one(
        "SELECT status FROM package_doc_tracks WHERE package_id = ? AND recipient = 'carrier'",
        (package["id"],),
    )
    assert carrier_row["status"] == "signed"
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM outbound_messages")["n"] == 0

    token = re.search(r'name="csrf" value="([^"]+)"', card.text).group(1)
    manual = client.post(
        f"/packages/{package['id']}/tracks",
        data={"csrf": token, "recipient": "driver", "status": "received", "channel": "max"},
        follow_redirects=False,
    )
    assert manual.status_code == 303
    driver_row = client.app.state.db.one(
        "SELECT status, channel FROM package_doc_tracks WHERE package_id = ? AND recipient = 'driver'",
        (package["id"],),
    )
    assert driver_row["status"] == "received"
    assert driver_row["channel"] == "max"
    fresh = client.get(f"/packages/{package['id']}")
    assert "получено" in fresh.text


def test_add_forms_and_scans_follow_the_package(client):
    ids = _doronin(client)
    package = _package(client)
    menu = client.get("/carriers")
    for label in ("+ Добавить перевозчика", "+ Добавить машину", "+ Добавить водителя", "+ Добавить заказчика"):
        assert label in menu.text
    driver_form = client.get(f"/drivers/new?carrier_id={ids['carrier_id']}")
    assert "Добавить водителя" in driver_form.text
    assert 'name="passport"' in driver_form.text
    assert 'name="license"' in driver_form.text
    assert "Скан паспорта" in driver_form.text
    carrier_form = client.get("/carriers/new")
    assert 'name="doc"' in carrier_form.text
    vehicle_form = client.get("/vehicles/new")
    assert 'name="sts"' in vehicle_form.text
    client_form = client.get("/clients/new")
    assert "Добавить заказчика" in client_form.text
    assert 'name="doc"' in client_form.text
    deal = client.get(f"/packages/{package['id']}")
    assert deal.text.count("+ новый") >= 4
    assigned = client.post(
        f"/s/packages/{package['id']}/assign",
        headers=HEADERS,
        json={"carrier_id": ids["carrier_id"], "vehicle_id": ids["vehicle_id"], "driver_id": ids["driver_id"]},
    )
    assert assigned.status_code == 200, assigned.text
    page = client.get(f"/packages/{package['id']}")
    assert "Из карточек" in page.text
    assert "sts.pdf" in page.text
    assert ">СТС<" in page.text or "СТС" in page.text
    assert "Паспорт" in page.text
    token = re.search(r'name="csrf" value="([^"]+)"', driver_form.text).group(1)
    created = client.post(
        "/drivers",
        data={
            "csrf": token,
            "next": f"/packages/{package['id']}",
            "carrier_id": str(ids["carrier_id"]),
            "name": "Новый Водитель",
            "birth_date": "1990-01-02",
            "phone": "+7 903 111-22-33",
            "passport_series": "45 01",
            "passport_number": "111222",
            "passport_issued_by": "ОВД",
            "passport_issued_at": "2010-05-05",
            "passport_dept_code": "770-001",
            "license_series": "77 11",
            "license_number": "333444",
            "license_issued_at": "2020-01-01",
            "license_issued_by": "ГИБДД",
            "license_valid_until": "2030-01-01",
            "license_categories": "B, C",
            "consent": "1",
        },
        files={
            "passport": ("passport.pdf", PDF, "application/pdf"),
            "license": ("license.pdf", PDF, "application/pdf"),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    assert f"/packages/{package['id']}" in created.headers["location"]
    fresh = client.get(f"/packages/{package['id']}")
    assert "Новый Водитель" in fresh.text
    assert "passport.pdf" in fresh.text
    assert "license.pdf" in fresh.text


def test_roles_dispatcher_writes_operator_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "basic")
    monkeypatch.setenv("NOTIFY_TG", "0")
    monkeypatch.setenv("MESSENGER_CHECK", "off")
    monkeypatch.setenv("DELO_USER_SERGEY_PASSWORD", "sergey-test-password")
    monkeypatch.setenv("DELO_USER_SERGEY_ROLE", "operator")
    monkeypatch.setenv("DELO_USER_ANDREY_PASSWORD", "andrey-test-password")
    monkeypatch.setenv("DELO_USER_ANDREY_ROLE", "dispatcher")
    from app.main import create_app

    app = create_app()
    operator = TestClient(app)
    operator.auth = ("sergey", "sergey-test-password")
    page = operator.get("/carriers/new")
    assert page.status_code == 200
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    denied = operator.post(
        "/carriers",
        data={
            "csrf": token,
            "name": "ИП Тест",
            "kind": "ip",
            "inn": "770123456789",
            "ogrn": "123456789012345",
            "phone": "+7 903 000-00-01",
        },
        follow_redirects=False,
    )
    assert denied.status_code == 303
    assert "err=role" in denied.headers["location"]
    assert operator.app.state.db.one("SELECT COUNT(*) AS n FROM carriers")["n"] == 0

    dispatcher = TestClient(app)
    dispatcher.auth = ("andrey", "andrey-test-password")
    form = dispatcher.get("/carriers/new")
    token = re.search(r'name="csrf" value="([^"]+)"', form.text).group(1)
    created = dispatcher.post(
        "/carriers",
        data={
            "csrf": token,
            "name": "ИП Тест",
            "kind": "ip",
            "inn": "770123456789",
            "ogrn": "315213000004341",
            "phone": "+7 903 000-00-01",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    assert "err=" not in created.headers["location"]
    assert dispatcher.app.state.db.one("SELECT COUNT(*) AS n FROM carriers")["n"] == 1
    expeditor = dispatcher.get("/carriers/expeditor")
    token = re.search(r'name="csrf" value="([^"]+)"', expeditor.text).group(1)
    blocked = dispatcher.post(
        "/carriers/expeditor",
        data={"csrf": token, "name": "Чужое имя"},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert "err=role" in blocked.headers["location"]
