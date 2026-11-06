"""Проверка и отправка в мессенджеры.

Сеть отсюда не вызывается. Режимы:
- off — бейджи «неизвестно», отправка закрыта;
- mock — отметки из последней цифры телефона, запись остаётся в CRM;
- live — интерфейс для Вишну, HTTP нет, пока адаптер не подключён.

Боевой токен MAX и выгрузка сообщений по HTTPS в этот выпуск не входят:
кнопка MAX пишет черновик в SQLite и сеть не открывает.
Токены MESSENGER_* в репозиторий не класть и в лог не писать.
"""

from __future__ import annotations

from app.config import Settings

UNKNOWN = {"tg": "unknown", "wa": "unknown", "max": "unknown"}


def check_phone(settings: Settings, phone_key: str) -> dict:
    if not phone_key or settings.messenger_check == "off":
        return dict(UNKNOWN)
    if settings.messenger_check == "live":
        # Токен может быть задан, но адаптера ещё нет — наружу не ходим.
        return dict(UNKNOWN)
    last = int(phone_key[-1])
    if last % 5 == 0:
        max_status = "yes"
    elif last % 5 == 1:
        max_status = "no"
    else:
        max_status = "unknown"
    return {
        "tg": "yes" if last % 2 == 0 else "no",
        "wa": "yes" if last % 3 != 0 else "no",
        "max": max_status,
    }


def deliver_telegram(settings: Settings, phone_key: str, body: str) -> tuple[bool, str]:
    """True — можно пометить сообщение как локально подготовленное (mock).

    Ни один режим не открывает сокет. body и phone_key в лог не пишем.
    """
    del phone_key, body
    mode = settings.messenger_check
    if mode == "mock":
        return True, "mock: сеть не вызывалась"
    if mode == "live":
        if not settings.messenger_tg_token:
            return False, "Telegram не настроен: пустой MESSENGER_TG_TOKEN."
        return False, "TODO: живая отправка Telegram не подключена, сеть не вызывалась."
    return False, "Отправка выключена (MESSENGER_CHECK=off)."


def mode_note(settings: Settings) -> str:
    mode = settings.messenger_check
    if mode == "mock":
        return (
            "Заглушка MESSENGER_CHECK=mock: «есть / нет» считается из последней цифры телефона. "
            "Это не поиск по Telegram, WhatsApp или MAX. Сеть не вызывается."
        )
    if mode == "live":
        return (
            "MESSENGER_CHECK=live: боевой адаптер не подключён (TODO Вишну). "
            "Бейджи «неизвестно», сообщения наружу не уходят."
        )
    return "MESSENGER_CHECK=off: автопроверка выключена, бейджи «неизвестно», сеть не вызывается."
