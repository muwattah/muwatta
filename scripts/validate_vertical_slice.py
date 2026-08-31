#!/usr/bin/env python3
"""Safe Windows validation. No verify, no canonical writes, no bulk --write."""
from __future__ import annotations
import sqlite3, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import DB_PATH

def main():
    for s in ("test_segmentation.py", "test_review_workstation.py", "test_vertical_slice.py"):
        rc = subprocess.call([sys.executable, str(ROOT / "scripts" / s)])
        print(s, "exit", rc)
        if rc != 0:
            return rc
    for f in ("admin_static/reader.html", "admin_static/pilot_status.html", "admin_static/review_workstation.html"):
        if not (ROOT / f).exists():
            print("FAIL missing", f); return 1
    if DB_PATH.exists() and DB_PATH.stat().st_size:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        print("integrity_check", conn.execute("PRAGMA integrity_check").fetchone()[0])
        print("approved", conn.execute("SELECT COUNT(*) FROM text_units WHERE verification_status='approved' AND coalesce(is_test,0)=0").fetchone()[0])
        print("published", conn.execute("SELECT COUNT(*) FROM text_units WHERE published=1 AND coalesce(is_test,0)=0").fetchone()[0])
        conn.close()
        rc = subprocess.call([sys.executable, str(ROOT/"scripts"/"propose_segmentation.py"), "--pilot", "--volume","1","--pages","33-42"])
        print("pilot_dry_exit", rc)
    print("VALIDATION_OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
