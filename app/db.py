"""Database connection and helpers for the Al-Muwaṭṭaʾ source layer."""
from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / "storage" / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
SCHEMA_PATH = ROOT / "schema.sql"
EDITION_ID = "ed-bashshar-1997"
DB_PATH = RUNTIME_DIR / "muwatta_source.db"
SNAPSHOT_PATH = RUNTIME_DIR / "muwatta_source.snapshot.sqlite"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix: str = "") -> str:
    u = uuid.uuid4().hex
    return f"{prefix}{u}" if prefix else u


def stable_source_id(volume_number: int) -> str:
    return f"src-{EDITION_ID}-v{int(volume_number)}"


def stable_volume_id(volume_number: int) -> str:
    return f"vol-{EDITION_ID}-v{int(volume_number)}"


def stable_source_page_id(volume_number: int, pdf_page: int) -> str:
    return f"pg-{EDITION_ID}-v{int(volume_number)}-p{int(pdf_page):04d}"



PRIMARY_SNAPSHOT = RUNTIME_DIR / "muwatta_source.snapshot.sqlite"
SNAPSHOT_TMP = RUNTIME_DIR / "muwatta_source.snapshot.sqlite.tmp"


class PersistenceError(RuntimeError):
    pass


def _sqlite_ok(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            chk = conn.execute("PRAGMA integrity_check").fetchone()
            has = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='editions'"
            ).fetchone()
            return bool(chk and chk[0] == "ok" and has)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def persist_snapshot() -> bool:
    """Atomic snapshot AFTER a successful SQLite commit only."""
    if not _sqlite_ok(DB_PATH):
        return False
    SNAPSHOT_TMP.parent.mkdir(parents=True, exist_ok=True)
    if SNAPSHOT_TMP.exists():
        SNAPSHOT_TMP.unlink()
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(SNAPSHOT_TMP)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    if not _sqlite_ok(SNAPSHOT_TMP):
        SNAPSHOT_TMP.unlink(missing_ok=True)
        raise PersistenceError("snapshot temp failed integrity_check; previous snapshot kept")
    SNAPSHOT_TMP.replace(PRIMARY_SNAPSHOT)
    # Best-effort project copy; never fail the review write if artifacts FS errors.
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_bytes(PRIMARY_SNAPSHOT.read_bytes())
    except OSError:
        pass
    return True


def restore_snapshot_if_needed() -> str:
    if _sqlite_ok(DB_PATH):
        return "live"
    if PRIMARY_SNAPSHOT.exists() and PRIMARY_SNAPSHOT.stat().st_size > 0:
        if _sqlite_ok(PRIMARY_SNAPSHOT):
            DB_PATH.write_bytes(PRIMARY_SNAPSHOT.read_bytes())
            if _sqlite_ok(DB_PATH):
                return "restored"
        raise PersistenceError(
            f"Primary snapshot is corrupt: {PRIMARY_SNAPSHOT}. Refusing silent empty DB."
        )
    for cand in (SNAPSHOT_PATH, RUNTIME_DIR / "muwatta_source.snapshot.bak"):
        if cand.exists() and cand.stat().st_size > 0 and _sqlite_ok(cand):
            DB_PATH.write_bytes(cand.read_bytes())
            if _sqlite_ok(DB_PATH):
                return "restored"
        if cand.exists() and cand.stat().st_size > 0 and not _sqlite_ok(cand):
            raise PersistenceError(f"Snapshot is corrupt: {cand}. Refusing silent empty DB.")
    return "empty"


@contextmanager
def get_conn(*, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    restore_snapshot_if_needed()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        if readonly:
            conn.rollback()
            return
        conn.commit()
        persist_snapshot()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_has_schema() -> bool:
    restore_snapshot_if_needed()
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return False
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='editions'"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def init_db(*, force: bool = False) -> str:
    state = restore_snapshot_if_needed()
    if db_has_schema() and not force:
        ensure_review_columns()
        return "exists" if state != "restored" else "restored"
    if (DB_PATH.exists() and DB_PATH.stat().st_size > 0) and not force:
        raise RuntimeError(f"Database file exists but has no schema: {DB_PATH}. Refusing to overwrite.")
    if PRIMARY_SNAPSHOT.exists() and PRIMARY_SNAPSHOT.stat().st_size > 0 and not force:
        raise PersistenceError(
            f"Snapshot present at {PRIMARY_SNAPSHOT} but could not be used. Refusing empty DB."
        )
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    persist_snapshot()
    ensure_review_columns()
    return "created"


def ensure_review_columns() -> None:
    if not db_has_schema():
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(text_units)").fetchall()}
        if not cols:
            return
        if "arabic_text_proposed" not in cols:
            conn.execute("ALTER TABLE text_units ADD COLUMN arabic_text_proposed TEXT")
        if "review_flag" not in cols:
            conn.execute("ALTER TABLE text_units ADD COLUMN review_flag TEXT")
        conn.commit()
    finally:
        conn.close()
    persist_snapshot()


def migrate_stable_source_ids() -> dict:
    stats = {"pages": 0, "sources": 0, "volumes": 0}
    if not db_has_schema():
        return stats
    with get_conn() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        sources = list(conn.execute("SELECT source_id, volume_number FROM source_files"))
        for src in sources:
            new_sid = stable_source_id(src["volume_number"])
            if src["source_id"] == new_sid:
                continue
            conn.execute("UPDATE source_files SET source_id=? WHERE source_id=?", (new_sid, src["source_id"]))
            conn.execute("UPDATE volumes SET source_id=? WHERE source_id=?", (new_sid, src["source_id"]))
            conn.execute("UPDATE source_pages SET source_id=? WHERE source_id=?", (new_sid, src["source_id"]))
            stats["sources"] += 1
        volumes = list(conn.execute("SELECT volume_id, volume_number FROM volumes"))
        for vol in volumes:
            new_vid = stable_volume_id(vol["volume_number"])
            if vol["volume_id"] == new_vid:
                continue
            conn.execute("UPDATE volumes SET volume_id=? WHERE volume_id=?", (new_vid, vol["volume_id"]))
            conn.execute("UPDATE source_pages SET volume_id=? WHERE volume_id=?", (new_vid, vol["volume_id"]))
            conn.execute("UPDATE text_units SET volume_id=? WHERE volume_id=?", (new_vid, vol["volume_id"]))
            stats["volumes"] += 1
        pages = list(conn.execute(
            """SELECT sp.source_page_id, v.volume_number, sp.pdf_page_number
               FROM source_pages sp JOIN volumes v ON v.volume_id = sp.volume_id"""
        ))
        for pg in pages:
            new_pid = stable_source_page_id(pg["volume_number"], pg["pdf_page_number"])
            if pg["source_page_id"] == new_pid:
                continue
            old = pg["source_page_id"]
            conn.execute("UPDATE source_pages SET source_page_id=? WHERE source_page_id=?", (new_pid, old))
            conn.execute("UPDATE ocr_runs SET source_page_id=? WHERE source_page_id=?", (new_pid, old))
            conn.execute("UPDATE text_units SET source_page_id=? WHERE source_page_id=?", (new_pid, old))
            conn.execute("UPDATE text_unit_source_pages SET source_page_id=? WHERE source_page_id=?", (new_pid, old))
            conn.execute("UPDATE editorial_notes SET source_page_id=? WHERE source_page_id=?", (new_pid, old))
            stats["pages"] += 1
        conn.execute("PRAGMA foreign_keys = ON")
    return stats


def audit(conn, entity_type, entity_id, action, old_value=None, new_value=None, user_id="system", reason=""):
    conn.execute(
        """INSERT INTO audit_log (log_id, entity_type, entity_id, action, old_value, new_value, user_id, reason, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            new_id("log-"), entity_type, entity_id, action,
            json.dumps(old_value, ensure_ascii=False) if old_value is not None else None,
            json.dumps(new_value, ensure_ascii=False) if new_value is not None else None,
            user_id, reason, utcnow(),
        ),
    )


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)
