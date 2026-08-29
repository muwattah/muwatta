"""
Review API: human verification of OCR against scan.
Approve ≠ publish. arabic_text_verified only via human input.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .db import get_conn, audit, new_id, utcnow, row_to_dict


def list_review_pages(volume: int = 1, only_ocr: bool = True) -> list[dict]:
    with get_conn() as conn:
        q = """
            SELECT sp.source_page_id, v.volume_number, sp.pdf_page_number,
                   sp.printed_page_number, sp.printed_page_status,
                   sp.ocr_status, sp.verification_status, sp.blank_status,
                   (SELECT COUNT(*) FROM text_units t WHERE t.source_page_id = sp.source_page_id) AS text_count,
                   (SELECT o.ocr_confidence FROM ocr_runs o
                    WHERE o.source_page_id = sp.source_page_id
                    ORDER BY o.ocr_timestamp DESC LIMIT 1) AS ocr_confidence
            FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number = ?
        """
        if only_ocr:
            q += " AND sp.ocr_status IN ('done','needs_review')"
        q += " ORDER BY sp.pdf_page_number"
        return [dict(r) for r in conn.execute(q, (volume,)).fetchall()]


def get_page_review_bundle(volume: int, pdf_page: int) -> Optional[dict]:
    """Full bundle for the viewer: page meta + OCR + text units + books/chapters."""
    with get_conn() as conn:
        page = conn.execute(
            """
            SELECT sp.*, v.volume_number, sf.original_filename, sf.sha256, sf.storage_path
            FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            JOIN source_files sf ON sf.source_id = sp.source_id
            WHERE v.volume_number = ? AND sp.pdf_page_number = ?
            """,
            (volume, pdf_page),
        ).fetchone()
        if not page:
            return None
        page = dict(page)

        ocr = conn.execute(
            """
            SELECT ocr_run_id, ocr_engine, ocr_model, ocr_version,
                   ocr_confidence, ocr_output_raw, ocr_timestamp
            FROM ocr_runs WHERE source_page_id = ?
            ORDER BY ocr_timestamp DESC LIMIT 1
            """,
            (page["source_page_id"],),
        ).fetchone()
        page["ocr"] = dict(ocr) if ocr else None

        texts = conn.execute(
            """
            SELECT t.*,
                   b.arabic_title AS book_title,
                   c.arabic_title AS chapter_title
            FROM text_units t
            LEFT JOIN books b ON b.book_id = t.book_id
            LEFT JOIN chapters c ON c.chapter_id = t.chapter_id
            WHERE t.source_page_id = ?
            ORDER BY t.text_order, t.created_at
            """,
            (page["source_page_id"],),
        ).fetchall()
        text_list = []
        for t in texts:
            td = dict(t)
            pages = conn.execute(
                """
                SELECT tsp.page_role, tsp.sequence_order,
                       sp.pdf_page_number, sp.printed_page_number, sp.source_page_id
                FROM text_unit_source_pages tsp
                JOIN source_pages sp ON sp.source_page_id = tsp.source_page_id
                WHERE tsp.text_id = ?
                ORDER BY tsp.sequence_order
                """,
                (td["text_id"],),
            ).fetchall()
            td["source_pages"] = [dict(p) for p in pages]
            text_list.append(td)
        page["text_units"] = text_list

        notes = conn.execute(
            """
            SELECT * FROM editorial_notes WHERE source_page_id = ?
            """,
            (page["source_page_id"],),
        ).fetchall()
        page["editorial_notes"] = [dict(n) for n in notes]

        books = conn.execute(
            "SELECT book_id, book_order, arabic_title, verification_status FROM books WHERE edition_id = 'ed-bashshar-1997' ORDER BY book_order"
        ).fetchall()
        page["all_books"] = [dict(b) for b in books]

        chapters = conn.execute(
            """
            SELECT chapter_id, book_id, chapter_order, arabic_title, verification_status
            FROM chapters WHERE edition_id = 'ed-bashshar-1997' ORDER BY chapter_order
            """
        ).fetchall()
        page["all_chapters"] = [dict(c) for c in chapters]

        return page


def set_verified_text(
    text_id: str,
    verified_arabic: str,
    user_id: str = "reviewer",
    reason: str = "Human verification against scan",
) -> dict:
    if not verified_arabic or not verified_arabic.strip():
        raise ValueError("verified_arabic cannot be empty — use reject instead")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM text_units WHERE text_id = ?", (text_id,)).fetchone()
        if not row:
            raise ValueError("text_unit not found")
        old_status = row["verification_status"]
        old = {
            "arabic_text_verified": row["arabic_text_verified"],
            "verification_status": old_status,
            "published": row["published"],
        }
        # Changing verified text after approval/publish invalidates approval
        revoked = False
        if old_status in ("approved", "published") or row["published"]:
            revoked = True
            audit(
                conn, "text_unit", text_id, "revoke_approval_on_edit",
                old_value=old,
                new_value={"reason": "verified text changed after approval"},
                user_id=user_id,
                reason="Verified text edited after approval — requires re-approval",
            )
        conn.execute(
            """
            UPDATE text_units
            SET arabic_text_verified = ?,
                verification_status = 'verified',
                published = 0,
                updated_at = ?
            WHERE text_id = ?
            """,
            (verified_arabic.strip(), utcnow(), text_id),
        )
        audit(
            conn, "text_unit", text_id, "set_verified_text",
            old_value=old,
            new_value={
                "arabic_text_verified_len": len(verified_arabic.strip()),
                "status": "verified",
                "approval_revoked": revoked,
            },
            user_id=user_id, reason=reason,
        )
        return {"text_id": text_id, "status": "verified", "approval_revoked": revoked}


def approve_text_unit(
    text_id: str,
    user_id: str = "reviewer",
    reason: str = "",
) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM text_units WHERE text_id = ?", (text_id,)).fetchone()
        if not row:
            raise ValueError("text_unit not found")
        if not row["arabic_text_verified"]:
            raise ValueError("Cannot approve: arabic_text_verified is empty")
        if not row["source_page_id"]:
            # check multi-page links
            n = conn.execute(
                "SELECT COUNT(*) FROM text_unit_source_pages WHERE text_id = ?", (text_id,)
            ).fetchone()[0]
            if n == 0:
                raise ValueError("Cannot approve: no source page provenance")
        old = row["verification_status"]
        if old not in ("verified", "approved"):
            raise ValueError(f"Cannot approve from status {old}; set verified text first")
        conn.execute(
            "UPDATE text_units SET verification_status = 'approved', updated_at = ? WHERE text_id = ?",
            (utcnow(), text_id),
        )
        audit(conn, "text_unit", text_id, "approve",
              old_value={"status": old}, new_value={"status": "approved"},
              user_id=user_id, reason=reason or "Human approval")
        return {"text_id": text_id, "status": "approved", "published": 0}


def reject_text_unit(
    text_id: str,
    reason: str,
    user_id: str = "reviewer",
) -> dict:
    if not reason:
        raise ValueError("Rejection requires a reason")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM text_units WHERE text_id = ?", (text_id,)).fetchone()
        if not row:
            raise ValueError("text_unit not found")
        old = row["verification_status"]
        conn.execute(
            "UPDATE text_units SET verification_status = 'rejected', published = 0, updated_at = ?, notes = ? WHERE text_id = ?",
            (utcnow(), f"REJECTED: {reason}", text_id),
        )
        audit(conn, "text_unit", text_id, "reject",
              old_value={"status": old}, new_value={"status": "rejected", "reason": reason},
              user_id=user_id, reason=reason)
        return {"text_id": text_id, "status": "rejected"}


def try_publish(text_id: str, user_id: str = "reviewer") -> dict:
    """Publication gate: approved + verified + provenance + not test."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM text_units WHERE text_id = ?", (text_id,)).fetchone()
        if not row:
            raise ValueError("text_unit not found")
        errors = []
        # is_test hard block
        try:
            if row["is_test"]:
                errors.append("test record cannot be published to production")
        except (KeyError, IndexError):
            pass
        if row["verification_status"] != "approved":
            errors.append(f"status is {row['verification_status']}, need approved")
        if not row["arabic_text_verified"]:
            errors.append("arabic_text_verified is empty")
        if not row["edition_id"]:
            errors.append("missing edition_id")
        if not row["volume_id"]:
            errors.append("missing volume_id")
        n_pages = conn.execute(
            "SELECT COUNT(*) FROM text_unit_source_pages WHERE text_id = ?", (text_id,)
        ).fetchone()[0]
        if n_pages == 0 and not row["source_page_id"]:
            errors.append("no source page provenance")
        if errors:
            return {"ok": False, "published": 0, "errors": errors}
        conn.execute(
            "UPDATE text_units SET published = 1, verification_status = 'published', updated_at = ? WHERE text_id = ?",
            (utcnow(), text_id),
        )
        audit(conn, "text_unit", text_id, "publish",
              new_value={"published": 1}, user_id=user_id, reason="Publication gate passed")
        return {"ok": True, "published": 1, "text_id": text_id}


def update_text_meta(
    text_id: str,
    text_type: Optional[str] = None,
    book_id: Optional[str] = None,
    chapter_id: Optional[str] = None,
    hadith_number: Optional[str] = None,
    clear_hadith_number: bool = False,
    user_id: str = "reviewer",
    reason: str = "",
) -> dict:
    allowed_types = {"hadith", "athar", "qawl_malik", "other", "needs_review", "heading"}
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM text_units WHERE text_id = ?", (text_id,)).fetchone()
        if not row:
            raise ValueError("not found")
        old = dict(row)
        updates = []
        params: list[Any] = []
        if text_type is not None:
            if text_type not in allowed_types:
                raise ValueError(f"invalid text_type: {text_type}")
            updates.append("text_type = ?")
            params.append(text_type)
        if book_id is not None:
            updates.append("book_id = ?")
            params.append(book_id if book_id else None)
        if chapter_id is not None:
            updates.append("chapter_id = ?")
            params.append(chapter_id if chapter_id else None)
        if clear_hadith_number:
            updates.append("hadith_number = NULL")
        elif hadith_number is not None:
            updates.append("hadith_number = ?")
            params.append(hadith_number if hadith_number else None)
        if not updates:
            return {"text_id": text_id, "changed": False}
        updates.append("updated_at = ?")
        params.append(utcnow())
        params.append(text_id)
        conn.execute(
            f"UPDATE text_units SET {', '.join(updates)} WHERE text_id = ?",
            params,
        )
        audit(conn, "text_unit", text_id, "update_meta",
              old_value={"text_type": old["text_type"], "book_id": old["book_id"],
                         "chapter_id": old["chapter_id"], "hadith_number": old["hadith_number"]},
              new_value={"text_type": text_type, "book_id": book_id,
                         "chapter_id": chapter_id, "hadith_number": hadith_number},
              user_id=user_id, reason=reason)
        return {"text_id": text_id, "changed": True}


def create_book(
    arabic_title: str,
    volume_number: int,
    book_order: int,
    start_pdf_page: Optional[int] = None,
    user_id: str = "reviewer",
) -> str:
    """Reviewer creates book title EXACTLY as on scan."""
    if not arabic_title.strip():
        raise ValueError("title required from scan")
    with get_conn() as conn:
        vol = conn.execute(
            "SELECT volume_id FROM volumes WHERE volume_number = ?", (volume_number,)
        ).fetchone()
        if not vol:
            raise ValueError("volume not found")
        start_page_id = None
        printed = None
        if start_pdf_page:
            sp = conn.execute(
                """
                SELECT sp.source_page_id, sp.printed_page_number
                FROM source_pages sp JOIN volumes v ON v.volume_id = sp.volume_id
                WHERE v.volume_number = ? AND sp.pdf_page_number = ?
                """,
                (volume_number, start_pdf_page),
            ).fetchone()
            if sp:
                start_page_id = sp["source_page_id"]
                printed = sp["printed_page_number"]
        book_id = new_id("bk-")
        conn.execute(
            """
            INSERT INTO books (
                book_id, edition_id, volume_id, book_order, arabic_title,
                title_status, start_source_page_id, start_printed_page,
                verification_status, notes
            ) VALUES (?, 'ed-bashshar-1997', ?, ?, ?, 'verified', ?, ?, 'verified', ?)
            """,
            (book_id, vol["volume_id"], book_order, arabic_title.strip(),
             start_page_id, printed, f"Created by reviewer {user_id} from scan"),
        )
        audit(conn, "book", book_id, "create",
              new_value={"title": arabic_title.strip(), "order": book_order},
              user_id=user_id, reason="Reviewer-created from scan")
        return book_id


def create_chapter(
    book_id: str,
    arabic_title: str,
    chapter_order: int,
    start_pdf_page: Optional[int] = None,
    volume_number: int = 1,
    user_id: str = "reviewer",
) -> str:
    if not arabic_title.strip():
        raise ValueError("title required from scan")
    with get_conn() as conn:
        book = conn.execute("SELECT * FROM books WHERE book_id = ?", (book_id,)).fetchone()
        if not book:
            raise ValueError("book not found")
        start_page_id = None
        printed = None
        if start_pdf_page:
            sp = conn.execute(
                """
                SELECT sp.source_page_id, sp.printed_page_number
                FROM source_pages sp JOIN volumes v ON v.volume_id = sp.volume_id
                WHERE v.volume_number = ? AND sp.pdf_page_number = ?
                """,
                (volume_number, start_pdf_page),
            ).fetchone()
            if sp:
                start_page_id = sp["source_page_id"]
                printed = sp["printed_page_number"]
        chapter_id = new_id("ch-")
        conn.execute(
            """
            INSERT INTO chapters (
                chapter_id, book_id, edition_id, chapter_order, arabic_title,
                title_status, start_source_page_id, start_printed_page,
                verification_status, notes
            ) VALUES (?, ?, 'ed-bashshar-1997', ?, ?, 'verified', ?, ?, 'verified', ?)
            """,
            (chapter_id, book_id, chapter_order, arabic_title.strip(),
             start_page_id, printed, f"Created by reviewer {user_id} from scan"),
        )
        audit(conn, "chapter", chapter_id, "create",
              new_value={"title": arabic_title.strip(), "order": chapter_order},
              user_id=user_id, reason="Reviewer-created from scan")
        return chapter_id


def link_text_to_pages(
    text_id: str,
    page_links: list[dict],
    user_id: str = "reviewer",
) -> dict:
    """
    page_links: [{volume, pdf_page, page_role, sequence_order}, ...]
    """
    with get_conn() as conn:
        row = conn.execute("SELECT text_id FROM text_units WHERE text_id = ?", (text_id,)).fetchone()
        if not row:
            raise ValueError("text not found")
        conn.execute("DELETE FROM text_unit_source_pages WHERE text_id = ?", (text_id,))
        for i, link in enumerate(page_links):
            sp = conn.execute(
                """
                SELECT sp.source_page_id FROM source_pages sp
                JOIN volumes v ON v.volume_id = sp.volume_id
                WHERE v.volume_number = ? AND sp.pdf_page_number = ?
                """,
                (link["volume"], link["pdf_page"]),
            ).fetchone()
            if not sp:
                raise ValueError(f"page not found: vol{link['volume']} p{link['pdf_page']}")
            conn.execute(
                """
                INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    new_id("tsp-"),
                    text_id,
                    sp["source_page_id"],
                    link.get("page_role", "only"),
                    link.get("sequence_order", i + 1),
                ),
            )
            # also set primary source_page_id to first
            if i == 0:
                conn.execute(
                    "UPDATE text_units SET source_page_id = ?, pdf_page = ?, updated_at = ? WHERE text_id = ?",
                    (sp["source_page_id"], link["pdf_page"], utcnow(), text_id),
                )
        audit(conn, "text_unit", text_id, "link_pages",
              new_value=page_links, user_id=user_id, reason="Multi-page provenance")
        return {"text_id": text_id, "pages": len(page_links)}


def create_text_unit_manual(
    volume: int,
    pdf_page: int,
    verified_arabic: str,
    text_type: str = "needs_review",
    hadith_number: Optional[str] = None,
    book_id: Optional[str] = None,
    chapter_id: Optional[str] = None,
    user_id: str = "reviewer",
) -> str:
    """Create a text unit directly from human reading of the scan (no OCR required)."""
    if not verified_arabic.strip():
        raise ValueError("verified text required")
    with get_conn() as conn:
        page = conn.execute(
            """
            SELECT sp.*, v.volume_id FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number = ? AND sp.pdf_page_number = ?
            """,
            (volume, pdf_page),
        ).fetchone()
        if not page:
            raise ValueError("page not registered")
        text_id = new_id("txt-")
        conn.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, book_id, chapter_id, source_page_id,
                text_order, hadith_number, text_type,
                arabic_text_raw, arabic_text_verified,
                pdf_page, printed_page, verification_status, published, notes
            ) VALUES (?, 'ed-bashshar-1997', ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?, ?, 'verified', 0, ?)
            """,
            (
                text_id, page["volume_id"], book_id, chapter_id, page["source_page_id"],
                hadith_number, text_type,
                verified_arabic.strip(),
                page["pdf_page_number"], page["printed_page_number"],
                f"Created by {user_id} directly from scan",
            ),
        )
        conn.execute(
            """
            INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order)
            VALUES (?, ?, ?, 'only', 1)
            """,
            (new_id("tsp-"), text_id, page["source_page_id"]),
        )
        audit(conn, "text_unit", text_id, "create_from_scan",
              new_value={"pdf_page": pdf_page, "type": text_type},
              user_id=user_id, reason="Human transcription from scan")
        return text_id


def mark_needs_review(text_id: str, reason: str, user_id: str = "reviewer") -> dict:
    with get_conn() as conn:
        conn.execute(
            "UPDATE text_units SET verification_status = 'needs_review', published = 0, updated_at = ?, notes = ? WHERE text_id = ?",
            (utcnow(), reason, text_id),
        )
        audit(conn, "text_unit", text_id, "mark_needs_review",
              new_value={"reason": reason}, user_id=user_id, reason=reason)
        return {"text_id": text_id, "status": "needs_review"}
