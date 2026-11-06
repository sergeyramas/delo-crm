"""Уведомление в Telegram. По умолчанию выключено.

Бота на этом хосте нет: ни webhook, ни getUpdates. Если Вишну позже
включит NOTIFY_TG=1 и положит токен в env (не в репозиторий), смена
статуса пакета делает один sendMessage. Ошибка сети карточку не откатывает.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request

from app.config import Settings
from app.domain import status_info

log = logging.getLogger("delo.notify")


def maybe_notify(settings: Settings, package: dict, old_status: str, new_status: str) -> None:
    if not settings.notify_tg or old_status == new_status:
        return
    if not settings.tg_token or not settings.tg_chat:
        return
    old_l = status_info(old_status)["label"]
    new_l = status_info(new_status)["label"]
    text = f"{package['number']}: {old_l} → {new_l}"
    body = urllib.parse.urlencode({"chat_id": settings.tg_chat, "text": text}).encode()
    url = f"https://api.telegram.org/bot{settings.tg_token}/sendMessage"
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            response.read()
    except Exception as exc:
        # Без текста исключения: в URL сидит токен.
        log.warning("tg notify failed (%s)", type(exc).__name__)
