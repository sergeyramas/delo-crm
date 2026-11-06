import pytest
from fastapi.testclient import TestClient

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
