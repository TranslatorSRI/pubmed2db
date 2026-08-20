# pubmed2db

Download PubMed abstracts, load them into a [DuckDB](https://duckdb.org/) database
(keeping full version history), and export the latest version of every abstract to
JSON (for ingest into Node Annotator / ElasticSearch) and to Parquet (for downloadable
queries).

The JSON export follows the field names of the NCATS Translator
[DocumentMetadataAPI](https://github.com/NCATSTranslator/DocumentMetadataAPI). The
database itself uses PubMed's own field names.

## How it works

Downloading and XML parsing reuse [`pubmed-downloader`](https://github.com/cthoyt/pubmed-downloader)
(bulk download of the PubMed baseline + daily update files, plus the rich `Article`
data model). On top of it, pubmed2db adds:

- **MD5 tracking** — each `<file>.xml.gz.md5` sidecar is fetched and the checksum stored;
  a new or changed checksum triggers a (re)load. PubMed files are normally immutable, so
  this mostly just lets new files be discovered incrementally.
- **Full version history** — every loaded file's rows are tagged with their `source_file`
  provenance, so an abstract revised across several update files is kept as multiple rows.
  The `latest_article` view selects the newest non-deleted version of each PMID (honoring
  `<DeleteCitation>` removals).
- **Faithful publication dates** — raw `PubDate` components (`Year`/`Month`/`Day`/
  `MedlineDate`) are preserved rather than collapsed to a single date.
- **Journal names** — the journal title and abbreviations come from the NLM Catalog
  (`uv run pubmed2db journals`) and are joined on `nlm_catalog_id`.

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

# Refresh the journal dimension from the NLM Catalog.
uv run pubmed2db --data-dir data journals

# Parse downloaded files into the database (full history).
uv run pubmed2db --data-dir data load

# Export the latest abstract of every PMID as sharded NDJSON (DocumentMetadataAPI fields).
uv run pubmed2db --data-dir data export --format json --out data/json --shards 16

# Export the database to Parquet (latest version per table, or --all for full history).
# Note: unlike the JSON export, this has not yet been run against the full corpus.
uv run pubmed2db --data-dir data export --format parquet --out data/parquet

# Download + journals + load in one step (for scheduled runs).
uv run pubmed2db --data-dir data update

# Report what's been downloaded, loaded, and is ready to export (read-only).
uv run pubmed2db --data-dir data status

# Sanity-check a finished export and write validation_report.json alongside it.
# (Uses the DB when present; --offline skips the Entrez API cross-checks.)
uv run pubmed2db --data-dir data validate data/json --email you@example.com
```

`--data-dir data` is the default, so omitting it gives the same result.
The DuckDB database is stored as `<data-dir>/pubmed.duckdb`; override with `--db`.

### Identifiers in the JSON export

Alongside the DocumentMetadataAPI fields, each JSON record carries an
`identifiers` array of CURIEs — the PMID, plus the DOI and PMCID when PubMed
published them:

```json
{
  "id": "PMID:30690000",
  "identifiers": ["PMID:30690000", "PMCID:PMC6423490", "doi:10.1016/j.ejphar.2019.01.030"],
  "journal_name": "European journal of pharmacology",
  "...": "..."
}
```

`PMID` and the lowercase `doi` deliberately match Babel's `src/prefixes.py`, so
those CURIEs join directly against the Babel publication compendium. PubMed's
PMCID values already begin with `PMC`, hence the doubled `PMCID:PMC6423490`. A
record with neither a DOI nor a PMCID still gets its own `["PMID:<id>"]`; the
array is never empty and never null.

> **The PMCID prefix may still change.** Babel uses `PMC`, this export uses
> `PMCID`, and neither the Core Components specification nor the
> DocumentMetadataAPI README has a PMC example to settle it; the production
> endpoint does not resolve PMCIDs under either prefix. See
> [#33](https://github.com/TranslatorSRI/pubmed2db/issues/33).

> **Consumers must match identifiers case-insensitively.** Values are stored and
> exported exactly as PubMed published them, with no case normalization (Babel
> does the same). DOIs are case-insensitive by specification and PubMed is not
> internally consistent about them, so `doi:10.1234/ABC` and `doi:10.1234/abc`
> can both occur and refer to the same article.

Only DOIs and PMCIDs are promoted into this field. Any other `ArticleId` type
PubMed supplies (`pii`, `mid`, …) is still loaded and is available in the
`article_id` table and the Parquet export.

> **Databases built before this feature need rebuilding.** The identifiers come
> from the `article_id` table, and until now that table was populated by an
> upstream parser that also swept up every *cited reference's* DOI and PMCID (see
> `CLAUDE.md`). Nothing detects the stale rows automatically, because the files
> themselves have not changed. `load --force` re-parses the whole corpus and does
> replace those rows, but it cannot undo a table that left the schema — such a
> database still carries a populated `reference_citation`, which no reload clears
> (see [`load --force` or a fresh database?](#load---force-or-a-fresh-database)).
> Load into a fresh one:
>
> ```bash
> rm data/pubmed.duckdb
> uv run pubmed2db --data-dir data load
> ```

Two more group-level options tune DuckDB itself, mainly for large exports on a
cluster: `--threads N` (`PUBMED2DB_THREADS`) caps the thread pool, which
otherwise sizes itself from the machine's core count and so oversubscribes a
smaller allocation, and `--temp-dir PATH` (`PUBMED2DB_DUCKDB_TEMP_DIR`) sets
where DuckDB spills when a query exceeds memory. Both go before the subcommand.

The steps are independent and incremental (re-running `download` revalidates
existing files rather than refetching them), and each checks its prerequisites
against the database's own state: `load` errors if nothing has been downloaded,
and `export` errors if nothing has been loaded, warns if some downloaded files
have not been loaded yet, and warns if the journal dimension is empty (journal
names would be blank). When in doubt, `uv run pubmed2db update` runs the whole pipeline
(`download → journals → load`) in order.

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

Stdout is a test report: every check is listed with what it expected and what it
saw, so a reviewer can tell what was verified, what was skipped, and what is not
covered at all. Abridged, from a full-corpus run:

```
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
# Archive this export's PMID set (gzipped, one PMID per line).
uv run pubmed2db validate data/json --manifest data/json/pmids.txt.gz

# Next month: report which PMIDs disappeared since that export.
uv run pubmed2db validate data/json-new \
    --previous-manifest data/json/pmids.txt.gz --manifest data/json-new/pmids.txt.gz
```

A dropped PMID is fine if the database recorded a `DeleteCitation` for it; one
that vanished *without* a recorded deletion is an **error**, since it means
records were lost rather than retired. With no database available the drops can't
be attributed, so they downgrade to a warning to review.

The coverage check expects the export to be within ±5% of the live PubMed total
(the 2026-07-30 full run came in at 99.90% of it). A **partial** export — from a
`--limit` test download — is legitimately far below that, so pass something like
`--entrez-low 0.001` when validating one, or `--offline` to skip the comparison.

Use `--limit N` on `download`/`update` to fetch only the newest N files when testing.

> **Note:** The file layout under `data/` differs from Babel's PubMed download,
> so the two cannot share a download cache at this time.

### Re-running after a gap

Running `download` and then `load` again is safe: `load` picks up only the files
that are new or changed, and it cannot introduce duplicate data.

- **Only new/changed files are loaded.** `download` bumps a file's
  `downloaded_at` only when its published MD5 is new or differs from the stored
  one, and `load` skips every file whose `processed_at` is at or after its
  `downloaded_at`. Unchanged files are no-ops.
- **Reloading a file replaces it.** Before inserting, `load` deletes all rows
  previously loaded from that `source_file`, in the same transaction, so even a
  forced reload (`load --force`) is idempotent.
- **Several versions of a PMID are the design, not duplication.** A row's
  identity is `(pmid, source_file)`; the `latest_article` view returns the
  version with the highest `file_order_key` per PMID, and drops the PMID if a
  later file recorded a `<DeleteCitation>` for it. Exports therefore see one row
  per PMID no matter how many versions are stored.

### `load --force` or a fresh database?

A normal incremental run only re-parses files whose published checksum moved, so
neither a parser fix nor a schema change reaches data that is already loaded.
Two ways to apply one, and they are not interchangeable:

- **`load --force`** re-parses every local file and replaces its rows, so it
  applies a *parsing* change to the whole corpus: roughly a baseline's worth of
  time (~2–3 h), no re-download, and the database keeps its history.
- **A fresh database** (delete `<data-dir>/pubmed.duckdb`, then `load`) is the
  answer whenever the *schema* changed, and the safer default if you are unsure.

The asymmetry is that `schema.sql` only ever adds: `CREATE TABLE IF NOT EXISTS`
plus explicit `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations. Nothing
drops a table or a column, so a forced reload refreshes the rows of tables that
still exist while a table removed from the schema keeps its rows forever —
`reference_citation`, dropped during development, is exactly that case. A forced
reload also leaves any pre-existing wrong rows in place for files it re-parses
identically.

Rebuilding costs a full `load` and nothing else — the downloaded files are not
touched — so unless the corpus is already loaded on a machine where 2–3 hours of
`load` is cheaper than the disk churn, prefer the rebuild. At a new baseline year
the question is moot: a fresh database is the recommended path anyway, since
loading the new year into the old database stores a second version of every PMID
(see above).

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

Either way, delete the previous year's files yourself:

```bash
rm data/pubmed/baseline/pubmed25n*.xml.gz data/pubmed/updates/pubmed25n*.xml.gz
```

`download` never removes anything. Once PubMed drops a file from its listing we
stop hearing about it, so a full extra copy of the corpus sits in `data/` until
you clear it — for 2026 that is 51 GB of baseline plus 13 GB of updates.

Verification is on by default, but only hashes files that are new or whose
published checksum changed — re-running `download` over an unchanged baseline
costs no local I/O. Corruption happens at download time, which stays covered.
`--no-verify` skips the hashing entirely; either way the `.md5` sidecars are
still fetched, so a changed published checksum is always detected.

## Notes

- We reuse `pubmed-downloader` **as-is** for downloading and XML parsing, but work
  around three bugs in it (≤ 0.0.14), all tracked in [`FUTURE.md`](./FUTURE.md):
  `catalog.process_journal_overview()` requires `start_year`/`end_year` fields the
  real `J_Entrez.txt` does not contain, so it raises on live data; its reference
  extraction looks under `MedlineCitation` for a `<ReferenceList>` that PubMed puts
  under `<PubmedData>`, so it never finds one; and its article-ID extraction
  descends into that `<ReferenceList>`, attributing every cited reference's DOI to
  the citing article. We parse the journal file and the article IDs ourselves; the
  reference bug is moot here because we deliberately do not store the citation
  graph (one article carries ~444 references, and nothing consumes them).
- This tool is intended to eventually replace the PubMed download in
  [Babel](https://github.com/NCATSTranslator/Babel) (`createcompendia/publications.py`).

See [`AGENTS.md`](./AGENTS.md) for a map of the source and the design decisions
behind it, and [`FUTURE.md`](./FUTURE.md) for known limitations and planned work.

## Information on running this pipeline

- `uv run pubmed2db load` takes a long time to run, and it might be beneficial to parallelize this: transform each PubMed
  file into a separate DuckDB file, then use a query that spans multiple files to either load everything into one
  file or to simply export it from the multiple files (using PMIDs to group related queries might not take very long?).
  Running it with 64G of memory seems sufficient.
- `uv run pubmed2db export --format json` is fast but memory-hungry: 40,901,984 documents in ~23 minutes
  (≈30k documents/s) at a peak RSS of 201.1 GiB, run with `--mem 256G` (an earlier run peaked at 199.6 GiB).
  Unlike `load`, its memory scales with the whole database rather than the largest input file — see
  [`slurm/README.md`](./slurm/README.md#running-export) for why, and for what to request on a cluster.
- **`export` publishes in place, not atomically.** Both formats write straight into `--out`:
  the JSON export truncates each shard as it opens it (and removes shards left by a previous,
  wider export), and the Parquet export replaces one table file at a time. A run that dies
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

Helper scripts live in `scripts/` and document themselves — run one with
`--help` for its usage and options. `benchmark_load.py` times parsing against
insertion for a given file, which is where [`slurm/README.md`](slurm/README.md)'s
memory and wall-time figures come from.
