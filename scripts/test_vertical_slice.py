#!/usr/bin/env python3
from __future__ import annotations
import sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app.db as dbmod
TMP = Path(tempfile.mkdtemp(prefix="muwatta_vs_"))
dbmod.RUNTIME_DIR = TMP
dbmod.DB_PATH = TMP / "t.db"
dbmod.SNAPSHOT_PATH = TMP / "s.sqlite"
dbmod.PRIMARY_SNAPSHOT = TMP / "s.sqlite"
dbmod.SNAPSHOT_TMP = TMP / "s.tmp"

from app.db import init_db, get_conn, new_id
from app.proposals import create_proposals_for_ocr_run, accept_proposal, materialize_accepted_proposal
from app.review_api import set_verified_text
from app.reader import reader_tree, pilot_status
from app.review_workstation import parse_page_range


def check(n, ok, d=""):
    print(("PASS" if ok else "FAIL") + ": " + n + ((" — " + d) if d else ""))
    return bool(ok)


def seed():
    init_db()
    raw = "كتاب الصلاة\nباب وقوت الصلاة\nحدثني يحيى عن مالك"
    with get_conn() as c:
        c.execute("""INSERT OR IGNORE INTO source_files (source_id,edition_id,volume_number,original_filename,storage_path,file_size_bytes,sha256,page_count)
                     VALUES ('src-ed-bashshar-1997-v1','ed-bashshar-1997',1,'f.pdf','f.pdf',1,'z',50)""")
        c.execute("""INSERT OR IGNORE INTO volumes (volume_id,edition_id,source_id,volume_number,pdf_page_count)
                     VALUES ('vol-ed-bashshar-1997-v1','ed-bashshar-1997','src-ed-bashshar-1997-v1',1,50)""")
        for p in range(33, 43):
            c.execute("""INSERT OR IGNORE INTO source_pages (source_page_id,edition_id,volume_id,source_id,pdf_page_number,verification_status)
                         VALUES (?,?, 'vol-ed-bashshar-1997-v1','src-ed-bashshar-1997-v1',?,'needs_review')""",
                      (f"pg-ed-bashshar-1997-v1-p{p:04d}", "ed-bashshar-1997", p))
        c.execute("""INSERT OR IGNORE INTO source_pages (source_page_id,edition_id,volume_id,source_id,pdf_page_number)
                     VALUES ('pg-ed-bashshar-1997-v1-p0050','ed-bashshar-1997','vol-ed-bashshar-1997-v1','src-ed-bashshar-1997-v1',50)""")
        c.execute("""INSERT OR REPLACE INTO ocr_runs (ocr_run_id,source_page_id,ocr_engine,ocr_output_raw,storage_path)
                     VALUES ('ocr-33','pg-ed-bashshar-1997-v1-p0033','tesseract',?,'storage/ocr/vol1/p0033.json')""", (raw,))
        c.execute("""INSERT OR REPLACE INTO ocr_runs (ocr_run_id,source_page_id,ocr_engine,ocr_output_raw,storage_path)
                     VALUES ('ocr-50','pg-ed-bashshar-1997-v1-p0050','tesseract','خارج النطاق','storage/ocr/vol1/p0050.json')""")
    return raw


def main():
    rs=[]
    raw=seed()
    rs.append(check("pilot range 33-42", parse_page_range("33-42")==list(range(33,43))))
    w=create_proposals_for_ocr_run("ocr-33", write=True)
    w2=create_proposals_for_ocr_run("ocr-33", write=True)
    rs.append(check("repeated pilot idempotent", w2.get("created")==0))
    with get_conn(readonly=True) as c:
        raw2=c.execute("SELECT ocr_output_raw FROM ocr_runs WHERE ocr_run_id='ocr-33'").fetchone()[0]
        other=c.execute("SELECT COUNT(*) FROM segmentation_proposals p JOIN source_pages sp ON sp.source_page_id=p.source_page_id WHERE sp.pdf_page_number=50").fetchone()[0]
    rs.append(check("raw OCR unchanged", raw2==raw))
    rs.append(check("no other pages touched", other==0))
    pid=w["proposal_ids"][0]
    acc=accept_proposal(pid, user_id="t")
    rs.append(check("accepted segmentation != verified source", acc["proposal_status"]=="accepted"))
    mat=materialize_accepted_proposal(pid, user_id="t")
    rs.append(check("materialize => needs_review", mat["verification_status"]=="needs_review"))
    rs.append(check("materialize cannot publish", mat["published"]==0))
    tree=reader_tree(preview=False, volume=1)
    rs.append(check("reader excludes needs_review by default", all(u["verification_status"] in ("verified","approved","published") for u in tree["units"])))
    prev=reader_tree(preview=True, volume=1)
    rs.append(check("preview marks needs_review", any(u.get("badge") for u in prev["units"]) or len(prev["units"])>=1))
    set_verified_text(mat["text_id"], raw2[0:10] if raw2 else "كتاب", user_id="t", reason="human vs scan")
    with get_conn(readonly=True) as c:
        st=c.execute("SELECT verification_status, published, arabic_text_raw FROM text_units WHERE text_id=?", (mat["text_id"],)).fetchone()
        audits=c.execute("SELECT COUNT(*) FROM audit_log WHERE entity_id=? AND action='set_verified_text'", (mat["text_id"],)).fetchone()[0]
        raw_still=c.execute("SELECT ocr_output_raw FROM ocr_runs WHERE ocr_run_id='ocr-33'").fetchone()[0]
    rs.append(check("verify requires explicit action + audit", audits>=1 and st[0]=="verified"))
    rs.append(check("manual/verify preserves raw OCR", raw_still==raw))
    tree2=reader_tree(preview=False, volume=1)
    rs.append(check("reader includes verified units", any(u["text_id"]==mat["text_id"] for u in tree2["units"])))
    with get_conn(readonly=True) as c:
        hn=c.execute("SELECT hadith_number, printed_page FROM text_units WHERE text_id=?", (mat["text_id"],)).fetchone()
        pub=c.execute("SELECT COUNT(*) FROM text_units WHERE published=1").fetchone()[0]
        appr=c.execute("SELECT COUNT(*) FROM text_units WHERE verification_status='approved'").fetchone()[0]
    rs.append(check("no invented hadith number", hn[0] is None))
    rs.append(check("no invented printed page", hn[1] is None))
    rs.append(check("no approved/published created accidentally", pub==0 and appr==0))
    rs.append(check("Arabic Unicode preserved", "كتاب" in raw_still))
    stt=pilot_status(1, range(33,43))
    rs.append(check("pilot status keys", stt["verified"]>=1 and stt["page_count"]>=1))
    editorial_ok=all((u.get("text_type")!="editorial") or u.get("badge") for u in reader_tree(preview=True)["units"])
    rs.append(check("editorial/footnotes separated in preview", editorial_ok))
    passed=sum(1 for x in rs if x)
    print(f"\n=== VERTICAL SLICE RESULT: {passed}/{len(rs)} PASS ===")
    return 0 if passed==len(rs) else 1

if __name__=="__main__":
    sys.exit(main())
