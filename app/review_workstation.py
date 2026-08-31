"""Human review workstation helpers. No canonical writes. No publish."""
from __future__ import annotations

from typing import Optional

from .db import get_conn, audit, new_id, utcnow
from .proposals import (
    ensure_proposal_schema,
    select_ocr_run,
    excerpt_from_raw,
    PROPOSED_TYPES,
)

FLAGS = (
    "ocr_suspect",
    "editorial_uncertain",
    "boundary_uncertain",
    "type_uncertain",
    "needs_source_check",
    "possible_cross_page",
    "other",
)

FULLY_REVIEWED = (
    "A page is fully_reviewed only if it is not expected_blank, has at least one "
    "proposal, and every non-superseded proposal is accepted or rejected "
    "(unknown/editorial still needs_review until accepted/rejected)."
)


def ensure_workstation_schema() -> None:
    ensure_proposal_schema()
    with get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(segmentation_proposals)").fetchall()}
        if cols and "lock_version" not in cols:
            conn.execute("ALTER TABLE segmentation_proposals ADD COLUMN lock_version INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proposal_flags (
                flag_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                flag TEXT NOT NULL,
                note TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proposal_links (
                link_id TEXT PRIMARY KEY,
                from_proposal_id TEXT NOT NULL,
                to_proposal_id TEXT NOT NULL,
                link_type TEXT NOT NULL,
                confirmed INTEGER NOT NULL DEFAULT 0,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(from_proposal_id, to_proposal_id, link_type)
            )
            """
        )


def require_lock(conn, proposal_id: str, expected_version: Optional[int]) -> dict:
    row = conn.execute("SELECT * FROM segmentation_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("proposal not found")
    row = dict(row)
    if expected_version is not None and int(row.get("lock_version") or 1) != int(expected_version):
        raise ConflictError("proposal changed; reload and retry")
    return row


class ConflictError(RuntimeError):
    pass


def bump_lock(conn, proposal_id: str) -> None:
    conn.execute(
        "UPDATE segmentation_proposals SET lock_version = COALESCE(lock_version,1)+1, updated_at=? WHERE proposal_id=?",
        (utcnow(), proposal_id),
    )


def add_flag(proposal_id: str, flag: str, note: str = "", user_id: str = "reviewer", expected_version: Optional[int] = None) -> dict:
    if flag not in FLAGS:
        raise ValueError("invalid flag")
    ensure_workstation_schema()
    with get_conn() as conn:
        require_lock(conn, proposal_id, expected_version)
        fid = new_id("flg-")
        conn.execute(
            "INSERT INTO proposal_flags (flag_id, proposal_id, flag, note, created_by, created_at) VALUES (?,?,?,?,?,?)",
            (fid, proposal_id, flag, note, user_id, utcnow()),
        )
        bump_lock(conn, proposal_id)
        audit(conn, "segmentation_proposal", proposal_id, "flag", new_value={"flag": flag, "note": note}, user_id=user_id, reason=note)
        return {"flag_id": fid, "flag": flag}


def link_continues_to(from_id: str, to_id: str, user_id: str = "reviewer", confirmed: bool = True) -> dict:
    """Human-confirmed continuation. No automatic text merge."""
    ensure_workstation_schema()
    with get_conn() as conn:
        a = conn.execute("SELECT * FROM segmentation_proposals WHERE proposal_id=?", (from_id,)).fetchone()
        b = conn.execute("SELECT * FROM segmentation_proposals WHERE proposal_id=?", (to_id,)).fetchone()
        if not a or not b:
            raise ValueError("proposal not found")
        if a["source_page_id"] == b["source_page_id"]:
            raise ValueError("cross-page link requires two different source pages")
        if a["ocr_run_id"] == b["ocr_run_id"]:
            raise ValueError("cross-page link must use distinct page OCR runs")
        lid = new_id("lnk-")
        conn.execute(
            """INSERT INTO proposal_links (link_id, from_proposal_id, to_proposal_id, link_type, confirmed, created_by, created_at)
               VALUES (?, ?, ?, 'continues_to', ?, ?, ?)""",
            (lid, from_id, to_id, 1 if confirmed else 0, user_id, utcnow()),
        )
        conn.execute(
            "UPDATE segmentation_proposals SET continues_to_page_id=? WHERE proposal_id=?",
            (b["source_page_id"], from_id),
        )
        audit(conn, "segmentation_proposal", from_id, "continues_to",
              new_value={"to": to_id, "to_page": b["source_page_id"]}, user_id=user_id, reason="cross-page link")
        return {"link_id": lid, "from": from_id, "to": to_id}


def page_bundle(volume: int, pdf_page: int, ocr_run_id: Optional[str] = None, ocr_role: str = "original") -> dict:
    ensure_workstation_schema()
    with get_conn(readonly=True) as conn:
        page = conn.execute(
            """
            SELECT sp.*, v.volume_number
            FROM source_pages sp JOIN volumes v ON v.volume_id=sp.volume_id
            WHERE v.volume_number=? AND sp.pdf_page_number=?
            """,
            (volume, pdf_page),
        ).fetchone()
        if not page:
            raise ValueError("page not found")
        page = dict(page)
        runs = [dict(r) for r in conn.execute(
            """SELECT ocr_run_id, ocr_engine, ocr_model, ocr_timestamp, storage_path,
                      length(ocr_output_raw) AS chars, ocr_confidence
               FROM ocr_runs WHERE source_page_id=? ORDER BY ocr_timestamp ASC""",
            (page["source_page_id"],),
        ).fetchall()]
        selected = select_ocr_run(page["source_page_id"], ocr_run_id=ocr_run_id, role=ocr_role)
        raw_row = conn.execute("SELECT ocr_output_raw FROM ocr_runs WHERE ocr_run_id=?", (selected["ocr_run_id"],)).fetchone()
        raw = raw_row["ocr_output_raw"] if raw_row else ""
        props = [dict(r) for r in conn.execute(
            "SELECT * FROM segmentation_proposals WHERE source_page_id=? ORDER BY start_offset",
            (page["source_page_id"],),
        ).fetchall()]
        for pr in props:
            pr["flags"] = [dict(f) for f in conn.execute(
                "SELECT * FROM proposal_flags WHERE proposal_id=?", (pr["proposal_id"],)
            ).fetchall()]
            pr["links"] = [dict(f) for f in conn.execute(
                "SELECT * FROM proposal_links WHERE from_proposal_id=? OR to_proposal_id=?",
                (pr["proposal_id"], pr["proposal_id"]),
            ).fetchall()]
        unresolved = [p for p in props if p["proposal_status"] == "needs_review"]
        return {
            "page": page,
            "ocr_runs": runs,
            "ocr_selected": {
                "ocr_run_id": selected["ocr_run_id"],
                "role": ocr_role if not ocr_run_id else "explicit",
                "chars": len(raw or ""),
                "ocr_output_raw": raw,
            },
            "proposals": props,
            "unresolved_count": len(unresolved),
            "labels": {
                "ocr": "RAW OCR (immutable)",
                "proposal": "MACHINE PROPOSAL (needs_review until human acts)",
                "confidence": "Machine confidence — not source reliability",
                "accepted": "Accepted segmentation ≠ verified Arabic",
            },
            "fully_reviewed_definition": FULLY_REVIEWED,
        }


def is_fully_reviewed(source_page_id: str) -> bool:
    with get_conn(readonly=True) as conn:
        page = conn.execute("SELECT blank_status FROM source_pages WHERE source_page_id=?", (source_page_id,)).fetchone()
        if not page:
            return False
        if page["blank_status"] == "expected_blank":
            return False
        props = list(conn.execute(
            "SELECT proposal_status FROM segmentation_proposals WHERE source_page_id=? AND proposal_status != 'superseded'",
            (source_page_id,),
        ))
        if not props:
            return False
        return all(p["proposal_status"] in ("accepted", "rejected") for p in props)


def progress_report() -> dict:
    ensure_workstation_schema()
    with get_conn(readonly=True) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {"fully_reviewed_definition": FULLY_REVIEWED}
        if "segmentation_proposals" not in tables:
            out.update({"total_proposals": 0, "note": "no proposals table yet"})
            return out
        def cnt(q, params=()):
            return conn.execute(q, params).fetchone()[0]
        out["total_proposals"] = cnt("SELECT COUNT(*) FROM segmentation_proposals")
        for st in ("needs_review", "accepted", "rejected", "superseded"):
            out[st] = cnt("SELECT COUNT(*) FROM segmentation_proposals WHERE proposal_status=?", (st,))
        out["unknown"] = cnt("SELECT COUNT(*) FROM segmentation_proposals WHERE proposed_type='unknown'")
        out["editorial"] = cnt(
            "SELECT COUNT(*) FROM segmentation_proposals WHERE proposed_type IN ('editorial_note','editorial_candidate','footnote')"
        )
        out["flagged"] = cnt("SELECT COUNT(DISTINCT proposal_id) FROM proposal_flags") if "proposal_flags" in tables else 0
        pages = list(conn.execute("SELECT source_page_id, blank_status FROM source_pages"))
        untouched = partial = full = 0
        for pg in pages:
            n = cnt("SELECT COUNT(*) FROM segmentation_proposals WHERE source_page_id=?", (pg["source_page_id"],))
            if n == 0:
                untouched += 1
            elif is_fully_reviewed(pg["source_page_id"]):
                full += 1
            else:
                partial += 1
        out["pages_untouched"] = untouched
        out["pages_partially_reviewed"] = partial
        out["pages_fully_reviewed"] = full
        return out


def parse_page_range(spec: str) -> list[int]:
    """33-42 or 33,34,40"""
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi or hi - lo > 200:
                raise ValueError("unsafe page range")
            pages.extend(range(lo, hi + 1))
        else:
            pages.append(int(part))
    return pages


def should_skip_page(volume: int, pdf_page: int) -> tuple[bool, str]:
    with get_conn(readonly=True) as conn:
        row = conn.execute(
            """
            SELECT sp.blank_status, sp.verification_status
            FROM source_pages sp JOIN volumes v ON v.volume_id=sp.volume_id
            WHERE v.volume_number=? AND sp.pdf_page_number=?
            """,
            (volume, pdf_page),
        ).fetchone()
        if not row:
            return True, "page not registered"
        if row["blank_status"] == "expected_blank":
            return True, "expected_blank"
        return False, ""
