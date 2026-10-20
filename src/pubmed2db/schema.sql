-- pubmed2db DuckDB schema: data model 1.0 (`datamodel.DATA_MODEL_VERSION`).
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
-- The `COMMENT ON` statements are the data dictionary, and they ship *inside*
-- the database: `SELECT table_name, column_name, comment FROM duckdb_columns()`
-- documents a copy of the file with no repository in sight. Each table comment
-- starts with its stability tier -- "Stable.", "Provenance." or "Internal." --
-- which `docs/data-model.md` defines. `tests/test_data_model.py` requires a
-- comment on every stable and provenance column, pins every column's name and
-- type to the version, and checks the generated half of `docs/data-model.md`
-- against these comments, so a change here fails a test until the version is
-- bumped deliberately (and `python -m pubmed2db.datamodel` regenerates the doc).
--
-- Convention: from 1.0 on, this file only ever ADDS, and it runs on every
-- connect. Tables are `CREATE TABLE IF NOT EXISTS`, so a column added to a table
-- here does *not* appear in an existing database: it needs its own
-- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` line right below the table, placed
-- so that a fresh and a migrated database end up with the same column order
-- (`load` inserts BY POSITION). Adding a column is a minor version bump;
-- renaming or dropping one is a major bump, which `datamodel.check_compatible`
-- answers by refusing the old database rather than migrating it. Databases from
-- before 1.0 are refused the same way.

-- The data model version and who built the database (see datamodel.py). Written
-- when the database is created and checked every time it is opened.
CREATE TABLE IF NOT EXISTS pubmed2db_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
COMMENT ON TABLE pubmed2db_metadata IS 'Provenance. Facts about this database as a whole, one per row: data_model_version (check its major version before reading anything else), created_at and created_by (the pubmed2db commit, as git describe), last_loaded_at and last_loaded_by. Timestamps are ISO 8601 in UTC.';
COMMENT ON COLUMN pubmed2db_metadata.key IS 'The fact''s name, e.g. data_model_version.';
COMMENT ON COLUMN pubmed2db_metadata.value IS 'Its value, as text.';

-- Registry of every PubMed XML file we know about. Drives incremental loads.
CREATE TABLE IF NOT EXISTS source_file (
    file_name      TEXT PRIMARY KEY,           -- e.g. 'pubmed25n1274.xml.gz'
    kind           TEXT NOT NULL,              -- 'baseline' | 'update'
    year_yy        INTEGER NOT NULL,           -- 2-digit release year, e.g. 25
    file_number    INTEGER NOT NULL,           -- e.g. 1274
    file_order_key BIGINT  NOT NULL,           -- year_yy * 1000000 + file_number
    published_md5  TEXT,                       -- checksum from the .md5 sidecar
    published_at   TIMESTAMPTZ,                -- when NCBI posted it (the sidecar's Last-Modified)
    downloaded_at  TIMESTAMPTZ,
    processed_at   TIMESTAMPTZ,                -- NULL until loaded into the tables
    n_articles     INTEGER,
    n_deletions    INTEGER,
    n_failed       INTEGER,                    -- records the extractor rejected
    n_book_records INTEGER                     -- <PubmedBookArticle>, not parsed
);
COMMENT ON TABLE source_file IS 'Provenance. One row per PubMed XML file known to this database, loaded or not. The loaded file with the highest file_order_key is the build''s cutoff: the next file belongs to the next build.';
COMMENT ON COLUMN source_file.file_name IS 'PubMed''s file name, e.g. pubmed26n1636.xml.gz. Every per-version row''s source_file joins here.';
COMMENT ON COLUMN source_file.kind IS '''baseline'' (the annual full re-issue) or ''update'' (a daily updatefile).';
COMMENT ON COLUMN source_file.year_yy IS 'Two-digit release year from the file name: 26 for the 2026 baseline and the updatefiles that follow it.';
COMMENT ON COLUMN source_file.file_number IS 'Sequence number from the file name: 1636 in pubmed26n1636.xml.gz. Updatefiles continue numbering after the baseline.';
COMMENT ON COLUMN source_file.file_order_key IS 'year_yy * 1000000 + file_number: PubMed''s chronological order as one integer. A row from a higher key supersedes one from a lower key.';
COMMENT ON COLUMN source_file.published_md5 IS 'Lower-case MD5 from NCBI''s .md5 sidecar. NULL for a file that was loaded without ever being synced by pubmed2db download.';
COMMENT ON COLUMN source_file.published_at IS 'When NCBI posted the file: the Last-Modified time of its .md5 sidecar. NULL for a file never synced by pubmed2db download.';
COMMENT ON COLUMN source_file.downloaded_at IS 'When pubmed2db download last fetched a new or changed copy. A file loaded without being downloaded is stamped with its first load time.';
COMMENT ON COLUMN source_file.processed_at IS 'When the file''s rows were last loaded. NULL means downloaded but never loaded: none of its rows are in this database.';
COMMENT ON COLUMN source_file.n_articles IS 'PubmedArticle records stored from the file. A PMID repeated within one file is stored once (the last occurrence) and counted once.';
COMMENT ON COLUMN source_file.n_deletions IS 'PMIDs listed in the file''s DeleteCitation element.';
COMMENT ON COLUMN source_file.n_failed IS 'PubmedArticle records the parser rejected and did not store (a missing title, journal ID or other required element).';
COMMENT ON COLUMN source_file.n_book_records IS 'PubmedBookArticle records (Bookshelf citations) in the file. They are counted but not parsed: no table holds them.';

-- Records when steps that leave no other timestamp were last run (currently just
-- `journals`, whose tables are replaced wholesale). Download/load recency is
-- derived from source_file instead, so it can never disagree with the data.
CREATE TABLE IF NOT EXISTS pipeline_run (
    step        TEXT PRIMARY KEY,                 -- e.g. 'journals'
    last_run_at TIMESTAMPTZ
);
COMMENT ON TABLE pipeline_run IS 'Internal. When pipeline steps that leave no other trace last ran (currently only ''journals''). Not part of the versioned data model; may change in any release.';

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
    pub_month      TEXT,                        -- raw PubDate/Month or /Season ('Mar', '03', 'Sep-Dec')
    pub_day        TEXT,                        -- raw PubDate/Day
    medline_date   TEXT,                        -- raw PubDate/MedlineDate (e.g. '1998 Spring')
    date_completed DATE,
    date_revised   DATE,
    loaded_at      TIMESTAMPTZ
);
COMMENT ON TABLE article IS 'Stable. One row per version of a PubMed article: each file that carries a PMID adds a version. (pmid, source_file) is unique, by construction rather than by constraint. Use latest_article for the current version of each PMID. Text is verbatim from the XML: not trimmed, inline markup flattened to its text, NULL where the element is absent.';
COMMENT ON COLUMN article.pmid IS 'PubMed ID (MedlineCitation/PMID).';
COMMENT ON COLUMN article.pmid_version IS 'MedlineCitation/PMID/@Version: 1 for almost every record, higher only for PubMed''s rare versioned citations. Not what orders versions here; file_order_key is.';
COMMENT ON COLUMN article.source_file IS 'The file this version came from; joins source_file.file_name. With pmid, identifies the version, and every per-version child table joins on the pair.';
COMMENT ON COLUMN article.file_order_key IS 'source_file.file_order_key of that file, copied here so latest_article can rank versions without a join.';
COMMENT ON COLUMN article.article_title IS 'Article/ArticleTitle. An empty element is stored as an empty string. PubMed brackets translated titles: [Title in English].';
COMMENT ON COLUMN article.nlm_catalog_id IS 'MedlineJournalInfo/NlmUniqueID; joins journal.nlm_catalog_id.';
COMMENT ON COLUMN article.issn_linking IS 'MedlineJournalInfo/ISSNLinking: the linking ISSN that groups a journal''s print and electronic ISSNs.';
COMMENT ON COLUMN article.volume IS 'Journal/JournalIssue/Volume.';
COMMENT ON COLUMN article.issue IS 'Journal/JournalIssue/Issue.';
COMMENT ON COLUMN article.pub_year IS 'JournalIssue/PubDate/Year. NULL when the date is a free-text medline_date instead.';
COMMENT ON COLUMN article.pub_month IS 'JournalIssue/PubDate/Month or, when the date is approximate, PubDate/Season (the DTD makes them exclusive): ''Mar'', ''03'', ''Spring'' and ''Sep-Dec'' all occur.';
COMMENT ON COLUMN article.pub_day IS 'JournalIssue/PubDate/Day.';
COMMENT ON COLUMN article.medline_date IS 'JournalIssue/PubDate/MedlineDate: a free-text date used instead of Year/Month/Day, e.g. ''1998 Spring'', ''1978 Jul-Aug'', ''1998 Dec-1999 Jan''.';
COMMENT ON COLUMN article.date_completed IS 'MedlineCitation/DateCompleted: when NLM finished indexing the record. A missing month or day reads as the 1st.';
COMMENT ON COLUMN article.date_revised IS 'MedlineCitation/DateRevised: when NLM last revised the record. A missing month or day reads as the 1st.';
COMMENT ON COLUMN article.loaded_at IS 'When this version was loaded into the database.';

-- Journal dimension, not versioned. Joined to articles on nlm_catalog_id.
-- Titles, abbreviations and ISSNs come from NLM's J_Entrez.txt; start_year,
-- end_year and active from the serial catalog (serfile). docs/JournalCatalog.md
-- says why it takes two sources.
CREATE TABLE IF NOT EXISTS journal (
    nlm_catalog_id       TEXT PRIMARY KEY,
    title                TEXT,
    abbreviation_medline TEXT,
    start_year           INTEGER,
    end_year             INTEGER,
    active               BOOLEAN
);
COMMENT ON TABLE journal IS 'Stable. One row per journal in NLM''s J_Entrez.txt, with publication years from NLM''s serial catalog. Not versioned: pubmed2db journals replaces the table wholesale. Join to article on nlm_catalog_id.';
COMMENT ON COLUMN journal.nlm_catalog_id IS 'NLM Catalog ID (NlmId in J_Entrez.txt).';
COMMENT ON COLUMN journal.title IS 'JournalTitle from J_Entrez.txt: the same string as NCBI esummary''s fulljournalname.';
COMMENT ON COLUMN journal.abbreviation_medline IS 'MedAbbr from J_Entrez.txt, the MEDLINE title abbreviation. J_Entrez''s ISO abbreviation equalled it for every journal measured, so it is not stored separately.';
COMMENT ON COLUMN journal.start_year IS 'PublicationFirstYear from NLM''s serial catalog. NULL when it is not a four-digit year (MARC wildcards like 19uu) or the catalog lacks the journal.';
COMMENT ON COLUMN journal.end_year IS 'PublicationEndYear from NLM''s serial catalog. NULL for a journal still publishing (9999), an unknown or wildcard year, or one the catalog lacks.';
COMMENT ON COLUMN journal.active IS 'Whether the journal is still publishing: true for end year 9999; false for a ceased journal, including partly known years like 19uu; NULL when unknown (uuuu) or not in the catalog.';

CREATE TABLE IF NOT EXISTS journal_issn (
    nlm_catalog_id TEXT NOT NULL,
    issn           TEXT NOT NULL,
    issn_type      TEXT
);
COMMENT ON TABLE journal_issn IS 'Stable. The print and electronic ISSNs J_Entrez.txt lists for each journal. Replaced with journal.';
COMMENT ON COLUMN journal_issn.nlm_catalog_id IS 'The journal; joins journal.nlm_catalog_id.';
COMMENT ON COLUMN journal_issn.issn IS 'The ISSN, e.g. 0028-0836.';
COMMENT ON COLUMN journal_issn.issn_type IS '''Print'' (J_Entrez''s ISSN (Print)) or ''Electronic'' (ISSN (Online)).';

CREATE TABLE IF NOT EXISTS abstract_text (
    pmid         BIGINT NOT NULL,
    source_file  TEXT   NOT NULL,
    position     INTEGER NOT NULL,             -- 0-based order of the section
    label        TEXT,
    nlm_category TEXT,
    text         TEXT
);
COMMENT ON TABLE abstract_text IS 'Stable. Per version: one row per non-empty Abstract/AbstractText section, in order. A structured abstract has several; most have one. OtherAbstract and CopyrightInformation are not stored.';
COMMENT ON COLUMN abstract_text.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN abstract_text.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN abstract_text.position IS '0-based order of the section among the stored (non-empty) ones, so without gaps.';
COMMENT ON COLUMN abstract_text.label IS 'The section''s @Label as the publisher wrote it, e.g. ''BACKGROUND''. NULL for an unstructured abstract.';
COMMENT ON COLUMN abstract_text.nlm_category IS 'NLM''s normalization of the label (@NlmCategory): BACKGROUND, OBJECTIVE, METHODS, RESULTS, CONCLUSIONS or UNASSIGNED. NULL when absent.';
COMMENT ON COLUMN abstract_text.text IS 'The section''s text, inline markup flattened.';

CREATE TABLE IF NOT EXISTS author (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    position    INTEGER NOT NULL,              -- 0-based <Author> index; gaps are dropped authors
    kind        TEXT,                           -- 'author' | 'collective'
    name        TEXT,
    orcid       TEXT,
    valid       BOOLEAN
);
COMMENT ON TABLE author IS 'Stable. Per version: one row per AuthorList/Author, a person or a collective (group) author. Authors the parser cannot name are dropped and leave a gap in position.';
COMMENT ON COLUMN author.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN author.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN author.position IS '0-based index of the Author element in its AuthorList. A gap is an author the parser could not name (no last name, or no fore name or initials) and who has no ORCID: such authors are not stored.';
COMMENT ON COLUMN author.kind IS '''author'' for a person, ''collective'' for a CollectiveName.';
COMMENT ON COLUMN author.name IS 'A person''s ''ForeName LastName'' (Initials when there is no fore name; Suffix dropped), or a collective''s name without its trailing period. NULL for a person identified only by ORCID.';
COMMENT ON COLUMN author.orcid IS 'Bare ORCID iD (0000-0002-1825-0097) from Identifier[@Source=ORCID], URL prefix removed; the checksum is not validated. NULL for collectives and authors without one.';
COMMENT ON COLUMN author.valid IS '@ValidYN: false for a name PubMed marks as erroneous, true otherwise (including when the attribute is absent). NULL for collectives.';

CREATE TABLE IF NOT EXISTS author_affiliation (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    author_position INTEGER NOT NULL,          -- joins author.position
    position    INTEGER NOT NULL,              -- 0-based order within the author's affiliations
    affiliation TEXT
);
COMMENT ON TABLE author_affiliation IS 'Stable. Per version: one row per non-empty AffiliationInfo/Affiliation of a person author. Collective authors'' affiliations are not stored, nor are affiliation identifiers.';
COMMENT ON COLUMN author_affiliation.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN author_affiliation.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN author_affiliation.author_position IS 'The author: joins author.position (with pmid and source_file).';
COMMENT ON COLUMN author_affiliation.position IS '0-based order among this author''s affiliations.';
COMMENT ON COLUMN author_affiliation.affiliation IS 'The affiliation as free text.';

CREATE TABLE IF NOT EXISTS mesh_heading (
    pmid          BIGINT NOT NULL,
    source_file   TEXT   NOT NULL,
    position      INTEGER NOT NULL,            -- 0-based order of the heading
    descriptor_ui TEXT,
    descriptor_name TEXT,
    major_topic   BOOLEAN
);
COMMENT ON TABLE mesh_heading IS 'Stable. Per version: one row per MeshHeadingList/MeshHeading, the MeSH descriptors NLM indexed the article with. Records not yet indexed have none.';
COMMENT ON COLUMN mesh_heading.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN mesh_heading.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN mesh_heading.position IS '0-based order of the heading. A descriptor can repeat with different qualifiers, so this, not descriptor_ui, is what mesh_qualifier.heading_position joins.';
COMMENT ON COLUMN mesh_heading.descriptor_ui IS 'DescriptorName/@UI, the MeSH descriptor ID, e.g. D006801.';
COMMENT ON COLUMN mesh_heading.descriptor_name IS 'DescriptorName text, e.g. Humans.';
COMMENT ON COLUMN mesh_heading.major_topic IS 'The descriptor''s own @MajorTopicYN. A heading that is major only through a qualifier is false here; see mesh_qualifier.major_topic.';

CREATE TABLE IF NOT EXISTS mesh_qualifier (
    pmid          BIGINT NOT NULL,
    source_file   TEXT   NOT NULL,
    heading_position INTEGER NOT NULL,         -- joins mesh_heading.position
    descriptor_ui TEXT,
    qualifier_ui  TEXT,
    qualifier_name TEXT,
    major_topic   BOOLEAN
);
COMMENT ON TABLE mesh_qualifier IS 'Stable. Per version: one row per QualifierName (subheading) under a MeSH heading.';
COMMENT ON COLUMN mesh_qualifier.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN mesh_qualifier.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN mesh_qualifier.heading_position IS 'The heading: joins mesh_heading.position (with pmid and source_file).';
COMMENT ON COLUMN mesh_qualifier.descriptor_ui IS 'The heading''s descriptor UI, repeated for convenience. Not a join key on its own: a descriptor can head more than one heading.';
COMMENT ON COLUMN mesh_qualifier.qualifier_ui IS 'QualifierName/@UI, the MeSH qualifier ID, e.g. Q000235.';
COMMENT ON COLUMN mesh_qualifier.qualifier_name IS 'QualifierName text, e.g. genetics.';
COMMENT ON COLUMN mesh_qualifier.major_topic IS 'The qualifier''s @MajorTopicYN.';

-- The name ("Review") and PubMed's order, which esummary's `pubtype` shares.
CREATE TABLE IF NOT EXISTS publication_type (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    position    INTEGER NOT NULL,              -- 0-based, PubMed's order
    type_ui     TEXT,                           -- MeSH UI of the publication type
    type_name   TEXT
);
COMMENT ON TABLE publication_type IS 'Stable. Per version: one row per Article/PublicationTypeList/PublicationType (Journal Article, Review, Retracted Publication, ...).';
COMMENT ON COLUMN publication_type.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN publication_type.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN publication_type.position IS '0-based, in PubMed''s order, which is not alphabetical and matches NCBI esummary''s pubtype list.';
COMMENT ON COLUMN publication_type.type_ui IS 'The publication type''s MeSH UI (@UI), e.g. D016454.';
COMMENT ON COLUMN publication_type.type_name IS 'Its name, e.g. Review. An empty string if the element is empty.';

-- Grants listed on the article (<GrantList>). Named `article_grant`, not
-- `grant`: GRANT is reserved in standard SQL and in Postgres, where anyone
-- loading the Parquet export would otherwise have to quote it.
CREATE TABLE IF NOT EXISTS article_grant (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    grant_id    TEXT,
    acronym     TEXT,
    agency      TEXT,
    country     TEXT
);
COMMENT ON TABLE article_grant IS 'Stable. Per version: one row per GrantList/Grant, the funding the article acknowledges. Unordered.';
COMMENT ON COLUMN article_grant.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN article_grant.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN article_grant.grant_id IS 'GrantID, the award number as written, e.g. R01 GM123456. NULL when absent.';
COMMENT ON COLUMN article_grant.acronym IS 'Acronym: the funding institute''s two-letter code, e.g. GM. NULL when absent.';
COMMENT ON COLUMN article_grant.agency IS 'Agency, e.g. NIGMS NIH HHS.';
COMMENT ON COLUMN article_grant.country IS 'Country of the agency, e.g. United States.';

-- No `reference_citation` table: the citation graph is deliberately not stored.
-- One real article carries ~444 references, so it would have been the largest
-- table here, and nothing downstream consumes it. `parse._cited_pmids` keeps the
-- extraction if that changes.

CREATE TABLE IF NOT EXISTS article_id (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    id_type     TEXT,                           -- 'doi', 'pmc', ...
    id_value    TEXT
);
COMMENT ON TABLE article_id IS 'Stable. Per version: the article''s own identifiers, from PubmedData/ArticleIdList (never those of the references it cites). The pubmed entry is omitted, since it repeats pmid. Unordered.';
COMMENT ON COLUMN article_id.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN article_id.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN article_id.id_type IS 'ArticleId/@IdType as PubMed writes it: doi, pmc, pii, mid and others. An open set: a new type appearing is not a data model change.';
COMMENT ON COLUMN article_id.id_value IS 'The identifier, whitespace-trimmed, with its published case (PubMed is not consistent; DOIs are case-insensitive). A pmc value already starts with PMC.';

CREATE TABLE IF NOT EXISTS history (
    pmid        BIGINT NOT NULL,
    source_file TEXT   NOT NULL,
    pub_status  TEXT,
    date        DATE
);
COMMENT ON TABLE history IS 'Stable. Per version: one row per PubmedData/History/PubMedPubDate, the record''s dated life events (received, accepted, entered PubMed, ...). Unordered.';
COMMENT ON COLUMN history.pmid IS 'With source_file, the article version; joins article.';
COMMENT ON COLUMN history.source_file IS 'With pmid, the article version; joins article.';
COMMENT ON COLUMN history.pub_status IS '@PubStatus: received, accepted, revised, aheadofprint, ecollection, pubmed, medline, entrez, pmc-release or retracted. Entries with any other status are not stored.';
COMMENT ON COLUMN history.date IS 'The event''s date. A missing month or day reads as the 1st; Hour and Minute are dropped.';

-- PMIDs removed via <DeleteCitation>. file_order_key lets the latest_article
-- view decide whether a deletion supersedes the latest article version.
CREATE TABLE IF NOT EXISTS deleted_pmid (
    pmid           BIGINT NOT NULL,
    source_file    TEXT   NOT NULL,
    file_order_key BIGINT NOT NULL
);
COMMENT ON TABLE deleted_pmid IS 'Provenance. One row per PMID listed in a file''s DeleteCitation, whether or not that PMID was ever loaded. latest_article hides a PMID whose last deletion is in the same file as its latest version or a later one.';
COMMENT ON COLUMN deleted_pmid.pmid IS 'The deleted PubMed ID.';
COMMENT ON COLUMN deleted_pmid.source_file IS 'The file that deleted it; joins source_file.file_name.';
COMMENT ON COLUMN deleted_pmid.file_order_key IS 'That file''s file_order_key, compared against article versions'' to decide which came last.';

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
COMMENT ON VIEW latest_article IS 'Stable. The current version of every PMID: its article row from the highest file_order_key, unless a deletion in that file or a later one removed it. A PMID re-added after a deletion is current again. Columns as article. Computed on every read (a window over all of article); materialize it for repeated use.';

-- The latest version's rows of each per-version child table: the
-- (pmid, source_file) join a consumer would otherwise write against
-- latest_article by hand, and get wrong by joining on pmid alone (which mixes
-- every version's rows). Each is a semi-join, so it costs a scan of
-- latest_article; for many queries, materialize latest_article first, as the
-- exports do.
CREATE OR REPLACE VIEW latest_abstract_text AS
SELECT c.* FROM abstract_text c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_abstract_text IS 'Stable. The abstract_text rows of each PMID''s latest_article version. Columns as abstract_text.';

CREATE OR REPLACE VIEW latest_author AS
SELECT c.* FROM author c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_author IS 'Stable. The author rows of each PMID''s latest_article version. Columns as author.';

CREATE OR REPLACE VIEW latest_author_affiliation AS
SELECT c.* FROM author_affiliation c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_author_affiliation IS 'Stable. The author_affiliation rows of each PMID''s latest_article version. Columns as author_affiliation.';

CREATE OR REPLACE VIEW latest_mesh_heading AS
SELECT c.* FROM mesh_heading c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_mesh_heading IS 'Stable. The mesh_heading rows of each PMID''s latest_article version. Columns as mesh_heading.';

CREATE OR REPLACE VIEW latest_mesh_qualifier AS
SELECT c.* FROM mesh_qualifier c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_mesh_qualifier IS 'Stable. The mesh_qualifier rows of each PMID''s latest_article version. Columns as mesh_qualifier.';

CREATE OR REPLACE VIEW latest_publication_type AS
SELECT c.* FROM publication_type c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_publication_type IS 'Stable. The publication_type rows of each PMID''s latest_article version. Columns as publication_type.';

CREATE OR REPLACE VIEW latest_article_grant AS
SELECT c.* FROM article_grant c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_article_grant IS 'Stable. The article_grant rows of each PMID''s latest_article version. Columns as article_grant.';

CREATE OR REPLACE VIEW latest_article_id AS
SELECT c.* FROM article_id c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_article_id IS 'Stable. The article_id rows of each PMID''s latest_article version. Columns as article_id.';

CREATE OR REPLACE VIEW latest_history AS
SELECT c.* FROM history c
SEMI JOIN latest_article la ON la.pmid = c.pmid AND la.source_file = c.source_file;
COMMENT ON VIEW latest_history IS 'Stable. The history rows of each PMID''s latest_article version. Columns as history.';
