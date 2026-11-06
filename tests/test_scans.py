import base64

from fastapi.testclient import TestClient

from tests.conftest import SECRET
from tests.test_crm import _post

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"png"
JPEG = b"\xff\xd8\xff" + b"\x00jpg"


def _driver(client, plate="А000АА 77", vehicle="тент"):
    created = _post(client, {
        "source": "manual",
        "text": "заявка со водителем",
        "client_hint": "ООО Поле",
        "client_inn": "7701234567",
        "driver_name": "Сидоров Сидор",
        "driver_phone": "+7 903 000-00-77",
        "vehicle": vehicle,
        "plate": plate,
        "driver_license": "77 00 000777",
        "driver_passport": "4500 000777",
    })
    assert created.status_code == 200, created.text
    body = created.json()
    driver = client.app.state.db.one("SELECT * FROM parties WHERE kind = 'driver'")
    assert driver["plate"] == plate
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM party_scans")["n"] == 0
    page = client.get(f"/parties/{driver['id']}")
    token = client.cookies["delo_csrf"]
    return body, driver, token, page


def _upload(client, party_id, token, kind, filename, blob, content_type):
    return client.post(
        f"/parties/{party_id}/scans",
        data={"csrf": token, "kind": kind},
        files={"file": (filename, blob, content_type)},
        follow_redirects=True,
    )


def test_missing_badges_and_manual_upload(client):
    body, driver, token, page = _driver(client)
    assert "нет СТС" in page.text
    assert "нет прав" in page.text
    assert "нет паспорта" in page.text
    assert "Загрузить СТС" in page.text
    assert "А000АА 77" in page.text
    deal = client.get(f"/packages/{body['id']}")
    assert "нет СТС" in deal.text
    assert "нет прав" in deal.text
    assert "нет паспорта" in deal.text
    client_party = client.app.state.db.one("SELECT id FROM parties WHERE kind = 'client'")
    client_page = client.get(f"/parties/{client_party['id']}")
    assert "Сканы для пакета заказчику" not in client_page.text
    listed = client.get(f"/api/packages/{body['number']}/client-attachments")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["attachments"] == []
    assert payload["missing"] == ["sts", "license", "passport"]
    assert payload["org_card"] is False
    assert payload["plate"] == "А000АА 77"

    saved = _upload(client, driver["id"], token, "sts", "стс.pdf", PDF, "application/pdf")
    assert saved.status_code == 200, saved.text
    assert "Скан записан на диск" in saved.text
    assert "нет СТС" not in saved.text
    assert "СТС есть" in saved.text
    assert "нет прав" in saved.text
    row = client.app.state.db.one("SELECT * FROM party_scans")
    assert row["kind"] == "sts"
    assert row["plate"] == "А000АА 77"
    assert row["vehicle"] == "тент"
    assert row["stored_name"].startswith(f"parties/{driver['id']}/sts_")
    assert row["stored_name"].endswith(".pdf")
    path = client.app.state.db.data_dir / "files" / row["stored_name"]
    assert path.is_file()
    assert path.read_bytes().startswith(b"%PDF")
    assert "app/" not in row["stored_name"]

    opened = client.get(f"/parties/{driver['id']}/scans/sts")
    assert opened.status_code == 200
    assert opened.content.startswith(b"%PDF")
    assert opened.headers["content-type"].startswith("application/pdf")
    assert "inline" in opened.headers["content-disposition"]
    assert "no-store" in opened.headers["cache-control"]
    downloaded = client.get(f"/parties/{driver['id']}/scans/sts?download=1")
    assert "attachment" in downloaded.headers["content-disposition"]
    assert "\n" not in downloaded.headers["content-disposition"]
    assert "\r" not in downloaded.headers["content-disposition"]

    deal_after = client.get(f"/packages/{body['number']}")
    assert "СТС есть" in deal_after.text
    assert "нет прав" in deal_after.text
    assert "нет паспорта" in deal_after.text


def test_three_scans_org_card_and_replace(client):
    body, driver, token, _page = _driver(client, plate="О777ОО 77", vehicle="реф")
    _upload(client, driver["id"], token, "sts", "sts.pdf", PDF, "application/pdf")
    _upload(client, driver["id"], token, "license", "license.jpg", JPEG, "image/jpeg")
    _upload(client, driver["id"], token, "passport", "passport.png", PNG, "image/png")
    first = client.app.state.db.one("SELECT stored_name FROM party_scans WHERE kind = 'sts'")
    replaced = _upload(client, driver["id"], token, "sts", "sts-new.pdf", PDF + b"new", "application/pdf")
    assert "Скан записан" in replaced.text
    rows = client.app.state.db.q("SELECT kind, stored_name FROM party_scans ORDER BY kind")
    assert [row["kind"] for row in rows] == ["license", "passport", "sts"]
    current = client.app.state.db.one("SELECT stored_name FROM party_scans WHERE kind = 'sts'")
    assert current["stored_name"] != first["stored_name"]
    assert not (client.app.state.db.data_dir / "files" / first["stored_name"]).exists()
    assert (client.app.state.db.data_dir / "files" / current["stored_name"]).read_bytes().endswith(b"new")

    carrier = client.post(
        f"/packages/{body['id']}/file",
        data={"csrf": token, "title": "Карточка перевозчика", "note": "не организация"},
        files={"file": ("carrier.pdf", b"%PDF-1.4 carrier", "application/pdf")},
        follow_redirects=True,
    )
    assert carrier.status_code == 200
    org = client.post(
        f"/packages/{body['id']}/file",
        data={"csrf": token, "title": "Карточка организации", "note": "реквизиты"},
        files={"file": ("org.pdf", b"%PDF-1.4 org", "application/pdf")},
        follow_redirects=True,
    )
    assert org.status_code == 200
    assert "Карточка организации приложена" in org.text
    assert "нет СТС" not in org.text
    assert "СТС есть" in org.text

    listed = client.get(f"/api/packages/{body['number']}/client-attachments")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert [item["type"] for item in payload["attachments"]] == ["org_card", "sts", "license", "passport"]
    assert payload["missing"] == []
    assert payload["org_card"] is True
    sts = next(item for item in payload["attachments"] if item["type"] == "sts")
    assert sts["plate"] == "О777ОО 77"
    assert sts["vehicle"] == "реф"
    assert sts["content_type"] == "application/pdf"
    assert SECRET not in listed.text
    stream = client.get(sts["download_url"])
    assert stream.status_code == 200
    assert stream.content.startswith(b"%PDF")
    assert stream.content.endswith(b"new")
    assert "attachment" in stream.headers["content-disposition"]
    assert "no-store" in stream.headers["cache-control"]
    org_file = client.get(payload["attachments"][0]["download_url"])
    assert org_file.content.startswith(b"%PDF-1.4 org")
    assert client.get(f"/api/packages/{body['id']}/client-attachments/sts").content.endswith(b"new")
    missing = client.get(f"/api/packages/{body['number']}/client-attachments/nope")
    assert missing.status_code == 404


def test_rejects_bad_files_and_keeps_other_party_clean(client):
    body, driver, token, _page = _driver(client)
    data_dir = client.app.state.db.data_dir
    bad_type = _upload(client, driver["id"], token, "passport", "note.html", b"<html>secret</html>", "text/html")
    assert "Не удалось принять скан" in bad_type.text
    disguised = _upload(client, driver["id"], token, "sts", "sts.pdf", b"<html>%PDF</html>", "application/pdf")
    assert "Не удалось принять скан" in disguised.text
    wrong_kind = _upload(client, driver["id"], token, "org_card", "x.pdf", PDF, "application/pdf")
    assert "Не удалось принять скан" in wrong_kind.text
    client_party = client.app.state.db.one("SELECT id FROM parties WHERE kind = 'client'")
    foreign = _upload(client, client_party["id"], token, "sts", "sts.pdf", PDF, "application/pdf")
    assert "Не удалось принять скан" in foreign.text
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM party_scans")["n"] == 0
    party_dir = data_dir / "files" / "parties"
    assert not party_dir.exists() or not any(party_dir.rglob("*"))
    leaked = client.post(
        "/s/inbound/doc",
        json={
            "source": "manual",
            "text": "скан внутри заявки",
            "driver_name": "Сидоров Сидор",
            "driver_phone": "+7 903 000-00-77",
            "attachments": [{"name": "passport.pdf", "content_base64": base64.b64encode(PDF).decode()}],
        },
        headers={"X-Inbound-Secret": SECRET},
    )
    assert leaked.status_code == 200, leaked.text
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM party_scans")["n"] == 0
    from app.scans import MAX_SCAN

    huge = _upload(client, driver["id"], token, "license", "big.pdf", b"%PDF" + b"0" * MAX_SCAN, "application/pdf")
    assert "Не удалось принять скан" in huge.text
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM party_scans")["n"] == 0
    csrf = client.post(
        f"/parties/{driver['id']}/scans",
        data={"csrf": "nope", "kind": "sts"},
        files={"file": ("sts.pdf", PDF, "application/pdf")},
        follow_redirects=True,
    )
    assert "Сессия формы устарела" in csrf.text
    from app.scans import display_name

    shown = display_name("evil.pdf\r\nSet-Cookie: a=b", "pdf")
    assert "\r" not in shown and "\n" not in shown
    injected = _upload(client, driver["id"], token, "sts", "паспорт.pdf", PDF, "application/pdf")
    assert "Скан записан" in injected.text
    opened = client.get(f"/parties/{driver['id']}/scans/sts")
    assert opened.status_code == 200
    assert opened.content.startswith(b"%PDF")
    disposition = opened.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    with client.app.state.db.tx() as conn:
        conn.execute(
            "UPDATE party_scans SET stored_name = ? WHERE kind = 'sts'",
            ("../delo.db",),
        )
    blocked = client.get(f"/parties/{driver['id']}/scans/sts")
    assert blocked.status_code == 404
    assert b"SQLite format" not in blocked.content
    assert (data_dir / "delo.db").is_file()
    assert body["number"]


def test_api_auth_is_ui_or_inbound_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "basic")
    monkeypatch.setenv("UI_USER", "andrey")
    monkeypatch.setenv("UI_PASSWORD", "test-pass")
    monkeypatch.setenv("NOTIFY_TG", "0")
    from app.main import create_app

    local = TestClient(create_app())
    created = local.post(
        "/s/inbound/doc",
        json={
            "source": "manual",
            "text": "под замком",
            "driver_name": "Закрытый Водитель",
            "plate": "А001АА 01",
            "vehicle": "тент",
        },
        headers={"X-Inbound-Secret": SECRET},
    ).json()
    driver = local.app.state.db.one("SELECT id FROM parties WHERE kind = 'driver'")
    opened = local.get(f"/parties/{driver['id']}", auth=("andrey", "test-pass"))
    assert opened.status_code == 200
    token = local.cookies["delo_csrf"]
    uploaded = local.post(
        f"/parties/{driver['id']}/scans",
        data={"csrf": token, "kind": "passport"},
        files={"file": ("passport.pdf", PDF, "application/pdf")},
        auth=("andrey", "test-pass"),
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    api = f"/api/packages/{created['number']}/client-attachments"
    ui = f"/parties/{driver['id']}/scans/passport"
    assert local.get(api).status_code == 401
    assert local.get(ui).status_code == 401
    assert local.get(f"{api}?secret={SECRET}").status_code == 401
    by_secret = local.get(api, headers={"X-Inbound-Secret": SECRET})
    assert by_secret.status_code == 200, by_secret.text
    assert by_secret.json()["missing"] == ["sts", "license"]
    assert by_secret.json()["attachments"][0]["type"] == "passport"
    streamed = local.get(by_secret.json()["attachments"][0]["download_url"], headers={"X-Inbound-Secret": SECRET})
    assert streamed.status_code == 200
    assert streamed.content.startswith(b"%PDF")
    assert local.get(ui, headers={"X-Inbound-Secret": SECRET}).status_code == 401
    assert local.get(ui, auth=("andrey", "test-pass")).status_code == 200
    assert local.get("/", headers={"X-Inbound-Secret": SECRET}).status_code == 401
    home = local.get("/", auth=("andrey", "test-pass"))
    assert home.status_code == 200
    assert "Админ" in home.text or "Андрей" in home.text


def test_proxy_gate_still_accepts_inbound_on_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "delo.db"))
    monkeypatch.setenv("INBOUND_SECRET", SECRET)
    monkeypatch.setenv("AUTH_MODE", "proxy")
    monkeypatch.setenv("UI_GATE_SECRET", "gate-secret-value")
    monkeypatch.setenv("NOTIFY_TG", "0")
    from app.main import create_app

    local = TestClient(create_app())
    created = local.post(
        "/s/inbound/doc",
        json={"source": "manual", "text": "гейт", "driver_name": "Гейт Водитель", "plate": "В001ВВ 01"},
        headers={"X-Inbound-Secret": SECRET},
    ).json()
    api = f"/api/packages/{created['id']}/client-attachments"
    assert local.get(api).status_code == 401
    assert local.get(api, headers={"X-Delo-Gate": "gate-secret-value"}).status_code == 200
    assert local.get(api, headers={"X-Inbound-Secret": SECRET}).status_code == 200
    assert local.get("/").status_code == 401
