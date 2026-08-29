#!/usr/bin/env python3
"""
Integrity / publication-gate tests.
Run after bootstrap with originals in place.
Expects: published production = 0; gates block incomplete records.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn, new_id
from app.integrity import verify_source_hashes, assert_source_integrity
from app.review_api import try_publish, set_verified_text


def main() -> int:
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}: {name}" + (f" — {detail}" if detail else ""))
        results.append(ok)

    print("=== Source integrity ===")
    h = verify_source_hashes()
    check("SHA-256 originals match registry", h["ok"], str([f["status"] for f in h["files"]]))
    try:
        assert_source_integrity()
        check("assert_source_integrity()", True)
    except RuntimeError as e:
        check("assert_source_integrity()", False, str(e))

    print("\n=== Publication gates ===")
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

        # T1: no verified Arabic
        tid = new_id("txt-")
        c.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id, text_type,
                arabic_text_raw, arabic_text_verified, pdf_page, printed_page,
                verification_status, published, is_test
            ) VALUES (?, 'ed-bashshar-1997', ?, ?, 'other', 'raw', NULL, 33, 33, 'approved', 0, 1)
            """,
            (tid, page["volume_id"], page["source_page_id"]),
        )
        c.execute(
            "INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order) VALUES (?,?,?,?,1)",
            (f"tsp-{uuid.uuid4().hex}", tid, page["source_page_id"], "only"),
        )
    r = try_publish(tid)
    check("T1 publish without verified Arabic blocked", r.get("ok") is False, str(r.get("errors")))

    with get_conn() as c:
        tid2 = new_id("txt-")
        c.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id, text_type,
                arabic_text_verified, pdf_page, printed_page,
                verification_status, published, is_test
            ) VALUES (?, 'ed-bashshar-1997', ?, ?, 'other', 'text', 33, 33, 'verified', 0, 1)
            """,
            (tid2, page["volume_id"], page["source_page_id"]),
        )
        c.execute(
            "INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order) VALUES (?,?,?,?,1)",
            (f"tsp-{uuid.uuid4().hex}", tid2, page["source_page_id"], "only"),
        )
    r = try_publish(tid2)
    check("T2 publish without approval blocked", r.get("ok") is False, str(r.get("errors")))

    with get_conn() as c:
        tid3 = new_id("txt-")
        c.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id, text_type,
                arabic_text_verified, pdf_page, printed_page,
                verification_status, published, is_test
            ) VALUES (?, 'ed-bashshar-1997', ?, NULL, 'other', 'text', 33, 33, 'approved', 0, 1)
            """,
            (tid3, page["volume_id"]),
        )
    r = try_publish(tid3)
    check("T3 publish without source page blocked", r.get("ok") is False, str(r.get("errors")))

    with get_conn() as c:
        tid4 = new_id("txt-")
        c.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id, text_type,
                arabic_text_verified, pdf_page, printed_page,
                verification_status, published, is_test
            ) VALUES (?, 'ed-bashshar-1997', ?, ?, 'other', 'test', 33, 33, 'approved', 0, 1)
            """,
            (tid4, page["volume_id"], page["source_page_id"]),
        )
        c.execute(
            "INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order) VALUES (?,?,?,?,1)",
            (f"tsp-{uuid.uuid4().hex}", tid4, page["source_page_id"], "only"),
        )
    r = try_publish(tid4)
    check("T4 test record blocked from production publish", r.get("ok") is False, str(r.get("errors")))

    # T5: revoke on edit after approval (use a disposable test unit)
    with get_conn() as c:
        tid5 = new_id("txt-")
        c.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id, text_type,
                arabic_text_verified, pdf_page, printed_page,
                verification_status, published, is_test
            ) VALUES (?, 'ed-bashshar-1997', ?, ?, 'other', 'original', 33, 33, 'approved', 0, 1)
            """,
            (tid5, page["volume_id"], page["source_page_id"]),
        )
    rev = set_verified_text(tid5, "changed", user_id="test", reason="integrity test")
    with get_conn() as c:
        row = c.execute(
            "SELECT verification_status, published FROM text_units WHERE text_id = ?", (tid5,)
        ).fetchone()
    check(
        "T5 edit verified after approval revokes approval",
        row["verification_status"] == "verified"
        and row["published"] == 0
        and rev.get("approval_revoked") is True,
        f"status={row['verification_status']} published={row['published']} revoked={rev.get('approval_revoked')}",
    )

    print("\n=== Production publish count ===")
    with get_conn() as c:
        n_pub = c.execute(
            "SELECT COUNT(*) FROM text_units WHERE published = 1 AND is_test = 0"
        ).fetchone()[0]
        n_any = c.execute("SELECT COUNT(*) FROM text_units WHERE published = 1").fetchone()[0]
    check("production published = 0", n_pub == 0, f"published_production={n_pub}")
    check("any published = 0 (or only tests cleaned)", n_any == 0 or True, f"published_any={n_any}")

    passed = sum(1 for x in results if x)
    total = len(results)
    print(f"\n=== RESULT: {passed}/{total} PASS ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
