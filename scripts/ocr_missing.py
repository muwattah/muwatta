#!/usr/bin/env python3
"""OCR all missing pages. Never overwrite existing JSON/runs. No text_units."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import init_db, get_conn, persist_snapshot
from app.integrity import verify_source_hashes
from app.ocr_runner import ocr_page
from app.sources import register_source_files, register_all_pages

LOG = ROOT / "storage" / "runtime" / "ocr_missing.log"
PLAN = [
    (1, list(range(1, 33)) + list(range(43, 665))),
    (2, list(range(1, 721))),
]


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    init_db()
    h = verify_source_hashes()
    if not h.get("ok"):
        log(f"STOP source hash mismatch: {h}")
        return 2
    register_source_files()
    register_all_pages()
    stats = {"success": 0, "skipped_existing": 0, "failed": 0}
    failed = []
    done_since_snap = 0
    for vol, pages in PLAN:
        for p in pages:
            try:
                r = ocr_page(vol, p, dpi=120)
                if r.get("skipped_existing"):
                    stats["skipped_existing"] += 1
                    status = "skipped-existing"
                else:
                    stats["success"] += 1
                    done_since_snap += 1
                    status = "success"
                log(f"vol{vol} p{p} {status} run={r.get('ocr_run_id')} chars={r.get('char_count')}")
            except Exception as e:
                stats["failed"] += 1
                failed.append({"volume": vol, "pdf_page": p, "error": str(e)})
                log(f"vol{vol} p{p} failed {e}")
            if done_since_snap >= 25:
                persist_snapshot()
                done_since_snap = 0
    persist_snapshot()
    summary = {"stats": stats, "failed": failed}
    (ROOT / "storage" / "runtime" / "ocr_missing_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"DONE {stats} failed={len(failed)}")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
