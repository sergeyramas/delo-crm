"""Защищённые подстановки текстов диспетчерской.

Значения не редактируются через CRM: подписант, отправитель и реквизиты —
константы кода, взятые из Дела (sergeyramas/bot-ati-delivery, delo/1.101.0):

* подписант — ``FROM_DISPLAY_NAME`` из ``dg_send.py``;
* отправитель — ``DEFAULT_FROM`` из ``callback_worker.py``;
* реквизиты — в коде Дела отдельной константы нет, поэтому пустая строка.

В пользовательских текстах эти имена разрешены только как ``{имя}``.
Рендер — простая подстановка по белому списку, без ``str.format`` и Jinja.
"""

from __future__ import annotations

import os

SIGNER = "ИП Кртин А.С. («Перевозчик 21»)"
SENDER = os.environ.get("CRM_MAIL_SENDER", "")  # адрес — из окружения, в код не пишем
REQUISITES = ""

PROTECTED: dict[str, str] = {
    "подписант": SIGNER,
    "отправитель": SENDER,
    "реквизиты": REQUISITES,
}

PROTECTED_LABELS: dict[str, str] = {
    "подписант": "Подписант",
    "отправитель": "Отправитель (From)",
    "реквизиты": "Реквизиты",
}

PROTECTED_NAMES: tuple[str, ...] = tuple(PROTECTED)


def protected_rows() -> list[dict[str, str]]:
    """Для страницы /settings: только чтение."""
    return [
        {"name": name, "label": PROTECTED_LABELS[name], "value": PROTECTED[name]}
        for name in PROTECTED_NAMES
    ]
