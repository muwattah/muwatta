#!/usr/bin/env python3
"""Read-only local validation for the review workstation. No bulk write, no OCR, no publish."""
from __future__ import annotations
import sqlite3, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import DB_PATH

def run(script):
    return subprocess.call([sys.executable, str(ROOT / "scripts" / script)])

def main():
    print("DB_PATH", DB_PATH)
    for s in ("test_segmentation.py", "test_review_workstation.py"):
        rc = run(s)
        print(s, "exit", rc)
        if rc != 0:
            return rc
    html = ROOT / "admin_static" / "review_workstation.html"
    if not html.exists() or "Machine confidence" not in html.read_text(encoding="utf-8"):
        print("FAIL workstation html missing labels")
        return 1
    print("ui_static_ok")
    if DB_PATH.exists() and DB_PATH.stat().st_size:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print("integrity_check", integ)
        if integ != "ok":
            return 1
        print("source_pages", conn.execute("SELECT COUNT(*) FROM source_pages").fetchone()[0])
        print("ocr_runs", conn.execute("SELECT COUNT(*) FROM ocr_runs").fetchone()[0])
        print("approved", conn.execute("SELECT COUNT(*) FROM text_units WHERE verification_status='approved' AND coalesce(is_test,0)=0").fetchone()[0])
        print("published", conn.execute("SELECT COUNT(*) FROM text_units WHERE published=1 AND coalesce(is_test,0)=0").fetchone()[0])
        conn.close()
        dry = subprocess.call([sys.executable, str(ROOT/"scripts"/"propose_segmentation.py"), "--pilot", "--volume","1","--pages","33-42"])
        print("pilot_dry_exit", dry)
    print("VALIDATION_OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
