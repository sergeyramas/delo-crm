"""Настройки только из окружения. Секреты в репозиторий не пишем."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    sqlite_path: Path
    inbound_secret: str
    auth_mode: str
    ui_user: str
    ui_password: str
    ui_gate_secret: str
    display_name: str
    notify_tg: bool
    tg_token: str
    tg_chat: str

    def __repr__(self) -> str:
        return (
            f"Settings(auth_mode={self.auth_mode!r}, notify_tg={self.notify_tg}, "
            f"sqlite_path={self.sqlite_path})"
        )

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("DATA_DIR") or (ROOT / "data")).expanduser()
        sqlite = Path(os.environ.get("SQLITE_PATH") or (data_dir / "delo.db")).expanduser()
        auth_mode = os.environ.get("AUTH_MODE", "off").strip().lower() or "off"
        if auth_mode not in {"off", "basic", "proxy"}:
            raise RuntimeError("AUTH_MODE должен быть off, basic или proxy.")
        ui_user = os.environ.get("UI_USER", "")
        ui_password = os.environ.get("UI_PASSWORD", "")
        if auth_mode == "basic" and (not ui_user or not ui_password):
            raise RuntimeError("AUTH_MODE=basic требует UI_USER и UI_PASSWORD.")
        display = os.environ.get("UI_DISPLAY_NAME", "").strip() or "Андрей (логист)"
        return cls(
            data_dir=data_dir,
            sqlite_path=sqlite,
            inbound_secret=os.environ.get("INBOUND_SECRET", ""),
            auth_mode=auth_mode,
            ui_user=ui_user,
            ui_password=ui_password,
            ui_gate_secret=os.environ.get("UI_GATE_SECRET", ""),
            display_name=display,
            notify_tg=_flag("NOTIFY_TG", "0"),
            tg_token=os.environ.get("TG_BOT_TOKEN", ""),
            tg_chat=os.environ.get("TG_CHAT_ID", ""),
        )
