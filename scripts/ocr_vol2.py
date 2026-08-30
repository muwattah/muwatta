#!/usr/bin/env python3
from pathlib import Path
import json, sys, time
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import init_db, persist_snapshot
from app.ocr_runner import ocr_page
LOG = ROOT / "storage/runtime/ocr_vol2.log"
def log(m):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line=time.strftime("%Y-%m-%d %H:%M:%S ")+m
    print(line, flush=True)
    LOG.open("a").write(line+"\n")
init_db()
ok=sk=fail=0
failed=[]
n=0
for p in range(1,721):
    try:
        r=ocr_page(2,p,dpi=120)
        if r.get("skipped_existing"):
            sk+=1; st="skipped-existing"
        else:
            ok+=1; st="success"; n+=1
        log(f"vol2 p{p} {st} run={r.get('ocr_run_id')} chars={r.get('char_count')}")
    except Exception as e:
        fail+=1; failed.append((p,str(e))); log(f"vol2 p{p} failed {e}")
    if n>=25:
        persist_snapshot(); n=0
persist_snapshot()
log(f"DONE ok={ok} skip={sk} fail={fail}")
