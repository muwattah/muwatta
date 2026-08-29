"""Verification workflow: approve / reject / set printed page / gate publication."""
from __future__ import annotations

import json
from typing import Any, Optional

from .db import get_conn, audit, new_id, row_to_dict, utcnow


ALLOWED_TRANSITIONS = {
    "imported": {"extracted", "needs_review", "rejected"},
    "extracted": {"needs_review", "verified", "rejected"},
    "needs_review": {"verified", "rejected", "needs_review"},
    "verified": {"approved", "needs_review", "rejected"},
    "approved": {"published", "needs_review", "rejected"},
    "published": {"approved"},  # unpublish
    "rejected": {"needs_review"},
    "superseded": set(),
}


def _get_text(conn, text_id: str):
    return conn.execute(
        "SELECT * FROM text_units WHERE text_id = ?", (text_id,)
    ).fetchone()


def set_verification_status(
    entity_type: str,
    entity_id: str,
    new_status: str,
    user_id: str = "admin",
    reason: str = "",
) -> dict:
    table_map = {
        "source_page": ("source_pages", "source_page_id"),
        "text_unit": ("text_units", "text_id"),
        "editorial_note": ("editorial_notes", "editorial_note_id"),
        "book": ("books", "book_id"),
        "chapter": ("chapters", "chapter_id"),
    }
    if entity_type not in table_map:
        raise ValueError(f"Unknown entity_type: {entity_type}")

    table, pk = table_map[entity_type]
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {pk} = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"{entity_type} {entity_id} not found")

        old_status = row["verification_status"]
        allowed = ALLOWED_TRANSITIONS.get(old_status, set())
        if new_status not in allowed and new_status != old_status:
            raise ValueError(
                f"Illegal transition {old_status} → {new_status} for {entity_type}"
            )

        conn.execute(
            f"UPDATE {table} SET verification_status = ? WHERE {pk} = ?",
            (new_status, entity_id),
        )

        # Publication gate for text_units
        if entity_type == "text_unit":
            if new_status == "published":
                # Hard gate
                if not row["arabic_text_verified"]:
                    raise ValueError(
                        "Cannot publish: arabic_text_verified is NULL"
                    )
                if not row["source_page_id"]:
                    raise ValueError("Cannot publish: missing source_page_id")
                conn.execute(
                    "UPDATE text_units SET published = 1, updated_at = ? WHERE text_id = ?",
                    (utcnow(), entity_id),
                )
            elif old_status == "published" and new_status != "published":
                conn.execute(
                    "UPDATE text_units SET published = 0, updated_at = ? WHERE text_id = ?",
                    (utcnow(), entity_id),
                )

        audit(
            conn, entity_type, entity_id, f"status:{new_status}",
            old_value={"verification_status": old_status},
            new_value={"verification_status": new_status},
            user_id=user_id,
            reason=reason,
        )
        return {"entity_type": entity_type, "entity_id": entity_id,
                "old_status": old_status, "new_status": new_status}


def set_printed_page(
    source_page_id: str,
    printed_page_number: Optional[int],
    status: str = "verified",
    user_id: str = "admin",
    reason: str = "",
) -> dict:
    if status not in ("verified", "needs_review", "absent", "unclear"):
        raise ValueError("Invalid printed_page_status")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM source_pages WHERE source_page_id = ?",
            (source_page_id,),
        ).fetchone()
        if not row:
            raise ValueError("Page not found")
        old = {
            "printed_page_number": row["printed_page_number"],
            "printed_page_status": row["printed_page_status"],
        }
        conn.execute(
            """
            UPDATE source_pages
            SET printed_page_number = ?, printed_page_status = ?
            WHERE source_page_id = ?
            """,
            (printed_page_number, status, source_page_id),
        )
        audit(
            conn, "source_page", source_page_id, "set_printed_page",
            old_value=old,
            new_value={"printed_page_number": printed_page_number, "printed_page_status": status},
            user_id=user_id, reason=reason,
        )
        return {"source_page_id": source_page_id, **old,
                "new_printed": printed_page_number, "new_status": status}


def approve_text(
    text_id: str,
    verified_arabic: str,
    user_id: str = "admin",
    reason: str = "Human verification against scan",
) -> dict:
    """
    Set arabic_text_verified from human-checked text.
    Does NOT invent. Admin must supply the text that matches the scan.
    """
    if not verified_arabic or not verified_arabic.strip():
        raise ValueError("verified_arabic cannot be empty")

    with get_conn() as conn:
        row = _get_text(conn, text_id)
        if not row:
            raise ValueError("text_unit not found")
        if not row["source_page_id"]:
            raise ValueError("text_unit has no source_page_id — cannot approve")

        old = {
            "arabic_text_verified": row["arabic_text_verified"],
            "verification_status": row["verification_status"],
        }
        conn.execute(
            """
            UPDATE text_units
            SET arabic_text_verified = ?,
                verification_status = 'approved',
                updated_at = ?
            WHERE text_id = ?
            """,
            (verified_arabic.strip(), utcnow(), text_id),
        )
        audit(
            conn, "text_unit", text_id, "approve_text",
            old_value=old,
            new_value={
                "arabic_text_verified": verified_arabic.strip()[:200] + "...",
                "verification_status": "approved",
            },
            user_id=user_id, reason=reason,
        )
        return {"text_id": text_id, "status": "approved"}


def create_text_unit_from_page(
    source_page_id: str,
    arabic_text_raw: Optional[str] = None,
    text_type: str = "needs_review",
    hadith_number: Optional[str] = None,
    text_order: Optional[int] = None,
    user_id: str = "system",
) -> str:
    """
    Create a text unit linked to a page.
    arabic_text_verified remains NULL.
    verification_status = needs_review.
    """
    with get_conn() as conn:
        page = conn.execute(
            "SELECT * FROM source_pages WHERE source_page_id = ?",
            (source_page_id,),
        ).fetchone()
        if not page:
            raise ValueError("source_page not found")

        text_id = new_id("txt-")
        conn.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id,
                text_order, hadith_number, text_type,
                arabic_text_raw, arabic_text_verified,
                pdf_page, printed_page, verification_status, published
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'needs_review', 0)
            """,
            (
                text_id,
                page["edition_id"],
                page["volume_id"],
                source_page_id,
                text_order,
                hadith_number,
                text_type,
                arabic_text_raw,
                page["pdf_page_number"],
                page["printed_page_number"],
            ),
        )
        audit(
            conn, "text_unit", text_id, "create",
            new_value={
                "source_page_id": source_page_id,
                "text_type": text_type,
                "has_raw": bool(arabic_text_raw),
            },
            user_id=user_id,
            reason="Created from page; awaits human verification",
        )
        return text_id


def get_provenance(text_id: str) -> Optional[dict]:
    """Full provenance chain for a text unit."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                t.text_id, t.hadith_number, t.text_type,
                t.arabic_text_raw, t.arabic_text_verified,
                t.verification_status, t.published,
                t.pdf_page, t.printed_page,
                sp.source_page_id, sp.pdf_page_number, sp.printed_page_number,
                sp.printed_page_status, sp.image_path,
                v.volume_number, v.arabic_label AS volume_label,
                sf.original_filename, sf.sha256, sf.storage_path,
                e.title_arabic, e.muhqqiq, e.year_hijri, e.year_ce,
                e.edition_statement, e.publisher, e.place,
                b.arabic_title AS book_title, b.book_order,
                c.arabic_title AS chapter_title, c.chapter_order
            FROM text_units t
            LEFT JOIN source_pages sp ON sp.source_page_id = t.source_page_id
            LEFT JOIN volumes v ON v.volume_id = t.volume_id
            LEFT JOIN source_files sf ON sf.source_id = sp.source_id
            LEFT JOIN editions e ON e.edition_id = t.edition_id
            LEFT JOIN books b ON b.book_id = t.book_id
            LEFT JOIN chapters c ON c.chapter_id = t.chapter_id
            WHERE t.text_id = ?
            """,
            (text_id,),
        ).fetchone()
        return row_to_dict(row)


def get_audit_history(entity_type: str, entity_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM audit_log
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY timestamp ASC
            """,
            (entity_type, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]
