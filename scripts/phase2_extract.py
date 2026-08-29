#!/usr/bin/env python3
"""
Fase 2: OCR + structure proposals for canonical text pages.
Default: Volume 1, pages 33–42 (start of Al-Muwaṭṭaʾ text).
Everything remains needs_review. published = 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn
from app.ocr_runner import ocr_page
from app.segmenter import propose_structure_from_ocr, apply_proposals_as_needs_review


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--volume", type=int, default=1)
    ap.add_argument("--start", type=int, default=33)
    ap.add_argument("--end", type=int, default=42)
    ap.add_argument("--dpi", type=int, default=120)
    ap.add_argument("--apply", action="store_true", help="Write proposals to DB as needs_review")
    args = ap.parse_args()

    print(f"=== Phase 2 OCR+segment vol{args.volume} pages {args.start}-{args.end} ===\n")
    results = []
    for pdf_page in range(args.start, args.end + 1):
        print(f"--- PDF page {pdf_page} ---")
        try:
            ocr = ocr_page(args.volume, pdf_page, dpi=args.dpi)
        except Exception as e:
            print(f"  OCR failed: {e}")
            results.append({"pdf_page": pdf_page, "error": str(e)})
            continue

        print(f"  confidence={ocr.get('confidence')} chars={ocr.get('char_count')}")
        preview = (ocr.get("preview") or "")[:200].replace("\n", " | ")
        print(f"  preview: {preview}")

        # Get full OCR text from DB
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT ocr_output_raw FROM ocr_runs
                WHERE ocr_run_id = ?
                """,
                (ocr["ocr_run_id"],),
            ).fetchone()
            raw = row["ocr_output_raw"] if row else ""

        proposals = propose_structure_from_ocr(raw, ocr["source_page_id"])
        print(
            f"  proposals: books={len(proposals['books'])} "
            f"chapters={len(proposals['chapters'])} "
            f"texts={len(proposals['text_candidates'])} "
            f"flags={proposals['flags']} "
            f"seg_conf={proposals['segmentation_confidence']}"
        )
        for b in proposals["books"]:
            print(f"    KITAB?: {b.get('raw_title', '')[:80]}")
        for c in proposals["chapters"]:
            print(f"    BAB?: {c.get('raw_title', '')[:80]}")
        for t in proposals["text_candidates"][:5]:
            print(
                f"    TEXT? num={t.get('edition_hadith_number')} "
                f"preview={t.get('body_preview', '')[:60].replace(chr(10), ' ')}"
            )

        applied = None
        if args.apply:
            applied = apply_proposals_as_needs_review(
                args.volume, pdf_page, proposals, raw
            )
            print(f"  applied (needs_review): {applied}")

        results.append({
            "pdf_page": pdf_page,
            "ocr": {
                "ocr_run_id": ocr["ocr_run_id"],
                "confidence": ocr.get("confidence"),
                "chars": ocr.get("char_count"),
            },
            "proposals": {
                "books": len(proposals["books"]),
                "chapters": len(proposals["chapters"]),
                "texts": len(proposals["text_candidates"]),
                "flags": proposals["flags"],
                "seg_conf": proposals["segmentation_confidence"],
            },
            "applied": applied,
        })

    print("\n=== Phase 2 report ===")
    with get_conn() as conn:
        stats = {
            "ocr_runs": conn.execute("SELECT COUNT(*) FROM ocr_runs").fetchone()[0],
            "text_units": conn.execute("SELECT COUNT(*) FROM text_units").fetchone()[0],
            "books": conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            "chapters": conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
            "editorial_notes": conn.execute("SELECT COUNT(*) FROM editorial_notes").fetchone()[0],
            "pages_extracted": conn.execute(
                "SELECT COUNT(*) FROM source_pages WHERE ocr_status IN ('done','needs_review')"
            ).fetchone()[0],
            "low_conf_pages": conn.execute(
                "SELECT COUNT(*) FROM ocr_runs WHERE ocr_confidence IS NOT NULL AND ocr_confidence < 40"
            ).fetchone()[0],
            "text_needs_review": conn.execute(
                "SELECT COUNT(*) FROM text_units WHERE verification_status = 'needs_review'"
            ).fetchone()[0],
            "text_approved": conn.execute(
                "SELECT COUNT(*) FROM text_units WHERE verification_status = 'approved'"
            ).fetchone()[0],
            "text_published": conn.execute(
                "SELECT COUNT(*) FROM text_units WHERE published = 1"
            ).fetchone()[0],
            "open_tasks": conn.execute(
                "SELECT COUNT(*) FROM verification_tasks WHERE status = 'open'"
            ).fetchone()[0],
        }
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print("\npublished remains 0 (no auto-publish).")
    print("All new text units are needs_review.")


if __name__ == "__main__":
    main()
