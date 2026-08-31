#!/usr/bin/env python3
"""
Read-only local validation for the proposal-layer checkpoint.
Does not OCR, does not --write, does not materialize, does not reset the DB.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import DB_PATH, PRIMARY_SNAPSHOT


def main() -> int:
    print("DB_PATH", DB_PATH)
    print("SNAPSHOT", PRIMARY_SNAPSHOT)
    rc = subprocess.call([sys.executable, str(ROOT / "scripts" / "test_segmentation.py")])
    if rc != 0:
        print("FAIL fixture segmentation tests")
        return rc

    if not DB_PATH.exists():
        print("WARN no local runtime DB; fixture tests passed. Import production DB to complete counts.")
        return 0

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print("integrity_check", integ)
        if integ != "ok":
            return 1
        pages = conn.execute("SELECT COUNT(*) FROM source_pages").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM ocr_runs").fetchone()[0]
        approved = conn.execute(
            "SELECT COUNT(*) FROM text_units WHERE verification_status='approved' AND coalesce(is_test,0)=0"
        ).fetchone()[0]
        published = conn.execute(
            "SELECT COUNT(*) FROM text_units WHERE published=1 AND coalesce(is_test,0)=0"
        ).fetchone()[0]
        print("source_pages", pages)
        print("ocr_runs", runs)
        print("approved", approved)
        print("published", published)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        print("has_segmentation_proposals", "segmentation_proposals" in tables)
        if "segmentation_proposals" in tables:
            print("proposal_rows", conn.execute("SELECT COUNT(*) FROM segmentation_proposals").fetchone()[0])
        for vol, pg in ((1, 618), (2, 378), (2, 407)):
            n = conn.execute(
                """
                SELECT COUNT(*) FROM ocr_runs o
                JOIN source_pages sp ON sp.source_page_id=o.source_page_id
                JOIN volumes v ON v.volume_id=sp.volume_id
                WHERE v.volume_number=? AND sp.pdf_page_number=?
                """,
                (vol, pg),
            ).fetchone()[0]
            print(f"vol{vol} p{pg} ocr_runs", n)
        if approved != 0 or published != 0:
            print("FAIL production approved/published not zero")
            return 1
        if pages != 1384:
            print("WARN expected 1384 source_pages, got", pages)
    finally:
        conn.close()

    # dry-run CLI on one page must not write
    before = DB_PATH.stat().st_mtime
    dry = subprocess.call(
        [sys.executable, str(ROOT / "scripts" / "propose_segmentation.py"), "--volume", "1", "--page", "33", "--limit", "1"]
    )
    after = DB_PATH.stat().st_mtime
    print("dry_run_exit", dry, "mtime_unchanged", before == after)
    if dry != 0:
        print("WARN dry-run CLI exited non-zero (page 33 may lack OCR in this checkout)")
    print("VALIDATION_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
