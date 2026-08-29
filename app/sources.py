"""Immutable source registration and PDF page registry."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Optional

from .db import get_conn, audit, new_id, row_to_dict, ROOT

ORIGINALS = ROOT / "storage" / "originals"
PAGE_IMAGES = ROOT / "storage" / "page_images"

# Known expected blanks (from prior inspection)
EXPECTED_BLANKS = {
    1: {3, 4},  # volume 1
    2: {3, 4},  # volume 2 (candidate; confirm during review)
}

# Canonical text start (verified from scan)
CANONICAL_TEXT_START = {
    1: {"pdf_page": 33, "printed_page": 33, "note": "كتاب الصلاة / وقوت الصلاة"},
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def register_source_files() -> list[dict]:
    """Register the two immutable PDFs. Never overwrite."""
    expected = [
        {
            "volume_number": 1,
            "filename": "muwatta_bashshar_vol1_1417_1997.pdf",
            "sha256": "2b5c281a5acfe0d0a1d7eba3767c591b7cecaa636f13bca68677d7316703f075",
            "page_count": 664,
            "arabic_label": "المجلد الأول",
        },
        {
            "volume_number": 2,
            "filename": "muwatta_bashshar_vol2_1417_1997.pdf",
            "sha256": "f0743087410d7b284c4fe427b0a4607eb06c18d1c3c40fcd3a9ccac3a0607f55",
            "page_count": 720,
            "arabic_label": "المجلد الثاني",
        },
    ]
    registered = []
    with get_conn() as conn:
        edition = conn.execute(
            "SELECT edition_id FROM editions WHERE edition_id = 'ed-bashshar-1997'"
        ).fetchone()
        if not edition:
            raise RuntimeError("Canonical edition not seeded. Run init_db first.")

        for item in expected:
            path = ORIGINALS / item["filename"]
            if not path.exists():
                raise FileNotFoundError(f"Missing immutable original: {path}")

            actual_hash = sha256_file(path)
            if actual_hash != item["sha256"]:
                raise ValueError(
                    f"SHA-256 mismatch for {item['filename']}: "
                    f"expected {item['sha256']}, got {actual_hash}. "
                    "Refusing to register (integrity failure)."
                )

            source_id = new_id("src-")
            volume_id = new_id("vol-")

            # source_files
            conn.execute(
                """
                INSERT INTO source_files (
                    source_id, edition_id, volume_number, original_filename,
                    storage_path, file_size_bytes, sha256, page_count,
                    is_immutable, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    source_id,
                    "ed-bashshar-1997",
                    item["volume_number"],
                    item["filename"],
                    str(path.relative_to(ROOT)),
                    path.stat().st_size,
                    actual_hash,
                    item["page_count"],
                    "https://waqfeya.net/books/الموطأ-لإمام-دار-الهجرة-مالك-بن-أنس-رواية-يحيى-بن-يحيى-الليثي--ت-بشار/c7aea7234bc846b2aad4a1f67f55325f",
                ),
            )
            audit(
                conn, "source_file", source_id, "create",
                new_value={"filename": item["filename"], "sha256": actual_hash},
                reason="Immutable source registration",
            )

            # volumes
            conn.execute(
                """
                INSERT INTO volumes (
                    volume_id, edition_id, source_id, volume_number,
                    arabic_label, pdf_page_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    volume_id,
                    "ed-bashshar-1997",
                    source_id,
                    item["volume_number"],
                    item["arabic_label"],
                    item["page_count"],
                ),
            )
            audit(conn, "volume", volume_id, "create", new_value=item)

            registered.append(
                {
                    "source_id": source_id,
                    "volume_id": volume_id,
                    "volume_number": item["volume_number"],
                    "sha256": actual_hash,
                    "page_count": item["page_count"],
                }
            )
    return registered


def register_all_pages(volume_number: Optional[int] = None) -> int:
    """
    Register every PDF page for the given volume (or all volumes).
    Does NOT run OCR. Does NOT invent printed page numbers.
    """
    count = 0
    with get_conn() as conn:
        q = "SELECT * FROM volumes"
        params: tuple = ()
        if volume_number is not None:
            q += " WHERE volume_number = ?"
            params = (volume_number,)
        volumes = conn.execute(q, params).fetchall()

        for vol in volumes:
            source = conn.execute(
                "SELECT * FROM source_files WHERE source_id = ?",
                (vol["source_id"],),
            ).fetchone()
            expected_blanks = EXPECTED_BLANKS.get(vol["volume_number"], set())

            for pdf_page in range(1, vol["pdf_page_count"] + 1):
                # printed_page: only set when we have verified mapping
                printed = None
                printed_status = "needs_review"

                # Known mapping from prior verification (vol1 only, early pages)
                if vol["volume_number"] == 1 and pdf_page >= 5:
                    # From inspection: PDF page N ≈ printed N for muqaddima and start
                    # We still mark needs_review until admin confirms full mapping
                    if pdf_page == 33:
                        printed = 33
                        printed_status = "verified"
                    # Do not invent for other pages

                blank_status = "unknown"
                if pdf_page in expected_blanks:
                    blank_status = "expected_blank"

                page_id = new_id("pg-")
                conn.execute(
                    """
                    INSERT INTO source_pages (
                        source_page_id, edition_id, volume_id, source_id,
                        pdf_page_number, printed_page_number, printed_page_status,
                        blank_status, page_status, ocr_status, verification_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'present', 'not_run', 'imported')
                    """,
                    (
                        page_id,
                        "ed-bashshar-1997",
                        vol["volume_id"],
                        vol["source_id"],
                        pdf_page,
                        printed,
                        printed_status,
                        blank_status,
                    ),
                )
                count += 1

            audit(
                conn, "volume", vol["volume_id"], "register_pages",
                new_value={"page_count": vol["pdf_page_count"]},
                reason="Bulk page registry",
            )
    return count


def extract_page_image(volume_number: int, pdf_page: int, dpi: int = 120) -> Path:
    """Extract a single page image for the source viewer. Does not modify the PDF."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT sf.storage_path, sp.source_page_id
            FROM source_pages sp
            JOIN source_files sf ON sf.source_id = sp.source_id
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number = ? AND sp.pdf_page_number = ?
            """,
            (volume_number, pdf_page),
        ).fetchone()
        if not row:
            raise ValueError(f"Page not registered: vol {volume_number} pdf {pdf_page}")

        pdf_path = ROOT / row["storage_path"]
        out_dir = PAGE_IMAGES / f"vol{volume_number}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_prefix = out_dir / f"p{pdf_page:04d}"
        out_file = Path(str(out_prefix) + ".png")

        if not out_file.exists():
            subprocess.run(
                [
                    "pdftoppm", "-f", str(pdf_page), "-l", str(pdf_page),
                    "-png", "-r", str(dpi),
                    str(pdf_path), str(out_prefix),
                ],
                check=True, capture_output=True,
            )
            # pdftoppm may produce p0001-1.png style; normalize
            candidates = list(out_dir.glob(f"p{pdf_page:04d}*.png"))
            if not candidates:
                candidates = list(out_dir.glob("*.png"))
            if candidates:
                candidates[0].rename(out_file)

        # update image_path
        conn.execute(
            "UPDATE source_pages SET image_path = ? WHERE source_page_id = ?",
            (str(out_file.relative_to(ROOT)), row["source_page_id"]),
        )
        return out_file


def get_page(volume_number: int, pdf_page: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT sp.*, v.volume_number, sf.original_filename, sf.sha256
            FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            JOIN source_files sf ON sf.source_id = sp.source_id
            WHERE v.volume_number = ? AND sp.pdf_page_number = ?
            """,
            (volume_number, pdf_page),
        ).fetchone()
        return row_to_dict(row)


def list_pages_needing_review(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT sp.source_page_id, v.volume_number, sp.pdf_page_number,
                   sp.printed_page_number, sp.printed_page_status,
                   sp.blank_status, sp.verification_status, sp.ocr_status
            FROM source_pages sp
            JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE sp.verification_status IN ('imported','extracted','needs_review')
            ORDER BY v.volume_number, sp.pdf_page_number
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
