"""Проверка значений настроек. Ошибка — человеческим текстом, без исключений наружу."""

from __future__ import annotations

import json
import re

from app.settings_catalog import BY_KEY, TOKEN_RE, SettingKey

NAME_RE = re.compile(r"ИП\s+Кртин", re.IGNORECASE)
BANNED_RE = re.compile(r"моргунов", re.IGNORECASE)
BUTTON_LABEL_MAX = 64


class ValidationError(ValueError):
    """Значение не принято. Текст показываем на странице."""


def _allowed(item: SettingKey) -> str:
    return ", ".join(item.placeholders) if item.placeholders else "нет"


def _tokens(item: SettingKey, value: str) -> None:
    rest = TOKEN_RE.sub("", value)
    if "{" in rest or "}" in rest:
        raise ValidationError("Непарные скобки: подстановка должна быть вида {name}.")
    for match in TOKEN_RE.finditer(value):
        name = match.group(1) or match.group(2)
        if name not in item.placeholders:
            raise ValidationError(
                f"неизвестная подстановка {{{name}}}, допустимы: {_allowed(item)}."
            )


def _map(item: SettingKey, raw: str) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Нужен объект JSON: строка или число не подходят.") from exc
    if not isinstance(data, dict):
        raise ValidationError("Нужен объект JSON: ключ → значение.")
    if not data:
        raise ValidationError("Пустое значение. Для умолчания есть «Сбросить к умолчанию».")
    labels: list[str] = []
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("Ключ карты должен быть непустой строкой.")
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"Пустое значение у ключа «{key}».")
        if item.key == "buttons.labels":
            encoded = value.encode("utf-8")
            if len(encoded) > BUTTON_LABEL_MAX:
                raise ValidationError(
                    f"Подпись «{key}» длиннее {BUTTON_LABEL_MAX} байт UTF-8."
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
            raise ValidationError(f"Неизвестный ключ настройки: {item}.")
        item = found
    value = "" if raw is None else str(raw)
    if not value.strip():
        raise ValidationError("Пустое значение. Для умолчания есть «Сбросить к умолчанию».")
    if BANNED_RE.search(value):
        raise ValidationError("Имя «Моргунов» в настройках запрещено.")
    if item.key in {"mail.from_name", "mail.client.signature"} and not NAME_RE.search(value):
        raise ValidationError("Здесь обязательно должно быть «ИП Кртин».")
    if item.type == "map":
        canonical = _map(item, value)
        if len(canonical) > item.max_len:
            raise ValidationError(f"Слишком длинно: максимум {item.max_len} символов.")
        return canonical
    if len(value) > item.max_len:
        raise ValidationError(f"Слишком длинно: максимум {item.max_len} символов.")
    if item.type == "template":
        _tokens(item, value)
    return value


validate = validate_value
