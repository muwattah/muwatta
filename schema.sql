-- =============================================================================
-- Al-Muwaṭṭaʾ Source Layer — Database Schema
-- Primary edition: Bashshār ʿAwwād Maʿrūf, Dār al-Gharb al-Islāmī, 2nd ed. 1417/1997
-- Absolute rule: never invent information. needs_review is the safe default.
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;

-- ---------------------------------------------------------------------------
-- 1. EDITIONS
-- ---------------------------------------------------------------------------
CREATE TABLE editions (
    edition_id          TEXT PRIMARY KEY,          -- UUID
    title_arabic        TEXT NOT NULL,
    author              TEXT NOT NULL,
    riwaya              TEXT NOT NULL,
    muhqqiq             TEXT NOT NULL,
    publisher           TEXT NOT NULL,
    place               TEXT NOT NULL,
    edition_statement   TEXT,                      -- e.g. الطبعة الثانية
    year_hijri          INTEGER,
    year_ce             INTEGER,
    volume_count        INTEGER NOT NULL DEFAULT 2,
    source_url          TEXT,
    bibliographic_status TEXT NOT NULL DEFAULT 'verified'
                            CHECK (bibliographic_status IN ('verified','needs_review','rejected')),
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- 2. IMMUTABLE SOURCE FILES
-- ---------------------------------------------------------------------------
CREATE TABLE source_files (
    source_id           TEXT PRIMARY KEY,
    edition_id          TEXT NOT NULL REFERENCES editions(edition_id),
    volume_number       INTEGER NOT NULL,
    original_filename   TEXT NOT NULL,
    storage_path        TEXT NOT NULL UNIQUE,     -- relative path under storage/originals
    file_size_bytes     INTEGER NOT NULL,
    sha256              TEXT NOT NULL UNIQUE,
    page_count          INTEGER NOT NULL,
    is_immutable        INTEGER NOT NULL DEFAULT 1 CHECK (is_immutable = 1),
    imported_at         TEXT NOT NULL DEFAULT (datetime('now')),
    source_url          TEXT,
    notes               TEXT
);

CREATE UNIQUE INDEX idx_source_files_sha256 ON source_files(sha256);
CREATE UNIQUE INDEX idx_source_files_edition_vol ON source_files(edition_id, volume_number);

-- ---------------------------------------------------------------------------
-- 3. VOLUMES (logical)
-- ---------------------------------------------------------------------------
CREATE TABLE volumes (
    volume_id           TEXT PRIMARY KEY,
    edition_id          TEXT NOT NULL REFERENCES editions(edition_id),
    source_id           TEXT NOT NULL REFERENCES source_files(source_id),
    volume_number       INTEGER NOT NULL,
    arabic_label        TEXT,                      -- المجلد الأول / الثاني
    pdf_page_count      INTEGER NOT NULL,
    printed_page_start  INTEGER,                   -- if known
    printed_page_end    INTEGER,
    notes               TEXT,
    UNIQUE (edition_id, volume_number)
);

-- ---------------------------------------------------------------------------
-- 4. PDF PAGES (one row per rendered page)
-- ---------------------------------------------------------------------------
CREATE TABLE source_pages (
    source_page_id      TEXT PRIMARY KEY,
    edition_id          TEXT NOT NULL REFERENCES editions(edition_id),
    volume_id           TEXT NOT NULL REFERENCES volumes(volume_id),
    source_id           TEXT NOT NULL REFERENCES source_files(source_id),
    pdf_page_number     INTEGER NOT NULL,          -- 1-based in the PDF file
    printed_page_number INTEGER,                   -- NULL if unknown / needs_review
    printed_page_status TEXT NOT NULL DEFAULT 'needs_review'
                            CHECK (printed_page_status IN (
                                'verified','needs_review','absent','unclear'
                            )),
    image_path          TEXT,                      -- path to extracted page image (optional)
    image_hash          TEXT,                      -- content hash of page image
    page_width          INTEGER,
    page_height         INTEGER,
    orientation         TEXT,                      -- portrait / landscape
    blank_status        TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (blank_status IN (
                                'content','expected_blank','unexpected_blank','near_blank','unknown'
                            )),
    page_status         TEXT NOT NULL DEFAULT 'present'
                            CHECK (page_status IN (
                                'present','missing','corrupt','duplicate_suspect'
                            )),
    ocr_status          TEXT NOT NULL DEFAULT 'not_run'
                            CHECK (ocr_status IN (
                                'not_run','pending','done','failed','needs_review'
                            )),
    verification_status TEXT NOT NULL DEFAULT 'needs_review'
                            CHECK (verification_status IN (
                                'imported','extracted','needs_review','verified',
                                'approved','published','rejected','superseded'
                            )),
    notes               TEXT,
    UNIQUE (source_id, pdf_page_number)
);

CREATE INDEX idx_source_pages_volume ON source_pages(volume_id);
CREATE INDEX idx_source_pages_printed ON source_pages(printed_page_number);
CREATE INDEX idx_source_pages_status ON source_pages(verification_status);

-- ---------------------------------------------------------------------------
-- 5. BOOKS (Kitāb)
-- ---------------------------------------------------------------------------
CREATE TABLE books (
    book_id             TEXT PRIMARY KEY,
    edition_id          TEXT NOT NULL REFERENCES editions(edition_id),
    volume_id           TEXT NOT NULL REFERENCES volumes(volume_id),
    book_order          INTEGER NOT NULL,
    arabic_title        TEXT,                      -- NULL until verified from source
    title_status        TEXT NOT NULL DEFAULT 'needs_review'
                            CHECK (title_status IN ('needs_review','verified','approved')),
    start_source_page_id TEXT REFERENCES source_pages(source_page_id),
    end_source_page_id   TEXT REFERENCES source_pages(source_page_id),
    start_printed_page  INTEGER,
    end_printed_page    INTEGER,
    verification_status TEXT NOT NULL DEFAULT 'needs_review',
    is_test             INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0,1)),
    notes               TEXT,
    UNIQUE (edition_id, book_order)
);

-- ---------------------------------------------------------------------------
-- 6. CHAPTERS (Bāb)
-- ---------------------------------------------------------------------------
CREATE TABLE chapters (
    chapter_id          TEXT PRIMARY KEY,
    book_id             TEXT NOT NULL REFERENCES books(book_id),
    edition_id          TEXT NOT NULL REFERENCES editions(edition_id),
    chapter_order       INTEGER NOT NULL,
    arabic_title        TEXT,
    title_status        TEXT NOT NULL DEFAULT 'needs_review',
    start_source_page_id TEXT REFERENCES source_pages(source_page_id),
    start_printed_page  INTEGER,
    verification_status TEXT NOT NULL DEFAULT 'needs_review',
    is_test             INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0,1)),
    notes               TEXT,
    UNIQUE (book_id, chapter_order)
);

-- ---------------------------------------------------------------------------
-- 7. TEXT UNITS (hadith / athar / qawl / other)
-- ---------------------------------------------------------------------------
CREATE TABLE text_units (
    text_id             TEXT PRIMARY KEY,
    edition_id          TEXT NOT NULL REFERENCES editions(edition_id),
    volume_id           TEXT NOT NULL REFERENCES volumes(volume_id),
    book_id             TEXT REFERENCES books(book_id),
    chapter_id          TEXT REFERENCES chapters(chapter_id),
    source_page_id      TEXT REFERENCES source_pages(source_page_id),
    text_order          INTEGER,                   -- order within chapter / page
    hadith_number       TEXT,                      -- numbering of THIS edition only
    text_type           TEXT NOT NULL DEFAULT 'needs_review'
                            CHECK (text_type IN (
                                'hadith','athar','qawl_malik','heading',
                                'editorial','unknown','other','needs_review'
                            )),
    -- RAW vs PROPOSED vs VERIFIED (raw OCR is immutable)
    arabic_text_raw     TEXT,                      -- direct OCR / extraction; never overwrite
    arabic_text_proposed TEXT,                     -- segmenter/reviewer proposal only
    arabic_text_verified TEXT,                     -- NULL until human checked against scan
    review_flag         TEXT,                      -- human flag; does not delete content
    pdf_page            INTEGER,
    printed_page        INTEGER,
    verification_status TEXT NOT NULL DEFAULT 'needs_review'
                            CHECK (verification_status IN (
                                'imported','extracted','needs_review','verified',
                                'approved','published','rejected','superseded'
                            )),
    published           INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0,1)),
    is_test             INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0,1)),
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_text_units_status ON text_units(verification_status);
CREATE INDEX idx_text_units_page ON text_units(source_page_id);
CREATE INDEX idx_text_units_book ON text_units(book_id);

-- Publication gate: published may only be 1 when approved + provenance exists
-- Enforced in application logic and optionally via trigger.

-- ---------------------------------------------------------------------------
-- 8. EDITORIAL NOTES (muḥaqqiq content — NEVER mixed with Mālik text)
-- ---------------------------------------------------------------------------
CREATE TABLE editorial_notes (
    editorial_note_id   TEXT PRIMARY KEY,
    edition_id          TEXT NOT NULL REFERENCES editions(edition_id),
    source_page_id      TEXT REFERENCES source_pages(source_page_id),
    related_text_id     TEXT REFERENCES text_units(text_id),
    note_type           TEXT NOT NULL DEFAULT 'needs_review'
                            CHECK (note_type IN (
                                'tahqiq','takhrij','footnote','commentary',
                                'muqaddima','other','needs_review'
                            )),
    arabic_text_raw     TEXT,
    arabic_text_verified TEXT,
    verification_status TEXT NOT NULL DEFAULT 'needs_review',
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- 9. OCR RUNS (reproducibility)
-- ---------------------------------------------------------------------------
CREATE TABLE ocr_runs (
    ocr_run_id          TEXT PRIMARY KEY,
    source_page_id      TEXT NOT NULL REFERENCES source_pages(source_page_id),
    ocr_engine          TEXT NOT NULL,
    ocr_model           TEXT,
    ocr_version         TEXT,
    ocr_confidence      REAL,                      -- overall page confidence if available
    ocr_output_raw      TEXT,                      -- full page OCR text
    ocr_timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
    storage_path        TEXT,                      -- path to detailed OCR JSON if any
    notes               TEXT
);

CREATE INDEX idx_ocr_runs_page ON ocr_runs(source_page_id);

-- ---------------------------------------------------------------------------
-- 10. AUDIT LOG (immutable history)
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
    log_id              TEXT PRIMARY KEY,
    entity_type         TEXT NOT NULL,             -- edition|source_file|source_page|book|chapter|text_unit|editorial_note
    entity_id           TEXT NOT NULL,
    action              TEXT NOT NULL,             -- create|update|approve|reject|publish|unpublish|...
    old_value           TEXT,                      -- JSON
    new_value           TEXT,                      -- JSON
    user_id             TEXT,                      -- admin identifier
    reason              TEXT,
    timestamp           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_time ON audit_log(timestamp);

-- ---------------------------------------------------------------------------
-- 11. VERIFICATION TASKS (workflow queue)
-- ---------------------------------------------------------------------------
CREATE TABLE verification_tasks (
    task_id             TEXT PRIMARY KEY,
    entity_type         TEXT NOT NULL,
    entity_id           TEXT NOT NULL,
    task_type           TEXT NOT NULL,             -- page_number|ocr_check|segmentation|text_approve|...
    priority            INTEGER NOT NULL DEFAULT 100,
    status              TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open','in_progress','done','cancelled')),
    assigned_to         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT,
    notes               TEXT
);

-- ---------------------------------------------------------------------------
-- Seed: the one canonical edition
-- ---------------------------------------------------------------------------
INSERT INTO editions (
    edition_id, title_arabic, author, riwaya, muhqqiq, publisher, place,
    edition_statement, year_hijri, year_ce, volume_count, source_url,
    bibliographic_status, notes
) VALUES (
    'ed-bashshar-1997',
    'الموطأ',
    'الإمام مالك بن أنس',
    'يحيى بن يحيى الليثي الأندلسي',
    'بشار عواد معروف',
    'دار الغرب الإسلامي',
    'بيروت',
    'الطبعة الثانية',
    1417,
    1997,
    2,
    'https://waqfeya.net/books/الموطأ-لإمام-دار-الهجرة-مالك-بن-أنس-رواية-يحيى-بن-يحيى-الليثي--ت-بشار/c7aea7234bc846b2aad4a1f67f55325f',
    'verified',
    'Primary canonical edition for version 1 of the application. Bibliographic data verified from title pages and colophon of the actual PDF files.'
);


-- Multi-page text units
CREATE TABLE IF NOT EXISTS text_unit_source_pages (
    id TEXT PRIMARY KEY,
    text_id TEXT NOT NULL REFERENCES text_units(text_id),
    source_page_id TEXT NOT NULL REFERENCES source_pages(source_page_id),
    page_role TEXT NOT NULL DEFAULT 'start'
        CHECK (page_role IN ('start','continuation','end','only')),
    sequence_order INTEGER NOT NULL DEFAULT 1,
    UNIQUE(text_id, source_page_id)
);

-- is_test column added via ALTER on text_units, books, chapters (0=production, 1=test)
-- Production queries MUST filter is_test = 0

-- ---------------------------------------------------------------------------
-- 12. SEGMENTATION PROPOSALS (machine suggestions only; never canonical)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS segmentation_proposals (
    proposal_id         TEXT PRIMARY KEY,
    source_page_id      TEXT NOT NULL REFERENCES source_pages(source_page_id),
    ocr_run_id          TEXT REFERENCES ocr_runs(ocr_run_id),
    start_offset        INTEGER,
    end_offset          INTEGER,
    raw_excerpt         TEXT,
    proposed_type       TEXT NOT NULL
                            CHECK (proposed_type IN (
                                'book_heading','chapter_heading','hadith','athar',
                                'editorial_note','footnote','page_header','page_number',
                                'unknown','editorial_candidate'
                            )),
    confidence          REAL NOT NULL DEFAULT 0.0,
    reason              TEXT,
    evidence            TEXT,
    proposal_status     TEXT NOT NULL DEFAULT 'needs_review'
                            CHECK (proposal_status IN (
                                'needs_review','accepted','rejected','superseded'
                            )),
    generator           TEXT NOT NULL DEFAULT 'regex_v1',
    generator_version   TEXT NOT NULL DEFAULT '1',
    content_hash        TEXT,
    parent_proposal_id  TEXT REFERENCES segmentation_proposals(proposal_id),
    continues_to_page_id TEXT REFERENCES source_pages(source_page_id),
    materialized_text_id TEXT REFERENCES text_units(text_id),
    reviewed_by         TEXT,
    reviewed_at         TEXT,
    review_reason       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_segprop_page ON segmentation_proposals(source_page_id);
CREATE INDEX IF NOT EXISTS idx_segprop_status ON segmentation_proposals(proposal_status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_segprop_hash
    ON segmentation_proposals(content_hash)
    WHERE content_hash IS NOT NULL AND proposal_status != 'superseded';
