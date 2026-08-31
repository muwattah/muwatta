#!/usr/bin/env python3
"""Dry-run segmentation proposals. Use --write to persist proposals only (never canonical)."""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn, init_db
from app.proposals import create_proposals_for_ocr_run, detect_proposals, ensure_proposal_schema, summarize_hits


def main() -> int:
    p = argparse.ArgumentParser(description="Segmentation proposals (default: dry-run)")
    p.add_argument("--write", action="store_true", help="persist proposals as needs_review")
    p.add_argument("--volume", type=int, default=None)
    p.add_argument("--page", type=int, default=None)
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args()

    init_db()
    ensure_proposal_schema()
    q = """
        SELECT o.ocr_run_id, v.volume_number, sp.pdf_page_number, o.ocr_output_raw
        FROM ocr_runs o
        JOIN source_pages sp ON sp.source_page_id = o.source_page_id
        JOIN volumes v ON v.volume_id = sp.volume_id
        WHERE 1=1
    """
    params = []
    if args.volume is not None:
        q += " AND v.volume_number = ?"
        params.append(args.volume)
    if args.page is not None:
        q += " AND sp.pdf_page_number = ?"
        params.append(args.page)
    q += " ORDER BY v.volume_number, sp.pdf_page_number LIMIT ?"
    params.append(args.limit)

    totals = Counter()
    pages_ambiguous = 0
    with get_conn(readonly=True) as conn:
        rows = list(conn.execute(q, params))
    print(f"pages/runs scanned: {len(rows)}  write={args.write}")
    for row in rows:
        raw = row["ocr_output_raw"] or ""
        hits = detect_proposals(raw)
        s = summarize_hits(hits)
        for k, v in s["by_type"].items():
            totals[k] += v
        if s["unknown"] or s["total"] == 0:
            pages_ambiguous += 1
        if args.write:
            create_proposals_for_ocr_run(row["ocr_run_id"], write=True)
        print(
            f"  vol{row['volume_number']} p{row['pdf_page_number']}: "
            f"hits={s['total']} headings={s['headings']} editorial={s['editorial']} unknown={s['unknown']}"
        )
    print("TOTALS", dict(totals))
    print("ambiguous_pages", pages_ambiguous)
    print("No canonical units created. No approvals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
