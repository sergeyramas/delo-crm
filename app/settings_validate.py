"""Проверка значений настроек. Ошибка — с машинным кодом и коротким текстом.

Текст ошибки никогда не цитирует запрещённое слово и не перечисляет запретный
список: снаружи (в API) отдаётся только ``code`` и нейтральное ``message``.
"""

from __future__ import annotations

import json
import re

from app.settings_catalog import BY_KEY, TOKEN_RE, SettingKey

# Коды отказа. Их видит API и тесты.
UNKNOWN_PLACEHOLDER = "unknown_placeholder"
FORBIDDEN_WORD = "forbidden_word"
LINK_NOT_ALLOWED = "link_not_allowed"
REQUISITES_NOT_ALLOWED = "requisites_not_allowed"
MISSING_SIGNER = "missing_signer"
TOO_LONG = "too_long"
EMPTY = "empty"

BANNED_RE = re.compile(r"моргунов", re.IGNORECASE)
LINK_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# ИНН — 10 или 12 цифр, р/с — 20, БИК — 9. Ровно такая длина подряд.
REQUISITES_RE = re.compile(r"(?<!\d)(?:\d{9}|\d{10}|\d{12}|\d{20})(?!\d)")

SIGNER_TOKEN = "подписант"
SIGNER_REQUIRED = frozenset({"mail.client.body"})
SIGNER_SUFFIX = ".template_md"

BUTTON_LABEL_MAX = 64

NEUTRAL = "текст не принят"


class ValidationError(ValueError):
    """Значение не принято. ``code`` — машинный код, ``message`` — без цитат."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def _allowed(item: SettingKey) -> str:
    return ", ".join(item.placeholders) if item.placeholders else "нет"


def _tokens(item: SettingKey, value: str) -> None:
    rest = TOKEN_RE.sub("", value)
    if "{" in rest or "}" in rest:
        raise ValidationError(
            "Непарные скобки: подстановка должна быть вида {name}.",
            UNKNOWN_PLACEHOLDER,
        )
    for match in TOKEN_RE.finditer(value):
        name = match.group(1) or match.group(2)
        if name not in item.placeholders:
            raise ValidationError(
                f"неизвестная подстановка {{{name}}}, допустимы: {_allowed(item)}.",
                UNKNOWN_PLACEHOLDER,
            )


def _has_signer(value: str) -> bool:
    return any(
        (match.group(1) or match.group(2)) == SIGNER_TOKEN
        for match in TOKEN_RE.finditer(value)
    )


def _needs_signer(item: SettingKey) -> bool:
    return item.key in SIGNER_REQUIRED or item.key.endswith(SIGNER_SUFFIX)


def _text_guards(value: str) -> None:
    if BANNED_RE.search(value):
        raise ValidationError(NEUTRAL, FORBIDDEN_WORD)
    if LINK_RE.search(value) or EMAIL_RE.search(value):
        raise ValidationError(NEUTRAL, LINK_NOT_ALLOWED)
    if REQUISITES_RE.search(value):
        raise ValidationError(NEUTRAL, REQUISITES_NOT_ALLOWED)


def _map(item: SettingKey, raw: str) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Нужен объект JSON: строка или число не подходят.") from exc
    if not isinstance(data, dict):
        raise ValidationError("Нужен объект JSON: ключ → значение.")
    if not data:
        raise ValidationError(
            "Пустое значение. Для умолчания есть «Сбросить к умолчанию».",
            EMPTY,
        )
    labels: list[str] = []
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("Ключ карты должен быть непустой строкой.")
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"Пустое значение у ключа «{key}».", EMPTY)
        _text_guards(value)
        if item.key == "buttons.labels":
            encoded = value.encode("utf-8")
            if len(encoded) > BUTTON_LABEL_MAX:
                raise ValidationError(
                    f"Подпись «{key}» длиннее {BUTTON_LABEL_MAX} байт UTF-8.",
                    TOO_LONG,
                )
            labels.append(value)
    if len(labels) != len(set(labels)):
        raise ValidationError("Подписи кнопок не должны повторяться.")
    return json.dumps(data, ensure_ascii=False, sort_keys=False)


def validate_value(item: SettingKey | str, raw: str | None) -> str:
    """Вернуть каноническое значение или бросить ValidationError."""
    if isinstance(item, str):
        found = BY_KEY.get(item)
        if found is None:
            raise ValidationError(f"Неизвестный ключ настройки: {item}.", "unknown_key")
        item = found
    value = "" if raw is None else str(raw)
    if not value.strip():
        raise ValidationError(
            "Пустое значение. Для умолчания есть «Сбросить к умолчанию».",
            EMPTY,
        )
    _text_guards(value)
    if item.type == "map":
        canonical = _map(item, value)
        if len(canonical) > item.max_len:
            raise ValidationError(
                f"Слишком длинно: максимум {item.max_len} символов.", TOO_LONG
            )
        return canonical
    if len(value) > item.max_len:
        raise ValidationError(
            f"Слишком длинно: максимум {item.max_len} символов.", TOO_LONG
        )
    if item.type == "template":
        _tokens(item, value)
        if _needs_signer(item) and not _has_signer(value):
            raise ValidationError(
                "В шаблоне обязательна подстановка {подписант}.", MISSING_SIGNER
            )
    return value


validate = validate_value
