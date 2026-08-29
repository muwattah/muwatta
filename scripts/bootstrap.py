#!/usr/bin/env python3
"""
Bootstrap the Al-Muwaṭṭaʾ source layer:
1. Init DB + seed edition
2. Register immutable PDFs (SHA-256 verified)
3. Register all PDF pages
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import init_db, get_conn, row_to_dict
from app.sources import register_source_files, register_all_pages, CANONICAL_TEXT_START


def main() -> None:
    print("=== Al-Muwaṭṭaʾ Source Layer Bootstrap ===\n")

    print("1. Initializing database + seeding edition...")
    if (ROOT / "muwatta_source.db").exists():
        print("   DB already exists — skipping schema (delete file to re-init).")
    else:
        init_db()
        print("   Schema applied and edition seeded.")

    with get_conn() as conn:
        ed = conn.execute("SELECT * FROM editions").fetchone()
        print(f"   Edition: {ed['title_arabic']} — {ed['muhqqiq']} — {ed['year_hijri']}/{ed['year_ce']}")
        print(f"   Bibliographic status: {ed['bibliographic_status']}")

    print("\n2. Registering immutable source files...")
    try:
        registered = register_source_files()
        for r in registered:
            print(f"   Vol {r['volume_number']}: {r['page_count']} pages | SHA-256 OK")
            print(f"      source_id={r['source_id']}")
    except Exception as e:
        # Already registered?
        with get_conn() as conn:
            existing = conn.execute("SELECT volume_number, sha256, page_count FROM source_files ORDER BY volume_number").fetchall()
            if existing:
                print("   Sources already registered:")
                for row in existing:
                    print(f"   Vol {row['volume_number']}: {row['page_count']} pages | {row['sha256'][:16]}...")
            else:
                raise

    print("\n3. Registering PDF pages...")
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM source_pages").fetchone()[0]
    if n == 0:
        count = register_all_pages()
        print(f"   Registered {count} pages.")
    else:
        print(f"   Pages already registered: {n}")

    print("\n4. Canonical text start (verified):")
    info = CANONICAL_TEXT_START[1]
    print(f"   Volume 1, PDF page {info['pdf_page']} = printed page {info['printed_page']}")
    print(f"   Content starts: {info['note']}")

    print("\n5. Summary")
    with get_conn() as conn:
        print(f"   Editions:     {conn.execute('SELECT COUNT(*) FROM editions').fetchone()[0]}")
        print(f"   Source files: {conn.execute('SELECT COUNT(*) FROM source_files').fetchone()[0]}")
        print(f"   Volumes:      {conn.execute('SELECT COUNT(*) FROM volumes').fetchone()[0]}")
        print(f"   Pages:        {conn.execute('SELECT COUNT(*) FROM source_pages').fetchone()[0]}")
        print(f"   Text units:   {conn.execute('SELECT COUNT(*) FROM text_units').fetchone()[0]}")
        blanks = conn.execute(
            "SELECT COUNT(*) FROM source_pages WHERE blank_status = 'expected_blank'"
        ).fetchone()[0]
        print(f"   Expected blanks: {blanks}")
        needs = conn.execute(
            "SELECT COUNT(*) FROM source_pages WHERE verification_status IN ('imported','needs_review')"
        ).fetchone()[0]
        print(f"   Pages needing review: {needs}")

    print("\n=== Bootstrap complete ===")
    print("Next: use scripts/cli.py or the admin source viewer.")
    print("No educational content has been generated.")
    print("All text units default to needs_review / not published.")


if __name__ == "__main__":
    main()
