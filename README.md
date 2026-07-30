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
downloaded PubMed files and the database. `pubmed-downloader` creates its own
`pubmed_downloader/` subdirectory inside it, so the layout under `data/` is
managed automatically.

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

**Watch for a new baseline year.** Each December PubMed publishes a fresh
baseline (`pubmed26n*.xml.gz` after `pubmed25n*.xml.gz`), which is a complete
re-issue of the corpus, not an increment. Downloading it makes ~1,300 files new
at once, so the following `load` re-parses everything and the `article` table
ends up holding a second full copy of every PMID. The result is still correct —
`file_order_key` puts the newer year first, so `latest_article` resolves to it —
but the database roughly doubles in size and the load takes as long as the
original one. Check with `status` before starting: if `pending_files` is in the
thousands rather than the dozens, a new baseline has landed, and building a
fresh database from it is cheaper than growing the old one.

`download --no-verify` skips re-hashing already-downloaded files. Verification
is on by default and MD5s every local `.xml.gz` on every run, which is the main
cost of re-running `download` over a complete baseline; `--no-verify` still
fetches the `.md5` sidecars, so a changed published checksum is still detected.

## Notes

- We reuse `pubmed-downloader` **as-is** for downloading and XML parsing, but parse
  the NLM journal-overview file ourselves: that library's
  `catalog.process_journal_overview()` (≤ 0.0.14) requires `start_year`/`end_year`
  fields that the real `J_Entrez.txt` does not contain, so it raises on live data.
- This tool is intended to eventually replace the PubMed download in
  [Babel](https://github.com/NCATSTranslator/Babel) (`createcompendia/publications.py`).

See [`CLAUDE.md`](./CLAUDE.md) for architecture and design decisions, and
[`FUTURE.md`](./FUTURE.md) for known limitations and planned work.

## Information on running this pipeline

- `uv run pubmed2db load` takes a long time to run, and it might be beneficial to parallelize this: transform each PubMed
  file into a separate DuckDB file, then use a query that spans multiple files to either load everything into one
  file or to simply export it from the multiple files (using PMIDs to group related queries might not take very long?).
  Running it with 64G of memory seems sufficient.
- `uv run pubmed2db export --format json` is fast but memory-hungry: 40,901,984 documents in ~23 minutes
  (≈30k documents/s) at a peak RSS of 201.1 GiB, run with `--mem 256G` (an earlier run peaked at 199.6 GiB).
  Unlike `load`, its memory scales with the whole database rather than the largest input file — see
  [`slurm/README.md`](./slurm/README.md#running-export) for why, and for what to request on a cluster.
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
