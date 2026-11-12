"""Каталог текстов диспетчерской. Значения по умолчанию — из кода Дела.

Источник: sergeyramas/bot-ati-delivery, ветка delo/1.101.0 @ b4e0d55,
каталог deploy/ramos/delo-mail-forward/. Ничего не переписываем «по смыслу»:
текст умолчания должен совпадать с тем, что Дело берёт само.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from app.settings_constants import PROTECTED, PROTECTED_NAMES

SECTIONS = (
    ("client", "Письмо заказчику"),
    ("mirror", "Заявка перевозчику"),
    ("group", "Посты в группе"),
    ("buttons", "Кнопки"),
    ("stages", "Стадии и нюансы"),
)

CLIENT_TEMPLATE_MD = """# Шаблон исходящего заказчику · send_client

**От:** {отправитель}
**Кому:** {{to}}
**Тема:** Заявка № {{zayavka}} {{route}}, {{dates}} ({{n}})
**Вложение:** {{pdf_name}}

## Тело

Здравствуйте.

Направляем подписанную договор-заявку № {{zayavka}}{{date_doc_clause}} (наш № {{n}}):
маршрут {{route}}, груз {{gruz}}, ставка {{stavka}}.

Просим подтвердить получение и прислать подписанный скан в ответ на это письмо.

С уважением,
{подписант}
Клиент: {{klient}}
"""

MIRROR_TEMPLATE_MD = """# Шаблон исходящего перевозчику · send_mirror (зеркало)

**От:** {отправитель}
**Кому:** {{to}}
**Тема:** Заявка {{n}} / № {{zayavka}} — {{route}} (зеркало)
**Вложение:** {{pdf_name}}

## Тело

Здравствуйте.

Направляем копию (зеркало) подписанной договор-заявки № {{zayavka}} (наш № {{n}}):
маршрут {{route}}, груз {{gruz}}, ставка {{stavka}}.

Клиент: {{klient}}.
Вложение: {{pdf_name}}.

Просим подтвердить получение и прислать подписанный скан в ответ на это письмо.

С уважением,
{подписант}
"""

GROUP_REASONS = {
    "no_deal": "нет номера дела (Д-…) в посте",
    "no_card": "дела {deal} нет в реестре",
    "no_address": "в карточке {deal} нет адреса ({field})",
    "mismatch": "адрес в посте не совпадает с карточкой {deal}",
    "other_address": "в карточке другой адрес",
    "deal_conflict": "в посте несколько разных номеров дела",
    "no_reestr": "нет файла реестра",
    "bad_reestr": "реестр не читается (не utf-8 или битые кавычки) — правка только руками",
}

BUTTON_LABELS = {
    "take_yes": "Берём",
    "take_no": "Не берём",
    "send": "Отправить",
    "redo": "Переделать",
    "send_me": "Отправить мне",
    "sign": "Подписать",
    "mirror": "🪞 Зеркало перевозчику",
    "decline": "❌ Отказ",
    "send_force": "Точно отправить повторно",
    "retry": "Повторить",
    "confirm_addr": "Подтвердить адрес",
    "drv_vu": "💾 ВУ",
    "drv_pass": "💾 Паспорт",
    "drv_both": "💾 ВУ и паспорт (оба)",
    "drv_no": "✖ Не сохранять",
}

STAGE_LABELS = {
    "polucheno": "Получено",
    "podgotovleno": "Подготовлено",
    "otpravleno": "Отправлено",
    "podpisano": "Подписано",
    "arhiv": "Архив",
    "otkaz": "Отказ",
}


@dataclass(frozen=True)
class SettingKey:
    key: str
    title: str
    type: str
    default: str | dict
    placeholders: tuple[str, ...]
    max_len: int
    section: str


KEYS = (
    SettingKey(
        key="mail.client.body",
        title="Тело письма заказчику",
        type="template",
        default="Добрый день!\n\nВо вложении: {file_name}.\n\nС уважением,\n{подписант}\n",
        placeholders=("file_name", *PROTECTED_NAMES),
        max_len=4000,
        section="client",
    ),
    SettingKey(
        key="mail.client.template_md",
        title="Полный шаблон письма заказчику",
        type="template",
        default=CLIENT_TEMPLATE_MD,
        placeholders=(
            "to", "zayavka", "route", "dates", "n", "pdf_name",
            "date_doc_clause", "gruz", "stavka", "klient", *PROTECTED_NAMES,
        ),
        max_len=20000,
        section="client",
    ),
    SettingKey(
        key="mail.mirror.subject",
        title="Тема письма перевозчику",
        type="template",
        default="Заявка перевозчику ({deal})",
        placeholders=("deal",),
        max_len=300,
        section="mirror",
    ),
    SettingKey(
        key="mail.mirror.template_md",
        title="Полный шаблон заявки перевозчику",
        type="template",
        default=MIRROR_TEMPLATE_MD,
        placeholders=(
            "to", "n", "zayavka", "route", "pdf_name", "gruz", "stavka", "klient",
            *PROTECTED_NAMES,
        ),
        max_len=20000,
        section="mirror",
    ),
    SettingKey(
        key="group.checking",
        title="Пока идёт проверка",
        type="text",
        default="⏳ Проверяю…",
        placeholders=(),
        max_len=300,
        section="group",
    ),
    SettingKey(
        key="group.not_sent_prefix",
        title="Префикс «не отправлено»",
        type="text",
        default="⚠ НЕ отправлено: ",
        placeholders=(),
        max_len=300,
        section="group",
    ),
    SettingKey(
        key="group.already_sent",
        title="Уже отправлено",
        type="text",
        default="✅ Уже отправлено",
        placeholders=(),
        max_len=300,
        section="group",
    ),
    SettingKey(
        key="group.sent_line",
        title="Строка «Отправлено»",
        type="template",
        default="✅ Отправлено: {who}, {what}, {ts}",
        placeholders=("who", "what", "ts"),
        max_len=500,
        section="group",
    ),
    SettingKey(
        key="group.reasons",
        title="Причины «не отправлено»",
        type="map",
        default=GROUP_REASONS,
        placeholders=(),
        max_len=20000,
        section="group",
    ),
    SettingKey(
        key="buttons.labels",
        title="Подписи кнопок",
        type="map",
        default=BUTTON_LABELS,
        placeholders=(),
        max_len=8000,
        section="buttons",
    ),
    SettingKey(
        key="stages.labels",
        title="Названия стадий",
        type="map",
        default=STAGE_LABELS,
        placeholders=(),
        max_len=8000,
        section="stages",
    ),
    SettingKey(
        key="nuances.header",
        title="Заголовок блока нюансов",
        type="text",
        default="🔴 НЮАНСЫ",
        placeholders=(),
        max_len=300,
        section="stages",
    ),
)

BY_KEY: dict[str, SettingKey] = {item.key: item for item in KEYS}
CATALOG = BY_KEY


# Синтетическое дело для предпросмотра. Никаких боевых адресов и ФИО.
SAMPLE = {
    "n": "Д-2026-0001",
    "zayavka": "2026-0001",
    "route": "Нижний Новгород — Москва",
    "marshrut": "Нижний Новгород — Москва",
    "dates": "12.11–13.11.2026",
    "srok": "12.11–13.11",
    "pdf_name": "Д-2026-0001-zayavka.pdf",
    "date_doc": "12.11.2026",
    "date_doc_clause": " от 12.11.2026",
    "gruz": "тент",
    "stavka": "68 000 ₽",
    "klient": "ООО «Пример»",
    "to": "client@example.test",
    "from": "docs@example.test",
    "deal": "Д-2026-0001",
    "file_name": "Д-2026-0001-zayavka.pdf",
    "who": "Диспетчер",
    "what": "письмо заказчику",
    "ts": "",
}


def default_text(item: SettingKey) -> str:
    """Умолчание в том виде, в каком оно лежит в базе: map — канонический JSON."""
    if item.type == "map":
        return json.dumps(item.default, ensure_ascii=False, sort_keys=False)
    return str(item.default)


def display_text(item: SettingKey, value: str) -> str:
    """Значение для textarea: map показываем отформатированным JSON."""
    if item.type != "map":
        return value
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return value


def parse_map(value: str) -> dict:
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("Нужен объект JSON: ключ → подпись.")
    return data


TOKEN_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}|\{(\w+)\}", re.UNICODE)


def render_placeholders(value: str, values: dict[str, str]) -> str:
    """Безопасная замена ``{имя}``/``{{имя}}`` строго по переданному белому списку.

    Никакого ``str.format`` и Jinja: только регулярка и точное совпадение имени.
    Неизвестные подстановки остаются как есть (их не пропустит валидация).
    """

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return values.get(name, match.group(0))

    return TOKEN_RE.sub(repl, value)


def render_preview(item: SettingKey, value: str, today: date | None = None) -> str:
    """Подставить синтетическое дело и защищённые реквизиты. Чужие имена не трогаем."""
    moment = (today or date.today()).strftime("%d.%m.%Y")
    values = dict(PROTECTED)
    values.update(SAMPLE)
    values["ts"] = moment
    values["dates"] = f"12.11–13.11.{moment[-4:]}"
    values["date_doc"] = moment
    values["date_doc_clause"] = f" от {moment}"
    return render_placeholders(value, values)
