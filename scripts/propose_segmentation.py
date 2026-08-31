#!/usr/bin/env python3
"""Dry-run segmentation proposals. Use --write to persist proposals only."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DB_PATH
from app.proposals import detect_proposals, summarize_hits, create_proposals_for_ocr_run, select_ocr_run


def _readonly():
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        raise SystemExit(f"No runtime DB at {DB_PATH}. Not creating one.")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    p = argparse.ArgumentParser(description="Segmentation proposals (default: dry-run, no writes)")
    p.add_argument("--write", action="store_true", help="persist proposals as needs_review only")
    p.add_argument("--volume", type=int, default=None)
    p.add_argument("--page", type=int, default=None)
    p.add_argument("--ocr-run-id", default=None, help="required if a page has multiple OCR runs and you do not want the original run")
    p.add_argument("--ocr-role", choices=["original", "review"], default="original")
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args()

    conn = _readonly()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ocr_runs" not in tables:
            raise SystemExit("ocr_runs missing; aborting without writes")
        q = """
            SELECT DISTINCT v.volume_number, sp.pdf_page_number, sp.source_page_id
            FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            JOIN ocr_runs o ON o.source_page_id = sp.source_page_id
            WHERE 1=1
        """
        params: list = []
        if args.volume is not None:
            q += " AND v.volume_number = ?"
            params.append(args.volume)
        if args.page is not None:
            q += " AND sp.pdf_page_number = ?"
            params.append(args.page)
        q += " ORDER BY v.volume_number, sp.pdf_page_number LIMIT ?"
        params.append(args.limit)
        pages = list(conn.execute(q, params))
    finally:
        conn.close()

    totals = Counter()
    pages_ambiguous = 0
    print(f"pages scanned: {len(pages)} write={args.write} role={args.ocr_role}")
    for page in pages:
        run = select_ocr_run(page["source_page_id"], ocr_run_id=args.ocr_run_id, role=args.ocr_role)
        raw = run.get("ocr_output_raw") or ""
        hits = detect_proposals(raw)
        s = summarize_hits(hits)
        for k, v in s["by_type"].items():
            totals[k] += v
        if s["unknown"] or s["total"] == 0:
            pages_ambiguous += 1
        if args.write:
            create_proposals_for_ocr_run(run["ocr_run_id"], write=True)
        print(
            f"  vol{page['volume_number']} p{page['pdf_page_number']} run={run['ocr_run_id']}: "
            f"hits={s['total']} headings={s['headings']} editorial={s['editorial']} unknown={s['unknown']}"
        )
    print("TOTALS", dict(totals))
    print("ambiguous_pages", pages_ambiguous)
    print("No canonical units created. No approvals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
