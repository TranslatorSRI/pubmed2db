-- pubmed2db DuckDB schema.
--
-- Fully normalized, full-version-history. Field names follow PubMed's own
-- naming (DocumentMetadataAPI names are only applied at JSON export time).
--
-- Versioning model: every loaded file contributes rows tagged with their
-- provenance (`source_file`, `file_order_key`). A PMID may therefore appear
-- in many files (baseline + multiple updatefiles). The `latest_article` view
-- selects, per PMID, the row from the highest `file_order_key`, honoring
-- deletions recorded in `deleted_pmid`.
--
-- `file_order_key` encodes PubMed's chronological order: baseline files come
-- first, then updatefiles in ascending number, and the year prefix increments
-- across years. It is computed as (year_yy * 1000000 + file_number).
--
-- Convention: this file only ever ADDS, and it runs on every connect. Tables are
-- `CREATE TABLE IF NOT EXISTS`, so a column added to a table here does *not*
-- appear in a database created before it — it needs its own
-- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` line right below the table (see
-- `source_file.n_failed`). Nothing here drops a table or column, so a table
-- removed from this file keeps its rows forever in an existing database: after a
-- schema change, rebuild rather than `load --force` (see the README's
-- "`load --force` or a fresh database?").

-- Registry of every PubMed XML file we know about. Drives incremental loads.
CREATE TABLE IF NOT EXISTS source_file (
    file_name      TEXT PRIMARY KEY,           -- e.g. 'pubmed25n1274.xml.gz'
    kind           TEXT NOT NULL,              -- 'baseline' | 'update'
    year_yy        INTEGER NOT NULL,           -- 2-digit release year, e.g. 25
    file_number    INTEGER NOT NULL,           -- e.g. 1274
    file_order_key BIGINT  NOT NULL,           -- year_yy * 1000000 + file_number
    published_md5  TEXT,                       -- checksum from the .md5 sidecar
    downloaded_at  TIMESTAMP,
    processed_at   TIMESTAMP,                  -- NULL until loaded into the tables
    n_articles     INTEGER,
    n_deletions    INTEGER,
    n_failed       INTEGER,                    -- records the extractor rejected
    n_book_records INTEGER                     -- <PubmedBookArticle>, not parsed
);

-- CREATE TABLE IF NOT EXISTS leaves an existing database at its old shape, so
-- columns added after the first release need their own migration. Adding a
-- nullable column is a metadata-only change in DuckDB; NULL reads as "loaded
-- before this column existed", which is not the same as a counted zero.
ALTER TABLE source_file ADD COLUMN IF NOT EXISTS n_failed INTEGER;
ALTER TABLE source_file ADD COLUMN IF NOT EXISTS n_book_records INTEGER;

-- Records when steps that leave no other timestamp were last run (currently just
-- `journals`, whose tables are replaced wholesale). Download/load recency is
-- derived from source_file instead, so it can never disagree with the data.
CREATE TABLE IF NOT EXISTS pipeline_run (
    step        TEXT PRIMARY KEY,                 -- e.g. 'journals'
    last_run_at TIMESTAMP
);

-- One row per loaded version of an article. Identity is (pmid, source_file).
CREATE TABLE IF NOT EXISTS article (
    pmid           BIGINT  NOT NULL,
    pmid_version   INTEGER,
    source_file    TEXT    NOT NULL,
    file_order_key BIGINT  NOT NULL,
    article_title  TEXT,
    nlm_catalog_id TEXT,                        -- joins to journal.nlm_catalog_id
    issn_linking   TEXT,
    volume         TEXT,
    issue          TEXT,
    pub_year       TEXT,                        -- raw PubDate/Year
    pub_month      TEXT,                        -- raw PubDate/Month ('Mar', '03', ...)
    pub_day        TEXT,                        -- raw PubDate/Day
    medline_date   TEXT,                        -- raw PubDate/MedlineDate (e.g. '1998 Spring')
    date_completed DATE,
    date_revised   DATE,
    loaded_at      TIMESTAMP
);

-- Journal dimension, sourced from the NLM Catalog (J_Entrez/J_Medline), not
-- versioned. Joined to articles on nlm_catalog_id.
CREATE TABLE IF NOT EXISTS journal (
    nlm_catalog_id       TEXT PRIMARY KEY,
    title                TEXT,
    abbreviation_medline TEXT,
    abbreviation_iso     TEXT,
    start_year           INTEGER,
    end_year             INTEGER,
    active               BOOLEAN
);

CREATE TABLE IF NOT EXISTS journal_issn (
    nlm_catalog_id TEXT NOT NULL,
    issn           TEXT NOT NULL,
    issn_type      TEXT
);

CREATE TABLE IF NOT EXISTS abstract_text (
    pmid         BIGINT NOT NULL,
    source_file  TEXT   NOT NULL,
    seq          INTEGER NOT NULL,             -- order of the section within the abstract
    label        TEXT,
    nlm_category TEXT,
    text         TEXT
);

CREATE TABLE IF NOT EXISTS author (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    position    INTEGER NOT NULL,
    kind        TEXT,                           -- 'author' | 'collective'
    name        TEXT,
    orcid       TEXT,
    valid       BOOLEAN
);

CREATE TABLE IF NOT EXISTS author_affiliation (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    position    INTEGER NOT NULL,              -- author position
    affiliation TEXT,
    ror         TEXT                            -- grounded ROR curie, if present
);

CREATE TABLE IF NOT EXISTS mesh_heading (
    pmid          BIGINT NOT NULL,
    source_file   TEXT   NOT NULL,
    descriptor_ui TEXT,
    descriptor_name TEXT,
    major_topic   BOOLEAN
);

CREATE TABLE IF NOT EXISTS mesh_qualifier (
    pmid          BIGINT NOT NULL,
    source_file   TEXT   NOT NULL,
    descriptor_ui TEXT,
    qualifier_ui  TEXT,
    qualifier_name TEXT,
    major_topic   BOOLEAN
);

CREATE TABLE IF NOT EXISTS publication_type (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    type_ui     TEXT                            -- MeSH LUID of the publication type
);

CREATE TABLE IF NOT EXISTS grant_ (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    grant_id    TEXT,
    acronym     TEXT,
    agency      TEXT,
    country     TEXT
);

-- No `reference_citation` table: the citation graph is deliberately not stored.
-- One real article carries ~444 references, so it would have been the largest
-- table here, and nothing downstream consumes it. `parse._cited_pmids` keeps the
-- extraction if that changes. Databases built before this keep a stale table;
-- `DROP TABLE reference_citation` clears it.

CREATE TABLE IF NOT EXISTS article_id (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    id_type     TEXT,                           -- 'doi', 'pmc', ...
    id_value    TEXT
);

CREATE TABLE IF NOT EXISTS history (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    pub_status  TEXT,
    date        DATE
);

-- PMIDs removed via <DeleteCitation>. file_order_key lets the latest_article
-- view decide whether a deletion supersedes the latest article version.
CREATE TABLE IF NOT EXISTS deleted_pmid (
    pmid           BIGINT NOT NULL,
    source_file    TEXT   NOT NULL,
    file_order_key BIGINT NOT NULL
);

-- Latest non-deleted version of each article.
CREATE OR REPLACE VIEW latest_article AS
WITH ranked AS (
    SELECT
        a.*,
        row_number() OVER (PARTITION BY a.pmid ORDER BY a.file_order_key DESC) AS _rn
    FROM article a
),
latest AS (
    SELECT * EXCLUDE (_rn) FROM ranked WHERE _rn = 1
),
last_delete AS (
    SELECT pmid, max(file_order_key) AS del_order
    FROM deleted_pmid
    GROUP BY pmid
)
SELECT l.*
FROM latest l
LEFT JOIN last_delete d ON l.pmid = d.pmid
WHERE d.del_order IS NULL OR d.del_order < l.file_order_key;
