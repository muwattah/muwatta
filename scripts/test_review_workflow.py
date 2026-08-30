#!/usr/bin/env python3
"""Review workflow tests. Does not publish production content."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn, new_id, ensure_review_columns
from app.review_api import (
    set_verified_text,
    approve_text_unit,
    reject_text_unit,
    try_publish,
    flag_text_unit,
    split_text_unit,
    merge_text_units,
    update_text_meta,
)


def main() -> int:
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}: {name}" + (f" — {detail}" if detail else ""))
        results.append(ok)

    ensure_review_columns()

    print("=== Review workflow ===")
    with get_conn() as c:
        page = c.execute(
            """
            SELECT sp.source_page_id, sp.volume_id
            FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number = 1 AND sp.pdf_page_number = 33
            """
        ).fetchone()
        if not page:
            print("  FAIL: page 33 not registered — run bootstrap first")
            return 1

        def insert_unit(raw: str, status: str = "needs_review") -> str:
            tid = new_id("txt-")
            c.execute(
                """
                INSERT INTO text_units (
                    text_id, edition_id, volume_id, source_page_id, text_type,
                    arabic_text_raw, arabic_text_verified, pdf_page, printed_page,
                    verification_status, published, is_test
                ) VALUES (?, 'ed-bashshar-1997', ?, ?, 'other', ?, NULL, 33, 33, ?, 0, 1)
                """,
                (tid, page["volume_id"], page["source_page_id"], raw, status),
            )
            c.execute(
                "INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order) VALUES (?,?,?,?,1)",
                (new_id("tsp-"), tid, page["source_page_id"], "only"),
            )
            return tid

        parent = insert_unit("RAW-OCR-PARENT")
        a = insert_unit("RAW-A")
        b = insert_unit("RAW-B")
        ed = insert_unit("RAW-EDITORIAL")

    raw_before = None
    with get_conn() as c:
        raw_before = c.execute("SELECT arabic_text_raw FROM text_units WHERE text_id=?", (parent,)).fetchone()[0]
    set_verified_text(parent, "human from scan", user_id="test", reason="review test")
    with get_conn() as c:
        row = c.execute("SELECT arabic_text_raw, arabic_text_verified FROM text_units WHERE text_id=?", (parent,)).fetchone()
    check("raw OCR unchanged after verified edit", row["arabic_text_raw"] == raw_before == "RAW-OCR-PARENT")
    check("verified stored separately", row["arabic_text_verified"] == "human from scan")

    # automatic extraction cannot approve: status stays needs_review unless human path
    with get_conn() as c:
        auto = new_id("txt-")
        c.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id, text_type,
                arabic_text_raw, verification_status, published, is_test, pdf_page
            ) VALUES (?, 'ed-bashshar-1997', ?, ?, 'needs_review', 'ocr only', 'needs_review', 0, 1, 33)
            """,
            (auto, page["volume_id"], page["source_page_id"]),
        )
    try:
        approve_text_unit(auto, user_id="test")
        check("auto extract cannot approve without verified text", False)
    except ValueError as e:
        check("auto extract cannot approve without verified text", "arabic_text_verified" in str(e))

    reject_text_unit(b, "ocr noise", user_id="test")
    with get_conn() as c:
        st = c.execute("SELECT verification_status, published FROM text_units WHERE text_id=?", (b,)).fetchone()
    check("reject works", st["verification_status"] == "rejected" and st["published"] == 0)

    flag_text_unit(a, "twijfel_voetnoot", reason="maybe footnote", user_id="test")
    with get_conn() as c:
        fl = c.execute("SELECT review_flag, verification_status, arabic_text_raw FROM text_units WHERE text_id=?", (a,)).fetchone()
    check("flag works and keeps raw", fl["review_flag"] == "twijfel_voetnoot" and fl["arabic_text_raw"] == "RAW-A")

    split = split_text_unit(parent, ["deel een", "deel twee"], user_id="test", reason="scan has two units")
    check("split creates two children", len(split.get("child_ids") or []) == 2)
    with get_conn() as c:
        pst = c.execute("SELECT verification_status, arabic_text_raw FROM text_units WHERE text_id=?", (parent,)).fetchone()
        n_audit = c.execute(
            "SELECT COUNT(*) FROM audit_log WHERE entity_id=? AND action='split'", (parent,)
        ).fetchone()[0]
    check("split keeps parent raw and supersedes parent", pst["arabic_text_raw"] == "RAW-OCR-PARENT" and pst["verification_status"] == "superseded")
    check("split writes audit log", n_audit >= 1)

    # new units for merge (b was rejected but raw remains)
    with get_conn() as c:
        m1 = new_id("txt-")
        m2 = new_id("txt-")
        for tid, raw in ((m1, "RAW-M1"), (m2, "RAW-M2")):
            c.execute(
                """
                INSERT INTO text_units (
                    text_id, edition_id, volume_id, source_page_id, text_type,
                    arabic_text_raw, pdf_page, printed_page, verification_status, published, is_test
                ) VALUES (?, 'ed-bashshar-1997', ?, ?, 'other', ?, 33, 33, 'needs_review', 0, 1)
                """,
                (tid, page["volume_id"], page["source_page_id"], raw),
            )
            c.execute(
                "INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order) VALUES (?,?,?,?,1)",
                (new_id("tsp-"), tid, page["source_page_id"], "only"),
            )
    merged = merge_text_units([m1, m2], user_id="test", reason="one passage")
    with get_conn() as c:
        r1 = c.execute("SELECT arabic_text_raw, verification_status FROM text_units WHERE text_id=?", (m1,)).fetchone()
        pages = c.execute(
            "SELECT COUNT(*) FROM text_unit_source_pages WHERE text_id=?", (merged["merged_id"],)
        ).fetchone()[0]
    check("merge keeps source raw", r1["arabic_text_raw"] == "RAW-M1" and r1["verification_status"] == "superseded")
    check("merge keeps source_page provenance", pages >= 1)

    update_text_meta(ed, text_type="editorial", user_id="test", reason="muhaqqiq footnote")
    set_verified_text(ed, "footnote text", user_id="test")
    approve_text_unit(ed, user_id="test")
    pub = try_publish(ed, user_id="test")
    check("editorial cannot publish as canonical", pub.get("ok") is False)

    with get_conn() as c:
        n_pub = c.execute(
            "SELECT COUNT(*) FROM text_units WHERE published=1 AND is_test=0"
        ).fetchone()[0]
    check("production published remains 0", n_pub == 0)

    passed = sum(1 for x in results if x)
    print(f"\n=== REVIEW RESULT: {passed}/{len(results)} PASS ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
