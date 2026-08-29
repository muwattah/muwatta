"""Database connection and helpers for the Al-Muwaṭṭaʾ source layer."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path("/tmp/muwatta_source.db")  # reliable path in this environment
SCHEMA_PATH = ROOT / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix: str = "") -> str:
    u = uuid.uuid4().hex
    return f"{prefix}{u}" if prefix else u


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def audit(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    action: str,
    old_value: Any = None,
    new_value: Any = None,
    user_id: str = "system",
    reason: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (log_id, entity_type, entity_id, action, old_value, new_value, user_id, reason, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("log-"),
            entity_type,
            entity_id,
            action,
            json.dumps(old_value, ensure_ascii=False) if old_value is not None else None,
            json.dumps(new_value, ensure_ascii=False) if new_value is not None else None,
            user_id,
            reason,
            utcnow(),
        ),
    )


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)
