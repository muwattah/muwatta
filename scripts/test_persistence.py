#!/usr/bin/env python3
"""Durable DB + stable source-page identity tests."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import (
    DB_PATH,
    SNAPSHOT_PATH,
    init_db,
    get_conn,
    db_has_schema,
    stable_source_page_id,
    migrate_stable_source_ids,
    persist_snapshot,
)
from app.ocr_runner import import_existing_ocr_json
from app.sources import register_source_files, register_all_pages


def main() -> int:
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}: {name}" + (f" — {detail}" if detail else ""))
        results.append(ok)

    print("=== Persistence ===")
    check("live DB path is explicit", str(DB_PATH).endswith("muwatta_source.db"))
    check("durable snapshot path under storage/runtime", "storage/runtime" in str(SNAPSHOT_PATH))

    first = init_db()
    check("init_db does not wipe existing schema", first in ("exists", "created"))
    second = init_db()
    check("second init_db returns exists", second == "exists")
    check("schema still present", db_has_schema())

    register_source_files()
    register_all_pages()
    pid = stable_source_page_id(1, 33)
    with get_conn() as c:
        row = c.execute(
            """
            SELECT source_page_id FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number=1 AND sp.pdf_page_number=33
            """
        ).fetchone()
        n_pages = c.execute("SELECT COUNT(*) FROM source_pages").fetchone()[0]
        n_ocr_before = c.execute("SELECT COUNT(*) FROM ocr_runs").fetchone()[0]
    check("page 33 uses stable source_page_id", row is not None and row["source_page_id"] == pid, pid)
    n2 = register_all_pages()
    with get_conn() as c:
        n_pages2 = c.execute("SELECT COUNT(*) FROM source_pages").fetchone()[0]
        n_ocr_after_reg = c.execute("SELECT COUNT(*) FROM ocr_runs").fetchone()[0]
    check("re-register pages does not duplicate", n_pages2 == n_pages and n2 == 0)
    check("re-register does not drop OCR runs", n_ocr_after_reg == n_ocr_before)

    import_existing_ocr_json(1, 33)
    import_existing_ocr_json(1, 33)
    with get_conn() as c:
        n_runs = c.execute(
            "SELECT COUNT(*) FROM ocr_runs WHERE source_page_id=?", (pid,)
        ).fetchone()[0]
        n_units = c.execute(
            "SELECT COUNT(*) FROM text_units WHERE COALESCE(is_test,0)=0"
        ).fetchone()[0]
        n_pub = c.execute(
            "SELECT COUNT(*) FROM text_units WHERE published=1 AND COALESCE(is_test,0)=0"
        ).fetchone()[0]
    check("OCR stays linked to stable page id", n_runs == 1)
    check("reopen path keeps production units unpublished", n_pub == 0)

    migrate_stable_source_ids()
    with get_conn() as c:
        row2 = c.execute(
            """
            SELECT source_page_id FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number=1 AND sp.pdf_page_number=33
            """
        ).fetchone()
        n_runs2 = c.execute(
            "SELECT COUNT(*) FROM ocr_runs WHERE source_page_id=?",
            (stable_source_page_id(1, 33),),
        ).fetchone()[0]
    check("stable IDs unchanged after second migrate", row2["source_page_id"] == pid)
    check("OCR still attached after migrate", n_runs2 == 1)

    persist_snapshot()
    DB_PATH.unlink()
    check("after deleting live DB, snapshot still exists", SNAPSHOT_PATH.exists())
    third = init_db()
    check("init after live-DB loss restores snapshot", third in ("restored", "exists"))
    with get_conn() as c:
        row3 = c.execute(
            """
            SELECT source_page_id FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number=1 AND sp.pdf_page_number=33
            """
        ).fetchone()
        n_runs3 = c.execute(
            "SELECT COUNT(*) FROM ocr_runs WHERE source_page_id=?", (pid,)
        ).fetchone()[0]
    check("restored page 33 identity unchanged", row3 is not None and row3["source_page_id"] == pid)
    check("restored OCR runs still present", n_runs3 == 1)

    passed = sum(1 for x in results if x)
    print(f"\n=== PERSISTENCE RESULT: {passed}/{len(results)} PASS ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
