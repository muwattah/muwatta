# Al-Muwaṭṭaʾ — Source Layer

Source-grounded foundation for studying **Al-Muwaṭṭaʾ** of Imam Mālik, riwāya Yaḥyā ibn Yaḥyā al-Laythī, taḥqīq Bashshār ʿAwwād Maʿrūf (Dār al-Gharb al-Islāmī, 2nd ed., 1417/1997).

## Absolute rules

1. **Never invent information** — only the approved edition.
2. PDF scan is the primary authority; OCR is extraction only.
3. `arabic_text_verified` is set **only** by human review (never from OCR).
4. `published = 1` only when: approved + verified text + provenance + `is_test = 0`.
5. Muḥaqqiq notes live in `editorial_notes`, never mixed with Mālik text.
6. Original PDFs are **immutable** — verify SHA-256; never overwrite.

See [SOURCE_MANIFEST.md](SOURCE_MANIFEST.md) for edition metadata and file hashes.

## Layout

```
muwatta_source/
  schema.sql              # reproducible DB schema + seed edition
  SOURCE_MANIFEST.md      # hashes & provenance (no PDFs)
  requirements.txt
  app/
    db.py                 # connection, audit helpers
    sources.py            # immutable registration, page registry
    integrity.py          # SHA-256 verification
    ocr_runner.py         # OCR → ocr_runs + arabic_text_raw only
    segmenter.py          # structure proposals (needs_review)
    verification.py       # status transitions, provenance
    review_api.py         # human review actions + publish gate
    viewer.py             # FastAPI Source Viewer
  admin_static/viewer.html
  scripts/
    bootstrap.py          # init DB, register sources & pages
    cli.py
    phase2_extract.py
    test_integrity.py     # 6 integrity / gate tests
  storage/
    originals/            # PLACE PDFs HERE (gitignored)
    page_images/          # gitignored
    ocr/                  # gitignored
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Place the two canonical PDFs in storage/originals/ (see SOURCE_MANIFEST.md)
mkdir -p storage/originals
# copy muwatta_bashshar_vol1_1417_1997.pdf and vol2...

# Optional: Arabic tessdata for OCR
mkdir -p tessdata
# download ara.traineddata into tessdata/

python3 scripts/bootstrap.py
python3 -c "from app.integrity import assert_source_integrity; assert_source_integrity()"
python3 scripts/test_integrity.py
```

Database file defaults to `/tmp/muwatta_source.db` (see `app/db.py`). Schema is always rebuildable from `schema.sql`.

## Source Viewer

```bash
uvicorn app.viewer:app --host 0.0.0.0 --port 8000
# http://localhost:8000
```

## Status workflow

```
imported → extracted → needs_review → verified → approved → published
```

Changing `arabic_text_verified` after approval **revokes** approval and unpublishes.

## What is not in this repo

- Original PDF scans (too large; immutable local storage)
- Tessdata models
- Runtime SQLite DB
- Generated page images / OCR JSON
