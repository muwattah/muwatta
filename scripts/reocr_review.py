"""Manual review OCR runner. Creates additional OCR candidates only."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from PIL import Image

from app.db import ROOT, audit, get_conn, new_id
from app.ocr_runner import OCR_ENGINE, OCR_MODEL, OCR_VERSION, OCR_REVIEW_STORAGE, run_ocr_on_image, utcnow
from app.sources import get_page, get_source_pdf_path


def render_review_image(volume: int, pdf_page: int, dpi: int) -> Path:
    pdf_path = get_source_pdf_path(volume, pdf_page)
    out_dir = OCR_REVIEW_STORAGE / f"vol{volume}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = out_dir / f"p{pdf_page:04d}_{dpi}dpi"
    out_file = Path(str(out_prefix) + ".png")
    subprocess.run(
        [
            "pdftoppm", "-f", str(pdf_page), "-l", str(pdf_page),
            "-singlefile", "-png", "-r", str(dpi),
            str(pdf_path), str(out_prefix),
        ],
        check=True, capture_output=True,
    )
    if not out_file.exists():
        raise RuntimeError(f"Review render was not created: {out_file}")
    return out_file


def threshold_image(image_path: Path, threshold: int) -> Path:
    out_file = image_path.with_name(image_path.stem + f"_thr{threshold}.png")
    im = Image.open(image_path).convert("L")
    im = im.point(lambda p: 0 if p < threshold else 255, mode="1")
    im.save(out_file)
    return out_file


def create_review_ocr(volume: int, pdf_page: int, dpi: int = 300, threshold: int | None = 220, psm: int = 6) -> dict:
    if dpi < 72:
        raise ValueError("dpi must be at least 72")
    if threshold is not None and not 0 <= threshold <= 255:
        raise ValueError("threshold must be between 0 and 255")
    if psm < 0:
        raise ValueError("psm must be non-negative")

    page = get_page(volume, pdf_page)
    if not page:
        raise ValueError(f"Page not registered: vol {volume} pdf {pdf_page}")
    if page["verification_status"] != "needs_review":
        raise ValueError(f"Page must be needs_review, got: {page['verification_status']}")

    threshold_label = "none" if threshold is None else str(threshold)
    config_name = f"dpi{dpi}_thr{threshold_label}_psm{psm}"
    out_dir = OCR_REVIEW_STORAGE / f"vol{volume}" / f"p{pdf_page:04d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{config_name}.json"

    with get_conn(readonly=True) as conn:
        existing = conn.execute(
            "SELECT ocr_run_id, ocr_output_raw, ocr_confidence FROM ocr_runs WHERE source_page_id = ? AND storage_path = ?",
            (page["source_page_id"], str(json_path.relative_to(ROOT)).replace("\\", "/")),
        ).fetchone()
    if existing:
        return {"status": "skipped_existing", "ocr_run_id": existing["ocr_run_id"], "char_count": len(existing["ocr_output_raw"] or ""), "confidence": existing["ocr_confidence"], "config": config_name}

    image_path = render_review_image(volume, pdf_page, dpi)
    ocr_image = image_path
    if threshold is not None:
        ocr_image = threshold_image(image_path, threshold)

    raw_text, confidence = run_ocr_on_image(ocr_image, psm=psm)
    timestamp = utcnow()
    ocr_run_id = new_id("ocr-review-")
    storage_path = str(json_path.relative_to(ROOT)).replace("\\", "/")

    notes = {
        "purpose": "human_review_candidate",
        "dpi": dpi,
        "threshold": threshold,
        "psm": psm,
        "source_image": str(image_path.relative_to(ROOT)).replace("\\", "/"),
        "ocr_image": str(ocr_image.relative_to(ROOT)).replace("\\", "/"),
    }
    payload = {
        "ocr_run_id": ocr_run_id,
        "source_page_id": page["source_page_id"],
        "ocr_engine": OCR_ENGINE,
        "ocr_model": OCR_MODEL,
        "ocr_version": OCR_VERSION,
        "ocr_confidence": confidence,
        "ocr_output_raw": raw_text,
        "ocr_timestamp": timestamp,
        "storage_path": storage_path,
        "notes": notes,
    }

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ocr_runs (
                ocr_run_id, source_page_id, ocr_engine, ocr_model,
                ocr_version, ocr_confidence, ocr_output_raw,
                ocr_timestamp, storage_path, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ocr_run_id,
                page["source_page_id"],
                OCR_ENGINE,
                OCR_MODEL,
                OCR_VERSION,
                confidence,
                raw_text,
                timestamp,
                storage_path,
                json.dumps(notes, ensure_ascii=False, sort_keys=True),
            ),
        )
        audit(
            conn,
            "source_page",
            page["source_page_id"],
            "ocr_review_run",
            None,
            {"ocr_run_id": ocr_run_id, "config": config_name},
            "human-review",
            "Alternative OCR candidate for human review; original OCR preserved",
        )

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "done",
        "ocr_run_id": ocr_run_id,
        "char_count": len(raw_text),
        "confidence": confidence,
        "config": config_name,
        "preview": raw_text[:300],
    }
