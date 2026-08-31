"""Read-only book view. Public default: verified+ only. Preview marks unverified."""
from __future__ import annotations

from typing import Optional

from .db import get_conn


PUBLIC_STATUSES = ("verified", "approved", "published")


def reader_tree(*, preview: bool = False, volume: Optional[int] = None) -> dict:
    with get_conn(readonly=True) as conn:
        books = [dict(r) for r in conn.execute(
            """SELECT book_id, book_order, arabic_title, verification_status
               FROM books WHERE edition_id='ed-bashshar-1997' ORDER BY book_order"""
        ).fetchall()]
        chapters = [dict(r) for r in conn.execute(
            """SELECT chapter_id, book_id, chapter_order, arabic_title, verification_status
               FROM chapters ORDER BY chapter_order"""
        ).fetchall()]
        q = """
            SELECT t.text_id, t.book_id, t.chapter_id, t.text_type, t.hadith_number,
                   t.arabic_text_verified, t.arabic_text_raw, t.verification_status,
                   t.published, t.pdf_page, t.printed_page, t.volume_id
            FROM text_units t
            WHERE coalesce(t.is_test,0)=0
        """
        if not preview:
            q += " AND t.verification_status IN ('verified','approved','published') AND t.text_type NOT IN ('editorial')"
        q += " ORDER BY t.pdf_page, t.text_order"
        units = [dict(r) for r in conn.execute(q).fetchall()]
        if volume is not None:
            vol_id = f"vol-ed-bashshar-1997-v{volume}"
            units = [u for u in units if u.get("volume_id") == vol_id]
        for u in units:
            if not preview:
                u.pop("arabic_text_raw", None)
            u["visible_arabic"] = u.get("arabic_text_verified") if u.get("verification_status") in PUBLIC_STATUSES else (
                u.get("arabic_text_raw") if preview else None
            )
            u["badge"] = None if u.get("verification_status") in PUBLIC_STATUSES else "UNVERIFIED / NEEDS REVIEW"
            if u.get("text_type") in ("editorial",):
                u["badge"] = (u.get("badge") or "") + " EDITORIAL APPARATUS"
        return {
            "edition": "ed-bashshar-1997",
            "preview": preview,
            "public_rule": "Default reader shows verified/approved/published only.",
            "books": books,
            "chapters": chapters,
            "units": units,
        }


def pilot_status(volume: int = 1, pages: range = range(33, 43)) -> dict:
    pages = list(pages)
    with get_conn(readonly=True) as conn:
        ph = ",".join("?" * len(pages))
        page_rows = list(conn.execute(
            f"""SELECT sp.pdf_page_number, sp.source_page_id, sp.blank_status, sp.verification_status
                FROM source_pages sp JOIN volumes v ON v.volume_id=sp.volume_id
                WHERE v.volume_number=? AND sp.pdf_page_number IN ({ph})
                ORDER BY sp.pdf_page_number""",
            [volume, *pages],
        ))
        props = {"total": 0, "accepted": 0, "rejected": 0, "needs_review": 0}
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='segmentation_proposals'").fetchone():
            row = conn.execute(
                f"""SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN proposal_status='accepted' THEN 1 ELSE 0 END) AS accepted,
                    SUM(CASE WHEN proposal_status='rejected' THEN 1 ELSE 0 END) AS rejected,
                    SUM(CASE WHEN proposal_status='needs_review' THEN 1 ELSE 0 END) AS needs_review
                    FROM segmentation_proposals p
                    JOIN source_pages sp ON sp.source_page_id=p.source_page_id
                    JOIN volumes v ON v.volume_id=sp.volume_id
                    WHERE v.volume_number=? AND sp.pdf_page_number IN ({ph})""",
                [volume, *pages],
            ).fetchone()
            props = {k: int(row[k] or 0) for k in ("total", "accepted", "rejected", "needs_review")}
        units = conn.execute(
            f"""SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN t.verification_status='needs_review' THEN 1 ELSE 0 END) AS needs_review,
                SUM(CASE WHEN t.verification_status='verified' THEN 1 ELSE 0 END) AS verified,
                SUM(CASE WHEN t.published=1 THEN 1 ELSE 0 END) AS published
                FROM text_units t
                JOIN volumes v ON v.volume_id=t.volume_id
                WHERE coalesce(t.is_test,0)=0 AND v.volume_number=? AND t.pdf_page IN ({ph})""",
            [volume, *pages],
        ).fetchone()
        return {
            "volume": volume,
            "pages": pages,
            "page_count": len(page_rows),
            "proposals": props,
            "unresolved": props.get("needs_review", 0),
            "materialized": int(units["total"] or 0),
            "needs_text_review": int(units["needs_review"] or 0),
            "verified": int(units["verified"] or 0),
            "visible_in_reader": int(units["verified"] or 0),
            "published": int(units["published"] or 0),
        }
