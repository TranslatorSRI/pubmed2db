# The pubmed2db data model, version 1.0

pubmed2db turns PubMed's XML distribution into a local copy of PubMed that can be
queried and redistributed:

- a DuckDB database that keeps every version of every record;
- a Parquet export of the same tables;
- an NDJSON export with one flat record per current article.

This document is the contract for all three: what each table and field means,
where it comes from in PubMed's XML, what was left out and why, and how the
design was decided.

It is written to be read without the repository. Anything it links outside
itself is an absolute URL, and the column-by-column reference at the end is
generated from the comments shipped inside the database, so a copy of
`pubmed.duckdb` documents itself the same way:

```sql
SELECT table_name, column_name, comment FROM duckdb_columns() WHERE schema_name = 'main';
```

**Version.** This is data model **1.0.0**, first produced by the data build after
`2026sep26`. Earlier builds (`2026aug21`, `2026sep26`) predate the data model:
current code refuses their databases, and their tables differ in the ways
[listed in the changelog](#changelog).

## Contents

- [What pubmed2db produces](#what-pubmed2db-produces)
- [Versioning and stability](#versioning-and-stability)
- [How article versions work](#how-article-versions-work)
- [The tables](#the-tables)
- [The NDJSON record](#the-ndjson-record)
- [What is not in the data](#what-is-not-in-the-data)
- [How the design was decided](#how-the-design-was-decided)
- [Recipes](#recipes)
- [Known limitations](#known-limitations)
- [Changelog](#changelog)
- [Column reference](#column-reference)

## What pubmed2db produces

| Output | What it holds | Use it when |
| --- | --- | --- |
| `pubmed.duckdb` | Every table, with **every version** of every article, plus `latest_*` views of the current versions | You want history, joins across everything, or to run pubmed2db's own steps |
| Parquet export (`<table>.parquet`) | The same tables and columns as files. By default only the current versions (`export --format parquet`); `--all` for history | You want all of PubMed in a portable columnar form, readable by DuckDB, Polars, Spark or pandas |
| NDJSON export (`pubmed_metadata_*.ndjson.gz`) | One JSON object per current PMID, with the [DocumentMetadataAPI](https://github.com/NCATSTranslator/DocumentMetadataAPI) field names | You need citation metadata, identifiers and abstracts, and nothing else (Babel, Node Annotator) |

Every export directory also holds `metadata.json`. It records:
- the data model version and the export format;
- the record count (`records`: current articles, the same in every format; an `--all` Parquet export adds `article_versions`, every version in its article file);
- the code and database it came from;
- the corpus cutoff;
- a SHA-256 for every file, repeated in `SHA256SUMS` for `sha256sum -c`.

`pubmed2db validate` checks the directory against it and writes
`validation_report.json`, which repeats the data model version.

Builds made by the Translator team are published at
`https://stars.renci.org/var/babel_outputs/pubmed2db/<build>/`. A build is named
for its cutoff: the UTC date NCBI posted the last file it loaded
(`metadata.json`'s `corpus.build_name`). The two pre-1.0 builds, `2026aug21`
and `2026sep26`, were named for the day their export finished. The directory holds the
database, the NDJSON shards and a `manifests/` directory with the validation
report and a sorted list of the build's PMIDs.

### The size of a build

The 2026sep26 build, measured from its published database on 2026-10-19. It
loaded the 2026 baseline (`pubmed26n0001`–`pubmed26n1334`) and 302
updatefiles (`pubmed26n1335`–`pubmed26n1636`). Its tables are named as before
1.0, but they hold the same rows.

| What | Count |
| --- | --- |
| `pubmed.duckdb` | 76,216,283,136 bytes (71 GiB) |
| NDJSON shards (8, gzipped) | 16.9 GB |
| Current articles (`latest_article`, one NDJSON record each) | 41,153,516 |
| Article versions (`article`): 39,994,988 from the baseline, 5,840,554 from updates | 45,835,542 |
| Distinct PMIDs ever loaded | 41,160,893 |
| Deletions (`deleted_pmid`) | 11,872 |
| `mesh_heading` / `mesh_qualifier` | 378,757,395 / 206,230,780 |
| `author` (474,928 of them collective) / `author_affiliation` | 227,185,451 / 175,345,534 |
| `history` | 181,957,651 |
| `publication_type` | 75,706,563 |
| `article_id` | 73,864,609 |
| `abstract_text` | 63,700,472 |
| `article_grant` | 20,638,054 |
| `journal` / `journal_issn` | 42,097 / 51,772 |

The per-version tables count every version, not just the current ones.
`article_id.id_type` held `doi` (36,875,372), `pii` (21,841,008), `pmc`
(13,787,614), `mid` (1,360,187), and a few hundred `pmcid`, `medline` and
`pmpid`.

### Getting a build, and checking it

**Download, don't query in place.** DuckDB can `ATTACH` a database over HTTPS
read-only, but against this server even listing the tables of the 76 GB
`2026sep26` database had not finished after five minutes (tried 2026-10-19).
Download the files first.

**Use HTTP/1.1, and resume.** The same day, an HTTP/2 transfer of that database
dropped after 6 MB with `HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR`.
HTTP/1.1 with resume was reliable:

```bash
BUILD=https://stars.renci.org/var/babel_outputs/pubmed2db/<build>
curl --http1.1 --retry 5 -C - -O "$BUILD/pubmed.duckdb"
```

**Check the shards against the build's own list.** A consumer that takes only
the NDJSON, as Babel does, should download `metadata.json` and `SHA256SUMS`
with the shards, then:

1. Check `metadata.json`'s `data_model_version` against the major version the
   consumer was written for.
2. Run `sha256sum -c SHA256SUMS` (`shasum -a 256 -c` on macOS) in the download
   directory. This fails for a shard that is missing, or that is intact but
   comes from another build, such as one left over from the previous download.
3. Optionally, compare the records read against `metadata.json`'s `records`.

A truncated or corrupted shard needs none of this. DuckDB writes each shard as
one gzip stream, so damage fails decompression, either as a premature end of
stream or as a CRC error. Checksums cover the export's files only, not
`pubmed.duckdb`.

## Versioning and stability

The data model has its own version, `MAJOR.MINOR.PATCH` in the standard
[semantic versioning](https://semver.org/) sense. It says how *your* code is
affected. pubmed2db's code version (`v1.2`, …) is separate and says how the
code itself changed, so many code releases can share one data model.

- **Patch** (1.0.0 → 1.0.1) fixes values without changing the shape or meaning
  of anything: a parsing bug that left a field blank, a normalization that
  missed a case. Your code needs no change; some outputs will differ, for the
  better. The changelog says whether an existing database needs a reload to
  pick the fix up.
- **Minor** (1.0 → 1.1) only *adds*: a table, a column (appended, never
  inserted), a view, or an NDJSON field. Code written against 1.0 keeps working
  against 1.1.
- **Major** (1 → 2) renames, removes, retypes or changes the meaning of
  something. Changing an ordering column's base, or what NULL means in a
  column, counts as a change of meaning. A major bump often leaves one output
  untouched while breaking another, so its [changelog](#changelog) entry says,
  **output by output**, what a consumer must do. Pin the major version, and
  read that entry before moving to the next one.
- **No bump** is needed for:
  - new values in a column documented as an open vocabulary, such as `article_id.id_type`;
  - new rows;
  - reworded comments;
  - anything in the *internal* tier.

**Check the major version before reading anything else.** Each output gives it:

| Output | Where the version is |
| --- | --- |
| DuckDB | `SELECT value FROM pubmed2db_metadata WHERE key = 'data_model_version'` |
| Parquet | `pubmed2db_metadata.parquet`, the same key, or `metadata.json` |
| NDJSON | `metadata.json`'s `data_model_version`, also in `validation_report.json` |

Each table and view belongs to one of three **stability tiers**. A table's
comment starts with its tier.

| Tier | What | Promise |
| --- | --- | --- |
| **Stable** | `article` and its per-version child tables, `journal`, `journal_issn`, every `latest_*` view, every NDJSON field | Changes only with the version, as above |
| **Provenance** | `source_file`, `deleted_pmid`, `pubmed2db_metadata` | Columns are versioned like stable ones; their values describe how this copy was built (timestamps, counts), so expect them to differ between two builds of the same data |
| **Internal** | `pipeline_run` | Bookkeeping for pubmed2db itself; may change in any release |

pubmed2db's own code enforces the rules on the database it writes:
- It refuses to open a database from another major version, or from a newer
  minor version, and leaves that database untouched.
- It upgrades an older minor version in place, since that only adds columns.
- It opens any patch level, since a patch cannot change the shape.
- It refuses every database built before 1.0.

The repository's tests pin each version's exact shape, and check that a minor
version only ever adds to the one before it.

## How article versions work

PubMed distributes its corpus as an annual **baseline**, a complete re-issue,
followed by daily **updatefiles**. An updatefile carries every record that
changed, in full, plus a `<DeleteCitation>` list of PMIDs that were removed.
pubmed2db loads every file it is given and keeps all of it:

- **A version** is one PMID as it appeared in one file. `article` has one row per
  version, and `(pmid, source_file)` identifies it. Every per-version child
  table (`author`, `mesh_heading`, …) carries the same pair, and that pair is
  the join key, never `pmid` alone. The pair is unique because of how the loader
  works, not because of a constraint: a PMID repeated within one file is stored
  once, as its last occurrence.
- **Order** comes from the file, not from a date in the record.
  `file_order_key = year_yy * 1000000 + file_number` gives
  `pubmed26n0001` < `pubmed26n1334` (the last baseline file) < `pubmed26n1335`
  (the first update) < `pubmed27n0001`. A higher key supersedes a lower one.
- **The current version** of a PMID is its row with the highest
  `file_order_key`, unless a `DeleteCitation` in that same file or a later one
  removed it. `latest_article` computes exactly this, and `latest_<table>`
  gives each child table's rows for those versions. Three consequences:
  - **Deletion in the same file.** A PMID that a file both carries and deletes
    is deleted.
  - **Re-adding.** A PMID re-added by a file after the one that deleted it is
    current again.
  - **Stale deletions.** A deletion older than the PMID's newest version has no
    effect.
- **A new baseline year is a second full copy.** Loading `pubmed27n*` on top of
  a 2026 database stores every PMID again. It stays correct, since the 2027
  rows outrank the 2026 ones, but it is additive: a PMID dropped from the new
  baseline *without* a `DeleteCitation` keeps its 2026 row as its current
  version. pubmed2db's operating notes prescribe a fresh database each
  baseline year. A database built that way holds that year's baseline and its
  updates, not all of PubMed's history; `source_file` says which files any
  given database holds.

The **cutoff** of a build is the last file it loaded. The next file in the
sequence belongs to the next build. `source_file.published_at` records when NCBI
posted each file, and `metadata.json`'s `corpus` block reports the cutoff file
and that time, so a consumer can tell exactly which daily update a build stops
at.

## The tables

The [column reference](#column-reference) lists every column. This section covers
what a column list can't: the grain of each table, how the tables join, and
the traps.

```
                          ┌──────────────┐
  journal ─< journal_issn │ source_file  │──< deleted_pmid
     │                    └──────┬───────┘
     │ nlm_catalog_id            │ file_name = source_file
     └──────────────<  article (pmid, source_file)  >──── latest_article (view)
                          │
     (pmid, source_file)  ├──< abstract_text        position
                          ├──< author               position ─< author_affiliation (author_position, position)
                          ├──< mesh_heading         position ─< mesh_qualifier (heading_position)
                          ├──< publication_type     position
                          ├──< article_grant
                          ├──< article_id
                          └──< history
```

**Field values are verbatim.** Text is stored as PubMed wrote it:
- not trimmed, and not case-folded;
- inline markup such as `<i>` flattened to its text;
- NULL where the element is absent.

The NDJSON export is where values get normalized.

### Articles

- **`article`**: one row per version, holding the citation: title, journal ID,
  volume, issue, and the publication date in its raw parts.
  - `pub_year`, `pub_month` and `pub_day` are `PubDate`'s `<Year>`,
    `<Month>`/`<Season>` and `<Day>`.
  - `medline_date` is the free-text `<MedlineDate>` PubMed uses instead for
    ranges and seasons, such as `1998 Dec-1999 Jan`.
  - A record has either the parts or `medline_date`, never both.
- **`abstract_text`**: one row per abstract section, in order. To get the whole
  abstract, concatenate `text` ordered by `position`; the NDJSON export does
  that, joining with a space and dropping the labels.
- **`author`** and **`author_affiliation`**:
  - `author.position` is the 0-based index of the `<Author>` element. Authors
    the parser cannot name are dropped, so the positions can have **gaps**. A
    position always identifies the same XML element, which matters because
    `author_affiliation.author_position` joins on it.
  - Collective (group) authors have `kind = 'collective'` and no ORCID, validity
    or affiliations.
- **`mesh_heading`** and **`mesh_qualifier`**:
  - A qualifier joins its heading on `heading_position`, **not** on
    `descriptor_ui`. The DTD lets a descriptor repeat within one record with
    different qualifiers, and a join on the UI would then attach each qualifier
    to every copy. That case does not occur today: not one article version
    in the whole 2026sep26 build repeats a descriptor, across 378,757,395
    headings. But a join on position is right whether or not it happens, and
    it stays right if PubMed starts to.
  - `mesh_heading.major_topic` is the descriptor's own flag. PubMed's "major
    topic" star can also come from a qualifier.
- **`publication_type`**: in PubMed's order. That order is not alphabetical, and
  it is the order NCBI's `esummary` serves as `pubtype`.
- **`article_id`**: the article's *own* identifiers (DOI, PMC ID, PII, …) from
  `PubmedData/ArticleIdList`.
  - It never includes the identifiers of references the article cites. A
    careless XPath picks those up, and one record would have gained 426 foreign
    DOIs.
  - `id_type` is an open vocabulary.
  - Match DOIs case-insensitively. PubMed is not consistent about their case.
- **`article_grant`** and **`history`**: unordered. `history` holds the record's
  dated events: `received`, `accepted`, `pubmed` (entered PubMed), `medline`,
  `retracted` and others.

### Journals

**`journal`** and **`journal_issn`** are a dimension, not versioned: `pubmed2db
journals` replaces them wholesale from NLM's current files. Join on
`nlm_catalog_id`.

They come from two NLM sources, deliberately:
- **`J_Entrez.txt`** supplies `title` and `abbreviation_medline`. Its title is
  byte-for-byte what NCBI's own services call `fulljournalname`.
- **The serial catalog** supplies `start_year`, `end_year` and `active`. About
  1% of journals, mostly proceedings volumes, are not in it and keep NULL years.

The [journal catalog note](https://github.com/TranslatorSRI/pubmed2db/blob/main/docs/JournalCatalog.md)
has the measurements behind that split.

### Provenance

- **`source_file`** is the registry of every file this database knows about:
  - which files were loaded (`processed_at` is not NULL);
  - what each contributed (`n_articles`, `n_deletions`);
  - what was skipped (`n_failed`, `n_book_records`);
  - when NCBI published it (`published_at`).

  The skip counts are the only trace of records that never became rows (see
  [What is not in the data](#what-is-not-in-the-data)).
- **`deleted_pmid`** lists every PMID a `DeleteCitation` named, whether or not
  that PMID was ever loaded.
- **`pubmed2db_metadata`** holds the data model version and which pubmed2db
  commit created and last loaded the database.

## The NDJSON record

One JSON object per line, one line per row of `latest_article`. Every field is
present on every record. Absent values are `""` or `[]`, never `null`. Records
are not in PMID order.

| Field | From | Notes |
| --- | --- | --- |
| `id` | `article.pmid` | `"PMID:<pmid>"` |
| `identifiers` | `article_id` (`doi`, `pmc`) | `PMID:` first, then the DOI and PMC ID as CURIEs (`doi:10…`, `PMC:PMC…`), sorted. Values verbatim; match case-insensitively. `PMC:` follows Babel until Translator settles the prefix ([see below](#known-limitations)). |
| `journal_name` | `journal.title` | `""` when the journal dimension has no row. |
| `journal_abbrev` | `journal.abbreviation_medline` | |
| `article_title` | `article.article_title` | |
| `volume`, `issue` | `article` | |
| `pub_year` | `pub_year`, else the leading year of `medline_date` | The *leading* year of a cross-year range, as NCBI's `sortpubdate` does. |
| `pub_month` | `pub_month`, else the text after `medline_date`'s year | Month names fold to `Jan`…`Dec` (`03`, `March`, `Sept` → `Mar`, `Sep`). Seasons and ranges pass through (`Spring`, `Sep-Dec`). A cross-year range gives `Dec-1999 Jan`. |
| `pub_day` | `article.pub_day` | Verbatim (`05` stays `05`). |
| `pub_date` | `medline_date`, else year + normalized month + day | PubMed's own date string, the same as `esummary`'s `pubdate`. Render citations from this; sort on `pub_year`. |
| `publication_types` | `publication_type` | `[{"id": "MESH:D016454", "name": "Review"}, …]` in PubMed's order. |
| `abstract` | `abstract_text` | Sections joined with a space, labels dropped. |

The field list, and the SQL that computes each field, live in one place in the
code (`export._JSON_FIELDS`); the data model's tests pin the list.

## What is not in the data

Every omission below is deliberate or measured. None of it is silent:
whole-record skips are counted in `source_file`.

**Records PubMed's bulk files never contain.** About 37,000 PubMed records,
the NCBI Bookshelf chapters (StatPearls and other books), are in no build. On
2026-10-19 live PubMed had 40,108 records matching `pubmed books[filter]`.
2,694 of them reach a build as ordinary `PubmedArticle` records (monograph
series such as IARC's, catalogued with a journal ID). The other 37,414,
about 0.09% of PubMed, never appear in the baseline or updatefiles, which held
no `<PubmedBookArticle>` across all 1,636 files of the 2026sep26 build. E-utilities
`efetch` does serve them, as `<PubmedBookArticle>`. Whether a build should
fetch them that way is [#27](https://github.com/TranslatorSRI/pubmed2db/issues/27).

**Whole records that are not stored:**

| What | Why | Where it shows |
| --- | --- | --- |
| `<PubmedBookArticle>`, if a file ever carries one | Only journal citations are parsed; none occurred in the 2026 files (above) | `source_file.n_book_records` |
| Records the parser rejects: no `ArticleTitle`, no `MedlineJournalInfo`/`NlmUniqueID`, a `<Grant>` without `Agency` or `Country`, a non-numeric date, an unexpected `Y`/`N` flag | The upstream parser requires these | `source_file.n_failed` |
| An earlier copy of a PMID repeated within one file | The last copy wins | — |

**Parts of records that are not stored:**
- Authors the parser cannot name: no last name, or no fore name or initials,
  and no ORCID. They leave gaps in `author.position`. That was 159 of 318,366
  authors (0.05%) in the two files measured below.
- Empty abstract sections and empty affiliations.
- `history` entries whose `PubStatus` is outside the ten listed for
  `history.pub_status`, and the hour and minute of every entry.
- The `pubmed` entry of `ArticleIdList`, which repeats the PMID.
- Affiliations of collective authors.

**PubMed elements never read.** Nothing stores:
- `ReferenceList`: the citation graph. One article carries ~444 references, so
  it would be the largest table by far, and no consumer has asked for it
  ([#71](https://github.com/TranslatorSRI/pubmed2db/issues/71)).
- `KeywordList`, `ChemicalList`, `SupplMeshList`, `CommentsCorrectionsList`
  (errata and retraction notices), `DataBankList`
  ([#20](https://github.com/TranslatorSRI/pubmed2db/issues/20)), `GeneSymbolList`,
  `InvestigatorList`, `PersonalNameSubjectList`, `OtherID` and `OtherAbstract`.
- `Language`, `VernacularTitle`, `Pagination`, `ELocationID`
  ([#35](https://github.com/TranslatorSRI/pubmed2db/issues/35)), `ArticleDate`,
  `CoiStatement`, and `PublicationStatus`.
- `MedlineCitation`'s `@Status`, `@Owner` and `@IndexingMethod`,
  `CitationSubset` and `NumberOfReferences`.
- `Journal/Title`, `ISOAbbreviation` and the article's own `Journal/ISSN`. The
  journal dimension supplies the journal instead.
- Author `Suffix`, `Role`, `@EqualContrib`, non-ORCID `Identifier`s, and
  affiliation identifiers such as ROR.
- MeSH `DescriptorName/@Type`.

Each would be a new table or column, which is a minor version bump.

**How often the parser drops something.** Whole-record skips are counted in
`source_file`, so the corpus figure comes from the 2026sep26 build: across all
1,636 files (45,835,542 article versions), `n_failed` and `n_book_records`
are both **0**. Nothing was rejected, and the 2026 files carry no Bookshelf
records. The same build has at least 76,451 gaps in `author.position`, from
authors dropped as unnameable: 0.03% of 227,185,451 authors. That is a lower
bound, since a dropped *last* author leaves no gap. The parts of records that
`source_file` doesn't count were measured on 2026-10-19, by parsing two real
files with pubmed2db's parser and counting the raw elements beside it:

| File | Records | Rejected (`n_failed`) | Book records | Unnamed authors dropped | History entries dropped | Records with a repeated descriptor |
| --- | --- | --- | --- | --- | --- | --- |
| `pubmed26n1200` (baseline) | 30,000 | 0 | 0 | 113 of 196,432 | 0 | 0 |
| `pubmed26n1636` (update) | 16,414 | 0 | 0 | 46 of 121,934 | 0 | 0 |

Every `<Grant>` in both files had the `Agency` and `Country` the parser
requires. Every `PubStatus` was one of the ten stored. The DTD also allows other
statuses (`epublish`, `ppublish`, `pmc`, `pmcr`, `pubmedr`, `premedline`,
`medliner`); none occurred.

## How the design was decided

Most choices above were settled by measurement, not by reading the DTD. The
methods are reusable.

- **The archival XML is the ground truth, not Entrez.** NCBI's `efetch` serves a
  *re-serialization* of a record, not the file it was loaded from. It serves
  PMID 152567 as `<Year>1978</Year><Season>Jul-Aug</Season>`, where the baseline
  holds `<MedlineDate>1978 Jul-Aug</MedlineDate>`. It can even serve values the
  archive never had: an issue number, a whole abstract, a DOI. So before
  concluding anything about parsing, download the baseline file holding the PMID
  and read the raw element. `validate` compares against `efetch` and folds those
  renderings before it counts a mismatch.
- **`esummary` shows NCBI's own normalization decisions.** It settled two
  things:
  - `pub_date` copies `esummary`'s `pubdate`, including its unpadded day. NCBI
    renders `<Day>01</Day>` as `1`.
  - `pub_year` takes a range's leading year, as `esummary`'s `sortpubdate` does
    (`"1998 Dec-1999 Jan"` → `1998/01/01`).

  The DocumentMetadataAPI spec contradicts itself on approximate months. Its
  prose asks for three-letter abbreviations, while its worked example emits
  `"pub_month": "Sep-Dec"`. The export follows the example.
- **Measure the corpus by sampling PMIDs.** Draw random integers below the
  highest PMID, send them to `esummary` 300 at a time (PMIDs that don't exist
  simply drop out), and tally. About 6,000 records take under a minute, and
  showed:
  - cross-year date ranges are ~0.07% of PubMed, so a verbatim `pub_date` beat
    a special case;
  - single-year ranges are ~7%, so passing them through mattered.

  A sample of 3,600 random PMIDs showed that about half carry two or more
  publication types, and 89% carry Journal Article.
- **Journals need two sources.** `J_Entrez.txt`'s title is NCBI's
  `fulljournalname` byte for byte, while the serial catalog's title differs for
  20% of journals; but only the catalog has publication years. `J_Entrez.txt`'s
  ISO abbreviation equalled its MEDLINE abbreviation in 42,056 of 42,056
  journals when first measured, and in all 42,097 of the 2026sep26 build,
  which is why 1.0 stores one. The measurements and the recipe to
  repeat them are in the
  [journal catalog note](https://github.com/TranslatorSRI/pubmed2db/blob/main/docs/JournalCatalog.md).
- **Check every XPath's scope.** pubmed2db reuses
  [`pubmed-downloader`](https://github.com/cthoyt/pubmed-downloader)'s parser,
  whose descendant (`.//`) searches reach into `<ReferenceList>`. That is how
  one record gained 426 cited DOIs as its own. `article_id` and
  `publication_type` use exact paths for that reason.
- **Times are instants.** Every timestamp is `TIMESTAMPTZ`. The pre-1.0 naive
  timestamps held the loading machine's local time, so they could not be read
  without knowing that machine's zone.

## Recipes

Open a build read-only so nothing tries to write to it:

```sql
ATTACH 'pubmed.duckdb' AS pubmed (READ_ONLY);
USE pubmed;
SELECT key, value FROM pubmed2db_metadata;
```

The current version of one article, with its abstract and identifiers:

```sql
SELECT la.pmid, la.article_title, j.title AS journal,
       (SELECT string_agg(text, ' ' ORDER BY position) FROM latest_abstract_text t
        WHERE t.pmid = la.pmid) AS abstract,
       (SELECT list(id_type || ':' || id_value) FROM latest_article_id i
        WHERE i.pmid = la.pmid) AS ids
FROM latest_article la LEFT JOIN journal j USING (nlm_catalog_id)
WHERE la.pmid = 30690000;
```

Reviews by publication year, from the Parquet export:

```sql
SELECT a.pub_year, count(*)
FROM 'parquet/article.parquet' a
JOIN 'parquet/publication_type.parquet' pt USING (pmid, source_file)
WHERE pt.type_name = 'Review'
GROUP BY ALL ORDER BY 1;
```

The authors of an article, with each one's affiliations:

```sql
SELECT a.position, a.name, a.orcid, list(af.affiliation ORDER BY af.position) AS affiliations
FROM latest_author a
LEFT JOIN latest_author_affiliation af
  ON af.pmid = a.pmid AND af.source_file = a.source_file AND af.author_position = a.position
WHERE a.pmid = 30690000
GROUP BY ALL ORDER BY a.position;
```

MeSH headings with their qualifiers, joined by position:

```sql
SELECT h.descriptor_name, h.major_topic, list(q.qualifier_name) FILTER (q.qualifier_name IS NOT NULL)
FROM latest_mesh_heading h
LEFT JOIN latest_mesh_qualifier q
  ON q.pmid = h.pmid AND q.source_file = h.source_file AND q.heading_position = h.position
WHERE h.pmid = 30690000
GROUP BY ALL ORDER BY any_value(h.position);
```

What a Babel-style compendium needs, which is every current PMID with its title
and its DOI and PMC ID:

```sql
SELECT la.pmid, la.article_title,
       list(i.id_value) FILTER (i.id_type = 'doi') AS dois,
       list(i.id_value) FILTER (i.id_type = 'pmc') AS pmcids
FROM latest_article la
LEFT JOIN article_id i USING (pmid, source_file)
GROUP BY ALL;
```

Every PMID deleted since an earlier build, given that build's cutoff file:

```sql
SELECT DISTINCT d.pmid
FROM deleted_pmid d JOIN source_file s ON s.file_name = d.source_file
WHERE s.file_order_key > (SELECT file_order_key FROM source_file
                          WHERE file_name = 'pubmed26n1636.xml.gz')
  AND d.pmid NOT IN (SELECT pmid FROM latest_article);
```

How an article changed across versions:

```sql
SELECT a.source_file, s.published_at, a.article_title, a.date_revised
FROM article a JOIN source_file s ON s.file_name = a.source_file
WHERE a.pmid = 30690000 ORDER BY a.file_order_key;
```

`latest_article` is recomputed on every read: it runs a window over every
version. For many queries against a large database, materialize it once:

```sql
CREATE TEMP TABLE current AS SELECT * FROM latest_article;
```

## Known limitations

- **NCBI Bookshelf chapters are absent**: about 37,000 PMIDs, which PubMed's
  bulk files don't contain
  ([#27](https://github.com/TranslatorSRI/pubmed2db/issues/27); see
  [What is not in the data](#what-is-not-in-the-data)).
- **Grounding is off.** Affiliations and author names are not linked to ROR or
  ORCID beyond the ORCIDs PubMed itself supplies, and those checksums are not
  validated.
- **No indexes or keys** on the per-version tables. The uniqueness of
  `(pmid, source_file)` comes from how the loader works, which keeps bulk loads
  fast. A point lookup relies on DuckDB's per-block min/max statistics rather
  than an index.
- **No sortable date.** There is nothing like NCBI's `sortpubdate`. Sort on
  `pub_year`, or parse `pub_date`.
- **`pub_month` can carry a year** for a cross-year range (`Dec-1999 Jan`),
  because that is what PubMed wrote. Whether consumers accept it is
  [#43](https://github.com/TranslatorSRI/pubmed2db/issues/43).
- **The PMC ID prefix in `identifiers` is interim.** `PMC:` follows Babel
  until Translator picks between it and `PMCID:`, in
  [biolink-model#1366](https://github.com/biolink/biolink-model/issues/1366) and
  [Babel#1044](https://github.com/NCATSTranslator/Babel/issues/1044). This
  export follows that decision
  ([#33](https://github.com/TranslatorSRI/pubmed2db/issues/33)), as a major
  version. It affects only the NDJSON; the database stores PubMed's value.
- **The Parquet export has not yet been run on the full corpus**
  ([#29](https://github.com/TranslatorSRI/pubmed2db/issues/29)).

## Changelog

Newest first. Each entry says, per output, what a consumer has to do.

### 1.0.0

The first versioned data model, first built after `2026sep26`. The
`2026aug21` and `2026sep26` builds predate it.

| Output | What a consumer must do |
| --- | --- |
| NDJSON | Match PMC IDs in `identifiers` as `PMC:PMC…`, the prefix Babel uses, where earlier builds used `PMCID:`. Every other field is unchanged. `metadata.json` is new beside the shards, and worth checking downloads against. |
| Parquet | Update table and column names as below. `pubmed2db_metadata.parquet` and `metadata.json` are new. |
| DuckDB | Rebuild: pubmed2db refuses a pre-1.0 database. Update queries as below. |

| Pre-1.0 | 1.0 |
| --- | --- |
| no version anywhere | `pubmed2db_metadata`, `metadata.json`, and `data_model_version` in the validation report |
| `grant_` | `article_grant` |
| `abstract_text.seq` | `abstract_text.position` |
| `author.position` 1-based | 0-based, still with gaps |
| `author_affiliation.position` (the author's) | `author_position`, plus the affiliation's own `position` |
| `mesh_heading` unordered; qualifiers join on `descriptor_ui` | `mesh_heading.position`, `mesh_qualifier.heading_position` |
| `publication_type (…, type_ui, type_name, position)` | `position` moved next to `source_file` |
| `journal.abbreviation_iso` | dropped (equal to `abbreviation_medline` for every journal) |
| `author_affiliation.ror` | dropped (always NULL) |
| naive `TIMESTAMP`s in the loading machine's local time | `TIMESTAMPTZ` |
| — | `source_file.published_at`, and the `latest_<table>` views |

## Column reference

Generated from the comments in the database. Regenerate after changing
`schema.sql` with `python -m pubmed2db.datamodel --docs docs/DataModel.md`.

<!-- BEGIN GENERATED REFERENCE: python -m pubmed2db.datamodel --docs docs/DataModel.md -->

### `abstract_text`

Stable. Per version: one row per non-empty Abstract/AbstractText section, in order. A structured abstract has several; most have one. OtherAbstract and CopyrightInformation are not stored.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `position` | INTEGER | 0-based order of the section among the stored (non-empty) ones, so without gaps. |
| `label` | VARCHAR | The section's @Label as the publisher wrote it, e.g. 'BACKGROUND'. NULL for an unstructured abstract. |
| `nlm_category` | VARCHAR | NLM's normalization of the label (@NlmCategory): BACKGROUND, OBJECTIVE, METHODS, RESULTS, CONCLUSIONS or UNASSIGNED. NULL when absent. |
| `text` | VARCHAR | The section's text, inline markup flattened. |

### `article`

Stable. One row per version of a PubMed article: each file that carries a PMID adds a version. (pmid, source_file) is unique, by construction rather than by constraint. Use latest_article for the current version of each PMID. Text is verbatim from the XML: not trimmed, inline markup flattened to its text, NULL where the element is absent.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | PubMed ID (MedlineCitation/PMID). |
| `pmid_version` | INTEGER | MedlineCitation/PMID/@Version: 1 for almost every record, higher only for PubMed's rare versioned citations. Not what orders versions here; file_order_key is. |
| `source_file` | VARCHAR | The file this version came from; joins source_file.file_name. With pmid, identifies the version, and every per-version child table joins on the pair. |
| `file_order_key` | BIGINT | source_file.file_order_key of that file, copied here so latest_article can rank versions without a join. |
| `article_title` | VARCHAR | Article/ArticleTitle. An empty element is stored as an empty string. PubMed brackets translated titles: [Title in English]. |
| `nlm_catalog_id` | VARCHAR | MedlineJournalInfo/NlmUniqueID; joins journal.nlm_catalog_id. |
| `issn_linking` | VARCHAR | MedlineJournalInfo/ISSNLinking: the linking ISSN that groups a journal's print and electronic ISSNs. |
| `volume` | VARCHAR | Journal/JournalIssue/Volume. |
| `issue` | VARCHAR | Journal/JournalIssue/Issue. |
| `pub_year` | VARCHAR | JournalIssue/PubDate/Year. NULL when the date is a free-text medline_date instead. |
| `pub_month` | VARCHAR | JournalIssue/PubDate/Month or, when the date is approximate, PubDate/Season (the DTD makes them exclusive): 'Mar', '03', 'Spring' and 'Sep-Dec' all occur. |
| `pub_day` | VARCHAR | JournalIssue/PubDate/Day. |
| `medline_date` | VARCHAR | JournalIssue/PubDate/MedlineDate: a free-text date used instead of Year/Month/Day, e.g. '1998 Spring', '1978 Jul-Aug', '1998 Dec-1999 Jan'. |
| `date_completed` | DATE | MedlineCitation/DateCompleted: when NLM finished indexing the record. A missing month or day reads as the 1st. |
| `date_revised` | DATE | MedlineCitation/DateRevised: when NLM last revised the record. A missing month or day reads as the 1st. |
| `loaded_at` | TIMESTAMP WITH TIME ZONE | When this version was loaded into the database. |

### `article_grant`

Stable. Per version: one row per GrantList/Grant, the funding the article acknowledges. Unordered.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `grant_id` | VARCHAR | GrantID, the award number as written, e.g. R01 GM123456. NULL when absent. |
| `acronym` | VARCHAR | Acronym: the funding institute's two-letter code, e.g. GM. NULL when absent. |
| `agency` | VARCHAR | Agency, e.g. NIGMS NIH HHS. |
| `country` | VARCHAR | Country of the agency, e.g. United States. |

### `article_id`

Stable. Per version: the article's own identifiers, from PubmedData/ArticleIdList (never those of the references it cites). The pubmed entry is omitted, since it repeats pmid. Unordered.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `id_type` | VARCHAR | ArticleId/@IdType as PubMed writes it: doi, pmc, pii, mid and others. An open set: a new type appearing is not a data model change. |
| `id_value` | VARCHAR | The identifier, whitespace-trimmed, with its published case (PubMed is not consistent; DOIs are case-insensitive). A pmc value already starts with PMC. |

### `author`

Stable. Per version: one row per AuthorList/Author, a person or a collective (group) author. Authors the parser cannot name are dropped and leave a gap in position.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `position` | INTEGER | 0-based index of the Author element in its AuthorList. A gap is an author the parser could not name (no last name, or no fore name or initials) and who has no ORCID: such authors are not stored. |
| `kind` | VARCHAR | 'author' for a person, 'collective' for a CollectiveName. |
| `name` | VARCHAR | A person's 'ForeName LastName' (Initials when there is no fore name; Suffix dropped), or a collective's name without its trailing period. NULL for a person identified only by ORCID. |
| `orcid` | VARCHAR | Bare ORCID iD (0000-0002-1825-0097) from Identifier[@Source=ORCID], URL prefix removed; the checksum is not validated. NULL for collectives and authors without one. |
| `valid` | BOOLEAN | @ValidYN: false for a name PubMed marks as erroneous, true otherwise (including when the attribute is absent). NULL for collectives. |

### `author_affiliation`

Stable. Per version: one row per non-empty AffiliationInfo/Affiliation of a person author. Collective authors' affiliations are not stored, nor are affiliation identifiers.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `author_position` | INTEGER | The author: joins author.position (with pmid and source_file). |
| `position` | INTEGER | 0-based order among this author's affiliations. |
| `affiliation` | VARCHAR | The affiliation as free text. |

### `history`

Stable. Per version: one row per PubmedData/History/PubMedPubDate, the record's dated life events (received, accepted, entered PubMed, ...). Unordered.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `pub_status` | VARCHAR | @PubStatus: received, accepted, revised, aheadofprint, ecollection, pubmed, medline, entrez, pmc-release or retracted. Entries with any other status are not stored. |
| `date` | DATE | The event's date. A missing month or day reads as the 1st; Hour and Minute are dropped. |

### `journal`

Stable. One row per journal in NLM's J_Entrez.txt, with publication years from NLM's serial catalog. Not versioned: pubmed2db journals replaces the table wholesale. Join to article on nlm_catalog_id.

| Column | Type | Meaning |
| --- | --- | --- |
| `nlm_catalog_id` | VARCHAR | NLM Catalog ID (NlmId in J_Entrez.txt). |
| `title` | VARCHAR | JournalTitle from J_Entrez.txt: the same string as NCBI esummary's fulljournalname. |
| `abbreviation_medline` | VARCHAR | MedAbbr from J_Entrez.txt, the MEDLINE title abbreviation. J_Entrez's ISO abbreviation equalled it for every journal measured, so it is not stored separately. |
| `start_year` | INTEGER | PublicationFirstYear from NLM's serial catalog. NULL when it is not a four-digit year (MARC wildcards like 19uu) or the catalog lacks the journal. |
| `end_year` | INTEGER | PublicationEndYear from NLM's serial catalog. NULL for a journal still publishing (9999), an unknown or wildcard year, or one the catalog lacks. |
| `active` | BOOLEAN | Whether the journal is still publishing: true for end year 9999; false for a ceased journal, including partly known years like 19uu; NULL when unknown (uuuu) or not in the catalog. |

### `journal_issn`

Stable. The print and electronic ISSNs J_Entrez.txt lists for each journal. Replaced with journal.

| Column | Type | Meaning |
| --- | --- | --- |
| `nlm_catalog_id` | VARCHAR | The journal; joins journal.nlm_catalog_id. |
| `issn` | VARCHAR | The ISSN, e.g. 0028-0836. |
| `issn_type` | VARCHAR | 'Print' (J_Entrez's ISSN (Print)) or 'Electronic' (ISSN (Online)). |

### `mesh_heading`

Stable. Per version: one row per MeshHeadingList/MeshHeading, the MeSH descriptors NLM indexed the article with. Records not yet indexed have none.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `position` | INTEGER | 0-based order of the heading. A descriptor can repeat with different qualifiers, so this, not descriptor_ui, is what mesh_qualifier.heading_position joins. |
| `descriptor_ui` | VARCHAR | DescriptorName/@UI, the MeSH descriptor ID, e.g. D006801. |
| `descriptor_name` | VARCHAR | DescriptorName text, e.g. Humans. |
| `major_topic` | BOOLEAN | The descriptor's own @MajorTopicYN. A heading that is major only through a qualifier is false here; see mesh_qualifier.major_topic. |

### `mesh_qualifier`

Stable. Per version: one row per QualifierName (subheading) under a MeSH heading.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `heading_position` | INTEGER | The heading: joins mesh_heading.position (with pmid and source_file). |
| `descriptor_ui` | VARCHAR | The heading's descriptor UI, repeated for convenience. Not a join key on its own: a descriptor can head more than one heading. |
| `qualifier_ui` | VARCHAR | QualifierName/@UI, the MeSH qualifier ID, e.g. Q000235. |
| `qualifier_name` | VARCHAR | QualifierName text, e.g. genetics. |
| `major_topic` | BOOLEAN | The qualifier's @MajorTopicYN. |

### `publication_type`

Stable. Per version: one row per Article/PublicationTypeList/PublicationType (Journal Article, Review, Retracted Publication, ...).

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `position` | INTEGER | 0-based, in PubMed's order, which is not alphabetical and matches NCBI esummary's pubtype list. |
| `type_ui` | VARCHAR | The publication type's MeSH UI (@UI), e.g. D016454. |
| `type_name` | VARCHAR | Its name, e.g. Review. An empty string if the element is empty. |

### `deleted_pmid`

Provenance. One row per PMID listed in a file's DeleteCitation, whether or not that PMID was ever loaded. latest_article hides a PMID whose last deletion is in the same file as its latest version or a later one.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | The deleted PubMed ID. |
| `source_file` | VARCHAR | The file that deleted it; joins source_file.file_name. |
| `file_order_key` | BIGINT | That file's file_order_key, compared against article versions' to decide which came last. |

### `pubmed2db_metadata`

Provenance. Facts about this database as a whole, one per row: data_model_version (check its major version before reading anything else), created_at and created_by (the pubmed2db commit, as git describe), last_loaded_at and last_loaded_by. Timestamps are ISO 8601 in UTC.

| Column | Type | Meaning |
| --- | --- | --- |
| `key` | VARCHAR | The fact's name, e.g. data_model_version. |
| `value` | VARCHAR | Its value, as text. |

### `source_file`

Provenance. One row per PubMed XML file known to this database, loaded or not. The loaded file with the highest file_order_key is the build's cutoff: the next file belongs to the next build.

| Column | Type | Meaning |
| --- | --- | --- |
| `file_name` | VARCHAR | PubMed's file name, e.g. pubmed26n1636.xml.gz. Every per-version row's source_file joins here. |
| `kind` | VARCHAR | 'baseline' (the annual full re-issue) or 'update' (a daily updatefile). |
| `year_yy` | INTEGER | Two-digit release year from the file name: 26 for the 2026 baseline and the updatefiles that follow it. |
| `file_number` | INTEGER | Sequence number from the file name: 1636 in pubmed26n1636.xml.gz. Updatefiles continue numbering after the baseline. |
| `file_order_key` | BIGINT | year_yy * 1000000 + file_number: PubMed's chronological order as one integer. A row from a higher key supersedes one from a lower key. |
| `published_md5` | VARCHAR | Lower-case MD5 from NCBI's .md5 sidecar. NULL for a file that was loaded without ever being synced by pubmed2db download. |
| `published_at` | TIMESTAMP WITH TIME ZONE | When NCBI posted the file: the Last-Modified time of its .md5 sidecar. NULL for a file never synced by pubmed2db download. |
| `downloaded_at` | TIMESTAMP WITH TIME ZONE | When pubmed2db download last fetched a new or changed copy. A file loaded without being downloaded is stamped with its first load time. |
| `processed_at` | TIMESTAMP WITH TIME ZONE | When the file's rows were last loaded. NULL means downloaded but never loaded: none of its rows are in this database. |
| `n_articles` | INTEGER | PubmedArticle records stored from the file. A PMID repeated within one file is stored once (the last occurrence) and counted once. |
| `n_deletions` | INTEGER | PMIDs listed in the file's DeleteCitation element. |
| `n_failed` | INTEGER | PubmedArticle records the parser rejected and did not store (a missing title, journal ID or other required element). |
| `n_book_records` | INTEGER | PubmedBookArticle records (Bookshelf citations) in the file. They are counted but not parsed: no table holds them. |

### `pipeline_run`

Internal. When pipeline steps that leave no other trace last ran (currently only 'journals'). Not part of the versioned data model; may change in any release.

### Views

| View | Meaning |
| --- | --- |
| `latest_abstract_text` | Stable. The abstract_text rows of each PMID's latest_article version. Columns as abstract_text. |
| `latest_article` | Stable. The current version of every PMID: its article row from the highest file_order_key, unless a deletion in that file or a later one removed it. A PMID re-added after a deletion is current again. Columns as article. Computed on every read (a window over all of article); materialize it for repeated use. |
| `latest_article_grant` | Stable. The article_grant rows of each PMID's latest_article version. Columns as article_grant. |
| `latest_article_id` | Stable. The article_id rows of each PMID's latest_article version. Columns as article_id. |
| `latest_author` | Stable. The author rows of each PMID's latest_article version. Columns as author. |
| `latest_author_affiliation` | Stable. The author_affiliation rows of each PMID's latest_article version. Columns as author_affiliation. |
| `latest_history` | Stable. The history rows of each PMID's latest_article version. Columns as history. |
| `latest_mesh_heading` | Stable. The mesh_heading rows of each PMID's latest_article version. Columns as mesh_heading. |
| `latest_mesh_qualifier` | Stable. The mesh_qualifier rows of each PMID's latest_article version. Columns as mesh_qualifier. |
| `latest_publication_type` | Stable. The publication_type rows of each PMID's latest_article version. Columns as publication_type. |

<!-- END GENERATED REFERENCE -->
