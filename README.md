# pubmed2db

Download PubMed abstracts, load them into a [DuckDB](https://duckdb.org/) database
(keeping full version history), and export the latest version of every abstract to
JSON (for ingest into Node Annotator / ElasticSearch) and to Parquet (for downloadable
queries).

The JSON export follows the field names of the NCATS Translator
[DocumentMetadataAPI](https://github.com/NCATSTranslator/DocumentMetadataAPI),
plus four fields of our own: `id`, [`identifiers`](#identifiers-in-the-json-export),
[`pub_date`](#pub_date-in-the-json-export) and
[`publication_types`](#publication-types-in-the-json-export). The database itself
uses PubMed's own field names.

All three outputs — the database, the Parquet export and the NDJSON export —
follow one versioned **data model**, currently 1.0.0, documented for consumers in
[`docs/data-model/`](./docs/data-model/README.md): what every table and field means,
what PubMed data is left out, and how the design was decided. Each output carries
its version, and the database documents its own columns
(`SELECT table_name, column_name, comment FROM duckdb_columns()`).

## How it works

File listing and XML parsing reuse [`pubmed-downloader`](https://github.com/cthoyt/pubmed-downloader)
(the PubMed baseline + daily update file listings and cache layout, plus the rich
`Article` data model); the transfers themselves use pubmed2db's own download,
which times out a stalled connection. On top of it, pubmed2db adds:

- **MD5 tracking** — each `<file>.xml.gz.md5` sidecar is fetched and the checksum stored;
  a new or changed checksum triggers a (re)load. PubMed files are normally immutable, so
  this mostly just lets new files be discovered incrementally.
- **Full version history** — every loaded file's rows are tagged with their `source_file`
  provenance, so an abstract revised across several update files is kept as multiple rows.
  The `latest_article` view selects the newest non-deleted version of each PMID (honoring
  `<DeleteCitation>` removals).
- **Faithful publication dates** — raw `PubDate` components (`Year`/`Month`/`Season`/
  `Day`/`MedlineDate`) are preserved rather than collapsed to a single date.
  `<Season>` shares the `pub_month` column with `<Month>` — the DTD makes the two
  mutually exclusive — so a Parquet consumer reading `article.pub_month` sees
  `Winter` or `Sep-Dec` as readily as `Mar`.
- **Journal names** — the journal title and abbreviations come from NLM's
  `J_Entrez.txt` (`uv run pubmed2db journals`) and are joined on `nlm_catalog_id`;
  publication years come from NLM's serial catalog. Two sources, deliberately:
  see [`docs/JournalCatalog.md`](./docs/JournalCatalog.md).

## Setup

Clone the repository and let [uv](https://docs.astral.sh/uv/) build the
environment; there is no need to install pubmed2db itself — every command below
is run from the repo root with `uv run`, which syncs dependencies on demand.

```bash
git clone https://github.com/TranslatorSRI/pubmed2db.git
cd pubmed2db
uv sync
```

If uv's default cache directory (`~/.cache/uv`) is not writable or is on a small
quota — common on HPC login nodes — point it somewhere else once per shell (or
in your `~/.bashrc` / job script):

```bash
export UV_CACHE_DIR=/path/to/writable/uv-cache
```

## Usage

All commands share `--data-dir` (default `data/`), which sets the root for
downloaded PubMed files and the database. `pubmed-downloader` keeps its cache in
a `pubmed/` subdirectory inside it (`data/pubmed/baseline/`,
`data/pubmed/updates/`), so the layout under `data/` is managed automatically.

Budget disk accordingly: the 2026 files alone are **51 GB of baseline and 13 GB
of updates**, before the DuckDB database and any export. A new baseline year
adds that much again — see [Re-running after a gap](#re-running-after-a-gap).

```bash
# Download the baseline + update files to data/ (MD5-checked, incremental).
uv run pubmed2db --data-dir data download

# Refresh the journal dimension from J_Entrez.txt and NLM's serial catalog
# (~890 MB on a first run).
uv run pubmed2db --data-dir data journals

# Parse downloaded files in parallel into Parquet shards under data/stage/
# (one directory per file; --workers defaults to every CPU).
uv run pubmed2db --data-dir data stage --workers 8

# Insert the shards into the database (full history), 50 files per
# transaction, deleting each shard as it lands; a file with no shard is
# parsed here, one at a time.
uv run pubmed2db --data-dir data load

# Export the latest abstract of every PMID as sharded NDJSON (DocumentMetadataAPI fields).
uv run pubmed2db --data-dir data export --format json --out data/json --shards 16

# Export the database to Parquet (latest version per table, or --all for full history).
# Note: unlike the JSON export, this has not yet been run against the full corpus.
uv run pubmed2db --data-dir data export --format parquet --out data/parquet

# Download + journals + stage + load in one step (for scheduled runs).
uv run pubmed2db --data-dir data update

# Report what's been downloaded, loaded, and is ready to export (read-only).
uv run pubmed2db --data-dir data status

# Sanity-check a finished export and write validation_report.json alongside it.
# (Uses the DB when present; --offline skips the Entrez API cross-checks.)
uv run pubmed2db --data-dir data validate data/json --email you@example.com
```

`--data-dir data` is the default, so omitting it gives the same result.
The DuckDB database is stored as `<data-dir>/pubmed.duckdb`; override with `--db`.
The shards go under `<data-dir>/stage/`; override with `--stage-dir`.

`stage` exists because DuckDB has a single writer and the parse is the part
that parallelizes: a serial parse-and-insert of the 2026 corpus was a day
(21h 29m for 1,636 files), nearly all of it in the database. `stage` opens no
database, so it can use every CPU; `load` keeps the one writer and inserts
whole groups of files per transaction. The two produce the same rows by
construction, since both build them with the same code and differ only in
whether the rows reach DuckDB through memory or through a Parquet file; a test
loads the fixtures both ways and compares every table. `load --no-stage`
ignores the shards and parses every file itself, which is the pre-v1.3
behaviour.

Both exports finish by writing `metadata.json` into the output directory: the
data model version, the record count, the corpus cutoff (the last file loaded,
and when NCBI posted it), and a SHA-256 per file. The same hashes go into
`SHA256SUMS`, so `sha256sum -c SHA256SUMS` checks a download with no JSON parser.
Both files are deleted when an export starts, so a directory holding a
`metadata.json` is one whose export finished: `export` refuses to run over one
until it has been archived, unless given `--overwrite` (see
[Between runs](#between-runs-what-to-keep-archive-and-delete)). `validate`
checks the shards against `metadata.json`, and `status` prints the same cutoff.

### How the JSON export is written

DuckDB writes the NDJSON itself — a single `COPY ... (FORMAT JSON)` whose
projection is built from `export._JSON_FIELDS`, rather than Python serializing
row by row. The serialization therefore runs in C++ across every thread (3x
faster on a 2M-document benchmark), and two things follow:

- **`--shards N` is a maximum, not a count.** Output is one file per writer
  thread, so `--shards` caps that statement's threads; a small dataset can be
  written by fewer. Omit it to use DuckDB's own thread count.
- **Records are not in PMID order.** The export no longer sorts — that sort cost
  ~3 minutes and most of the peak memory of a full run. Nothing downstream needs
  the order; `validate` builds its own sorted PMID manifest.
- **Shards are gzipped** (`pubmed_metadata_0.ndjson.gz`) unless you pass
  `--no-gzip`. NDJSON compresses ~4-5x — a full corpus goes from ~52 GiB to
  ~12 — and each shard is compressed as it is written, so there is no second
  pass. `validate <dir>` reads either form with no flag, and `zcat` still works
  line by line.

Re-running an export first deletes the `pubmed_metadata_*` files already in the
output directory, so a shorter run cannot leave a previous run's shards behind.

### `pub_date` in the JSON export

The three parsed date fields are conveniences, and no arrangement of them
represents every `PubDate` PubMed publishes: a cross-year range like
`<MedlineDate>1998 Dec-1999 Jan</MedlineDate>` (PMID:10188493) has no single
month, so splitting it leaves a year inside `pub_month`. So every record also
carries **`pub_date`**, PubMed's own date string verbatim — the same thing NCBI
`esummary` ships as `pubdate`:

```json
{
  "id": "PMID:10188493",
  "pub_date": "1998 Dec-1999 Jan",
  "pub_year": "1998",
  "pub_month": "Dec-1999 Jan",
  "pub_day": "",
  "...": "..."
}
```

Render citations from `pub_date`; sort and filter on `pub_year`. Neither has to
parse the other. Records with an ordinary date get the same treatment —
`"2019 Mar 15"` — so a consumer never needs to know which shape it is looking at.

`pub_year` takes the **leading** year of a cross-year range, matching what NCBI's
own `sortpubdate` does. A consumer that needs the trailing year reads it out of
`pub_date`.

> **`pub_date` is new, and a re-export is required to ship it.** An export made
> before this field existed has no `pub_date` field.

### Publication types in the JSON export

Each record carries `publication_types`, PubMed's publication types. Each entry
has the type's MeSH descriptor as a CURIE and PubMed's name for it. They are in
PubMed's own order, the order NCBI's `esummary` serves as `pubtype`:

```json
{
  "id": "PMID:22456213",
  "publication_types": [
    {"id": "MESH:D002363", "name": "Case Reports"},
    {"id": "MESH:D016428", "name": "Journal Article"},
    {"id": "MESH:D016454", "name": "Review"}
  ],
  "...": "..."
}
```

- **Verbatim.** Every type PubMed lists is included, funding labels such as
  `Research Support, Non-U.S. Gov't` too (about a fifth of records carry one).
  Filter on `name` or `id` to pick out reviews, trials, errata and so on.
- **Usually more than one.** In a sample of 3,600 random PMIDs, about half
  carried two or more types and 89% carried `Journal Article`. A record with no
  types gets `[]`, never null.
- `id` uses the `MESH:` prefix, Biolink's casing.

The same data is in the database's `publication_type` table (`type_ui`,
`type_name`, and `position` for PubMed's order), so a direct query needs no MeSH
lookup:

```sql
SELECT count(*) FROM latest_article la
JOIN publication_type pt USING (pmid, source_file)
WHERE pt.type_name = 'Review';
```

### Identifiers in the JSON export

Alongside the DocumentMetadataAPI fields, each JSON record carries an
`identifiers` array of CURIEs — the PMID, plus the DOI and PMCID when PubMed
published them:

```json
{
  "id": "PMID:30690000",
  "identifiers": ["PMID:30690000", "PMC:PMC6423490", "doi:10.1016/j.ejphar.2019.01.030"],
  "journal_name": "European journal of pharmacology",
  "...": "..."
}
```

All three prefixes (`PMID`, `PMC`, and the lowercase `doi`) deliberately match
Babel's `src/prefixes.py`, so these CURIEs join directly against the Babel
publication compendium. PubMed's PMCID values already begin with `PMC`, hence
the doubled `PMC:PMC6423490`. A record with neither a DOI nor a PMCID still gets
its own `["PMID:<id>"]`; the array is never empty and never null.

> **The PMCID prefix may still change.** `PMC` follows Babel for now. Biolink
> registers `PMC` on `biolink:Publication` and `PMCID` on
> `biolink:JournalArticle`, and Translator has not chosen between them: see
> [biolink-model#1366](https://github.com/biolink/biolink-model/issues/1366) and
> [Babel#1044](https://github.com/NCATSTranslator/Babel/issues/1044). This export
> follows whatever they decide
> ([#33](https://github.com/TranslatorSRI/pubmed2db/issues/33)). The production
> DocumentMetadataAPI endpoint does not resolve PMCIDs under either prefix.

> **Consumers must match identifiers case-insensitively.** Values are stored and
> exported exactly as PubMed published them, with no case normalization (Babel
> does the same). DOIs are case-insensitive by specification and PubMed is not
> internally consistent about them, so `doi:10.1234/ABC` and `doi:10.1234/abc`
> can both occur and refer to the same article.

Only DOIs and PMCIDs are promoted into this field. Any other `ArticleId` type
PubMed supplies (`pii`, `mid`, …) is still loaded and is available in the
`article_id` table and the Parquet export.

Three more group-level options tune DuckDB itself, which matters on a cluster:

- `--threads N` (`PUBMED2DB_THREADS`) caps the thread pool. Rarely needed on
  Slurm: DuckDB reads the cgroup's CPU quota and already sizes the pool from
  `--cpus-per-task` (it uses the machine's core count only off a cluster).
- `--memory-limit SIZE` (`PUBMED2DB_DUCKDB_MEMORY_LIMIT`, e.g. `48GB`) caps the
  buffer pool. DuckDB does read the Slurm cgroup, defaulting to ~76% of `--mem`
  (measured on duckdb 1.5.4), so this is not about rescuing it from a node-sized
  default — it is about leaving room for the memory DuckDB's limit does *not*
  cover: the lxml tree, the parsed records and the Arrow batch all live in the
  same cgroup.
- `--temp-dir PATH` (`PUBMED2DB_DUCKDB_TEMP_DIR`) sets where DuckDB spills when a
  query exceeds its memory budget.

All three go before the subcommand. See [`slurm/README.md`](./slurm/README.md).

The steps are independent and incremental (re-running `download` revalidates
existing files rather than refetching them), and each checks its prerequisites
against the database's own state: `load` errors if nothing has been downloaded,
and `export` errors if nothing has been loaded, warns if some downloaded files
have not been loaded yet, and warns if the journal dimension is empty (journal
names would be blank). When in doubt, `uv run pubmed2db update` runs the whole pipeline
(`download → journals → load`) in order.

A PubMed file that fails to download (an HTTP error, or no data for 120 seconds)
is skipped while the rest are still fetched; `download` then exits non-zero
naming the failures, and a re-run fetches only those.

`journals` reads two NLM sources: `J_Entrez.txt` (10 MB, refetched every run) for
the titles and abbreviations the export ships, and the serial catalog
(`serfilebase.YYYY.xml`, ~449 MB, plus that year's monthly updates — usually
~1 MB but occasionally a bulk re-release of 150-250 MB, so budget ~890 MB of
cache per year) for `start_year` / `end_year` / `active`. NLM publishes no `.md5`
sidecars for the catalog, so each cached file gets an `.etag` sidecar holding the
server's ETag instead, and a HEAD per file per run decides whether to re-fetch —
a warm run downloads nothing. The catalog is an enrichment: if it is unreachable, or
its baseline will not parse, the step logs a warning and refreshes the titles while
keeping the years a previous run loaded (NULL on a first run) rather than failing.
A monthly update that fails to download or parse is skipped with a warning and
fetched again next run, and a republished file whose re-fetch fails keeps its
cached copy. Unlike the PubMed files, the catalog cache cleans up after itself:
once a new baseline year has downloaded, the previous year's files are deleted.
About 1% of journals (proceedings volumes, mostly) have no catalog record and keep
NULL years permanently.

`uv run pubmed2db validate <dir>` inspects a finished JSON export and writes a
`validation_report.json` (pretty-printed, archivable) whose leading
`errors`/`warnings` arrays are empty when the export looks good. It confirms
every record parses and has the expected fields, compares the exported count to
both the live PubMed total and the local database, re-fetches a random sample
from NCBI Entrez to confirm field values match, and confirms a sample of dropped
PMIDs are genuinely gone. It exits non-zero on errors (`--fail-on-warn` also
fails on warnings) so it can gate an HPC run; pass `--offline` to skip the
network checks. Set `--email` (or `NCBI_EMAIL`) and optionally `--api-key` (or
`NCBI_API_KEY`, which raises the rate limit) for the API checks.

The STRUCTURE heading also carries the share of records that came out with each
identifier type (`identifier_coverage` in the report). Nothing gates on it — one
export in isolation cannot say whether 96% is right and 6% is not — but a rate
that collapsed between two runs is invisible without the number.

A long run narrates itself: one start line naming the shards and confirming
whether the database and an API key were picked up (never the key itself), a
progress line with an ETA once a minute while the shards are read, and a line
per phase after that — so a run waiting on NCBI is distinguishable from one
still reading. Peak RSS is logged at the end and stored in the report.

Stdout is a test report: every check is listed with what it expected and what it
saw, so a reviewer can tell what was verified, what was skipped, and what is not
covered at all. Abridged, from a full-corpus run:

```text
Validation WARN: 40,901,984 record(s) in 16 shard(s) of data/json
  database available · Entrez online, no API key (3 req/s; set NCBI_API_KEY for 10/s)
  ran in 10m 51s, peak RSS 5.177 GiB

COVERAGE
  [pass] vs-entrez       within [95%, 105%] of the live PubMed total  40,901,984 of 40,944,369 (99.896%)
  [pass] vs-database     matches the database's latest_article count  exact match (40,901,984)
  [skip] vs-previous     coverage within 10% of the previous report's no --previous-report

FIELD ACCURACY  (240 records sampled: 15/shard x 16 shards, seed 0)
  [WARN] core-fields     <20% of compared fields differ from Entrez   20 of 1,600 differ (1.25%)
         by field: 18x pub_year, 1x article_title, 1x issue
            0 exported a different value (incorrect data)
           20 exported blank where Entrez has a value (missing data)
         e.g. PMID:152567 pub_year exported "" vs. Entrez "1978"

NOT CHECKED
  - compared strictly against Entrez: article_title, volume, issue, pub_year, ...
  - article_title compared after folding efetch's rendering (terminal period, [ ] around a translated title, [Not Available] for an empty title)
  - compared but never fails the run (assigned upstream after our last update file): identifiers
  - MeSH terms, authors, affiliations and grants are stored in the DB, never exported
```

The `values_differ` count is printed **even when it is zero**, because that zero
is the decision a reviewer has to make: fields left blank are a completeness gap
that is safe to pass downstream, whereas a field exported with the *wrong* value
is a correctness bug. `[skip]` means a check could have run with another flag or
a network; `[n/a ]` means there was nothing to verify (no deletions to sample).

Every line of that output is rendered from `validation_report.json`, which also
gains a `checks_run` array — the same list, machine-readable, with structured
mismatch tallies.

Coverage counts alone cannot tell you that two exports of the same size hold the
same *records*, so `validate` can write a sorted PMID manifest and diff against
an earlier one:

```bash
# Archive this export's PMID set (gzipped, one PMID per line). Date-stamp it,
# and keep it outside the export directory: one shared name means the next run
# overwrites the manifest it is diffing against.
uv run pubmed2db validate data/json --manifest data/manifests/pmids-20260820.txt.gz

# Next month: report which PMIDs disappeared since that export.
uv run pubmed2db validate data/json-new \
    --previous-manifest data/manifests/pmids-20260820.txt.gz \
    --manifest data/manifests/pmids-20260921.txt.gz
```

A dropped PMID is fine if the database recorded a `DeleteCitation` for it, or
if PubMed no longer serves it either (asked online, for the drops the database
cannot explain). One that PubMed *still* serves is an **error**, since it means
records were lost rather than retired. Drops PubMed has retired but no loaded
file deleted are a **warning** (`drops_retired_upstream`) only on a database
created since the previous manifest: that is the expected result of the first
run after a [fresh database](#load---force-or-a-fresh-database), which never
loads the update files that carried those deletions. On a continuing database
they are an **error** (`missed_deletions`), because it means an update file
was skipped, and so they are on a rebuilt database with a gap in its files
(a number inside the loaded range that never loaded, listed as `load_gaps`).
Where PubMed's answer cannot excuse a drop, validate asks about only one
request's worth (200), enough to say whether records were lost or deletions
missed; both errors are reported when both happen. Offline, or with no database available, the drops can't be
attributed: offline they stay errors, and without a database they downgrade to
a warning to review. More than 200,000 unexplained drops are not looked up and
fail the run; the message says how to restart the manifest chain if that much
really changed.

The coverage check expects the export to be within ±5% of the live PubMed total
(the 2026-07-30 full run came in at 99.90% of it). A **partial** export — from a
`--limit` test download — is legitimately far below that, so pass something like
`--entrez-low 0.001` when validating one, or `--offline` to skip the comparison.

Use `--limit N` on `download`/`update` to fetch only the newest N files when testing.

> **Note:** The file layout under `data/` differs from Babel's PubMed download,
> so the two cannot share a download cache at this time.

### Re-running after a gap

Running `download`, `stage` and `load` again is safe: `load` picks up only
the files that are new or changed, and it cannot introduce duplicate data.

- **Only new/changed files are loaded.** `download` bumps a file's
  `downloaded_at` only when its published MD5 is new or differs from the stored
  one, and `load` skips every file whose `processed_at` is at or after its
  `downloaded_at`. Unchanged files are no-ops. `stage` applies the same rule
  (it reads the database read-only), and also skips a file whose shard is
  fresh: cut from the XML now on disk (same size and mtime) by the code now
  running. A shard that is not fresh is re-staged, so a re-downloaded file is
  never served from an old shard.
- **Reloading a file replaces it.** Before inserting, `load` deletes all rows
  previously loaded from that `source_file`, in the same transaction, so even a
  forced reload (`load --force`) is idempotent. From shards, a transaction
  covers a group of files (`--files-per-txn`, default 50): a group that fails
  is rolled back and retried one file at a time, and a shard that fails on its
  own is set aside as `<name>.bad` so the next `load` parses its XML instead.
- **Several versions of a PMID are the design, not duplication.** A row's
  identity is `(pmid, source_file)`; the `latest_article` view returns the
  version with the highest `file_order_key` per PMID, and drops the PMID if a
  later file recorded a `<DeleteCitation>` for it. Exports therefore see one row
  per PMID no matter how many versions are stored.

### `load --force` or a fresh database?

A normal incremental run only re-parses files whose published checksum moved, so
neither a parser fix nor a schema change reaches data that is already loaded.
Two ways to apply one, and they are not interchangeable:

> **A database from before data model 1.0 must be rebuilt.** Every command
> refuses one, naming the problem and leaving the file untouched: 1.0 renamed and
> dropped columns (see
> [the data model changelog](./docs/data-model/Changelog.md)),
> which no migration can do in place. The same holds for a database from another
> major version. An older *minor* version is upgraded on open, since it only
> lacks columns.

- **`load --force`** replaces every local file's rows, so it applies a
  *parsing* change to the whole corpus; run `stage --force` first so the
  shards carry the new parse (a shard written by other code is never used, so
  without it `load --force` would re-parse every file itself, serially). The
  cost is a full load's: at corpus scale, about a day serially before v1.3
  (see [`slurm/README.md`](./slurm/README.md#running-load-how-long---time)),
  no re-download, and the database keeps its history.
- **A fresh database** (delete `<data-dir>/pubmed.duckdb`, then `journals` and
  `load`) is the answer whenever the schema *removed or redefined* something,
  and the safer default if you are unsure.

The asymmetry is that within a major version `schema.sql` only ever adds:
`CREATE TABLE IF NOT EXISTS` plus explicit `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS` migrations. So a column added in a minor version is applied on the
next connect and filled by `load --force`. A forced reload refreshes the rows a
parser change affects, but it leaves any pre-existing wrong rows in place for
files it re-parses identically, and only a rebuild is guaranteed to leave
exactly the shape and rows the current code would write.

Rebuilding costs a full `load` and a `journals` refresh, and nothing else — the
downloaded files are not touched — so unless the corpus is already loaded on a
machine where a day of `load` is cheaper than the disk churn, prefer the
rebuild. At a new baseline year the question is moot: a fresh database is the
recommended path anyway, since loading the new year into the old database stores
a second version of every PMID (see above).

**A fresh database needs `journals` run again.** The journal dimension lives in
the database, so deleting it deletes the journals too, and `load` does not
rebuild them: skip `journals` and `export` warns, then ships blank journal names.
`update` and `./slurm/submit.sh all` both include it. Nothing is assumed current
from an earlier run: `J_Entrez.txt` is re-fetched every time, and the serial
catalog cache (`<data-dir>/pubmed2db/serfile/`, which survives deleting the
database) is re-checked against NLM's listing and ETags, so a warm cache
downloads only what NLM has added or republished. What a fresh database cannot
do is fall back on earlier years. If the catalog is unreachable or its baseline
will not parse, `journals` still succeeds, but every journal's `start_year`,
`end_year` and `active` is NULL rather than last run's values — so check the
`journals` output for `serial catalog unavailable` before exporting, and re-run
`journals` if it is there.

**Watch for a new baseline year.** Around November–December PubMed publishes a
fresh baseline for the *coming* year (`pubmed26n*.xml.gz` lands in late 2025,
after `pubmed25n*.xml.gz`), which is a complete re-issue of the corpus, not an
increment. Downloading it makes ~1,300 files new at once, so the following
`load` re-parses everything and the `article` table ends up holding a second
full copy of every PMID. The result is still correct — `file_order_key` puts the
newer year first, so `latest_article` resolves to it — but the database roughly
doubles in size and the load takes as long as the original one. `status` says so
directly once a second baseline year is on disk, and `pending_files` in the
thousands rather than the dozens is the same signal.

**Prefer a fresh database at the year boundary**, for a reason beyond size: a
grown database is *additive*. It can gain PMIDs but never lose them. A PMID that
is in last year's baseline and absent from this year's, without a
`<DeleteCitation>` ever reaching us — a missed updatefile, or a record PubMed
drops quietly — keeps last year's row as its newest version and stays in every
export from then on. A database built from the new baseline simply doesn't
contain it.

Either way, get the previous year's files out of the download cache yourself.
`download` never removes anything: once PubMed drops a file from its listing we
stop hearing about it, so a full extra copy of the corpus sits in `data/` until
you move it — for 2026 that is 51 GB of baseline plus 13 GB of updates. And
`load` reads every `*.xml.gz` in `data/pubmed/baseline/` and
`data/pubmed/updates/`, registered or not, so files left there are parsed
straight back into a fresh database. `scripts/fresh_database.py` moves the
superseded year to `data/pubmed-archive/<year>/`, which nothing reads, and
deletes the database in the same step (see
[Between runs](#between-runs-what-to-keep-archive-and-delete)). To reclaim
the disk instead of keeping the files:

```bash
rm -r data/pubmed-archive/2025
```

Verification is on by default, but only hashes files that are new or whose
published checksum changed — re-running `download` over an unchanged baseline
costs no local I/O. Corruption happens at download time, which stays covered.
`--no-verify` skips the hashing entirely; either way the `.md5` sidecars are
still fetched, so a changed published checksum is always detected.

### Between runs: what to keep, archive and delete

The data directory mixes three lifetimes: files that stay for the life of the
machine, a database that lives for months, and an export that belongs to one
build. Everything the pipeline writes, and what to do with it:

| Path (under `--data-dir`) | Written by | Lifetime |
| --- | --- | --- |
| `pubmed/baseline/`, `pubmed/updates/` | `download` | Keep. `load` parses every `*.xml.gz` here, so a superseded baseline year must *leave* these two directories before a fresh database is loaded: `scripts/fresh_database.py` moves it to `pubmed-archive/<year>/`. |
| `pubmed-archive/<year>/` | `fresh_database.py` | The kept XML; nothing reads it. `rm -r` it if you do not want it. |
| `pubmed.duckdb` (+ `.wal`, `.tmp/`) | every command | Months: one database per baseline year, or until a major data-model bump. Deleted by `fresh_database.py`. |
| `stage/` | `stage` | **Transient.** One directory of Parquet shards per file awaiting `load`, which deletes each as its transaction commits (`--keep-stage` to keep them). Reproducible from the XML; never archived. Deleted by `fresh_database.py` with the database. |
| `pubmed2db/serfile/`, `pubmed/J_Entrez.txt` | `journals` | Caches. The catalog prunes itself and `J_Entrez.txt` is refetched every run; nothing to do. |
| `json/` | `export` | **One build.** `metadata.json` marks it finished; `scripts/archive_build.py` moves it to `builds/<build>/`, and `export` refuses to overwrite it until then (`--overwrite`, or `EXPORT_OVERWRITE=1` under Slurm). |
| `builds/<build>/` | `archive_build.py` | Until published (and after, as long as you like). Laid out as on stars: shards, `metadata.json` and `SHA256SUMS` at the top, `manifests/`, `logs/`, `source_file.csv`. |
| `manifests/` | `validate` (via Slurm) | **Permanent, in place.** The dated PMID manifests and reports are the chain each `validate` diffs against. Never move or delete them; `archive_build.py` copies, not moves. |
| `logs/` | `submit.sh` | Permanent. Step logs are copied into each build; `submit.log` is the record of every submission. |
| `ncbi/`, `README.md` | pystow | Noise; ignore. |

#### Before a new run on the same database

1. If `data/json/metadata.json` exists, archive the build:
   `uv run python scripts/archive_build.py`. It refuses an export that has not
   been validated or whose validation failed, and refuses while it cannot read
   the database (a `load` still running), since the `source_file.csv` it writes
   from there is the only record of NCBI's checksums. It reads `$DATA_DIR`,
   `$EXPORT_DIR` and `$MANIFEST_DIR` as `slurm/config.sh` does; logs from an
   `.sbatch` file submitted by hand rather than through `submit.sh` need
   `--logs-dir data/logs`.
2. If you publish the database as well as the export, copy
   `data/pubmed.duckdb` now — the next `download`/`load` changes it. The script
   prints both `rsync` lines.
3. Nothing else. Manifests and logs accumulate by design, and `download`,
   `journals` and `load` are incremental.

#### Before a new run on a fresh database

Do this for a new baseline year, or after a major data-model bump.

1. Steps 1–2 above.
2. `uv run python scripts/fresh_database.py`. It shows what it will do and asks:
   move every year's XML but the baseline NCBI serves now to
   `data/pubmed-archive/<year>/`, delete the database, the stage directory
   and any spill files a dead job left (in `$DUCKDB_TEMP_DIR` too). At the year boundary
   that is all of last year's, before the new baseline is downloaded in step 3;
   mid-year it moves nothing. It asks NCBI which year that is (`--keep-year 2027`
   names it instead), and leaves `manifests/`, `logs/`, `builds/` and the
   catalog cache.
3. `./slurm/submit.sh all` — a fresh database needs `journals` too.
4. Expect the first `validate` to warn `drops_retired_upstream`: the PMIDs
   PubMed retired between the previous build and the new baseline, which the
   new database never saw a `DeleteCitation` for. Read the list in the dated
   report; an *error* (`unexplained_drops`) means PubMed still serves a PMID the
   export lost, and `missed_deletions` means the fresh load skipped a file
   (`load_gaps` names it).

## Notes

- We reuse `pubmed-downloader` **as-is** for file listings and XML parsing, but work
  around two bugs in it, both tracked in [`FUTURE.md`](./FUTURE.md) and each
  pinned by a test in `tests/test_parse.py` that fails once upstream fixes it: its reference
  extraction looks under `MedlineCitation` for a `<ReferenceList>` that PubMed puts
  under `<PubmedData>`, so it never finds one; and its article-ID extraction
  descends into that `<ReferenceList>`, attributing every cited reference's DOI to
  the citing article. We parse the article IDs ourselves, and the MeSH headings
  too, since upstream's model of them rewrites IDs between releases; the
  reference bug is moot here because we deliberately do not store the citation
  graph (one article carries ~444 references, and nothing consumes them).
  We also parse `J_Entrez.txt` ourselves. Upstream's
  `catalog.process_journal_overview()` raised on the real file until
  [pubmed-downloader#16](https://github.com/cthoyt/pubmed-downloader/pull/16)
  (0.1.0), and it still raises on any field it does not recognise, where ours
  ignores the field.
  Separately, `catalog.ensure_serfile_catalog()` skips the `serfilebase*` baseline
  and takes all 83 monthly deltas instead; that costs 2.52 GiB rather than 872 MiB
  and returns 80% duplicate records for the same coverage, which is why `journals`
  enumerates NLM's listing itself.
- **MeSH names are PubMed's, not MeSH's.** The descriptor, qualifier and
  publication-type names come from each record's own XML. They are never looked
  up in the MeSH vocabulary or reconciled across records, so if PubMed names a
  descriptor differently in two records, both names are kept. Each version of a
  PMID keeps the names its file gave it, and exports read the latest version.
  This keeps the data identical to PubMed's files. Any drift between older and
  newer records is cleared by the
  [year-boundary rebuild](#load---force-or-a-fresh-database), which reloads
  every record from the new baseline.
- This tool is intended to eventually replace the PubMed download in
  [Babel](https://github.com/NCATSTranslator/Babel) (`createcompendia/publications.py`).

See [`AGENTS.md`](./AGENTS.md) for a map of the source and the design decisions
behind it, [`FUTURE.md`](./FUTURE.md) for known limitations and planned work, and
[`docs/JournalCatalog.md`](./docs/JournalCatalog.md) for why the journal
dimension reads from two NLM sources rather than one.

## Information on running this pipeline

- `uv run pubmed2db load` on its own took a day for the corpus (21h 29m for 1,636 files on 2026-09-25),
  which is why the parse is now a separate, parallel `stage` step writing per-file Parquet shards that
  `load` inserts in groups (#68). Locally, DuckDB's own work per 30,000-article file is under a second
  even at 26 GB (insert ~0.4 s, commit ~0.4 s), so the cluster's 30–65 s per file is its environment,
  and the load's log now says per file where the time went (parse, insert, commit).
  Running `load` with 64G of memory seems sufficient.
- `uv run pubmed2db export --format json` is fast but memory-hungry: 41,153,516 documents in 13m 14s
  (≈52k documents/s) at a peak RSS of 191.9 GiB, run with `--mem 256G` and a 200GB DuckDB memory limit
  (153.3 GiB and 12m 03s under a 160GB limit — the peak follows the limit, not the query).
  Unlike `load`, its memory scales with the whole database rather than the largest input file — see
  [`slurm/README.md`](./slurm/README.md#running-export) for why, and for what to request on a cluster.
- **`export` publishes in place, not atomically.** Both formats write straight into `--out`:
  the JSON export deletes the shards of the previous run before DuckDB writes the new ones,
  and the Parquet export replaces one table file at a time (sweeping any left by a table that
  has since left the schema). A run that dies
  partway — OOM, full disk — therefore leaves a half-written dataset that looks complete to
  anything globbing the directory. Export into a fresh directory and swap it into place
  yourself if consumers read the output while exports run.
- **`export --format parquet` is untested at full scale.** Only the JSON export has been run against the
  whole corpus. Parquet should be the lighter of the two (each table is written by a DuckDB `COPY ... TO`
  rather than pulled through Python), but that is reasoning, not a measurement: request the same 256 GB
  the first time and check the logged peak RSS.

## Development

```bash
uv sync --extra dev
uv run pytest
```

Tests gzip the readable XML fixtures under `tests/fixtures/` into temporary
`pubmedNNnNNNN.xml.gz` files; scratch downloads and databases go under `./data`
(gitignored).

Releases, and the dated data builds they produce, follow
[`docs/Releases.md`](./docs/Releases.md).

Helper scripts live in `scripts/` and document themselves — run one with
`--help` for its usage and options. `benchmark_load.py` times a file's parse
and each phase of its load (build, delete, insert, commit) separately, and with
`--synthetic N --db PATH` grows an on-disk database from a few files to see
what changes with size; its knobs (`--checkpoint-threshold`, `--files-per-txn`,
`--no-preserve-insertion-order`) are the candidate settings,
which is where [`slurm/README.md`](slurm/README.md)'s
memory and wall-time figures come from; `archive_build.py` and
`fresh_database.py` are the two steps of
[Between runs](#between-runs-what-to-keep-archive-and-delete).
