"""
Admin Source Viewer — human review of scan vs OCR.
Scan is primary authority. Approve ≠ publish.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.sources import extract_page_image
from app.verification import get_audit_history, get_provenance
from app.review_api import (
    list_review_pages,
    get_page_review_bundle,
    set_verified_text,
    approve_text_unit,
    reject_text_unit,
    try_publish,
    update_text_meta,
    create_book,
    create_chapter,
    create_text_unit_manual,
    mark_needs_review,
    flag_text_unit,
    split_text_unit,
    merge_text_units,
)
from app.db import get_conn

app = FastAPI(title="Al-Muwaṭṭaʾ Source Viewer", docs_url="/api/docs")

VIEWER_HTML = open(ROOT / "admin_static" / "viewer.html", encoding="utf-8").read() if (ROOT / "admin_static" / "viewer.html").exists() else "<h1>viewer.html missing</h1>"


class VerifiedBody(BaseModel):
    text_id: str
    verified_arabic: str
    reason: str = "Human verification against scan"
    user_id: str = "reviewer"


class IdBody(BaseModel):
    text_id: str
    user_id: str = "reviewer"
    reason: str = ""


class RejectBody(BaseModel):
    text_id: str
    reason: str
    user_id: str = "reviewer"


class MetaBody(BaseModel):
    text_id: str
    text_type: Optional[str] = None
    book_id: Optional[str] = None
    chapter_id: Optional[str] = None
    hadith_number: Optional[str] = None
    clear_hadith_number: bool = False
    user_id: str = "reviewer"
    reason: str = ""


class CreateFromScanBody(BaseModel):
    volume: int
    pdf_page: int
    verified_arabic: str
    text_type: str = "needs_review"
    hadith_number: Optional[str] = None
    book_id: Optional[str] = None
    chapter_id: Optional[str] = None
    user_id: str = "reviewer"


class CreateBookBody(BaseModel):
    arabic_title: str
    volume_number: int = 1
    book_order: int = 1
    start_pdf_page: Optional[int] = None
    user_id: str = "reviewer"


class CreateChapterBody(BaseModel):
    book_id: str
    arabic_title: str
    chapter_order: int = 1
    start_pdf_page: Optional[int] = None
    volume_number: int = 1
    user_id: str = "reviewer"


class FlagBody(BaseModel):
    text_id: str
    flag: str
    reason: str = ""
    user_id: str = "reviewer"


class SplitBody(BaseModel):
    text_id: str
    parts: list[str]
    reason: str = ""
    user_id: str = "reviewer"


class MergeBody(BaseModel):
    text_ids: list[str]
    reason: str = ""
    user_id: str = "reviewer"


@app.get("/", response_class=HTMLResponse)
def home():
    return VIEWER_HTML


@app.get("/api/page-image")
def api_page_image(volume: int = Query(...), pdf_page: int = Query(...)):
    try:
        path = extract_page_image(volume, pdf_page, dpi=130)
    except Exception as e:
        raise HTTPException(500, str(e))
    return FileResponse(path, media_type="image/png")


@app.get("/api/review/pages")
def api_review_pages(volume: int = 1):
    return list_review_pages(volume)


@app.get("/api/review/page")
def api_review_page(volume: int = Query(...), pdf_page: int = Query(...)):
    data = get_page_review_bundle(volume, pdf_page)
    if not data:
        raise HTTPException(404, "Page not registered")
    return data


@app.post("/api/review/set-verified")
def api_set_verified(body: VerifiedBody):
    try:
        return set_verified_text(body.text_id, body.verified_arabic, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/approve")
def api_approve(body: IdBody):
    try:
        return approve_text_unit(body.text_id, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/reject")
def api_reject(body: RejectBody):
    try:
        return reject_text_unit(body.text_id, body.reason, body.user_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/needs-review")
def api_needs_review(body: IdBody):
    try:
        return mark_needs_review(body.text_id, body.reason or "needs_review", body.user_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/publish")
def api_publish(body: IdBody):
    try:
        return try_publish(body.text_id, body.user_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/meta")
def api_meta(body: MetaBody):
    try:
        return update_text_meta(
            body.text_id, body.text_type, body.book_id, body.chapter_id,
            body.hadith_number, body.clear_hadith_number, body.user_id, body.reason,
        )
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/create-from-scan")
def api_create_from_scan(body: CreateFromScanBody):
    try:
        text_id = create_text_unit_manual(
            body.volume, body.pdf_page, body.verified_arabic,
            body.text_type, body.hadith_number, body.book_id, body.chapter_id, body.user_id,
        )
        return {"text_id": text_id, "status": "verified"}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/create-book")
def api_create_book(body: CreateBookBody):
    try:
        book_id = create_book(
            body.arabic_title, body.volume_number, body.book_order,
            body.start_pdf_page, body.user_id,
        )
        return {"book_id": book_id}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/create-chapter")
def api_create_chapter(body: CreateChapterBody):
    try:
        chapter_id = create_chapter(
            body.book_id, body.arabic_title, body.chapter_order,
            body.start_pdf_page, body.volume_number, body.user_id,
        )
        return {"chapter_id": chapter_id}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/flag")
def api_flag(body: FlagBody):
    try:
        return flag_text_unit(body.text_id, body.flag, body.reason, body.user_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/split")
def api_split(body: SplitBody):
    try:
        return split_text_unit(body.text_id, body.parts, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/review/merge")
def api_merge(body: MergeBody):
    try:
        return merge_text_units(body.text_ids, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/audit/{entity_type}/{entity_id}")
def api_audit(entity_type: str, entity_id: str):
    return get_audit_history(entity_type, entity_id)


@app.get("/api/provenance/{text_id}")
def api_provenance(text_id: str):
    p = get_provenance(text_id)
    if not p:
        raise HTTPException(404, "not found")
    return p


@app.get("/api/stats")
def api_stats():
    with get_conn() as conn:
        return {
            "ocr_runs": conn.execute("SELECT COUNT(*) FROM ocr_runs").fetchone()[0],
            "text_units": conn.execute("SELECT COUNT(*) FROM text_units").fetchone()[0],
            "books": conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            "chapters": conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
            "editorial": conn.execute("SELECT COUNT(*) FROM editorial_notes").fetchone()[0],
            "needs_review": conn.execute(
                "SELECT COUNT(*) FROM text_units WHERE verification_status='needs_review'"
            ).fetchone()[0],
            "verified": conn.execute(
                "SELECT COUNT(*) FROM text_units WHERE verification_status='verified'"
            ).fetchone()[0],
            "approved": conn.execute(
                "SELECT COUNT(*) FROM text_units WHERE verification_status='approved'"
            ).fetchone()[0],
            "published": conn.execute(
                "SELECT COUNT(*) FROM text_units WHERE published=1"
            ).fetchone()[0],
            "open_tasks": conn.execute(
                "SELECT COUNT(*) FROM verification_tasks WHERE status='open'"
            ).fetchone()[0],
        }


class ProposalAction(BaseModel):
    proposal_id: str
    user_id: str = "reviewer"
    reason: str = ""
    new_type: Optional[str] = None
    cut_offset: Optional[int] = None
    proposal_ids: Optional[list[str]] = None
    expected_version: Optional[int] = None
    flag: Optional[str] = None
    to_proposal_id: Optional[str] = None


@app.get("/proposals", response_class=HTMLResponse)
def proposals_ui():
    path = ROOT / "admin_static" / "proposals.html"
    if not path.exists():
        raise HTTPException(404, "proposals.html missing")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/proposals")
def api_list_proposals(volume: int = 1, pdf_page: int = Query(..., ge=1)):
    with get_conn(readonly=True) as conn:
        page = conn.execute(
            """
            SELECT sp.source_page_id, v.volume_number, sp.pdf_page_number, sp.verification_status
            FROM source_pages sp JOIN volumes v ON v.volume_id = sp.volume_id
            WHERE v.volume_number=? AND sp.pdf_page_number=?
            """,
            (volume, pdf_page),
        ).fetchone()
        if not page:
            raise HTTPException(404, "page not found")
        runs = [dict(r) for r in conn.execute(
            "SELECT ocr_run_id, ocr_engine, ocr_timestamp, storage_path, length(ocr_output_raw) AS chars FROM ocr_runs WHERE source_page_id=? ORDER BY ocr_timestamp ASC",
            (page["source_page_id"],),
        ).fetchall()]
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='segmentation_proposals'"
        ).fetchone()
        props = []
        if has:
            props = [dict(r) for r in conn.execute(
                "SELECT * FROM segmentation_proposals WHERE source_page_id=? ORDER BY start_offset",
                (page["source_page_id"],),
            ).fetchall()]
        original = next((r for r in runs if not str(r.get("ocr_run_id","")).startswith("ocr-review-")), runs[0] if runs else None)
        raw = None
        if original:
            raw_row = conn.execute("SELECT ocr_output_raw FROM ocr_runs WHERE ocr_run_id=?", (original["ocr_run_id"],)).fetchone()
            raw = raw_row["ocr_output_raw"] if raw_row else None
        return {
            "page": dict(page),
            "ocr_runs": runs,
            "ocr_used": original,
            "ocr": {"ocr_run_id": original["ocr_run_id"], "ocr_output_raw": raw} if original else None,
            "proposals": props,
            "note": "Raw OCR below is the original run unless review is chosen explicitly. Detector score is not source reliability. Accepted is not verified.",
        }


@app.post("/api/proposals/accept")
def api_pr_accept(body: ProposalAction):
    from app.proposals import accept_proposal
    try:
        return accept_proposal(body.proposal_id, body.user_id, body.reason, expected_version=body.expected_version)
    except Exception as e:
        from app.proposals import ConflictError
        if isinstance(e, ConflictError):
            raise HTTPException(409, str(e))
        raise HTTPException(400, str(e))


@app.post("/api/proposals/reject")
def api_pr_reject(body: ProposalAction):
    from app.proposals import reject_proposal
    try:
        return reject_proposal(body.proposal_id, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/proposals/reclassify")
def api_pr_reclass(body: ProposalAction):
    from app.proposals import reclassify_proposal
    if not body.new_type:
        raise HTTPException(400, "new_type required")
    try:
        return reclassify_proposal(body.proposal_id, body.new_type, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/proposals/unknown")
def api_pr_unknown(body: ProposalAction):
    from app.proposals import mark_unknown
    try:
        return mark_unknown(body.proposal_id, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/proposals/editorial")
def api_pr_editorial(body: ProposalAction):
    from app.proposals import mark_editorial
    try:
        return mark_editorial(body.proposal_id, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/proposals/flag")
def api_pr_flag(body: ProposalAction):
    from app.proposals import flag_proposal
    try:
        return flag_proposal(body.proposal_id, body.user_id, body.reason or "flag")
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/proposals/split")
def api_pr_split(body: ProposalAction):
    from app.proposals import split_proposal
    if body.cut_offset is None:
        raise HTTPException(400, "cut_offset required")
    try:
        return split_proposal(body.proposal_id, body.cut_offset, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/proposals/merge")
def api_pr_merge(body: ProposalAction):
    from app.proposals import merge_proposals
    ids = body.proposal_ids or []
    try:
        return merge_proposals(ids, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/proposals/materialize")
def api_pr_materialize(body: ProposalAction):
    from app.proposals import materialize_accepted_proposal
    try:
        return materialize_accepted_proposal(body.proposal_id, body.user_id, body.reason)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/review", response_class=HTMLResponse)
def review_workstation():
    path = ROOT / "admin_static" / "review_workstation.html"
    if not path.exists():
        raise HTTPException(404, "review_workstation.html missing")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/review/page")
def api_review_page(volume: int = 1, pdf_page: int = Query(..., ge=1), ocr_role: str = "original", ocr_run_id: str | None = None):
    from app.review_workstation import page_bundle
    try:
        return page_bundle(volume, pdf_page, ocr_run_id=ocr_run_id, ocr_role=ocr_role)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/review/progress")
def api_review_progress():
    from app.review_workstation import progress_report
    return progress_report()


@app.get("/api/proposals/{proposal_id}/audit")
def api_proposal_audit(proposal_id: str):
    return get_audit_history("segmentation_proposal", proposal_id)


@app.post("/api/proposals/link")
def api_proposal_link(body: ProposalAction):
    from app.review_workstation import link_continues_to
    if not body.to_proposal_id:
        raise HTTPException(400, "to_proposal_id required")
    try:
        return link_continues_to(body.proposal_id, body.to_proposal_id, body.user_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/scan/{volume}/{pdf_page}")
def api_scan(volume: int, pdf_page: int):
    try:
        path = extract_page_image(volume, pdf_page, dpi=110)
    except Exception:
        raise HTTPException(404, "scan not available")
    if not path or not Path(path).exists():
        raise HTTPException(404, "scan not available")
    return FileResponse(path)


@app.get("/reader", response_class=HTMLResponse)
def reader_page():
    path = ROOT / "admin_static" / "reader.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/pilot", response_class=HTMLResponse)
def pilot_page():
    path = ROOT / "admin_static" / "pilot_status.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/reader")
def api_reader(volume: int = 1, preview: int = 0):
    from app.reader import reader_tree
    return reader_tree(preview=bool(preview), volume=volume)


@app.get("/api/pilot/status")
def api_pilot_status():
    from app.reader import pilot_status
    return pilot_status(1, range(33, 43))
