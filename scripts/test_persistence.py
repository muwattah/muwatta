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
    PRIMARY_SNAPSHOT,
    init_db,
    get_conn,
    db_has_schema,
    stable_source_page_id,
    migrate_stable_source_ids,
    persist_snapshot,
    PersistenceError,
    new_id,
)
from app.ocr_runner import import_existing_ocr_json
from app.review_api import set_verified_text, split_text_unit, merge_text_units, flag_text_unit
from app.sources import register_source_files, register_all_pages


def main() -> int:
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}: {name}" + (f" — {detail}" if detail else ""))
        results.append(ok)

    print("=== Persistence ===")
    check("live DB path is explicit", str(DB_PATH).endswith("muwatta_source.db"))
    check("durable snapshot path under storage/runtime", SNAPSHOT_PATH.parent.name == "runtime" and SNAPSHOT_PATH.parent.parent.name == "storage")

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
    check("after deleting live DB, snapshot still exists", PRIMARY_SNAPSHOT.exists() or SNAPSHOT_PATH.exists())
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

    print("=== Persistence mutation restore ===")
    with get_conn() as c:
        page = c.execute(
            """SELECT sp.source_page_id, sp.volume_id FROM source_pages sp
               JOIN volumes v ON v.volume_id=sp.volume_id
               WHERE v.volume_number=1 AND sp.pdf_page_number=33"""
        ).fetchone()
        tid = new_id("txt-")
        c.execute(
            """INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id, text_type,
                arabic_text_raw, pdf_page, printed_page, verification_status, published, is_test
            ) VALUES (?, 'ed-bashshar-1997', ?, ?, 'other', 'RAW-P', 33, 33, 'needs_review', 0, 1)""",
            (tid, page["volume_id"], page["source_page_id"]),
        )
        c.execute(
            "INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order) VALUES (?,?,?,?,1)",
            (new_id("tsp-"), tid, page["source_page_id"], "only"),
        )
    set_verified_text(tid, "نص محقق للاختبار", user_id="persist-test")
    flag_text_unit(tid, "restore_probe", user_id="persist-test")
    split_res = split_text_unit(tid, ["جزء أ", "جزء ب"], user_id="persist-test")
    child_a, child_b = split_res["child_ids"]
    merge_res = merge_text_units([child_a, child_b], user_id="persist-test")
    merged_id = merge_res["merged_id"]

    DB_PATH.unlink()
    init_db()
    with get_conn(readonly=True) as c:
        ver = c.execute("SELECT arabic_text_verified, review_flag FROM text_units WHERE text_id=?", (tid,)).fetchone()
        parent = c.execute("SELECT verification_status FROM text_units WHERE text_id=?", (tid,)).fetchone()
        merged = c.execute("SELECT verification_status FROM text_units WHERE text_id=?", (merged_id,)).fetchone()
        n_link = c.execute("SELECT COUNT(*) FROM text_unit_source_pages WHERE text_id=?", (merged_id,)).fetchone()[0]
        same_page = c.execute(
            "SELECT source_page_id FROM source_pages WHERE source_page_id=?", (pid,)
        ).fetchone()
    check("A/B verified Arabic + flag survive /tmp loss", ver is not None and ver["arabic_text_verified"] == "نص محقق للاختبار" and ver["review_flag"] == "restore_probe")
    check("C split parent superseded after restore", parent["verification_status"] == "superseded")
    check("C merge unit + provenance survive restore", merged is not None and n_link >= 1)
    check("F source_page_id stable after restore", same_page is not None)

    snap_before = PRIMARY_SNAPSHOT.read_bytes() if PRIMARY_SNAPSHOT.exists() else b""
    try:
        with get_conn() as c:
            c.execute("UPDATE text_units SET notes='should_rollback' WHERE text_id=?", (merged_id,))
            raise RuntimeError("forced failure")
    except RuntimeError:
        pass
    snap_after = PRIMARY_SNAPSHOT.read_bytes() if PRIMARY_SNAPSHOT.exists() else b""
    check("D failed transaction does not replace good snapshot", snap_before == snap_after)

    backup_snap = PRIMARY_SNAPSHOT.read_bytes()
    PRIMARY_SNAPSHOT.write_bytes(b"not-a-sqlite-file")
    try:
        if SNAPSHOT_PATH.exists():
            SNAPSHOT_PATH.write_bytes(b"not-a-sqlite-file")
    except OSError:
        pass
    DB_PATH.unlink(missing_ok=True)
    raised = False
    try:
        from app.db import restore_snapshot_if_needed
        restore_snapshot_if_needed()
    except PersistenceError:
        raised = True
    PRIMARY_SNAPSHOT.write_bytes(backup_snap)
    DB_PATH.write_bytes(backup_snap)
    try:
        SNAPSHOT_PATH.write_bytes(backup_snap)
    except OSError:
        pass
    check("E corrupt snapshot refuses silent empty DB", raised)

    passed = sum(1 for x in results if x)
    print(f"\n=== PERSISTENCE RESULT: {passed}/{len(results)} PASS ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
