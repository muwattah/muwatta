"""
Reproduceerbare OCR-runner voor de Bashshār-editie.

Regels:
- Schrijft ALLEEN naar ocr_runs + arabic_text_raw
- NOOIT naar arabic_text_verified
- NOOIT auto-approve / publish
- Scan blijft de autoriteit
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import get_conn, audit, new_id, ROOT

TESSDATA = ROOT / "tessdata"
OCR_STORAGE = ROOT / "storage" / "ocr"
OCR_STORAGE.mkdir(parents=True, exist_ok=True)

OCR_ENGINE = "tesseract"
OCR_MODEL = "ara"  # tessdata_best ara
OCR_VERSION = "5"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _preprocess_layout_only(text: str) -> str:
    """
    Alleen technische opschoning:
    - regelafbrekingen normaliseren
    - dubbele whitespace
    Geen inhoudelijke Arabische 'correcties'.
    """
    if not text:
        return ""
    # Normalize newlines
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse 3+ newlines to 2
    t = re.sub(r"\n{3,}", "\n\n", t)
    # Collapse horizontal whitespace (but keep newlines)
    lines = []
    for line in t.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)
    return "\n".join(lines).strip()


def run_ocr_on_image(image_path: Path) -> tuple[str, Optional[float]]:
    """
    Run Tesseract Arabic OCR on a page image.
    Returns (raw_text, confidence_or_None).
    """
    env = {"TESSDATA_PREFIX": str(TESSDATA)}
    # TSV for confidence; also plain text
    cmd_txt = [
        "tesseract", str(image_path), "stdout",
        "-l", "ara",
        "--psm", "6",  # assume uniform block of text
    ]
    r = subprocess.run(
        cmd_txt, capture_output=True, text=True, env={**dict(**{k: str(v) for k, v in __import__('os').environ.items()}), **env},
        timeout=120,
    )
    raw = r.stdout or ""
    # Confidence via TSV
    conf: Optional[float] = None
    cmd_tsv = [
        "tesseract", str(image_path), "stdout",
        "-l", "ara",
        "--psm", "6",
        "tsv",
    ]
    r2 = subprocess.run(
        cmd_tsv, capture_output=True, text=True, env={**dict(**{k: str(v) for k, v in __import__('os').environ.items()}), **env},
        timeout=120,
    )
    if r2.returncode == 0 and r2.stdout:
        confs = []
        for line in r2.stdout.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 12:
                try:
                    c = float(parts[10])
                    if c >= 0:
                        confs.append(c)
                except ValueError:
                    pass
        if confs:
            conf = sum(confs) / len(confs)

    cleaned = _preprocess_layout_only(raw)
    return cleaned, conf


def ocr_page(volume_number: int, pdf_page: int, dpi: int = 150) -> dict:
    """
    OCR one registered page.
    Creates ocr_runs row; does NOT set arabic_text_verified.
    """
    from .sources import extract_page_image, get_page

    page = get_page(volume_number, pdf_page)
    if not page:
        raise ValueError(f"Page not registered: vol={volume_number} pdf={pdf_page}")

    image_path = extract_page_image(volume_number, pdf_page, dpi=dpi)
    raw_text, confidence = run_ocr_on_image(image_path)

    ocr_run_id = new_id("ocr-")
    storage = OCR_STORAGE / f"vol{volume_number}" / f"p{pdf_page:04d}.json"
    storage.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ocr_run_id": ocr_run_id,
        "volume": volume_number,
        "pdf_page": pdf_page,
        "source_page_id": page["source_page_id"],
        "engine": OCR_ENGINE,
        "model": OCR_MODEL,
        "version": OCR_VERSION,
        "confidence": confidence,
        "raw_text": raw_text,
        "timestamp": utcnow(),
    }
    storage.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ocr_runs (
                ocr_run_id, source_page_id, ocr_engine, ocr_model, ocr_version,
                ocr_confidence, ocr_output_raw, ocr_timestamp, storage_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ocr_run_id,
                page["source_page_id"],
                OCR_ENGINE,
                OCR_MODEL,
                OCR_VERSION,
                confidence,
                raw_text,
                utcnow(),
                str(storage.relative_to(ROOT)),
            ),
        )
        # Update page ocr_status only
        ocr_status = "done"
        if confidence is not None and confidence < 40:
            ocr_status = "needs_review"
        conn.execute(
            """
            UPDATE source_pages
            SET ocr_status = ?, verification_status = CASE
                WHEN verification_status = 'imported' THEN 'extracted'
                ELSE verification_status
            END
            WHERE source_page_id = ?
            """,
            (ocr_status, page["source_page_id"]),
        )
        audit(
            conn, "source_page", page["source_page_id"], "ocr_run",
            new_value={
                "ocr_run_id": ocr_run_id,
                "confidence": confidence,
                "chars": len(raw_text),
            },
            reason="OCR extraction (raw only)",
        )

    return {
        "ocr_run_id": ocr_run_id,
        "source_page_id": page["source_page_id"],
        "volume": volume_number,
        "pdf_page": pdf_page,
        "confidence": confidence,
        "char_count": len(raw_text),
        "ocr_status": ocr_status,
        "preview": raw_text[:300] if raw_text else "",
    }


def ocr_range(
    volume_number: int,
    start_pdf: int,
    end_pdf: int,
    dpi: int = 120,
) -> list[dict]:
    """OCR a range of pages. Returns list of result dicts."""
    results = []
    for p in range(start_pdf, end_pdf + 1):
        try:
            r = ocr_page(volume_number, p, dpi=dpi)
            results.append(r)
            print(
                f"  OCR vol{volume_number} p{p}: conf={r['confidence']} chars={r['char_count']}",
                flush=True,
            )
        except Exception as e:
            results.append({
                "volume": volume_number,
                "pdf_page": p,
                "error": str(e),
            })
            print(f"  FAIL vol{volume_number} p{p}: {e}", flush=True)
    return results


def import_existing_ocr_json(volume_number: int, pdf_page: int) -> dict:
    """
    Register an already-saved OCR JSON into ocr_runs.
    Does NOT run Tesseract. Does NOT create text_units.
    Does NOT write arabic_text_verified. Idempotent on ocr_run_id.
    """
    from .sources import get_page

    storage = OCR_STORAGE / f"vol{volume_number}" / f"p{pdf_page:04d}.json"
    if not storage.exists():
        raise FileNotFoundError(f"Existing OCR JSON not found: {storage}")
    payload = json.loads(storage.read_text(encoding="utf-8"))
    raw_text = payload.get("raw_text")
    if raw_text is None:
        raise ValueError("JSON has no raw_text")
    page = get_page(volume_number, pdf_page)
    if not page:
        raise ValueError(f"Page not registered: vol={volume_number} pdf={pdf_page}")
    ocr_run_id = payload.get("ocr_run_id") or new_id("ocr-")
    engine = payload.get("engine") or OCR_ENGINE
    model = payload.get("model") or OCR_MODEL
    version = str(payload.get("version") or OCR_VERSION)
    confidence = payload.get("confidence")
    timestamp = payload.get("timestamp") or utcnow()
    rel = str(storage.relative_to(ROOT))
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT ocr_run_id FROM ocr_runs WHERE ocr_run_id = ?", (ocr_run_id,)
        ).fetchone()
        if existing:
            stored = conn.execute(
                "SELECT ocr_output_raw FROM ocr_runs WHERE ocr_run_id = ?",
                (ocr_run_id,),
            ).fetchone()["ocr_output_raw"]
            if stored != raw_text:
                raise RuntimeError("existing OCR raw does not match JSON; refusing to overwrite")
            return {
                "imported": False,
                "duplicate": True,
                "ocr_run_id": ocr_run_id,
                "source_page_id": page["source_page_id"],
                "volume": volume_number,
                "pdf_page": pdf_page,
                "text_units_created": 0,
            }
        conn.execute(
            """
            INSERT INTO ocr_runs (
                ocr_run_id, source_page_id, ocr_engine, ocr_model, ocr_version,
                ocr_confidence, ocr_output_raw, ocr_timestamp, storage_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ocr_run_id,
                page["source_page_id"],
                engine,
                model,
                version,
                confidence,
                raw_text,
                timestamp,
                rel,
            ),
        )
        conn.execute(
            """
            UPDATE source_pages
            SET ocr_status = 'done',
                verification_status = CASE
                    WHEN verification_status = 'imported' THEN 'extracted'
                    ELSE verification_status
                END
            WHERE source_page_id = ?
            """,
            (page["source_page_id"],),
        )
        audit(
            conn, "source_page", page["source_page_id"], "ocr_import_existing",
            new_value={
                "ocr_run_id": ocr_run_id,
                "json_path": rel,
                "engine": engine,
                "model": model,
                "confidence": confidence,
            },
            reason="Import existing OCR JSON; no engine run",
        )
    return {
        "imported": True,
        "duplicate": False,
        "ocr_run_id": ocr_run_id,
        "source_page_id": page["source_page_id"],
        "volume": volume_number,
        "pdf_page": pdf_page,
        "confidence": confidence,
        "char_count": len(raw_text),
        "json_path": rel,
        "text_units_created": 0,
        "verification_status": "needs_review",
    }
