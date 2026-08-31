"""
Structureel voorstel uit OCR — ALLES needs_review.

Herkent alleen patronen die in de tekst zichtbaar zijn.
Geen structuur uit andere edities of geheugen.
"""
from __future__ import annotations

import re
from typing import Optional

from .db import get_conn, audit, new_id, utcnow


# Patterns observed in classical Muwatta editions (generic markers only)
# We only FLAG; we do not invent titles from memory.
# OCR-tolerant patterns (scan noise, RTL marks, parentheses)
RE_KITAB = re.compile(
    r"(?:كتاب|كـتاب|کتاب)\s*\)?\s*([\u0600-\u06FF\s]{2,60})",
    re.MULTILINE,
)
RE_BAB = re.compile(
    r"(?:باب|بـاب)\s*\)?\s*([\u0600-\u06FF\s]{2,60})",
    re.MULTILINE,
)
# Numbered lines: -١ or ١- or )١( etc.
RE_NUMBERED = re.compile(
    r"(?:^|\n)\s*[-–—.)(\s]*([٠-٩0-9]{1,4})[-–—.)(\s]*\s*(.{20,}?)(?=(?:\n\s*[-–—.)(\s]*[٠-٩0-9]{1,4}[-–—.)(\s]|\Z))",
    re.MULTILINE | re.DOTALL,
)
RE_FOOTNOTE_MARK = re.compile(r"[⁽\(]\s*[٠-٩0-9]+\s*[⁾\)]")


def _ar_to_int(s: str) -> Optional[int]:
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    s2 = s.translate(trans).strip()
    try:
        return int(s2)
    except ValueError:
        return None


def propose_structure_from_ocr(ocr_text: str, source_page_id: str) -> dict:
    """Compatibility wrapper. Prefer app.proposals.detect_proposals."""
    from .proposals import detect_proposals, summarize_hits
    hits = detect_proposals(ocr_text or "")
    return {
        "source_page_id": source_page_id,
        "hits": hits,
        "summary": summarize_hits(hits),
        "segmentation_status": "needs_review",
        "flags": ["proposal_only", "not_canonical"],
    }


def propose_structure_from_ocr_legacy(ocr_text: str, source_page_id: str) -> dict:
    """
    Produce proposed segments from one page OCR.
    Everything is marked needs_review.
    """
    proposals = {
        "source_page_id": source_page_id,
        "books": [],
        "chapters": [],
        "text_candidates": [],
        "possible_editorial": False,
        "segmentation_status": "needs_review",
        "segmentation_confidence": 0.0,
        "flags": [],
    }
    if not ocr_text or not ocr_text.strip():
        proposals["flags"].append("empty_ocr")
        return proposals

    # Kitāb headings
    for m in RE_KITAB.finditer(ocr_text):
        proposals["books"].append({
            "raw_title": m.group(0).strip()[:120],
            "captured": (m.group(1) or "").strip()[:100],
            "status": "needs_review",
        })

    # Bāb headings
    for m in RE_BAB.finditer(ocr_text):
        proposals["chapters"].append({
            "raw_title": m.group(0).strip()[:120],
            "captured": (m.group(1) or "").strip()[:100],
            "status": "needs_review",
        })

    # Numbered text candidates
    for m in RE_NUMBERED.finditer(ocr_text):
        num_raw = m.group(1)
        body = (m.group(2) or "").strip()
        if len(body) < 15:
            continue
        num = _ar_to_int(num_raw)
        text_type = "needs_review"
        # Only light heuristics — still needs_review
        body_start = body[:40]
        if "قال" in body_start or "حدثني" in body_start or "حدثنا" in body_start:
            text_type = "needs_review"  # do not auto-classify as hadith
        proposals["text_candidates"].append({
            "edition_hadith_number": str(num) if num is not None else None,
            "number_raw": num_raw,
            "body_preview": body[:200],
            "body_full": body,
            "text_type": text_type,
            "status": "needs_review",
        })

    if RE_FOOTNOTE_MARK.search(ocr_text) or "انظر" in ocr_text[:500]:
        proposals["possible_editorial"] = True
        proposals["flags"].append("possible_editorial_content")

    # Confidence: very conservative
    conf = 0.2
    if proposals["books"] or proposals["chapters"]:
        conf += 0.15
    if proposals["text_candidates"]:
        conf += 0.15
    if proposals["possible_editorial"]:
        conf -= 0.05
    proposals["segmentation_confidence"] = max(0.0, min(0.6, conf))  # never high

    if not proposals["text_candidates"] and not proposals["books"] and not proposals["chapters"]:
        proposals["flags"].append("uncertain_segmentation")

    return proposals


def apply_proposals_as_needs_review(
    volume_number: int,
    pdf_page: int,
    proposals: dict,
    ocr_raw: str,
) -> dict:
    """
    Create books/chapters/text_units from proposals.
    ALL verification_status = needs_review.
    arabic_text_verified = NULL.
    published = 0.
    """
    created = {"books": 0, "chapters": 0, "text_units": 0, "editorial": 0}

    with get_conn() as conn:
        page = conn.execute(
            """
            SELECT sp.*, v.volume_id, v.volume_number
            FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number = ? AND sp.pdf_page_number = ?
            """,
            (volume_number, pdf_page),
        ).fetchone()
        if not page:
            raise ValueError("Page not found")

        source_page_id = page["source_page_id"]
        edition_id = page["edition_id"]
        volume_id = page["volume_id"]

        # Optional: create book stubs
        for b in proposals.get("books", []):
            book_id = new_id("bk-")
            # book_order unknown — use temp high number; reviewer will fix
            order_row = conn.execute(
                "SELECT COALESCE(MAX(book_order), 0) + 1 FROM books WHERE edition_id = ?",
                (edition_id,),
            ).fetchone()
            order = order_row[0]
            conn.execute(
                """
                INSERT INTO books (
                    book_id, edition_id, volume_id, book_order,
                    arabic_title, title_status, start_source_page_id,
                    start_printed_page, verification_status, notes
                ) VALUES (?, ?, ?, ?, ?, 'needs_review', ?, ?, 'needs_review', ?)
                """,
                (
                    book_id, edition_id, volume_id, order,
                    b.get("captured") or b.get("raw_title"),
                    source_page_id,
                    page["printed_page_number"],
                    f"Auto-proposed from OCR; raw={b.get('raw_title','')[:80]}",
                ),
            )
            created["books"] += 1
            audit(conn, "book", book_id, "propose", new_value=b, reason="OCR structure proposal")

        for ch in proposals.get("chapters", []):
            # Attach to latest book on this edition if any; else orphan needs_review
            book = conn.execute(
                "SELECT book_id FROM books WHERE edition_id = ? ORDER BY book_order DESC LIMIT 1",
                (edition_id,),
            ).fetchone()
            if not book:
                continue
            chapter_id = new_id("ch-")
            order_row = conn.execute(
                "SELECT COALESCE(MAX(chapter_order), 0) + 1 FROM chapters WHERE book_id = ?",
                (book["book_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO chapters (
                    chapter_id, book_id, edition_id, chapter_order,
                    arabic_title, title_status, start_source_page_id,
                    start_printed_page, verification_status, notes
                ) VALUES (?, ?, ?, ?, ?, 'needs_review', ?, ?, 'needs_review', ?)
                """,
                (
                    chapter_id, book["book_id"], edition_id, order_row[0],
                    ch.get("captured") or ch.get("raw_title"),
                    source_page_id,
                    page["printed_page_number"],
                    f"Auto-proposed; raw={ch.get('raw_title','')[:80]}",
                ),
            )
            created["chapters"] += 1
            audit(conn, "chapter", chapter_id, "propose", new_value=ch, reason="OCR structure proposal")

        # Text candidates
        book = conn.execute(
            "SELECT book_id FROM books WHERE edition_id = ? ORDER BY book_order DESC LIMIT 1",
            (edition_id,),
        ).fetchone()
        chapter = None
        if book:
            chapter = conn.execute(
                "SELECT chapter_id FROM chapters WHERE book_id = ? ORDER BY chapter_order DESC LIMIT 1",
                (book["book_id"],),
            ).fetchone()

        for i, tc in enumerate(proposals.get("text_candidates", [])):
            text_id = new_id("txt-")
            conn.execute(
                """
                INSERT INTO text_units (
                    text_id, edition_id, volume_id, book_id, chapter_id,
                    source_page_id, text_order, hadith_number, text_type,
                    arabic_text_raw, arabic_text_verified,
                    pdf_page, printed_page, verification_status, published, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'needs_review', ?, NULL, ?, ?, 'needs_review', 0, ?)
                """,
                (
                    text_id, edition_id, volume_id,
                    book["book_id"] if book else None,
                    chapter["chapter_id"] if chapter else None,
                    source_page_id,
                    i + 1,
                    tc.get("edition_hadith_number"),
                    tc.get("body_full") or tc.get("body_preview"),
                    page["pdf_page_number"],
                    page["printed_page_number"],
                    "Auto-segmented from OCR; needs human review",
                ),
            )
            created["text_units"] += 1
            audit(
                conn, "text_unit", text_id, "propose",
                new_value={"number": tc.get("edition_hadith_number"), "preview": (tc.get("body_preview") or "")[:80]},
                reason="OCR segmentation proposal",
            )

        # If segmentation found no text candidates, still store page OCR as one unit
        if not proposals.get("text_candidates") and ocr_raw and ocr_raw.strip():
            text_id = new_id("txt-")
            conn.execute(
                """
                INSERT INTO text_units (
                    text_id, edition_id, volume_id, book_id, chapter_id,
                    source_page_id, text_order, hadith_number, text_type,
                    arabic_text_raw, arabic_text_verified,
                    pdf_page, printed_page, verification_status, published, notes
                ) VALUES (?, ?, ?, ?, ?, ?, 1, NULL, 'needs_review', ?, NULL, ?, ?, 'needs_review', 0, ?)
                """,
                (
                    text_id, edition_id, volume_id,
                    book["book_id"] if book else None,
                    chapter["chapter_id"] if chapter else None,
                    source_page_id,
                    ocr_raw,
                    page["pdf_page_number"],
                    page["printed_page_number"],
                    "Full-page OCR stored; segmentation uncertain — needs human split",
                ),
            )
            created["text_units"] += 1
            audit(conn, "text_unit", text_id, "propose_full_page",
                  reason="Uncertain segmentation; full page OCR as raw")

        if proposals.get("possible_editorial"):
            note_id = new_id("ed-")
            conn.execute(
                """
                INSERT INTO editorial_notes (
                    editorial_note_id, edition_id, source_page_id,
                    note_type, arabic_text_raw, arabic_text_verified,
                    verification_status, notes
                ) VALUES (?, ?, ?, 'needs_review', ?, NULL, 'needs_review', ?)
                """,
                (
                    note_id, edition_id, source_page_id,
                    ocr_raw[:2000] if ocr_raw else None,
                    "Possible editorial/footnote content flagged from OCR",
                ),
            )
            created["editorial"] += 1

        # Queue review reasons
        flags = proposals.get("flags") or []
        if proposals.get("segmentation_confidence", 0) < 0.4:
            flags.append("uncertain_segmentation")
        for reason in flags:
            conn.execute(
                """
                INSERT INTO verification_tasks (
                    task_id, entity_type, entity_id, task_type, priority, status, notes
                ) VALUES (?, 'source_page', ?, ?, 50, 'open', ?)
                """,
                (new_id("task-"), source_page_id, reason, reason),
            )

    return created
