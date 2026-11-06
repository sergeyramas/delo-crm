"""Импорт CSV реестра в CRM через POST /s/inbound/doc и PATCH статуса.

Колонки как у воркера, разделитель «;»:
skvoznoj_n, data, klient, marshrut, gruz, stavka, status,
email_klient, email_perevozchik, komu, kommentarij.
Лишние колонки не мешают. Повторный прогон не создаёт второй пакет с тем же номером.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from app.domain import STAGE_STATUS, STATUSES, STATUS_STAGE, STAGES
from app.logic import MAX_FILE, ApiError, canonical_number
from app.sync_client import SyncError, request_json

DEFAULT_CARRIER = "Перевозчик 21"
OWN_MARKERS = ("камалюков", "кртин")

HEADER_ALIASES = {
    "skvoznoj_n": "skvoznoj_n",
    "skvoznoj": "skvoznoj_n",
    "skvoznoi_n": "skvoznoj_n",
    "nomer": "skvoznoj_n",
    "номер": "skvoznoj_n",
    "сквозной": "skvoznoj_n",
    "сквозной_номер": "skvoznoj_n",
    "сквозной_n": "skvoznoj_n",
    "data": "data",
    "дата": "data",
    "klient": "klient",
    "клиент": "klient",
    "marshrut": "marshrut",
    "маршрут": "marshrut",
    "gruz": "gruz",
    "груз": "gruz",
    "stavka": "stavka",
    "ставка": "stavka",
    "status": "status",
    "статус": "status",
    "email_klient": "email_klient",
    "email_клиента": "email_klient",
    "почта_клиента": "email_klient",
    "email_perevozchik": "email_perevozchik",
    "email_перевозчика": "email_perevozchik",
    "почта_перевозчика": "email_perevozchik",
    "kommentarij": "kommentarij",
    "комментарий": "kommentarij",
    "comment": "kommentarij",
    "komu": "komu",
    "кому": "komu",
    "voditel": "voditel",
    "водитель": "voditel",
    "driver": "voditel",
    "gosnomer": "plate",
    "госномер": "plate",
    "plate": "plate",
    "номер_авто": "plate",
    "ves": "weight",
    "вес": "weight",
    "weight": "weight",
}

# Точные подписи реестра, которых нет в кодах CRM. Пара (stage, status) сходится со STAGE_STATUS.
LABEL_TARGET = {
    "получено": ("nahodka", "polucheno"),
    "находка": ("nahodka", "polucheno"),
    "на одобрении": ("na_odobrenii", "polucheno"),
    "подтвержден": ("podtverzhden", "podgotovleno"),
    "подтверждено": ("podtverzhden", "podgotovleno"),
    "подготовлено": ("zayavka", "podgotovleno"),
    "заявка": ("zayavka", "podgotovleno"),
    "заявка сформирована": ("zayavka", "podgotovleno"),
    "отправлено": ("docs_sent", "otpravleno"),
    "документы отправлены": ("docs_sent", "otpravleno"),
    "подписано": ("podpisany", "podpisano"),
    "подписаны": ("podpisany", "podpisano"),
    "в пути": ("v_puti", "podpisano"),
    "водитель в пути": ("v_puti", "podpisano"),
    "доставлен": ("dostavlen", "podpisano"),
    "доставлено": ("dostavlen", "podpisano"),
    "архив": ("originaly", "arhiv"),
    "оригиналы": ("originaly", "arhiv"),
    "оригиналы/оплата": ("originaly", "arhiv"),
    "оплата": ("originaly", "arhiv"),
    "отказ": ("otkaz", "otkaz"),
}

PLATE_RE = re.compile(
    r"(?<![\w])([АВЕКМНОРСТУХABEKMHOPCTYX]\d{3}[АВЕКМНОРСТУХABEKMHOPCTYX]{2}\s?\d{2,3})(?![\w])",
    re.IGNORECASE,
)
DRIVER_RE = re.compile(
    r"водител[ьяю]\s*[:\-–—]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё.]*){0,2})",
    re.IGNORECASE,
)
IP_RE = re.compile(
    r"ИП\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z.\-]+(?:\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z.\-]*){0,3}"
)
FILE_RE = re.compile(r"^(Д-\d{4}-\d{4,})(?:-.+)?$")


class RowError(Exception):
    pass


@dataclass
class Planned:
    number: str
    payload: dict
    patch: dict | None
    row: int
    vat: str = ""


# Поля, которые шлюз реально пишет. Чужие ключи JSON в POST не кладём.
INBOUND_KEYS = {
    "source",
    "thread_id",
    "from",
    "subject",
    "text",
    "client_hint",
    "client_name",
    "client_email",
    "client_contact",
    "client_phone",
    "client_inn",
    "client_company",
    "client_note",
    "route_from",
    "route_to",
    "cargo",
    "weight",
    "pallets",
    "rate_rub",
    "load_date",
    "carrier_name",
    "carrier_email",
    "carrier_phone",
    "carrier_inn",
    "carrier_contract",
    "carrier_company",
    "driver_name",
    "driver_phone",
    "driver_email",
    "vehicle",
    "plate",
    "license_no",
    "driver_license",
    "passport_no",
    "driver_passport",
    "driver_channel",
    "source_detail",
    "list_note",
    "flags",
    "hint",
    "idempotent",
}
PATCH_KEYS = {"status", "stage", "decision", "signed_client", "signed_carrier", "comment", "actor"}
PREFIX_FILE_RE = re.compile(r"^\d{2}-.+")


@dataclass
class Attachment:
    number: str
    path: Path
    error: str = ""


@dataclass
class Counters:
    created: int = 0
    updated: int = 0
    attached: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"created={self.created} updated={self.updated} attached={self.attached} errors={self.errors}"

    def fail(self, message: str) -> None:
        self.errors += 1
        self.notes.append(message)


def norm_header(name: str) -> str:
    text = (name or "").strip().lower().replace("ё", "е").lstrip("\ufeff")
    text = re.sub(r"[\s\-]+", "_", text)
    return HEADER_ALIASES.get(text, text)


def _cell(row: dict, key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    return str(value).strip()


def parse_date(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    iso = raw
    match = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", raw)
    if match:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if year < 100:
            year += 2000
        iso = f"{year:04d}-{month:02d}-{day:02d}"
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise RowError(f"Дата не разобрана: {raw[:40]}")
    try:
        datetime.strptime(iso, "%Y-%m-%d")
    except ValueError as exc:
        raise RowError(f"Дата не разобрана: {raw[:40]}") from exc
    return iso


def normalize_number(raw: str, when: str) -> str:
    text = raw.strip().replace(" ", "")
    if len(text) >= 2 and text[0] in {"D", "d"} and text[1] == "-":
        text = "Д-" + text[2:]
    if re.fullmatch(r"\d{1,6}", text):
        year_text = when[:4] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when) else ""
        year = int(year_text) if year_text else datetime.now().year
        seq = int(text)
        if seq < 1:
            raise RowError("Номер должен быть больше нуля.")
        text = f"Д-{year}-{seq:04d}" if seq < 10000 else f"Д-{year}-{seq}"
    try:
        return canonical_number(text)[2]
    except ApiError as exc:
        raise RowError(exc.message) from exc


def split_route(value: str) -> tuple[str, str]:
    text = " ".join(value.split())
    if not text:
        return "", ""
    for sep in ("→", "->", "=>"):
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip(" -–—")[:120], right.strip(" -–—")[:120]
    parts = re.split(r"\s+[—–-]\s+", text, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip()[:120], parts[1].strip()[:120]
    return text[:120], ""


def parse_rate(value: str) -> tuple[int | None, str]:
    """Число из «30000 без НДС» или «68 000». Суффикс НДС не часть суммы."""
    original = value.strip()
    if not original:
        return None, ""
    folded = original.lower().replace("ё", "е")
    suffix = ""
    if "без ндс" in folded:
        suffix = "без НДС"
    elif "с ндс" in folded:
        suffix = "с НДС"
    match = re.search(r"(\d(?:[\d\s\u00a0]*\d)?)(?:[.,](\d+))?", original.replace("\u00a0", " "))
    if not match:
        raise RowError(f"Ставка не число: {original[:40]}")
    whole = re.sub(r"[\s\u00a0]", "", match.group(1))
    frac = match.group(2)
    raw = whole if not frac else f"{whole}.{frac}"
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        raise RowError(f"Ставка не число: {original[:40]}")
    number = int(float(raw))
    if number < 0 or number > 100_000_000:
        raise RowError("Ставка вне диапазона.")
    return number, suffix


def stamp_vat(payload: dict, suffix: str) -> None:
    if not suffix:
        return
    flags = str(payload.get("flags") or "")
    if suffix.lower() not in flags.lower():
        payload["flags"] = f"{flags}; {suffix}".strip("; ") if flags else suffix
    note = str(payload.get("list_note") or "")
    if suffix.lower() not in note.lower():
        joined = f"{note} · {suffix}".strip(" ·") if note else suffix
        payload["list_note"] = joined[:300]


def split_emails(value: str) -> list[str]:
    found: list[str] = []
    for part in re.split(r"[,;]+", value or ""):
        match = re.search(r"[^<>\s,;]+@[^<>\s,;]+", part)
        if not match:
            continue
        email = match.group(0)[:200]
        if email not in found:
            found.append(email)
    return found


def one_email(value: str) -> str:
    emails = split_emails(value)
    return emails[0] if emails else ""


def carrier_from_komu(komu: str) -> str:
    raw = " ".join(komu.split())
    folded = raw.lower().replace("ё", "е")
    if not raw or any(mark in folded for mark in OWN_MARKERS):
        return DEFAULT_CARRIER
    found = IP_RE.search(raw)
    if found:
        return found.group(0).strip()[:200]
    return raw[:200]


def driver_and_plate(comment: str, driver: str, plate: str) -> tuple[str, str]:
    text = comment
    if not plate:
        found = PLATE_RE.search(text)
        if found:
            plate = re.sub(r"\s+", " ", found.group(1)).upper()
            text = text[: found.start()] + " " + text[found.end() :]
    if not driver:
        found = DRIVER_RE.search(text)
        if found:
            driver = found.group(1).strip(" .,")
    return driver[:120], plate[:40]


def registry_target(raw: str) -> tuple[str, str] | None:
    text = " ".join(raw.replace("ё", "е").split()).strip().lower()
    if not text:
        return None
    if text in STAGE_STATUS:
        return text, STAGE_STATUS[text]
    if text in STATUS_STAGE:
        return STATUS_STAGE[text], text
    labels = dict(LABEL_TARGET)
    for item in STATUSES:
        labels.setdefault(item["label"].lower().replace("ё", "е"), (STATUS_STAGE[item["code"]], item["code"]))
    for item in STAGES:
        labels.setdefault(item["label"].lower().replace("ё", "е"), (item["code"], STAGE_STATUS[item["code"]]))
    if text not in labels:
        raise RowError(f"Неизвестный статус реестра: {raw.strip()[:80]}")
    stage, status = labels[text]
    if STAGE_STATUS.get(stage) != status:
        raise RowError("Статус и стадия реестра разошлись.")
    return stage, status


def build_row(row: dict) -> tuple[dict, dict | None]:
    when = parse_date(_cell(row, "data"))
    number = normalize_number(_cell(row, "skvoznoj_n"), when)
    client = _cell(row, "klient")
    route_raw = _cell(row, "marshrut")
    route_from, route_to = split_route(route_raw)
    cargo = _cell(row, "gruz")
    comment = _cell(row, "kommentarij")
    driver, plate = driver_and_plate(comment, _cell(row, "voditel"), _cell(row, "plate"))
    text = comment or " ".join(bit for bit in (client, route_raw, cargo) if bit)
    payload: dict = {
        "source": "manual",
        "number": number,
        "idempotent": True,
        "source_detail": "reestr",
        "subject": (route_raw or number)[:500],
        "text": (text or number)[:8000],
        "list_note": " · ".join(bit for bit in ("реестр", client, route_raw) if bit)[:300],
        "carrier_name": carrier_from_komu(_cell(row, "komu")),
    }
    if client:
        payload["client_name"] = client[:300]
        payload["client_hint"] = client[:300]
    client_emails = split_emails(_cell(row, "email_klient"))
    email_carrier = one_email(_cell(row, "email_perevozchik"))
    if client_emails:
        payload["client_email"] = client_emails[0]
    if len(client_emails) > 1:
        extra = ", ".join(client_emails[1:])
        payload["client_note"] = f"Также: {extra}"
    if email_carrier:
        payload["carrier_email"] = email_carrier
    if route_from:
        payload["route_from"] = route_from
    if route_to:
        payload["route_to"] = route_to
    if cargo:
        payload["cargo"] = cargo[:300]
    weight = _cell(row, "weight")
    if weight:
        payload["weight"] = weight[:60]
    if when:
        payload["load_date"] = when
    rate, vat = parse_rate(_cell(row, "stavka"))
    if rate is not None:
        payload["rate_rub"] = rate
    stamp_vat(payload, vat)
    payload["_vat"] = vat
    if driver:
        payload["driver_name"] = driver
    if plate:
        payload["plate"] = plate
    target = registry_target(_cell(row, "status"))
    patch = None
    if target:
        stage, status = target
        patch = {"status": status, "stage": stage, "actor": "reestr"}
        if status == "otkaz":
            patch["comment"] = comment or "Отказ из реестра."
    return payload, patch


def load_csv(path: Path) -> tuple[list[Planned], list[str]]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RowError(f"CSV не читается: {exc.__class__.__name__}") from exc
    reader = csv.DictReader(io.StringIO(raw), delimiter=";")
    if not reader.fieldnames:
        raise RowError("В CSV нет заголовка.")
    reader.fieldnames = [norm_header(name or "") for name in reader.fieldnames]
    if "skvoznoj_n" not in reader.fieldnames:
        raise RowError("В заголовке нет колонки skvoznoj_n.")
    plans: list[Planned] = []
    errors: list[str] = []
    for index, row in enumerate(reader, start=2):
        if not row or not any((value or "").strip() for value in row.values()):
            continue
        try:
            payload, patch = build_row(row)
        except RowError as exc:
            errors.append(f"строка {index}: {exc}")
            continue
        plans.append(Planned(
            number=payload["number"],
            payload=payload,
            patch=patch,
            row=index,
            vat=str(payload.get("_vat") or ""),
        ))
    return plans, errors


def _blank(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def overlay_payload(plan: Planned, extra: dict) -> None:
    """JSON поверх строки CSV с тем же номером. Пустые поля JSON уже заполненное не затирают."""
    for key, value in extra.items():
        if key in {"attachments", "secret", "number", "_vat"}:
            continue
        if _blank(value):
            continue
        if key in PATCH_KEYS:
            if plan.patch is None:
                plan.patch = {"actor": "reestr"}
            plan.patch[key] = value
            continue
        if key in INBOUND_KEYS:
            plan.payload[key] = value
    plan.payload["number"] = plan.number
    plan.payload["idempotent"] = True
    if plan.payload.get("source") not in {"mail", "tg", "manual"}:
        plan.payload["source"] = "manual"
    stamp_vat(plan.payload, plan.vat)


def apply_enrichment(plans: list[Planned], objects: list[dict]) -> list[str]:
    by_number = {plan.number: plan for plan in plans}
    errors: list[str] = []
    for obj in objects:
        if not isinstance(obj, dict):
            errors.append("payload JSON должен быть объектом.")
            continue
        raw_number = str(obj.get("number") or "").strip()
        if raw_number:
            try:
                number = normalize_number(raw_number, str(obj.get("load_date") or ""))
            except RowError as exc:
                errors.append(str(exc))
                continue
            plan = by_number.get(number)
            if plan is None:
                errors.append(f"payload {number} не совпал со строкой CSV.")
                continue
        elif len(plans) == 1:
            plan = plans[0]
        else:
            errors.append("payload без number при нескольких строках CSV.")
            continue
        overlay_payload(plan, obj)
    return errors


def load_enrichment(explicit: list[str], csv_path: Path) -> tuple[list[dict], dict[str, str], list[str]]:
    """Объекты JSON и карта имя-файла → номер. Sidecar: payload-*.json рядом с CSV."""
    paths: list[Path] = []
    seen: set[str] = set()

    def push(path: Path) -> None:
        if not path.is_file():
            return
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        paths.append(path)

    for raw in explicit:
        item = Path(raw)
        if item.is_dir():
            for child in sorted(item.glob("*.json")):
                push(child)
        else:
            push(item)
    for child in sorted(csv_path.parent.glob("payload-*.json")):
        push(child)
    objects: list[dict] = []
    owners: dict[str, str] = {}
    errors: list[str] = []
    for path in paths:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"Не разобран JSON: {path.name}")
            continue
        batch = parsed if isinstance(parsed, list) else [parsed]
        for obj in batch:
            if not isinstance(obj, dict):
                errors.append(f"В {path.name} нужен объект.")
                continue
            raw_number = str(obj.get("number") or "").strip()
            number = ""
            if raw_number:
                try:
                    number = normalize_number(raw_number, str(obj.get("load_date") or ""))
                except RowError as exc:
                    errors.append(f"{path.name}: {exc}")
                    continue
            objects.append(obj)
            for item in obj.get("attachments") or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or (Path(str(item["path"])).name if item.get("path") else "")
                if name and number:
                    owners[Path(str(name)).name] = number
    return objects, owners, errors


def number_from_filename(name: str) -> str | None:
    stem = Path(name).stem
    if len(stem) >= 2 and stem[0] in {"D", "d"} and stem[1] == "-":
        stem = "Д-" + stem[2:]
    match = FILE_RE.fullmatch(stem)
    if not match:
        return None
    try:
        return canonical_number(match.group(1))[2]
    except ApiError:
        return None


def _sole_number(numbers: list[str] | None) -> str:
    uniq = list(dict.fromkeys(numbers or []))
    return uniq[0] if len(uniq) == 1 else ""


def assign_doc_number(name: str, numbers: list[str] | None, owners: dict[str, str] | None) -> tuple[str, str]:
    """Номер пакета для файла или текст ошибки. Пустой номер — файл не из этой сделки."""
    owners = owners or {}
    base = Path(name).name
    explicit = number_from_filename(base)
    if explicit:
        return explicit, ""
    if base in owners:
        return owners[base], ""
    if PREFIX_FILE_RE.match(base):
        sole = _sole_number(numbers)
        if sole:
            return sole, ""
        if not numbers:
            return "", f"Файл {base} не привязан: в CSV нет номера."
        return "", f"Файл {base} не привязан: в CSV несколько номеров, нужен --payload-json."
    return "", ""


def collect_attachments(
    docs_dir: str | None,
    pairs: list[str],
    numbers: list[str] | None = None,
    owners: dict[str, str] | None = None,
) -> list[Attachment]:
    found: list[Attachment] = []
    seen: set[tuple[str, str]] = set()

    def add(number: str, path: Path, error: str = "") -> None:
        key = (number, str(path.resolve()) if path.exists() else str(path))
        if key in seen:
            return
        seen.add(key)
        found.append(Attachment(number=number, path=path, error=error))

    sole = _sole_number(numbers)
    for item in pairs:
        if "=" not in item:
            path = Path(item)
            number, error = assign_doc_number(path.name, numbers, owners)
            if error:
                add("", path, error)
            elif number:
                add(number, path)
            elif sole:
                add(sole, path)
            else:
                add("", path, "Флаг --attach без номера: укажите number=path или один номер в CSV.")
            continue
        raw_number, raw_path = item.split("=", 1)
        path = Path(raw_path)
        try:
            number = normalize_number(raw_number, "")
        except RowError as exc:
            add(raw_number.strip(), path, str(exc))
            continue
        add(number, path)
    if docs_dir:
        root = Path(docs_dir)
        if not root.is_dir():
            add("", root, "Каталог --docs-dir не найден.")
        else:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                number, error = assign_doc_number(path.name, numbers, owners)
                if error:
                    add("", path, error)
                elif number:
                    add(number, path)
    ready: list[Attachment] = []
    for item in found:
        if item.error:
            ready.append(item)
            continue
        if not item.path.is_file():
            item.error = f"Файл не найден: {item.path.name}"
            ready.append(item)
            continue
        size = item.path.stat().st_size
        if size <= 0:
            item.error = f"Пустой файл: {item.path.name}"
        elif size > MAX_FILE:
            item.error = f"Файл больше 8 МБ: {item.path.name}"
        ready.append(item)
    return ready


def _attached_names(detail: dict) -> set[str]:
    names: set[str] = set()
    for doc in detail.get("documents") or []:
        if isinstance(doc, dict) and doc.get("title"):
            names.add(str(doc["title"]))
    for event in detail.get("events") or []:
        if not isinstance(event, dict):
            continue
        for item in event.get("attachments") or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
    return names


def run_import(plans: list[Planned], attachments: list[Attachment], transport, dry_run: bool) -> Counters:
    counters = Counters()
    if dry_run:
        counters.created = len(plans)
        for item in attachments:
            if item.error:
                counters.fail(item.error)
            else:
                counters.attached += 1
        return counters
    for plan in plans:
        body_out = {key: value for key, value in plan.payload.items() if not str(key).startswith("_")}
        try:
            status, body = transport("POST", "/s/inbound/doc", body_out)
        except SyncError as exc:
            counters.fail(f"строка {plan.row}: {exc.message}")
            continue
        if status >= 400 or not isinstance(body, dict):
            message = body.get("error") if isinstance(body, dict) else status
            counters.fail(f"строка {plan.row}: {message}")
            continue
        if body.get("created") is True:
            counters.created += 1
        else:
            counters.updated += 1
        if not plan.patch:
            continue
        path = "/s/packages/" + quote(plan.number, safe="")
        try:
            pstatus, pbody = transport("PATCH", path, plan.patch)
        except SyncError as exc:
            counters.fail(f"строка {plan.row}: {exc.message}")
            continue
        if pstatus >= 400:
            counters.fail(f"строка {plan.row}: {pbody.get('error') or pstatus}")
    for item in attachments:
        if item.error:
            counters.fail(item.error)
            continue
        path = "/s/packages/" + quote(item.number, safe="")
        try:
            gstatus, detail = transport("GET", path, None)
        except SyncError as exc:
            counters.fail(f"{item.path.name}: {exc.message}")
            continue
        if gstatus >= 400 or not isinstance(detail, dict):
            counters.fail(f"{item.path.name}: {detail.get('error') if isinstance(detail, dict) else gstatus}")
            continue
        if item.path.name in _attached_names(detail):
            continue
        blob = item.path.read_bytes()
        event = {
            "type": "file",
            "text": item.path.name,
            "actor": "reestr",
            "attachments": [{"name": item.path.name, "content_base64": base64.b64encode(blob).decode("ascii")}],
        }
        try:
            estatus, ebody = transport("POST", path + "/events", event)
        except SyncError as exc:
            counters.fail(f"{item.path.name}: {exc.message}")
            continue
        if estatus >= 400:
            counters.fail(f"{item.path.name}: {ebody.get('error') or estatus}")
            continue
        counters.attached += 1
    return counters


def read_secret_file(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RowError(f"Файл секрета не читается: {exc.__class__.__name__}") from exc
    keys: dict[str, str] = {}
    plain: list[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", text):
            key, value = text.split("=", 1)
            keys[key] = value.strip().strip('"').strip("'")
        else:
            plain.append(text)
    for key in ("INBOUND_SECRET", "DELO_CRM_INBOUND_SECRET"):
        if keys.get(key):
            return keys[key]
    if len(plain) == 1:
        return plain[0]
    raise RowError("В файле секрета нет INBOUND_SECRET.")


def _secret_from_args(secret_file: str | None) -> str:
    if secret_file:
        return read_secret_file(Path(secret_file))
    return os.environ.get("INBOUND_SECRET", "").strip() or os.environ.get("DELO_CRM_INBOUND_SECRET", "").strip()


class HttpTransport:
    def __init__(self, base_url: str, secret: str, timeout: float, connect_host: str) -> None:
        self.base_url = base_url
        self.secret = secret
        self.timeout = timeout
        self.connect_host = connect_host

    def __call__(self, method: str, path: str, payload: dict | None) -> tuple[int, dict]:
        return request_json(
            method,
            path,
            payload,
            base_url=self.base_url,
            secret=self.secret,
            timeout=self.timeout,
            connect_host=self.connect_host,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Импорт CSV реестра в CRM (POST /s/inbound/doc).")
    parser.add_argument("csv_path", help="CSV с разделителем «;»")
    parser.add_argument("--base-url", default=os.environ.get("DELO_CRM_URL", "").strip() or "http://127.0.0.1:8091")
    parser.add_argument("--secret-file", help="Файл с секретом или env-файл с INBOUND_SECRET")
    parser.add_argument("--attach", action="append", default=[], metavar="NUMBER=PATH|PATH")
    parser.add_argument("--docs-dir", help="Каталог файлов Д-ГГГГ-NNNN-* или 01-имя, 02-имя, …")
    parser.add_argument(
        "--payload-json",
        action="append",
        default=[],
        metavar="FILE",
        help="JSON заявки поверх строки CSV с тем же number. Рядом с CSV ещё читаются payload-*.json",
    )
    parser.add_argument("--connect", default=os.environ.get("DELO_CRM_CONNECT", "").strip(), help="TCP-адрес, если URL — живое имя, а /s/ открыт только с loopback")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--dry-run", action="store_true", help="Разобрать CSV и посчитать план, без запросов")
    args = parser.parse_args(argv)
    path = Path(args.csv_path)
    if not path.is_file():
        print("CSV не найден.", file=sys.stderr)
        print(Counters(errors=1).line())
        return 2
    try:
        plans, row_errors = load_csv(path)
        objects, owners, enrich_errors = load_enrichment(args.payload_json, path)
        row_errors.extend(enrich_errors)
        row_errors.extend(apply_enrichment(plans, objects))
        known = {plan.number for plan in plans}
        owners = {name: number for name, number in owners.items() if number in known}
        attachments = collect_attachments(args.docs_dir, args.attach, [plan.number for plan in plans], owners)
    except RowError as exc:
        print(str(exc), file=sys.stderr)
        print(Counters(errors=1).line())
        return 2
    counters = Counters()
    for message in row_errors:
        counters.fail(message)
    secret = ""
    if not args.dry_run:
        try:
            secret = _secret_from_args(args.secret_file)
        except RowError as exc:
            print(str(exc), file=sys.stderr)
            print(Counters(errors=counters.errors + 1).line())
            return 2
        if len(secret) < 16:
            print("Нет секрета: INBOUND_SECRET или --secret-file.", file=sys.stderr)
            print(Counters(errors=counters.errors + 1).line())
            return 2
    transport = None if args.dry_run else HttpTransport(args.base_url.rstrip("/"), secret, args.timeout, args.connect)
    outcome = run_import(plans, attachments, transport, args.dry_run)
    counters.created += outcome.created
    counters.updated += outcome.updated
    counters.attached += outcome.attached
    counters.errors += outcome.errors
    counters.notes.extend(outcome.notes)
    for message in counters.notes:
        print(message, file=sys.stderr)
    print(counters.line())
    if args.dry_run:
        print("dry-run: запросы не отправлялись", file=sys.stderr)
    return 1 if counters.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
