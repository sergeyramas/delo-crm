"""Защищённые подстановки текстов диспетчерской.

Значения не редактируются через CRM (решение Сергея 12.11: в письмах заказчику
остаётся подпись Андрея, как на бою):

* подписант — блок подписи, читается из файла по пути в ``CRM_MAIL_SIGNATURE_FILE``
  (на бою это ``andrei_signature.txt`` из Дела). Умолчание пустое: нет переменной
  или файла — подстановка пустая. Текст подписи и телефоны в код CRM не пишем;
* отправитель — имя в ``From`` как на бою («Перевозчик 21»), без адреса;
* реквизиты — в коде Дела отдельной константы нет, поэтому пустая строка.

В пользовательских текстах эти имена разрешены только как ``{имя}``.
Рендер — простая подстановка по белому списку, без ``str.format`` и Jinja.
Подпись читается при каждом обращении, поэтому смена файла не требует перезапуска.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping

LOG = logging.getLogger("delo-crm.settings")

SIGNATURE_ENV = "CRM_MAIL_SIGNATURE_FILE"
SENDER = "Перевозчик 21"  # имя From как на бою; адрес в CRM не хранится
REQUISITES = ""


def signature_block() -> str:
    """Блок подписи из файла ``CRM_MAIL_SIGNATURE_FILE``; пусто, если путь не задан или файл не читается."""
    path = os.environ.get(SIGNATURE_ENV, "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        LOG.warning("signature file unreadable: %s", type(exc).__name__)
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def protected_values() -> dict[str, str]:
    return {"подписант": signature_block(), "отправитель": SENDER, "реквизиты": REQUISITES}


class _Protected(Mapping[str, str]):
    """Живое отображение защищённых подстановок: подпись перечитывается из файла."""

    def __getitem__(self, name: str) -> str:
        return protected_values()[name]

    def __iter__(self) -> Iterator[str]:
        return iter(protected_values())

    def __len__(self) -> int:
        return 3


PROTECTED: Mapping[str, str] = _Protected()

PROTECTED_LABELS: dict[str, str] = {
    "подписант": "Подписант",
    "отправитель": "Отправитель (From)",
    "реквизиты": "Реквизиты",
}

PROTECTED_NAMES: tuple[str, ...] = tuple(PROTECTED)


def protected_rows() -> list[dict[str, str]]:
    """Для страницы /settings: только чтение."""
    return [
        {"name": name, "label": PROTECTED_LABELS[name], "value": values[name]}
        for values in (protected_values(),)
        for name in PROTECTED_NAMES
    ]
