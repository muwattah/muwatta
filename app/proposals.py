"""
Segmentation proposals: machine suggestions only.

Raw OCR is never modified. Proposals start as needs_review.
Accept does not publish. Materialization is explicit and capped below published.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

class ConflictError(RuntimeError):
    pass

from .db import get_conn, audit, new_id, utcnow, ensure_review_columns

GENERATOR = "regex_v1"
GENERATOR_VERSION = "1"

PROPOSED_TYPES = (
    "book_heading",
    "chapter_heading",
    "hadith",
    "athar",
    "editorial_note",
    "footnote",
    "page_header",
    "page_number",
    "unknown",
    "editorial_candidate",
)

RE_KITAB = re.compile(r"كتاب[\s\u200f\u200e]*[\u0600-\u06FF]{0,80}")
RE_BAB = re.compile(r"باب[\s\u200f\u200e]*[\u0600-\u06FF]{0,80}")
RE_FOOTNOTE_MARK = re.compile(r"[\(\[]\s*[٠-٩0-9]{1,3}\s*[\)\]]")
RE_SEE = re.compile(r"انظر")
RE_PAGE_NUM = re.compile(r"^\s*[٠-٩0-9]{1,4}\s*$")
RE_HADITH_OPEN = re.compile(r"(حدّ?ثني|حدّ?ثنا|أخبرنا)")


def proposal_table_exists() -> bool:
    with get_conn(readonly=True) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='segmentation_proposals'"
        ).fetchone()
        return row is not None


def select_ocr_run(source_page_id: str, *, ocr_run_id: str | None = None, role: str = "original"):
    """Never silently pick review-OCR as canonical detector input."""
    with get_conn(readonly=True) as conn:
        if ocr_run_id:
            row = conn.execute(
                "SELECT * FROM ocr_runs WHERE ocr_run_id=? AND source_page_id=?",
                (ocr_run_id, source_page_id),
            ).fetchone()
            if not row:
                raise ValueError("ocr_run_id not found on this page")
            return dict(row)
        rows = list(conn.execute(
            "SELECT * FROM ocr_runs WHERE source_page_id=? ORDER BY ocr_timestamp ASC",
            (source_page_id,),
        ))
        if not rows:
            raise ValueError("no OCR runs for page")
        def is_review(r):
            rid = r["ocr_run_id"] or ""
            path = (r["storage_path"] or "").replace("\\", "/")
            return rid.startswith("ocr-review-") or "/ocr_review/" in path or path.startswith("storage/ocr_review")
        originals = [r for r in rows if not is_review(r)]
        reviews = [r for r in rows if is_review(r)]
        if role == "review":
            if not reviews:
                raise ValueError("no review OCR run on this page; pass --ocr-run-id")
            if len(reviews) > 1:
                raise ValueError("multiple review OCR runs; pass --ocr-run-id")
            return dict(reviews[0])
        if role != "original":
            raise ValueError("role must be original or review")
        if not originals:
            raise ValueError("no original OCR run; pass --ocr-run-id to use a specific run")
        if len(originals) > 1:
            # stable: earliest original timestamp, never latest-wins
            return dict(originals[0])
        return dict(originals[0])


def ensure_proposal_schema() -> None:
    """Additive migration only. Never drops or resets existing rows."""
    ensure_review_columns()
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS segmentation_proposals (
                proposal_id TEXT PRIMARY KEY,
                source_page_id TEXT NOT NULL,
                ocr_run_id TEXT,
                start_offset INTEGER,
                end_offset INTEGER,
                raw_excerpt TEXT,
                proposed_type TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                reason TEXT,
                evidence TEXT,
                proposal_status TEXT NOT NULL DEFAULT 'needs_review',
                generator TEXT NOT NULL DEFAULT 'regex_v1',
                generator_version TEXT NOT NULL DEFAULT '1',
                content_hash TEXT,
                parent_proposal_id TEXT,
                continues_to_page_id TEXT,
                materialized_text_id TEXT,
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_segprop_page ON segmentation_proposals(source_page_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_segprop_run ON segmentation_proposals(ocr_run_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_segprop_status ON segmentation_proposals(proposal_status)"
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(segmentation_proposals)").fetchall()}
        if "lock_version" not in cols:
            conn.execute("ALTER TABLE segmentation_proposals ADD COLUMN lock_version INTEGER NOT NULL DEFAULT 1")


def _span_hash(ocr_run_id: str, start: int, end: int, ptype: str) -> str:
    raw = f"{GENERATOR}:{GENERATOR_VERSION}|{ocr_run_id}|{start}|{end}|{ptype}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def excerpt_from_raw(raw: str, start: int, end: int) -> str:
    if raw is None:
        return ""
    if start is None or end is None:
        return ""
    if start < 0 or end > len(raw) or start > end:
        raise ValueError("invalid raw OCR span")
    return raw[start:end]


def detect_proposals(raw: str) -> list[dict]:
    """Return detector hits with offsets into the unmodified raw string."""
    if not raw:
        return []
    hits: list[dict] = []
    n = len(raw)
    # Bottom-of-page apparatus heuristic only on realistically long page OCR.
    bottom = int(n * 0.72) if n >= 400 else n + 1

    def add(start: int, end: int, ptype: str, confidence: float, reason: str, evidence: str) -> None:
        excerpt = raw[start:end]
        hits.append({
            "start_offset": start,
            "end_offset": end,
            "raw_excerpt": excerpt,
            "proposed_type": ptype,
            "confidence": confidence,
            "reason": reason,
            "evidence": evidence,
        })

    for m in RE_KITAB.finditer(raw):
        start, end = m.start(), m.end()
        window = raw[max(0, start - 24): min(len(raw), end + 24)]
        in_apparatus = start >= bottom or (n >= 400 and RE_SEE.search(window))
        if in_apparatus:
            add(start, end, "editorial_candidate", 0.35, "kitab-like token in apparatus/bottom context", m.group(0)[:80])
        else:
            add(start, end, "book_heading", 0.45, "visible كتاب sequence in raw OCR", m.group(0)[:80])

    for m in RE_BAB.finditer(raw):
        start, end = m.start(), m.end()
        nearby = raw[max(0, start - 12):end + 12]
        in_apparatus = start >= bottom and RE_FOOTNOTE_MARK.search(nearby)
        if in_apparatus:
            add(start, end, "editorial_candidate", 0.32, "bab-like token near footnote marks", m.group(0)[:80])
        else:
            add(start, end, "chapter_heading", 0.42, "visible باب sequence in raw OCR", m.group(0)[:80])

    for m in RE_FOOTNOTE_MARK.finditer(raw):
        add(m.start(), m.end(), "footnote", 0.4, "parenthetical digit mark typical of apparatus", m.group(0))

    for m in RE_SEE.finditer(raw):
        add(m.start(), m.end(), "editorial_note", 0.4, "انظر marker often editorial", m.group(0))

    for i, line in enumerate(raw.splitlines(keepends=True)):
        # reconstruct offset
        pass

    offset = 0
    lines = raw.split("\n")
    for line in lines:
        stripped = line.strip()
        start = offset
        end = offset + len(line)
        if RE_PAGE_NUM.match(stripped) and len(stripped) <= 4:
            add(start, start + len(line), "page_number", 0.3, "line is only digits", stripped)
        elif RE_HADITH_OPEN.search(line) and start < bottom:
            add(start, end if end > start else start + len(line), "hadith", 0.28, "isnad-opening verb visible; still needs_review", line[:80])
        offset += len(line) + 1

    if not hits:
        add(0, min(len(raw), 80), "unknown", 0.1, "no conservative pattern matched", raw[:80])
    return hits


def create_proposals_for_ocr_run(ocr_run_id: str, *, write: bool = False) -> dict:
    ensure_proposal_schema()
    with get_conn() as conn:
        run = conn.execute(
            "SELECT ocr_run_id, source_page_id, ocr_output_raw FROM ocr_runs WHERE ocr_run_id = ?",
            (ocr_run_id,),
        ).fetchone()
        if not run:
            raise ValueError(f"ocr_run not found: {ocr_run_id}")
        raw = run["ocr_output_raw"] or ""
        page_id = run["source_page_id"]
        blank = conn.execute("SELECT blank_status FROM source_pages WHERE source_page_id=?", (page_id,)).fetchone()
        if blank and blank["blank_status"] == "expected_blank":
            return {"skipped": "expected_blank", "ocr_run_id": ocr_run_id, "source_page_id": page_id, "created": 0}
        hits = detect_proposals(raw)
        created = 0
        skipped = 0
        out = []
        if not write:
            return {"dry_run": True, "ocr_run_id": ocr_run_id, "source_page_id": page_id, "proposals": hits, "count": len(hits)}
        for hit in hits:
            excerpt = excerpt_from_raw(raw, hit["start_offset"], hit["end_offset"])
            if excerpt != hit["raw_excerpt"]:
                raise RuntimeError("span/excerpt mismatch; refusing write")
            ch = _span_hash(ocr_run_id, hit["start_offset"], hit["end_offset"], hit["proposed_type"])
            existing = conn.execute(
                "SELECT proposal_id FROM segmentation_proposals WHERE content_hash = ? AND proposal_status != 'superseded'",
                (ch,),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            pid = new_id("prp-")
            conn.execute(
                """
                INSERT INTO segmentation_proposals (
                    proposal_id, source_page_id, ocr_run_id, start_offset, end_offset,
                    raw_excerpt, proposed_type, confidence, reason, evidence,
                    proposal_status, generator, generator_version, content_hash, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?, 'needs_review', ?, ?, ?, ?, ?)
                """,
                (
                    pid, page_id, ocr_run_id, hit["start_offset"], hit["end_offset"],
                    excerpt, hit["proposed_type"], hit["confidence"], hit["reason"], hit["evidence"],
                    GENERATOR, GENERATOR_VERSION, ch, utcnow(), utcnow(),
                ),
            )
            audit(conn, "segmentation_proposal", pid, "create", new_value=hit, reason="detector write")
            created += 1
            out.append(pid)
        return {"dry_run": False, "created": created, "skipped_duplicate": skipped, "proposal_ids": out}


def _get(conn, proposal_id: str):
    row = conn.execute("SELECT * FROM segmentation_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError(f"proposal not found: {proposal_id}")
    return row


def _set_status(proposal_id: str, status: str, user_id: str, reason: str, extra: Optional[dict] = None) -> dict:
    ensure_proposal_schema()
    extra = extra or {}
    with get_conn() as conn:
        row = _get(conn, proposal_id)
        old = dict(row)
        expected_version = extra.pop("expected_version", None) if extra else None
        if expected_version is not None and int(old.get("lock_version") or 1) != int(expected_version):
            raise ConflictError("proposal changed; reload and retry")
        if old["proposal_status"] == "superseded" and status != "superseded":
            raise ValueError("superseded proposal cannot change status")
        if old["proposal_status"] == "rejected" and status == "accepted":
            raise ValueError("rejected proposal cannot be accepted directly; reclassify first")
        if old["materialized_text_id"] and extra.get("proposed_type"):
            raise ValueError("cannot reclassify a materialized proposal")
        fields = ["proposal_status = ?", "reviewed_by = ?", "reviewed_at = ?", "review_reason = ?", "updated_at = ?"]
        params = [status, user_id, utcnow(), reason, utcnow()]
        if "proposed_type" in extra:
            fields.append("proposed_type = ?")
            params.append(extra["proposed_type"])
        params.append(proposal_id)
        fields.append("lock_version = COALESCE(lock_version,1) + 1")
        conn.execute(f"UPDATE segmentation_proposals SET {', '.join(fields)} WHERE proposal_id = ?", params)
        audit(
            conn, "segmentation_proposal", proposal_id, status,
            old_value={"status": old["proposal_status"], "type": old["proposed_type"]},
            new_value={"status": status, **extra},
            user_id=user_id, reason=reason,
        )
        return dict(_get(conn, proposal_id))


def accept_proposal(proposal_id: str, user_id: str = "reviewer", reason: str = "", expected_version: int | None = None) -> dict:
    """Accept = human agrees the span/type is a useful segmentation hypothesis.
    Does NOT publish and does NOT create canonical units."""
    return _set_status(proposal_id, "accepted", user_id, reason or "accepted", extra={"expected_version": expected_version})


def reject_proposal(proposal_id: str, user_id: str = "reviewer", reason: str = "", expected_version: int | None = None) -> dict:
    return _set_status(proposal_id, "rejected", user_id, reason or "rejected", extra={"expected_version": expected_version})


def reclassify_proposal(proposal_id: str, new_type: str, user_id: str = "reviewer", reason: str = "") -> dict:
    if new_type not in PROPOSED_TYPES:
        raise ValueError(f"invalid type: {new_type}")
    return _set_status(proposal_id, "needs_review", user_id, reason or "reclassify", extra={"proposed_type": new_type})


def mark_unknown(proposal_id: str, user_id: str = "reviewer", reason: str = "") -> dict:
    return reclassify_proposal(proposal_id, "unknown", user_id, reason or "mark unknown")


def mark_editorial(proposal_id: str, user_id: str = "reviewer", reason: str = "") -> dict:
    return reclassify_proposal(proposal_id, "editorial_candidate", user_id, reason or "mark editorial")


def flag_proposal(proposal_id: str, user_id: str = "reviewer", reason: str = "flag") -> dict:
    ensure_proposal_schema()
    with get_conn() as conn:
        _get(conn, proposal_id)
        audit(conn, "segmentation_proposal", proposal_id, "flag", new_value={"reason": reason}, user_id=user_id, reason=reason)
    return _set_status(proposal_id, "needs_review", user_id, reason)


def split_proposal(proposal_id: str, cut_offset: int, user_id: str = "reviewer", reason: str = "split") -> dict:
    """cut_offset is absolute offset in the same raw OCR string."""
    ensure_proposal_schema()
    with get_conn() as conn:
        row = _get(conn, proposal_id)
        start, end = row["start_offset"], row["end_offset"]
        if cut_offset <= start or cut_offset >= end:
            raise ValueError("cut_offset must lie inside the proposal span")
        raw_row = conn.execute("SELECT ocr_output_raw FROM ocr_runs WHERE ocr_run_id = ?", (row["ocr_run_id"],)).fetchone()
        raw = raw_row["ocr_output_raw"] if raw_row else ""
        left = excerpt_from_raw(raw, start, cut_offset)
        right = excerpt_from_raw(raw, cut_offset, end)
        conn.execute(
            "UPDATE segmentation_proposals SET proposal_status='superseded', updated_at=?, review_reason=? WHERE proposal_id=?",
            (utcnow(), reason, proposal_id),
        )
        ids = []
        for a, b, ex in ((start, cut_offset, left), (cut_offset, end, right)):
            pid = new_id("prp-")
            ch = _span_hash(row["ocr_run_id"], a, b, row["proposed_type"])
            conn.execute(
                """
                INSERT INTO segmentation_proposals (
                    proposal_id, source_page_id, ocr_run_id, start_offset, end_offset,
                    raw_excerpt, proposed_type, confidence, reason, evidence,
                    proposal_status, generator, generator_version, content_hash,
                    parent_proposal_id, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?, 'needs_review', ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid, row["source_page_id"], row["ocr_run_id"], a, b, ex,
                    row["proposed_type"], row["confidence"], "split child", reason,
                    GENERATOR, GENERATOR_VERSION, ch, proposal_id, utcnow(), utcnow(),
                ),
            )
            ids.append(pid)
        audit(conn, "segmentation_proposal", proposal_id, "split",
              old_value={"id": proposal_id}, new_value={"children": ids}, user_id=user_id, reason=reason)
        return {"parent": proposal_id, "children": ids}


def merge_proposals(proposal_ids: list[str], user_id: str = "reviewer", reason: str = "merge") -> dict:
    if len(proposal_ids) < 2:
        raise ValueError("merge needs at least two proposals")
    ensure_proposal_schema()
    with get_conn() as conn:
        rows = [_get(conn, pid) for pid in proposal_ids]
        page_ids = {r["source_page_id"] for r in rows}
        runs = {r["ocr_run_id"] for r in rows}
        if len(page_ids) != 1 or len(runs) != 1:
            # Multi-page merge is allowed as a link only, not auto-joined text.
            raise ValueError("V1 merge requires same page and same ocr_run; use continues_to_page_id for cross-page")
        start = min(r["start_offset"] for r in rows)
        end = max(r["end_offset"] for r in rows)
        raw = conn.execute("SELECT ocr_output_raw FROM ocr_runs WHERE ocr_run_id=?", (rows[0]["ocr_run_id"],)).fetchone()["ocr_output_raw"]
        excerpt = excerpt_from_raw(raw, start, end)
        pid = new_id("prp-")
        ch = _span_hash(rows[0]["ocr_run_id"], start, end, "unknown") + ":merge:" + utcnow()
        conn.execute(
            """
            INSERT INTO segmentation_proposals (
                proposal_id, source_page_id, ocr_run_id, start_offset, end_offset,
                raw_excerpt, proposed_type, confidence, reason, evidence,
                proposal_status, generator, generator_version, content_hash, created_at, updated_at
            ) VALUES (?,?,?,?,?,?, 'unknown', 0.2, ?, ?, 'needs_review', ?, ?, ?, ?, ?)
            """,
            (pid, rows[0]["source_page_id"], rows[0]["ocr_run_id"], start, end, excerpt,
             reason, "merge", GENERATOR, GENERATOR_VERSION, ch, utcnow(), utcnow()),
        )
        for r in rows:
            conn.execute(
                "UPDATE segmentation_proposals SET proposal_status='superseded', parent_proposal_id=?, updated_at=? WHERE proposal_id=?",
                (pid, utcnow(), r["proposal_id"]),
            )
        audit(conn, "segmentation_proposal", pid, "merge",
              old_value={"sources": proposal_ids}, new_value={"merged": pid}, user_id=user_id, reason=reason)
        return {"merged_id": pid, "sources": proposal_ids}


def materialize_accepted_proposal(proposal_id: str, user_id: str = "reviewer", reason: str = "materialize") -> dict:
    """Create a canonical text_unit at needs_review. Never published. Never verified."""
    ensure_proposal_schema()
    with get_conn() as conn:
        row = _get(conn, proposal_id)
        if row["proposal_status"] != "accepted":
            raise ValueError("only accepted proposals can be materialized")
        if row["materialized_text_id"]:
            return {"text_id": row["materialized_text_id"], "duplicate": True}
        page = conn.execute("SELECT * FROM source_pages WHERE source_page_id=?", (row["source_page_id"],)).fetchone()
        if not page:
            raise ValueError("source page missing")
        text_id = new_id("txt-")
        type_map = {
            "book_heading": "heading",
            "chapter_heading": "heading",
            "hadith": "hadith",
            "athar": "athar",
            "editorial_note": "editorial",
            "editorial_candidate": "editorial",
            "footnote": "editorial",
            "unknown": "unknown",
        }
        ttype = type_map.get(row["proposed_type"], "unknown")
        conn.execute(
            """
            INSERT INTO text_units (
                text_id, edition_id, volume_id, source_page_id, text_type,
                arabic_text_raw, arabic_text_verified, pdf_page, verification_status, published, is_test
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'needs_review', 0, 0)
            """,
            (
                text_id,
                page["edition_id"],
                page["volume_id"],
                row["source_page_id"],
                ttype,
                row["raw_excerpt"] or "",
                page["pdf_page_number"],
            ),
        )
        conn.execute(
            """
            INSERT INTO text_unit_source_pages (id, text_id, source_page_id, page_role, sequence_order)
            VALUES (?, ?, ?, 'only', 1)
            """,
            (new_id("tsp-"), text_id, row["source_page_id"]),
        )
        conn.execute(
            "UPDATE segmentation_proposals SET materialized_text_id=?, updated_at=? WHERE proposal_id=?",
            (text_id, utcnow(), proposal_id),
        )
        audit(conn, "segmentation_proposal", proposal_id, "materialize",
              new_value={"text_id": text_id, "published": 0, "verification_status": "needs_review"},
              user_id=user_id, reason=reason)
        unit = conn.execute("SELECT published, verification_status FROM text_units WHERE text_id=?", (text_id,)).fetchone()
        if unit["published"] == 1 or unit["verification_status"] in ("approved", "published"):
            raise RuntimeError("materialize produced forbidden status")
        return {"text_id": text_id, "verification_status": "needs_review", "published": 0}


def summarize_hits(hits: list[dict]) -> dict:
    from collections import Counter
    c = Counter(h["proposed_type"] for h in hits)
    return {
        "total": len(hits),
        "by_type": dict(c),
        "headings": c["book_heading"] + c["chapter_heading"],
        "editorial": c["editorial_note"] + c["footnote"] + c["editorial_candidate"],
        "unknown": c["unknown"],
    }
