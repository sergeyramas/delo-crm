"""Сканы водителя для пакета заказчику.

Файлы только на диске DATA_DIR/files/parties/<id>/. В базе — метаданные.
СТС привязан к карточке водителя и к госномеру/авто, которые были на ней в момент загрузки.
Заявка файлы не приносит: их кладут руками с карточки водителя.
Почты отсюда нет.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import quote

from app.db import Database
from app.domain import now_iso
from app.logic import ApiError, clean, fetch_package

SCAN_KINDS = ("sts", "license", "passport")
SCAN_LABEL = {"sts": "СТС", "license": "Права", "passport": "Паспорт"}
SCAN_MISSING = {"sts": "нет СТС", "license": "нет прав", "passport": "нет паспорта"}
ORG_KIND = "org_card"
ORG_LABEL = "Карточка организации"
ATTACHMENT_KINDS = (ORG_KIND, *SCAN_KINDS)
MAX_SCAN = 12 * 1024 * 1024

_MAGIC = {
    "pdf": (b"%PDF", "application/pdf"),
    "jpg": (b"\xff\xd8\xff", "image/jpeg"),
    "png": (b"\x89PNG\r\n\x1a\n", "image/png"),
}


def size_label(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} КБ"
    return f"{size / (1024 * 1024):.1f} МБ"


def _ext(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    if suffix == "jpeg":
        return "jpg"
    return suffix


def sniff(filename: str, blob: bytes) -> tuple[str, str]:
    if not blob:
        raise ApiError(422, "Пустой файл.")
    if len(blob) > MAX_SCAN:
        raise ApiError(413, "Файл больше 12 МБ.")
    ext = _ext(filename)
    if ext not in _MAGIC:
        raise ApiError(422, "Нужен файл PDF, JPG или PNG.")
    magic, content_type = _MAGIC[ext]
    if not blob.startswith(magic):
        raise ApiError(422, "Содержимое файла не совпадает с расширением.")
    return ext, content_type


def display_name(name: str, ext: str) -> str:
    base = Path(name or "").name
    base = re.sub(r"[\r\n\x00\"]+", "", base).strip()
    base = base[:120]
    if not base or base in {".", ".."}:
        return f"scan.{ext}"
    return base


def _org_text(value: str) -> str:
    text = " ".join((value or "").casefold().replace("_", " ").split())
    text = re.sub(r"\.(pdf|jpe?g|png)$", "", text).strip()
    return text


def is_org_card(title: str, original_name: str = "") -> bool:
    for value in (title, Path(original_name or "").stem):
        text = _org_text(value)
        if text == "карточка организации" or text.startswith("карточка организации "):
            return True
    return False


def resolve_under_files(data_dir: Path, stored: str) -> Path:
    if not stored or stored.startswith(("/", "\\")) or "\\" in stored:
        raise ApiError(404, "Файл не найден.")
    parts = Path(stored).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ApiError(404, "Файл не найден.")
    root = (data_dir / "files").resolve()
    path = (root / stored).resolve()
    if path != root and root not in path.parents:
        raise ApiError(404, "Файл не найден.")
    if not path.is_file():
        raise ApiError(404, "Файл не найден.")
    return path


def _content_type_of(path: Path, fallback: str = "application/octet-stream") -> str:
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return fallback
    for magic, content_type in _MAGIC.values():
        if head.startswith(magic):
            return content_type
    return fallback


def attachment_url(number: str, kind: str) -> str:
    return f"/api/packages/{quote(number, safe='')}/client-attachments/{kind}"


def driver_scan_view(db: Database, party_id: int | None) -> list[dict]:
    found: dict[str, dict] = {}
    if party_id:
        for row in db.q(
            """SELECT kind, original_name, size, plate, vehicle, content_type
               FROM party_scans WHERE party_id = ?""",
            (int(party_id),),
        ):
            found[row["kind"]] = row
    out = []
    for kind in SCAN_KINDS:
        row = found.get(kind)
        out.append({
            "kind": kind,
            "label": SCAN_LABEL[kind],
            "missing_label": SCAN_MISSING[kind],
            "present": row is not None,
            "original_name": row["original_name"] if row else "",
            "size": int(row["size"]) if row else 0,
            "size_label": size_label(int(row["size"])) if row else "",
            "plate": row["plate"] if row else "",
            "vehicle": row["vehicle"] if row else "",
            "content_type": row["content_type"] if row else "",
        })
    return out


def org_card_brief(db: Database, package_id: int) -> dict | None:
    with db.tx() as conn:
        row = _org_row(conn, package_id)
    if row is None:
        return None
    return {"file_id": row["file_id"], "filename": row["original_name"], "size": row["size"]}


def _org_row(conn, package_id: int) -> dict | None:
    rows = conn.execute(
        """SELECT d.id, d.title, d.file_id, f.stored_name, f.original_name, f.size
           FROM documents d
           JOIN files f ON f.id = d.file_id
           WHERE d.package_id = ?
           ORDER BY d.id DESC""",
        (package_id,),
    ).fetchall()
    for row in rows:
        item = dict(row)
        if is_org_card(item["title"], item["original_name"]):
            return item
    return None


def _driver(conn, party_id: int) -> dict:
    row = conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Карточка не найдена.")
    party = dict(row)
    if party["kind"] != "driver":
        raise ApiError(422, "Сканы СТС, прав и паспорта загружаются только на карточку водителя.")
    return party


def save_party_scan(db: Database, party_id: int, kind: str, filename: str, blob: bytes) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in SCAN_KINDS:
        raise ApiError(422, "Неизвестный тип скана.")
    ext, content_type = sniff(filename, blob)
    original = display_name(filename, ext)
    party_id = int(party_id)
    existing = db.one("SELECT kind FROM parties WHERE id = ?", (party_id,))
    if existing is None:
        raise ApiError(404, "Карточка не найдена.")
    if existing["kind"] != "driver":
        raise ApiError(422, "Сканы СТС, прав и паспорта загружаются только на карточку водителя.")
    stored = f"parties/{party_id}/{kind}_{uuid.uuid4().hex}.{ext}"
    path = (db.data_dir / "files" / stored)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    old_name = ""
    moment = now_iso()
    try:
        with db.tx() as conn:
            party = _driver(conn, int(party_id))
            plate = clean(party["plate"], 40)
            vehicle = clean(party["vehicle"], 80)
            current = conn.execute(
                "SELECT stored_name FROM party_scans WHERE party_id = ? AND kind = ?",
                (party["id"], kind),
            ).fetchone()
            if current:
                old_name = current["stored_name"]
                conn.execute(
                    """UPDATE party_scans
                       SET stored_name = ?, original_name = ?, content_type = ?, size = ?,
                           plate = ?, vehicle = ?, created_at = ?
                       WHERE party_id = ? AND kind = ?""",
                    (stored, original, content_type, len(blob), plate, vehicle, moment, party["id"], kind),
                )
            else:
                conn.execute(
                    """INSERT INTO party_scans (
                        party_id, kind, stored_name, original_name, content_type, size, plate, vehicle, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (party["id"], kind, stored, original, content_type, len(blob), plate, vehicle, moment),
                )
    except Exception:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        raise
    if old_name and old_name != stored:
        _unlink_stored(db.data_dir, old_name)
    return {"party_id": int(party_id), "kind": kind, "stored_name": stored, "size": len(blob)}


def _unlink_stored(data_dir: Path, stored: str) -> None:
    try:
        resolve_under_files(data_dir, stored).unlink(missing_ok=True)
    except ApiError:
        return


def scan_file(db: Database, party_id: int, kind: str) -> tuple[Path, str, str]:
    if kind not in SCAN_KINDS:
        raise ApiError(404, "Файл не найден.")
    row = db.one(
        """SELECT s.stored_name, s.original_name, s.content_type, p.kind AS party_kind
           FROM party_scans s
           JOIN parties p ON p.id = s.party_id
           WHERE s.party_id = ? AND s.kind = ?""",
        (party_id, kind),
    )
    if not row or row["party_kind"] != "driver":
        raise ApiError(404, "Файл не найден.")
    path = resolve_under_files(db.data_dir, row["stored_name"])
    return path, row["original_name"], row["content_type"]


def client_attachments(db: Database, key: str) -> dict:
    with db.tx() as conn:
        package = fetch_package(conn, key)
        driver = None
        if package.get("driver_party_id"):
            row = conn.execute(
                "SELECT * FROM parties WHERE id = ? AND kind = 'driver'",
                (package["driver_party_id"],),
            ).fetchone()
            driver = dict(row) if row else None
        scans = {}
        if driver:
            for row in conn.execute(
                "SELECT * FROM party_scans WHERE party_id = ?",
                (driver["id"],),
            ).fetchall():
                scans[row["kind"]] = dict(row)
        org = _org_row(conn, package["id"])
    number = package["number"]
    attachments = []
    if org is not None:
        try:
            org_path = resolve_under_files(db.data_dir, org["stored_name"])
        except ApiError:
            org_path = None
        if org_path is not None:
            attachments.append({
                "type": ORG_KIND,
                "label": ORG_LABEL,
                "filename": org["original_name"],
                "size": int(org["size"]),
                "content_type": _content_type_of(org_path),
                "download_url": attachment_url(number, ORG_KIND),
            })
    missing = []
    for kind in SCAN_KINDS:
        row = scans.get(kind)
        if row is None:
            missing.append(kind)
            continue
        attachments.append({
            "type": kind,
            "label": SCAN_LABEL[kind],
            "filename": row["original_name"],
            "size": int(row["size"]),
            "content_type": row["content_type"],
            "plate": row["plate"] or (driver["plate"] if driver else ""),
            "vehicle": row["vehicle"] or (driver["vehicle"] if driver else ""),
            "download_url": attachment_url(number, kind),
        })
    return {
        "number": number,
        "package_id": package["id"],
        "driver_party_id": driver["id"] if driver else None,
        "plate": (driver["plate"] if driver else package.get("plate")) or "",
        "vehicle": (driver["vehicle"] if driver else package.get("vehicle")) or "",
        "org_card": any(item["type"] == ORG_KIND for item in attachments),
        "attachments": attachments,
        "missing": missing,
    }


def open_client_attachment(db: Database, key: str, kind: str) -> tuple[Path, str, str]:
    if kind not in ATTACHMENT_KINDS:
        raise ApiError(404, "Вложение не найдено.")
    with db.tx() as conn:
        package = fetch_package(conn, key)
        if kind == ORG_KIND:
            org = _org_row(conn, package["id"])
            if org is None:
                raise ApiError(404, "Карточка организации не приложена.")
            stored = org["stored_name"]
            filename = org["original_name"]
            content_type = ""
        else:
            if not package.get("driver_party_id"):
                raise ApiError(404, "Вложение не найдено.")
            row = conn.execute(
                """SELECT stored_name, original_name, content_type
                   FROM party_scans WHERE party_id = ? AND kind = ?""",
                (package["driver_party_id"], kind),
            ).fetchone()
            if row is None:
                raise ApiError(404, "Вложение не найдено.")
            stored = row["stored_name"]
            filename = row["original_name"]
            content_type = row["content_type"]
    path = resolve_under_files(db.data_dir, stored)
    if not content_type:
        content_type = _content_type_of(path)
    return path, filename, content_type
