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
    carrier_id INTEGER,
    vehicle_id INTEGER,
    driver_id INTEGER,
    cargo_volume TEXT NOT NULL DEFAULT '',
    load_window TEXT NOT NULL DEFAULT '',
    payment_terms TEXT NOT NULL DEFAULT '',
    shipper_contact TEXT NOT NULL DEFAULT '',
    gps_status TEXT NOT NULL DEFAULT 'off',
    gps_invited_at TEXT NOT NULL DEFAULT '',
    dispatcher_login TEXT NOT NULL DEFAULT '',
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
    gps_status TEXT NOT NULL DEFAULT 'off',
    gps_invited_at TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    package_id INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    party_id INTEGER REFERENCES parties(id) ON DELETE SET NULL,
    channel TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    body_html TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    thread_key TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    author_user TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_pkg_sent ON messages(package_id, sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_channel_ext
    ON messages(channel, external_id) WHERE external_id <> '';

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

CREATE TABLE IF NOT EXISTS expeditor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT '',
    inn TEXT NOT NULL DEFAULT '',
    ogrnip TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    bank TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '',
    bik TEXT NOT NULL DEFAULT '',
    corr_account TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    dispatcher_name TEXT NOT NULL DEFAULT '',
    dispatcher_phone TEXT NOT NULL DEFAULT '',
    ati_code TEXT NOT NULL DEFAULT '',
    edo_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS carriers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    inn TEXT NOT NULL DEFAULT '',
    ogrn TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    max_contact TEXT NOT NULL DEFAULT '',
    tg_contact TEXT NOT NULL DEFAULT '',
    wa_contact TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY,
    carrier_id INTEGER NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
    make_model TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    volume TEXT NOT NULL DEFAULT '',
    loading TEXT NOT NULL DEFAULT '',
    plate TEXT NOT NULL DEFAULT '',
    trailer_plate TEXT NOT NULL DEFAULT '',
    vin TEXT NOT NULL DEFAULT '',
    sts_series TEXT NOT NULL DEFAULT '',
    sts_number TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drivers (
    id INTEGER PRIMARY KEY,
    carrier_id INTEGER NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    birth_date TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    passport_series TEXT NOT NULL DEFAULT '',
    passport_number TEXT NOT NULL DEFAULT '',
    passport_issued_by TEXT NOT NULL DEFAULT '',
    passport_issued_at TEXT NOT NULL DEFAULT '',
    passport_dept_code TEXT NOT NULL DEFAULT '',
    license_series TEXT NOT NULL DEFAULT '',
    license_number TEXT NOT NULL DEFAULT '',
    license_issued_at TEXT NOT NULL DEFAULT '',
    license_issued_by TEXT NOT NULL DEFAULT '',
    license_valid_until TEXT NOT NULL DEFAULT '',
    license_categories TEXT NOT NULL DEFAULT '',
    consent INTEGER NOT NULL DEFAULT 0,
    consent_at TEXT NOT NULL DEFAULT '',
    user_is_carrier INTEGER NOT NULL DEFAULT 0,
    gps_status TEXT NOT NULL DEFAULT 'off',
    gps_invited_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fleet_files (
    id INTEGER PRIMARY KEY,
    owner_kind TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    file_kind TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS negotiations (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    author TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_doc_tracks (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    status_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(package_id, recipient)
);

CREATE INDEX IF NOT EXISTS idx_vehicles_carrier ON vehicles(carrier_id);
CREATE INDEX IF NOT EXISTS idx_drivers_carrier ON drivers(carrier_id);
CREATE INDEX IF NOT EXISTS idx_fleet_files_owner ON fleet_files(owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS idx_negotiations_pkg ON negotiations(package_id, id);
CREATE INDEX IF NOT EXISTS idx_tracks_pkg ON package_doc_tracks(package_id);

-- Точки GPS. expires_at пустой, пока рейс не завершён
-- (стадия не dostavlen / originaly / otkaz). После завершения точка живёт 30 суток.
CREATE TABLE IF NOT EXISTS gps_points (
    id INTEGER PRIMARY KEY,
    package_id INTEGER REFERENCES packages(id) ON DELETE CASCADE,
    party_id INTEGER REFERENCES parties(id) ON DELETE CASCADE,
    driver_id INTEGER REFERENCES drivers(id) ON DELETE CASCADE,
    lat REAL,
    lon REAL,
    recorded_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'stub',
    expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gps_drafts (
    id INTEGER PRIMARY KEY,
    package_id INTEGER REFERENCES packages(id) ON DELETE CASCADE,
    party_id INTEGER REFERENCES parties(id) ON DELETE CASCADE,
    driver_id INTEGER REFERENCES drivers(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    body TEXT NOT NULL,
    attachment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    actor TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gps_polls (
    id INTEGER PRIMARY KEY,
    package_id INTEGER,
    party_id INTEGER,
    driver_id INTEGER,
    endpoint TEXT NOT NULL,
    called INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gps_points_pkg ON gps_points(package_id, id);
CREATE INDEX IF NOT EXISTS idx_gps_drafts_party ON gps_drafts(party_id, id);
CREATE INDEX IF NOT EXISTS idx_gps_polls_party ON gps_polls(party_id, id);
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
        adds = {
            "carrier_party_id": "carrier_party_id INTEGER",
            "driver_party_id": "driver_party_id INTEGER",
            "carrier_id": "carrier_id INTEGER",
            "vehicle_id": "vehicle_id INTEGER",
            "driver_id": "driver_id INTEGER",
            "cargo_volume": "cargo_volume TEXT NOT NULL DEFAULT ''",
            "load_window": "load_window TEXT NOT NULL DEFAULT ''",
            "payment_terms": "payment_terms TEXT NOT NULL DEFAULT ''",
            "shipper_contact": "shipper_contact TEXT NOT NULL DEFAULT ''",
            "gps_status": "gps_status TEXT NOT NULL DEFAULT 'off'",
            "gps_invited_at": "gps_invited_at TEXT NOT NULL DEFAULT ''",
            "dispatcher_login": "dispatcher_login TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in adds.items():
            if name not in cols:
                self.conn.execute(f"ALTER TABLE packages ADD COLUMN {ddl}")
        party_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(parties)")}
        if "gps_status" not in party_cols:
            self.conn.execute("ALTER TABLE parties ADD COLUMN gps_status TEXT NOT NULL DEFAULT 'off'")
        if "gps_invited_at" not in party_cols:
            self.conn.execute("ALTER TABLE parties ADD COLUMN gps_invited_at TEXT NOT NULL DEFAULT ''")
        driver_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(drivers)")}
        if "gps_status" not in driver_cols:
            self.conn.execute("ALTER TABLE drivers ADD COLUMN gps_status TEXT NOT NULL DEFAULT 'off'")
        if "gps_invited_at" not in driver_cols:
            self.conn.execute("ALTER TABLE drivers ADD COLUMN gps_invited_at TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            "INSERT OR IGNORE INTO expeditor (id, updated_at) VALUES (1, '')"
        )

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
