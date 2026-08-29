#!/usr/bin/env python3
"""
CLI for source-layer operations.
Examples:
  python scripts/cli.py status
  python scripts/cli.py page 1 33
  python scripts/cli.py extract-image 1 33
  python scripts/cli.py set-printed 1 33 33
  python scripts/cli.py needs-review
  python scripts/cli.py audit source_page <id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn, row_to_dict
from app.sources import get_page, extract_page_image, list_pages_needing_review
from app.verification import (
    set_printed_page,
    set_verification_status,
    get_provenance,
    get_audit_history,
    create_text_unit_from_page,
    approve_text,
)


def cmd_status(_: argparse.Namespace) -> None:
    with get_conn() as conn:
        print("=== Source Layer Status ===")
        for label, sql in [
            ("Editions", "SELECT COUNT(*) FROM editions"),
            ("Source files", "SELECT COUNT(*) FROM source_files"),
            ("Volumes", "SELECT COUNT(*) FROM volumes"),
            ("Pages", "SELECT COUNT(*) FROM source_pages"),
            ("Books", "SELECT COUNT(*) FROM books"),
            ("Chapters", "SELECT COUNT(*) FROM chapters"),
            ("Text units", "SELECT COUNT(*) FROM text_units"),
            ("Editorial notes", "SELECT COUNT(*) FROM editorial_notes"),
            ("OCR runs", "SELECT COUNT(*) FROM ocr_runs"),
            ("Audit log entries", "SELECT COUNT(*) FROM audit_log"),
        ]:
            print(f"  {label:20} {conn.execute(sql).fetchone()[0]}")
        print("\nPage verification statuses:")
        rows = conn.execute(
            "SELECT verification_status, COUNT(*) c FROM source_pages GROUP BY verification_status"
        ).fetchall()
        for r in rows:
            print(f"  {r['verification_status']:20} {r['c']}")
        print("\nBlank statuses:")
        rows = conn.execute(
            "SELECT blank_status, COUNT(*) c FROM source_pages GROUP BY blank_status"
        ).fetchall()
        for r in rows:
            print(f"  {r['blank_status']:20} {r['c']}")


def cmd_page(args: argparse.Namespace) -> None:
    p = get_page(args.volume, args.pdf_page)
    if not p:
        print("Page not found")
        sys.exit(1)
    print(json.dumps(p, ensure_ascii=False, indent=2))


def cmd_extract(args: argparse.Namespace) -> None:
    path = extract_page_image(args.volume, args.pdf_page, dpi=args.dpi)
    print(f"Image: {path}")


def cmd_set_printed(args: argparse.Namespace) -> None:
    p = get_page(args.volume, args.pdf_page)
    if not p:
        print("Page not found")
        sys.exit(1)
    result = set_printed_page(
        p["source_page_id"],
        args.printed,
        status="verified" if args.printed is not None else "absent",
        reason=args.reason or "CLI set-printed",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_needs_review(args: argparse.Namespace) -> None:
    rows = list_pages_needing_review(limit=args.limit)
    print(f"{'Vol':>3} {'PDF':>5} {'Printed':>8} {'PrintStat':>12} {'Blank':>16} {'Status':>12}")
    for r in rows:
        print(
            f"{r['volume_number']:>3} {r['pdf_page_number']:>5} "
            f"{str(r['printed_page_number'] or '-'):>8} "
            f"{r['printed_page_status']:>12} {r['blank_status']:>16} "
            f"{r['verification_status']:>12}"
        )


def cmd_audit(args: argparse.Namespace) -> None:
    rows = get_audit_history(args.entity_type, args.entity_id)
    for r in rows:
        print(f"{r['timestamp']} | {r['action']:20} | user={r['user_id']} | {r['reason'] or ''}")


def cmd_create_text(args: argparse.Namespace) -> None:
    p = get_page(args.volume, args.pdf_page)
    if not p:
        print("Page not found")
        sys.exit(1)
    text_id = create_text_unit_from_page(
        p["source_page_id"],
        arabic_text_raw=args.raw or None,
        text_type=args.type,
        hadith_number=args.number,
    )
    print(f"Created text_id={text_id} (needs_review, not published)")


def cmd_provenance(args: argparse.Namespace) -> None:
    p = get_provenance(args.text_id)
    if not p:
        print("Not found")
        sys.exit(1)
    print(json.dumps(p, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Al-Muwaṭṭaʾ source layer CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    p = sub.add_parser("page")
    p.add_argument("volume", type=int)
    p.add_argument("pdf_page", type=int)

    p = sub.add_parser("extract-image")
    p.add_argument("volume", type=int)
    p.add_argument("pdf_page", type=int)
    p.add_argument("--dpi", type=int, default=120)

    p = sub.add_parser("set-printed")
    p.add_argument("volume", type=int)
    p.add_argument("pdf_page", type=int)
    p.add_argument("printed", type=int, nargs="?", default=None)
    p.add_argument("--reason", default="")

    p = sub.add_parser("needs-review")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("audit")
    p.add_argument("entity_type")
    p.add_argument("entity_id")

    p = sub.add_parser("create-text")
    p.add_argument("volume", type=int)
    p.add_argument("pdf_page", type=int)
    p.add_argument("--raw", default=None)
    p.add_argument("--type", default="needs_review")
    p.add_argument("--number", default=None)

    p = sub.add_parser("provenance")
    p.add_argument("text_id")

    args = parser.parse_args()
    {
        "status": cmd_status,
        "page": cmd_page,
        "extract-image": cmd_extract,
        "set-printed": cmd_set_printed,
        "needs-review": cmd_needs_review,
        "audit": cmd_audit,
        "create-text": cmd_create_text,
        "provenance": cmd_provenance,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
