"""Демо-сделки с макетов 06.11.2026. Не для боевой базы: номера 0001–0009 занимаются явно.

Запуск: python -m app.seed
Повтор: python -m app.seed --force  (базу под /srv не трогает)
"""

from __future__ import annotations

import json
import sys

from app.config import Settings
from app.db import Database
from app.domain import STAGE_ORDER, status_for_stage

PACKAGES = [
    {
        "number": "Д-2026-0008",
        "stage": "nahodka",
        "client": "ООО «СкладКалуга»",
        "route_from": "Калуга",
        "route_to": "Тула",
        "load_date": "2026-11-07",
        "weight": "2,4 т",
        "rate_rub": 24000,
        "source": "mail",
        "list_note": "почта · a1 · 14 мин · 2,4 т · окна 09:00–16:00",
        "board_note": "почта · a1 · 2,4 т · 09:00–16:00 · 24 000 ₽",
        "hint": "ждёт карточку «лучшей связки»",
        "meta_left": "свежесть 14 мин",
        "meta_right": "Кабинетный",
        "kanban_pos": 10,
        "updated_at": "2026-11-06T18:00:00",
    },
    {
        "number": "Д-2026-0009",
        "stage": "na_odobrenii",
        "client": "АО «ЮгМеталл»",
        "route_from": "Коломна",
        "route_to": "Рязань",
        "load_date": "2026-11-07",
        "weight": "1,8 т",
        "rate_rub": 19500,
        "source": "manual",
        "list_note": "ЛК a1 · 32 мин · 1,8 т · ETA 15:20 · 80 км/ч",
        "board_note": "ЛК a1 · 1,8 т · ETA 15:20 · 19 500 ₽",
        "gate": "нужно «да» на отклик",
        "meta_left": "лучший в связке",
        "meta_right": "ATI · ГЛАВНЫЙ",
        "kanban_pos": 20,
        "updated_at": "2026-11-06T17:50:00",
    },
    {
        "number": "Д-2026-0001",
        "stage": "v_puti",
        "client": "ООО «Северный Хлеб»",
        "contact": "Иванова М.",
        "email": "docs@example.ru",
        "route_from": "Москва",
        "route_to": "Казань",
        "load_date": "2026-11-05",
        "cargo": "тент · до 3 т · 18 паллет",
        "pallets": "18",
        "rate_rub": 68000,
        "carrier_rate_rub": 61200,
        "source": "mail",
        "list_note": "почта · a1 · взят 05.11 · 18 паллет",
        "source_detail": "почта · кабинет a1 · связка окнами 08:00–17:30 · ETA погрузки 05.11 10:40 · 80 км/ч · +1/+1",
        "board_note": "Орлов К. · А123ВС 777 · 18 паллет",
        "meta_left": "ETA 06.11 16:10",
        "meta_right": "MAX-бот",
        "carrier_name": "ИП Орлов К.В.",
        "carrier_email": "orlov@example.ru",
        "carrier_contract": "договор № 105",
        "driver_name": "Орлов К.",
        "driver_channel": "MAX-бот",
        "plate": "А123ВС 777",
        "vehicle": "тент",
        "flags": "Оплата по оригиналам · отсрочка 5 дней",
        "decision": "berem",
        "signed_client": 1,
        "signed_carrier": 1,
        "kanban_pos": 10,
        "updated_at": "2026-11-06T17:40:00",
        "stage_times": {
            "nahodka": "2026-11-03T09:18:00",
            "na_odobrenii": "2026-11-03T09:22:00",
            "podtverzhden": "2026-11-03T10:12:00",
            "zayavka": "2026-11-03T11:40:00",
            "docs_sent": "2026-11-03T15:05:00",
            "podpisany": "2026-11-04T09:30:00",
            "v_puti": "2026-11-05T07:00:00",
        },
    },
    {
        "number": "Д-2026-0002",
        "stage": "docs_sent",
        "client": "АО «ТехноСклад»",
        "route_from": "Тверь",
        "route_to": "Санкт-Петербург",
        "load_date": "2026-11-05",
        "rate_rub": 42000,
        "source": "mail",
        "list_note": "почта · a1",
        "board_title": "Тверь → СПб · письмо ушло",
        "board_note": "ждём скан АО «ТехноСклад»",
        "meta_left": "perevozchik21ru",
        "meta_right": "Клиент",
        "carrier_name": "ИП Лебедев Д.А.",
        "driver_name": "Сомов П.",
        "plate": "В456КМ 69",
        "kanban_pos": 20,
        "updated_at": "2026-11-06T17:30:00",
    },
    {
        "number": "Д-2026-0003",
        "stage": "zayavka",
        "client": "ООО «Агро-Поле»",
        "route_from": "Рязань",
        "route_to": "Москва",
        "load_date": "2026-11-06",
        "cargo": "тент до 3 т",
        "rate_rub": 21000,
        "source": "manual",
        "list_note": "ЛК a1 · тент до 3 т",
        "board_title": "Рязань → Москва · заявка готова",
        "gate": "нужно «да» на отправку письма",
        "meta_left": "пакет заказчику",
        "meta_right": "Делопроизводитель",
        "carrier_name": "Самозан. Ершов И.Н.",
        "plate": "Е789ОР 62",
        "kanban_pos": 10,
        "updated_at": "2026-11-06T17:20:00",
    },
    {
        "number": "Д-2026-0004",
        "stage": "podtverzhden",
        "client": "ООО «МебельПро»",
        "route_from": "Москва",
        "route_to": "Нижний Новгород",
        "load_date": "2026-11-06",
        "rate_rub": 39500,
        "source": "mail",
        "list_note": "почта · a1",
        "board_title": "Москва → Н.Новгород · готовим пакет",
        "meta_left": "после «берём»",
        "meta_right": "Делопроизводитель",
        "carrier_name": "ИП Орлов К.В.",
        "driver_name": "Гусев А.",
        "plate": "К321МН 777",
        "decision": "berem",
        "kanban_pos": 30,
        "updated_at": "2026-11-06T17:10:00",
    },
    {
        "number": "Д-2026-0007",
        "stage": "podpisany",
        "client": "ООО «Северный Хлеб»",
        "route_from": "Тула",
        "route_to": "Москва",
        "load_date": "2026-11-07",
        "rate_rub": 18500,
        "source": "mail",
        "list_note": "почта · a1",
        "board_title": "Тула → Москва · подписаны",
        "board_note": "зеркало перевозчику получено",
        "meta_left": "к рейсу",
        "meta_right": "Андрей",
        "carrier_name": "ИП Карпов С.С.",
        "driver_name": "Карпов С.",
        "plate": "М654ТТ 71",
        "decision": "berem",
        "signed_client": 1,
        "signed_carrier": 1,
        "kanban_pos": 40,
        "updated_at": "2026-11-06T17:00:00",
    },
    {
        "number": "Д-2026-0005",
        "stage": "dostavlen",
        "client": "ООО «СтройБаза»",
        "route_from": "Калуга",
        "route_to": "Воронеж",
        "load_date": "2026-11-03",
        "rate_rub": 55000,
        "source": "manual",
        "list_note": "ЛК a1",
        "board_note": "ждём фото квитанции Почты России",
        "meta_left": "ТТН оригиналы",
        "meta_right": "Сомов П.",
        "carrier_name": "ИП Лебедев Д.А.",
        "driver_name": "Сомов П.",
        "plate": "В456КМ 69",
        "decision": "berem",
        "signed_client": 1,
        "signed_carrier": 1,
        "kanban_pos": 10,
        "updated_at": "2026-11-06T16:50:00",
    },
    {
        "number": "Д-2026-0006",
        "stage": "originaly",
        "client": "АО «ТехноСклад»",
        "route_from": "Москва",
        "route_to": "Ярославль",
        "load_date": "2026-11-01",
        "rate_rub": 27000,
        "source": "mail",
        "list_note": "почта · a1",
        "board_note": "оригиналы получены · таймер 7 банк. дней",
        "meta_left": "27 000 ₽",
        "meta_right": "Бухгалтерия",
        "carrier_name": "Самозан. Ершов И.Н.",
        "plate": "Е789ОР 62",
        "decision": "berem",
        "signed_client": 1,
        "signed_carrier": 1,
        "kanban_pos": 10,
        "updated_at": "2026-11-06T16:40:00",
    },
]

EVENTS_0001 = [
    ("2026-11-03T09:18:00", "note", "agent", "Бот ATI / пул", "Бот ATI / пул", "Новая выкладка по подписке. Кабинет a1, до 3 т, Москва → Казань, ставка 68 000 ₽, окна склада 08:00–17:30."),
    ("2026-11-03T09:20:00", "note", "agent", "Кабинетный", "Кабинетный", "Груз жив, дубля в пуле нет. Связка: погрузка +1 ч, ход 80 км/ч, прибытие 10:40 — в окно. Лучший из трёх кандидатов."),
    ("2026-11-03T09:22:00", "tg", "tg", "ATI · ГЛАВНЫЙ → Сергей", "ATI · ГЛАВНЫЙ", "Карточка «лучшей связки»: маршрут, ₽/км, окна, ETA, риски. Ждём «да» на отклик."),
    ("2026-11-03T09:40:00", "note", "yes", "Сергей", "Сергей", "«Да» на отклик. Контакт грузовладельца и трата атисов разрешены только этим решением."),
    ("2026-11-03T10:12:00", "note", "agent", "Груз взят", "Андрей", "Сделка подтверждена. Делопроизводитель получил задачу #дело."),
    ("2026-11-03T10:20:00", "tg", "tg", "Андрей, #дело", "Андрей", "Клиент: Северный Хлеб. Что: заявка на подпись. Груз: Москва → Казань, 05.11. №: Д-2026-0001."),
    ("2026-11-03T11:10:00", "note", "agent", "Делопроизводитель", "Делопроизводитель", "Красные флаги: оплата по оригиналам, отсрочка 5 дней. Ждём «берём»."),
    ("2026-11-03T11:28:00", "note", "yes", "Сергей", "Сергей", "«Берём». Подпись и печать можно ставить. Затем отдельное «да» на отправку письма."),
    ("2026-11-03T15:05:00", "mail_out", "mail", "Исходящее · perevozchik21ru", "perevozchik21ru", "Тема: Заявка Д-2026-0001 Москва → Казань, 05.11. Вложения: заявка PDF, карточка перевозчика. Запись о письме, которое ушло вручную, не из CRM."),
    ("2026-11-04T09:30:00", "mail_in", "mail", "Входящее · клиент", "клиент", "Подписанный скан заявки. Проброс в группу и в карточку сделки."),
    ("2026-11-04T10:05:00", "mail_out", "mail", "Зеркало перевозчику", "perevozchik21ru", "Заявка Д-2026-0001, ставка перевозчику 61 200 ₽. Скан вернулся в 16:40. Запись факта, CRM письмо не отправляла."),
    ("2026-11-05T06:40:00", "note", "driver", "Водителю (MAX)", "MAX-бот", "Погрузка сегодня в 08:00, склад Москва, ул. Примерная 1."),
    ("2026-11-05T11:15:00", "note", "driver", "Водитель", "Орлов К.", "Загрузился, выехал. 18 паллет."),
]

DOCS_0001 = [
    ("Заявка Д-2026-0001", "PDF · заказчику и зеркало перевозчику · стадия пакета: подписано", "Подтверждена"),
    ("Договор № 105", "ИП Орлов К.В. · Прил.1–3", "Подтверждён"),
    ("Карточка перевозчика", "для заказчика · после согласия Прил.2", "Отправлена"),
    ("Акт выполненных работ", "после доставки", "Не готов"),
]

TASKS = [
    ("Д-2026-0003", "Отправить пакет после «да»", "today", "сегодня 12:00", "Делопроизводитель"),
    ("Д-2026-0009", "Запросить «да» на отклик", "today", "сегодня 11:30", "ATI · ГЛАВНЫЙ"),
    ("Д-2026-0001", "Следить за рейсом, ETA Казань", "work", "06.11 16:10", "MAX-бот"),
    ("Д-2026-0002", "Подписанный скан от ТехноСклад", "wait", "06.11 12:00", "Грузовладелец"),
    ("Д-2026-0005", "Оригиналы ТТН почтой", "wait", "08.11", "Сомов П."),
    ("Д-2026-0007", "Обе стороны подписали", "done", "06.11 09:30", "Андрей"),
]


def _times(item: dict) -> dict:
    if item.get("stage_times"):
        return item["stage_times"]
    if item["stage"] not in STAGE_ORDER:
        return {}
    idx = STAGE_ORDER.index(item["stage"])
    day = item["load_date"]
    out = {}
    for pos, code in enumerate(STAGE_ORDER[: idx + 1]):
        minute = 10 + pos * 5
        out[code] = f"{day}T09:{minute:02d}:00"
    return out


def _wipe(conn) -> None:
    for table in (
        "package_doc_tracks",
        "negotiations",
        "fleet_files",
        "drivers",
        "vehicles",
        "carriers",
        "expeditor",
        "party_scans",
        "outbound_messages",
        "tasks",
        "documents",
        "events",
        "files",
        "packages",
        "parties",
        "clients",
        "year_counters",
    ):
        conn.execute(f"DELETE FROM {table}")


def _enrich(conn, settings) -> None:
    """Вымышленные телефоны и документы макета. Не боевые контакты."""
    from app.parties import upsert_party

    moment = "2026-11-06T18:10:00"
    specs = [
        (
            "client",
            "ООО «Северный Хлеб»",
            {
                "name": "ООО «Северный Хлеб»",
                "contact_name": "Иванова М.",
                "company": "ООО «Северный Хлеб»",
                "email": "docs@example.ru",
                "phone": "+7 (495) 111-11-11",
                "inn": "7701123456",
                "note": "грузовладелец",
            },
        ),
        (
            "carrier",
            "ИП Орлов К.В.",
            {
                "name": "ИП Орлов К.В.",
                "company": "ИП Орлов К.В.",
                "email": "orlov@example.ru",
                "phone": "+7 903 000-00-22",
                "inn": "770200000020",
                "contract": "договор № 105",
            },
        ),
        (
            "driver",
            "Орлов К.",
            {
                "name": "Орлов К.",
                "phone": "+7 903 555-00-10",
                "vehicle": "тент",
                "plate": "А123ВС 777",
                "license_no": "77 00 123456",
                "passport_no": "4500 000000",
                "channel": "MAX-бот",
            },
        ),
    ]
    for kind, name, fields in specs:
        row = conn.execute(
            "SELECT id FROM parties WHERE kind = ? AND name = ? COLLATE NOCASE ORDER BY id LIMIT 1",
            (kind, name),
        ).fetchone()
        upsert_party(conn, settings, kind, fields, moment, int(row["id"]) if row else None)


def main() -> int:
    settings = Settings.from_env()
    path = str(settings.sqlite_path)
    if path.startswith("/srv/") or path.startswith("/srv"):
        print("Отказ: демо не пишется в базу под /srv.", file=sys.stderr)
        return 2
    db = Database(settings)
    db.init()
    force = "--force" in sys.argv
    existing = db.one("SELECT COUNT(*) AS n FROM packages")
    if existing and existing["n"] and not force:
        print("В базе уже есть пакеты. Повторите с --force на локальной базе, не на /srv.")
        return 1
    with db.tx() as conn:
        if force:
            _wipe(conn)
        clients: dict[str, int] = {}
        for item in PACKAGES:
            name = item["client"]
            if name not in clients:
                cur = conn.execute(
                    "INSERT INTO clients (name, contact_name, email, phone, note, created_at) VALUES (?, ?, ?, '', '', ?)",
                    (name, item.get("contact", ""), item.get("email", ""), item["updated_at"]),
                )
                clients[name] = int(cur.lastrowid)
            elif item.get("contact") or item.get("email"):
                conn.execute(
                    "UPDATE clients SET contact_name = ?, email = ? WHERE id = ? AND contact_name = ''",
                    (item.get("contact", ""), item.get("email", ""), clients[name]),
                )
            seq = int(item["number"].rsplit("-", 1)[1])
            stage = item["stage"]
            status = status_for_stage(stage)
            conn.execute(
                """INSERT INTO packages (
                    number, year, seq, status, stage, client_id, route_from, route_to, cargo, weight, pallets,
                    rate_rub, carrier_rate_rub, load_date, source, source_detail, list_note, hint, board_title,
                    board_note, meta_left, meta_right, kanban_pos, carrier_name, carrier_email, carrier_contract,
                    driver_name, driver_channel, plate, vehicle, gate, flags, decision, signed_client, signed_carrier,
                    stage_times_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["number"], 2026, seq, status, stage, clients[name],
                    item["route_from"], item["route_to"], item.get("cargo", ""), item.get("weight", ""), item.get("pallets", ""),
                    item.get("rate_rub"), item.get("carrier_rate_rub"), item["load_date"], item["source"],
                    item.get("source_detail", ""), item.get("list_note", ""), item.get("hint", ""), item.get("board_title", ""),
                    item.get("board_note", ""), item.get("meta_left", ""), item.get("meta_right", ""), item.get("kanban_pos", 0),
                    item.get("carrier_name", ""), item.get("carrier_email", ""), item.get("carrier_contract", ""),
                    item.get("driver_name", ""), item.get("driver_channel", ""), item.get("plate", ""), item.get("vehicle", ""),
                    item.get("gate", ""), item.get("flags", ""), item.get("decision", ""),
                    item.get("signed_client", 0), item.get("signed_carrier", 0),
                    json.dumps(_times(item), ensure_ascii=False),
                    item["load_date"] + "T09:00:00", item["updated_at"],
                ),
            )
        conn.execute(
            "INSERT INTO year_counters (year, last_seq) VALUES (2026, 9) ON CONFLICT(year) DO UPDATE SET last_seq = 9"
        )
        pkg = {row["number"]: row["id"] for row in conn.execute("SELECT id, number FROM packages")}
        for moment, etype, group, title, actor, text in EVENTS_0001:
            conn.execute(
                """INSERT INTO events (package_id, type, text, actor, title, attachments_json, meta_json, created_at)
                   VALUES (?, ?, ?, ?, ?, '[]', ?, ?)""",
                (pkg["Д-2026-0001"], etype, text, actor, title, json.dumps({"group": group}), moment),
            )
        for title, note, state in DOCS_0001:
            conn.execute(
                """INSERT INTO documents (package_id, title, note, state_label, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (pkg["Д-2026-0001"], title, note, state, "2026-11-04T09:30:00"),
            )
        for number, title, bucket, due, assignee in TASKS:
            conn.execute(
                """INSERT INTO tasks (package_id, title, bucket, due_label, assignee, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (pkg[number], title, bucket, due, assignee, "2026-11-06T09:00:00"),
            )
        from app.parties import backfill_conn

        backfill_conn(conn, settings, "2026-11-06T18:05:00")
        _enrich(conn, settings)
        from app.carriers import seed_doronin, seed_negotiations, seed_package_link
        from app.tracks import sync_from_package

        fleet = seed_doronin(conn, settings.data_dir, "2026-11-06T18:20:00")
        seed_package_link(conn, "Д-2026-0003", fleet, "2026-11-06T18:21:00")
        seed_negotiations(
            conn,
            "Д-2026-0003",
            [
                ("2026-11-06T11:00:00", "Сергей", "Ставка 21 000 ₽, окна склада 09:00–15:00. Ждём паспорт."),
                ("2026-11-06T12:30:00", "Андрей", "Паспорт обещали сегодня. Оплата по сканам, оригиналы потом."),
            ],
        )
        for row in conn.execute("SELECT * FROM packages"):
            sync_from_package(conn, dict(row), row["updated_at"])
    print(f"Макет: 9 сделок в {settings.sqlite_path}. Доронин и экспедитор из образца карточки. Следующий живой номер 2026 года будет Д-2026-0010.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
