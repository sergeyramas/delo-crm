"""Стадии сделки с макетов и статусы пакета документов из ТЗ."""

from __future__ import annotations

import json
from datetime import datetime

STAGES: list[dict] = [
    {"code": "nahodka", "label": "Находка", "color": "#0f766e"},
    {"code": "na_odobrenii", "label": "На одобрении", "color": "#d97706"},
    {"code": "podtverzhden", "label": "Подтверждён", "color": "#64748b"},
    {"code": "zayavka", "label": "Заявка сформирована", "color": "#2563eb"},
    {"code": "docs_sent", "label": "Документы отправлены", "color": "#7c3aed"},
    {"code": "podpisany", "label": "Подписаны", "color": "#0891b2"},
    {"code": "v_puti", "label": "Водитель в пути", "color": "#ea580c"},
    {"code": "dostavlen", "label": "Доставлен", "color": "#16a34a"},
    {"code": "originaly", "label": "Оригиналы/оплата", "color": "#a16207"},
]

OTKAZ = {"code": "otkaz", "label": "Отказ", "color": "#b91c1c"}

STATUSES: list[dict] = [
    {"code": "polucheno", "label": "Получено"},
    {"code": "podgotovleno", "label": "Подготовлено"},
    {"code": "otpravleno", "label": "Отправлено"},
    {"code": "podpisano", "label": "Подписано"},
    {"code": "arhiv", "label": "Архив"},
    {"code": "otkaz", "label": "Отказ"},
]

# Стадия воронки → статус пакета. Рейс не отдельный статус документа.
STAGE_STATUS = {
    "nahodka": "polucheno",
    "na_odobrenii": "polucheno",
    "podtverzhden": "podgotovleno",
    "zayavka": "podgotovleno",
    "docs_sent": "otpravleno",
    "podpisany": "podpisano",
    "v_puti": "podpisano",
    "dostavlen": "podpisano",
    "originaly": "arhiv",
    "otkaz": "otkaz",
}

# Куда поставить стадию, если по API пришёл только статус и текущая стадия в другом статусе.
STATUS_STAGE = {
    "polucheno": "nahodka",
    "podgotovleno": "zayavka",
    "otpravleno": "docs_sent",
    "podpisano": "podpisany",
    "arhiv": "originaly",
    "otkaz": "otkaz",
}

STAGE_ORDER = [s["code"] for s in STAGES]

ADVANCE_LABEL = {
    "nahodka": "Отправить на одобрение",
    "na_odobrenii": "Подтвердить («берём»)",
    "podtverzhden": "Заявка сформирована",
    "zayavka": "Отметить: документы у клиента",
    "docs_sent": "Отметить: подписаны",
    "podpisany": "Водитель в пути",
    "v_puti": "Доставлен",
    "dostavlen": "Оригиналы / оплата",
}

KANBAN = [
    {"code": "find", "label": "Находки / «да»", "class": "st-nahodka", "stages": ["nahodka", "na_odobrenii"]},
    {"code": "docs", "label": "Документы", "class": "st-zayavka", "stages": ["podtverzhden", "zayavka", "docs_sent", "podpisany"]},
    {"code": "trip", "label": "В рейсе", "class": "st-v_puti", "stages": ["v_puti"]},
    {"code": "done", "label": "Доставлен", "class": "st-dostavlen", "stages": ["dostavlen"]},
    {"code": "pay", "label": "Оплата / архив", "class": "st-originaly", "stages": ["originaly"]},
]

BUCKETS = [
    {"code": "today", "label": "Сегодня", "class": "st-zayavka"},
    {"code": "work", "label": "В работе", "class": "st-v_puti"},
    {"code": "wait", "label": "Ждём клиента / водителя", "class": "st-docs_sent"},
    {"code": "done", "label": "Готово", "class": "st-dostavlen"},
]

# Ядро ~1.8G. Списки не читаем целиком: страница списка и ответ API короткие.
LIST_PAGE_SIZE = 50
LIST_PAGE_MAX = 100
KANBAN_CAP = 80
FEED_CAP = 200
MESSAGE_CAP = 200
TASK_CAP = 200
PARTY_DEAL_CAP = 50

EVENT_TYPES = ("mail_in", "mail_out", "tg", "status", "note", "file")

DOC_CLASS = {
    "Подтверждена": "st-dostavlen",
    "Подтверждён": "st-dostavlen",
    "Отправлена": "st-dostavlen",
    "Не готов": "st-podtverzhden",
    "Приложен": "st-zayavka",
}

FEED_FILTERS = [
    {"code": "all", "label": "Все", "class": "f-all"},
    {"code": "mail", "label": "Письма", "class": "f-mail"},
    {"code": "tg", "label": "Telegram", "class": "f-tg"},
    {"code": "agent", "label": "Агенты", "class": "f-agent"},
    {"code": "driver", "label": "Водитель", "class": "f-driver"},
    {"code": "file", "label": "Файлы", "class": "f-file"},
]

# Переписка v1(A): каналы хуков. Живая отправка сюда не входит.
MESSAGE_CHANNELS = [
    {"code": "email", "label": "Почта", "class": "f-mail", "css": "st-docs_sent"},
    {"code": "tg", "label": "Telegram", "class": "f-tg", "css": "st-zayavka"},
    {"code": "max", "label": "MAX", "class": "f-max", "css": "st-v_puti"},
]

MESSAGE_DIRECTIONS = [
    {"code": "in", "label": "Входящие", "item": "входящее", "class": "f-in"},
    {"code": "out", "label": "Исходящие", "item": "исходящее", "class": "f-out"},
]

MESSAGE_STATUSES = {
    "queued": "в очереди",
    "sent": "отправлено",
    "delivered": "доставлено",
    "failed": "ошибка",
    "received": "получено",
    "read": "прочитано",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(timespec="seconds")


def money(value) -> str:
    if value is None or value == "":
        return "—"
    return f"{int(value):,}".replace(",", " ") + " ₽"


def fmt_day(iso_date: str) -> str:
    if not iso_date:
        return "—"
    parts = iso_date[:10].split("-")
    if len(parts) == 3 and all(parts):
        return f"{parts[2]}.{parts[1]}"
    return iso_date


def fmt_dt(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", ""))
    except ValueError:
        return iso
    return dt.strftime("%d.%m %H:%M")


def stage_info(code: str) -> dict:
    if code == "otkaz":
        return OTKAZ
    for item in STAGES:
        if item["code"] == code:
            return item
    return {"code": code, "label": code, "color": "#64748b"}


def status_info(code: str) -> dict:
    for item in STATUSES:
        if item["code"] == code:
            return item
    return {"code": code, "label": code}


def status_for_stage(stage: str) -> str:
    if stage not in STAGE_STATUS:
        raise KeyError(stage)
    return STAGE_STATUS[stage]


def stage_for_status(status: str, current_stage: str) -> str:
    if current_stage in STAGE_STATUS and STAGE_STATUS[current_stage] == status:
        return current_stage
    if status not in STATUS_STAGE:
        raise KeyError(status)
    return STATUS_STAGE[status]


def next_stage(stage: str) -> str | None:
    if stage not in STAGE_ORDER:
        return None
    idx = STAGE_ORDER.index(stage)
    if idx + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[idx + 1]


def chain_classes(stage: str) -> list[str]:
    if stage not in STAGE_ORDER:
        return ["off"] * len(STAGES)
    idx = STAGE_ORDER.index(stage)
    out = []
    for i, item in enumerate(STAGES):
        out.append(f"on st-{item['code']}" if i <= idx else "off")
    return out


def default_group(event_type: str, meta: dict) -> str:
    if meta.get("group"):
        return str(meta["group"])
    if event_type in {"mail_in", "mail_out"}:
        return "mail"
    if event_type == "tg":
        return "tg"
    if event_type == "file":
        return "file"
    return "agent"


def event_class(group: str, event_type: str) -> str:
    return {
        "mail": "st-docs_sent",
        "tg": "st-zayavka",
        "driver": "st-v_puti",
        "file": "st-podpisany",
        "yes": "st-na_odobrenii",
        "agent": "st-nahodka",
    }.get(group, "st-podtverzhden" if event_type == "status" else "st-nahodka")


def loads(raw: str, fallback):
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return fallback
