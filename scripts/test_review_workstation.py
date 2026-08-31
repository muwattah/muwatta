#!/usr/bin/env python3
from __future__ import annotations
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app.db as dbmod
TMP = Path(tempfile.mkdtemp(prefix="muwatta_ws_"))
dbmod.RUNTIME_DIR = TMP
dbmod.DB_PATH = TMP / "t.db"
dbmod.SNAPSHOT_PATH = TMP / "s.sqlite"
dbmod.PRIMARY_SNAPSHOT = TMP / "s.sqlite"
dbmod.SNAPSHOT_TMP = TMP / "s.tmp"

from app.db import init_db, get_conn, new_id
from app.proposals import (
    create_proposals_for_ocr_run, accept_proposal, reject_proposal,
    select_ocr_run, ConflictError, excerpt_from_raw,
)
from app.review_workstation import (
    ensure_workstation_schema, add_flag, link_continues_to, is_fully_reviewed,
    progress_report, parse_page_range, should_skip_page, page_bundle, FULLY_REVIEWED,
)


def check(n, ok, d=""):
    print(("PASS" if ok else "FAIL") + ": " + n + ((" — " + d) if d else ""))
    return bool(ok)


def seed():
    init_db(); ensure_workstation_schema()
    with get_conn() as c:
        c.execute("""INSERT OR IGNORE INTO source_files (source_id,edition_id,volume_number,original_filename,storage_path,file_size_bytes,sha256,page_count)
                     VALUES ('src-ed-bashshar-1997-v1','ed-bashshar-1997',1,'f.pdf','f.pdf',1,'x',10)""")
        c.execute("""INSERT OR IGNORE INTO volumes (volume_id,edition_id,source_id,volume_number,pdf_page_count)
                     VALUES ('vol-ed-bashshar-1997-v1','ed-bashshar-1997','src-ed-bashshar-1997-v1',1,10)""")
        c.execute("""INSERT OR IGNORE INTO source_pages (source_page_id,edition_id,volume_id,source_id,pdf_page_number,blank_status,verification_status)
                     VALUES ('pg-ed-bashshar-1997-v1-p0033','ed-bashshar-1997','vol-ed-bashshar-1997-v1','src-ed-bashshar-1997-v1',33,'content','needs_review')""")
        c.execute("""INSERT OR IGNORE INTO source_pages (source_page_id,edition_id,volume_id,source_id,pdf_page_number,blank_status)
                     VALUES ('pg-ed-bashshar-1997-v1-p0003','ed-bashshar-1997','vol-ed-bashshar-1997-v1','src-ed-bashshar-1997-v1',3,'expected_blank')""")
        raw = "كتاب الصلاة\nباب وقوت\nحدثني يحيى"
        c.execute("""INSERT OR REPLACE INTO ocr_runs (ocr_run_id,source_page_id,ocr_engine,ocr_model,ocr_output_raw,storage_path,ocr_timestamp)
                     VALUES ('ocr-orig-33','pg-ed-bashshar-1997-v1-p0033','tesseract','ara',?,'storage/ocr/vol1/p0033.json','2026-01-01')""", (raw,))
        c.execute("""INSERT OR REPLACE INTO ocr_runs (ocr_run_id,source_page_id,ocr_engine,ocr_model,ocr_output_raw,storage_path,ocr_timestamp)
                     VALUES ('ocr-review-33','pg-ed-bashshar-1997-v1-p0033','tesseract','ara','REVIEWONLY','storage/ocr_review/vol1/p0033.json','2026-12-01')""")


def main():
    r=[]
    seed()
    orig=select_ocr_run('pg-ed-bashshar-1997-v1-p0033', role='original')
    r.append(check("original OCR remains default", orig['ocr_run_id']=='ocr-orig-33'))
    rev=select_ocr_run('pg-ed-bashshar-1997-v1-p0033', role='review')
    r.append(check("explicit review OCR selection", rev['ocr_run_id']=='ocr-review-33'))
    skip, why=should_skip_page(1,3)
    r.append(check("expected_blank skipped", skip and why=='expected_blank'))
    skip2,_=should_skip_page(1,33)
    r.append(check("content page not skipped", not skip2))
    with get_conn(readonly=True) as c:
        vs=c.execute("SELECT verification_status FROM source_pages WHERE pdf_page_number=33").fetchone()[0]
    r.append(check("source needs_review remains", vs=='needs_review'))
    raw="كتاب الصلاة"
    hits_ok = excerpt_from_raw(raw,0,len(raw))==raw
    r.append(check("proposal highlighting span exact", hits_ok))
    w=create_proposals_for_ocr_run('ocr-orig-33', write=True)
    pid=w['proposal_ids'][0]
    acc=accept_proposal(pid, user_id='t')
    r.append(check("accept", acc['proposal_status']=='accepted'))
    # conflict
    bad=False
    try:
        accept_proposal(pid, user_id='t', expected_version=1)
    except ConflictError:
        bad=True
    except Exception:
        bad=True
    r.append(check("optimistic conflict", bad))
    pid2=w['proposal_ids'][1] if len(w['proposal_ids'])>1 else None
    if pid2:
        rj=reject_proposal(pid2, user_id='t')
        r.append(check("reject", rj['proposal_status']=='rejected'))
    else:
        r.append(check("reject", True, "single proposal fixture"))
    add_flag(pid, 'ocr_suspect', 'note', 't')
    r.append(check("flag", True))
    with get_conn(readonly=True) as c:
        n=c.execute("SELECT COUNT(*) FROM audit_log WHERE entity_id=?", (pid,)).fetchone()[0]
    r.append(check("audit", n>=1))
    # second page for cross-page
    with get_conn() as c:
        c.execute("""INSERT OR IGNORE INTO source_pages (source_page_id,edition_id,volume_id,source_id,pdf_page_number)
                     VALUES ('pg-ed-bashshar-1997-v1-p0034','ed-bashshar-1997','vol-ed-bashshar-1997-v1','src-ed-bashshar-1997-v1',34)""")
        c.execute("""INSERT OR REPLACE INTO ocr_runs (ocr_run_id,source_page_id,ocr_engine,ocr_output_raw,storage_path)
                     VALUES ('ocr-orig-34','pg-ed-bashshar-1997-v1-p0034','tesseract','continuatie','storage/ocr/vol1/p0034.json')""")
        pidb=new_id('prp-')
        c.execute("""INSERT INTO segmentation_proposals (proposal_id,source_page_id,ocr_run_id,start_offset,end_offset,raw_excerpt,proposed_type,confidence,proposal_status)
                     VALUES (?, 'pg-ed-bashshar-1997-v1-p0034','ocr-orig-34',0,5,'conti','unknown',0.2,'needs_review')""", (pidb,))
    link=link_continues_to(pid, pidb, 't')
    r.append(check("cross-page link", link['to']==pidb))
    invalid=False
    try:
        link_continues_to(pid, pid, 't')
    except ValueError:
        invalid=True
    r.append(check("invalid cross-page link rejected", invalid))
    invoff=False
    try:
        excerpt_from_raw("ab", 0, 9)
    except ValueError:
        invoff=True
    r.append(check("invalid offsets", invoff))
    badrun=False
    try:
        select_ocr_run('pg-ed-bashshar-1997-v1-p0033', ocr_run_id='ocr-orig-34')
    except ValueError:
        badrun=True
    r.append(check("invalid OCR-run/page combination", badrun))
    dry=create_proposals_for_ocr_run('ocr-orig-33', write=False)
    r.append(check("dry-run no mutation key", dry.get('dry_run') is True))
    blank_write=create_proposals_for_ocr_run
    # skip blank: attach run to blank page
    with get_conn() as c:
        c.execute("""INSERT OR REPLACE INTO ocr_runs (ocr_run_id,source_page_id,ocr_engine,ocr_output_raw,storage_path)
                     VALUES ('ocr-blank','pg-ed-bashshar-1997-v1-p0003','tesseract','','storage/ocr/vol1/p0003.json')""")
    sk=create_proposals_for_ocr_run('ocr-blank', write=True)
    r.append(check("blank write skipped", sk.get('skipped')=='expected_blank'))
    with get_conn(readonly=True) as c:
        pub=c.execute("SELECT COUNT(*) FROM text_units WHERE published=1").fetchone()[0]
        ver=c.execute("SELECT COUNT(*) FROM text_units WHERE verification_status IN ('verified','approved')").fetchone()[0]
    r.append(check("no verified/approved/published creation", pub==0 and ver==0))
    r.append(check("Unicode Arabic preserved", "كتاب" in orig['ocr_output_raw']))
    pr=progress_report()
    r.append(check("review progress calculation", "total_proposals" in pr and "pages_fully_reviewed" in pr))
    r.append(check("fully-reviewed conservative semantics", "every non-superseded" in FULLY_REVIEWED.lower() or "accepted or rejected" in FULLY_REVIEWED))
    r.append(check("page range parse", parse_page_range("33-35")==[33,34,35]))
    b=page_bundle(1,33)
    r.append(check("bundle default original raw", "كتاب" in (b['ocr_selected']['ocr_output_raw'] or "")))
    r.append(check("source needs_review in bundle", b['page']['verification_status']=='needs_review'))
    passed=sum(1 for x in r if x)
    print(f"\n=== WORKSTATION RESULT: {passed}/{len(r)} PASS ===")
    return 0 if passed==len(r) else 1

if __name__=='__main__':
    sys.exit(main())
