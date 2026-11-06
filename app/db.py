"""SQLite. Одна база, явные транзакции, номера не переиспользуются."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from app.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL UNIQUE,
    year INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id),
    route_from TEXT NOT NULL DEFAULT '',
    route_to TEXT NOT NULL DEFAULT '',
    cargo TEXT NOT NULL DEFAULT '',
    weight TEXT NOT NULL DEFAULT '',
    pallets TEXT NOT NULL DEFAULT '',
    rate_rub INTEGER,
    carrier_rate_rub INTEGER,
    load_date TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    source_detail TEXT NOT NULL DEFAULT '',
    list_note TEXT NOT NULL DEFAULT '',
    hint TEXT NOT NULL DEFAULT '',
    board_title TEXT NOT NULL DEFAULT '',
    board_note TEXT NOT NULL DEFAULT '',
    meta_left TEXT NOT NULL DEFAULT '',
    meta_right TEXT NOT NULL DEFAULT '',
    kanban_pos INTEGER NOT NULL DEFAULT 0,
    subject TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    carrier_name TEXT NOT NULL DEFAULT '',
    carrier_email TEXT NOT NULL DEFAULT '',
    carrier_contract TEXT NOT NULL DEFAULT '',
    driver_name TEXT NOT NULL DEFAULT '',
    driver_channel TEXT NOT NULL DEFAULT '',
    plate TEXT NOT NULL DEFAULT '',
    vehicle TEXT NOT NULL DEFAULT '',
    gate TEXT NOT NULL DEFAULT '',
    flags TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    refusal_comment TEXT NOT NULL DEFAULT '',
    signed_client INTEGER NOT NULL DEFAULT 0,
    signed_carrier INTEGER NOT NULL DEFAULT 0,
    stage_times_json TEXT NOT NULL DEFAULT '{}',
    carrier_party_id INTEGER,
    driver_party_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(year, seq)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    state_label TEXT NOT NULL DEFAULT 'Приложен',
    file_id INTEGER REFERENCES files(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    bucket TEXT NOT NULL,
    due_label TEXT NOT NULL DEFAULT '',
    assignee TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS year_counters (
    year INTEGER PRIMARY KEY,
    last_seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_status ON packages(status);
CREATE INDEX IF NOT EXISTS idx_packages_stage ON packages(stage);
CREATE INDEX IF NOT EXISTS idx_events_pkg ON events(package_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_packages_thread
    ON packages(thread_id) WHERE thread_id <> '';

CREATE TABLE IF NOT EXISTS parties (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    phone_key TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    email_key TEXT NOT NULL DEFAULT '',
    inn TEXT NOT NULL DEFAULT '',
    inn_key TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    vehicle TEXT NOT NULL DEFAULT '',
    plate TEXT NOT NULL DEFAULT '',
    license_no TEXT NOT NULL DEFAULT '',
    passport_no TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    contract TEXT NOT NULL DEFAULT '',
    tg_status TEXT NOT NULL DEFAULT 'unknown',
    wa_status TEXT NOT NULL DEFAULT 'unknown',
    max_status TEXT NOT NULL DEFAULT 'unknown',
    messengers_checked_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    login TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    role_code TEXT NOT NULL REFERENCES roles(code),
    password_hash TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY,
    party_id INTEGER NOT NULL REFERENCES parties(id),
    channel TEXT NOT NULL,
    body TEXT NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

INSERT OR IGNORE INTO roles (code, label) VALUES
    ('admin', 'Админ'),
    ('dispatcher', 'Диспетчер'),
    ('operator', 'Оператор');

CREATE INDEX IF NOT EXISTS idx_parties_kind_inn ON parties(kind, inn_key);
CREATE INDEX IF NOT EXISTS idx_parties_kind_phone ON parties(kind, phone_key);
CREATE INDEX IF NOT EXISTS idx_parties_kind_email ON parties(kind, email_key);
CREATE INDEX IF NOT EXISTS idx_outbound_party ON outbound_messages(party_id, id);

CREATE TABLE IF NOT EXISTS party_scans (
    id INTEGER PRIMARY KEY,
    party_id INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    plate TEXT NOT NULL DEFAULT '',
    vehicle TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(party_id, kind)
);
"""


class Database:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.sqlite_path
        self.data_dir = settings.data_dir
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "files").mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")

    def init(self) -> None:
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(packages)")}
        if "carrier_party_id" not in cols:
            self.conn.execute("ALTER TABLE packages ADD COLUMN carrier_party_id INTEGER")
        if "driver_party_id" not in cols:
            self.conn.execute("ALTER TABLE packages ADD COLUMN driver_party_id INTEGER")

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        # Не вкладывать tx() в tx(): второе BEGIN упадёт.
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")
