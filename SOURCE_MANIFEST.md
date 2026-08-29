# Source Manifest — Al-Muwaṭṭaʾ (canonical edition)

**Do not commit the original PDF files to Git.**  
Store them locally under `storage/originals/` (or the path configured in `app/sources.py`).

## Canonical edition

| Field | Value |
|-------|--------|
| Title | الموطأ |
| Author | الإمام مالك بن أنس |
| Riwāya | يحيى بن يحيى الليثي الأندلسي |
| Muḥaqqiq | بشار عواد معروف |
| Publisher | دار الغرب الإسلامي |
| Place | بيروت |
| Edition | الطبعة الثانية |
| Year | 1417هـ / 1997م |
| Volumes | 2 |
| Edition ID | `ed-bashshar-1997` |
| Bibliographic status | verified |
| Source URL | https://waqfeya.net/books/الموطأ-لإمام-دار-الهجرة-مالك-بن-أنس-رواية-يحيى-بن-يحيى-الليثي--ت-بشار/c7aea7234bc846b2aad4a1f67f55325f |

## Immutable original files

Place these files **unchanged** in `storage/originals/`:

| Volume | Filename | Pages | Size (bytes) | SHA-256 |
|--------|----------|-------|--------------|---------|
| 1 | `muwatta_bashshar_vol1_1417_1997.pdf` | 664 | 10886272 | `2b5c281a5acfe0d0a1d7eba3767c591b7cecaa636f13bca68677d7316703f075` |
| 2 | `muwatta_bashshar_vol2_1417_1997.pdf` | 720 | 11252568 | `f0743087410d7b284c4fe427b0a4607eb06c18d1c3c40fcd3a9ccac3a0607f55` |

### Integrity rule

On every bootstrap / import start, compare on-disk SHA-256 with the hashes above.  
On mismatch: **SOURCE INTEGRITY FAILURE** — stop; do not overwrite or auto-replace.

```bash
sha256sum storage/originals/muwatta_bashshar_vol1_1417_1997.pdf
sha256sum storage/originals/muwatta_bashshar_vol2_1417_1997.pdf
# or: python3 -c "from app.integrity import assert_source_integrity; assert_source_integrity()"
```

## Canonical text start

| Volume | PDF page | Printed page | Content |
|--------|----------|--------------|---------|
| 1 | 33 | 33 | Begin of Al-Muwaṭṭaʾ text (`كتاب الصلاة` / `وقوت الصلاة`) |

Pages before PDF 33 are front matter / muqaddima of the muḥaqqiq — not canonical Mālik text.

## Local setup

```bash
mkdir -p storage/originals
# Copy the two PDFs into storage/originals/ with the exact filenames above
python3 scripts/bootstrap.py
python3 -c "from app.integrity import assert_source_integrity; assert_source_integrity()"
```

## Absolute content rule

Never invent, reconstruct, or supplement text from memory or other editions.  
OCR → `arabic_text_raw` only.  
Human review → `arabic_text_verified`.  
`published = 1` only when approved + provenance complete + `is_test = 0`.
