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
  (`pubmed2db journals`) and are joined on `nlm_catalog_id`.

## Install

```bash
uv sync --extra dev
```

## Usage

All commands share `--data-dir` (default `data/`), which sets the root for
downloaded PubMed files and the database. `pubmed-downloader` creates its own
`pubmed_downloader/` subdirectory inside it, so the layout under `data/` is
managed automatically.

```bash
# Download the baseline + update files to data/ (MD5-checked, incremental).
pubmed2db --data-dir data download

# Refresh the journal dimension from the NLM Catalog.
pubmed2db --data-dir data journals

# Parse downloaded files into the database (full history).
pubmed2db --data-dir data load

# Export the latest abstract of every PMID as sharded NDJSON (DocumentMetadataAPI fields).
pubmed2db --data-dir data export --format json --out data/json --shards 16

# Export the database to Parquet (latest version per table, or --all for full history).
pubmed2db --data-dir data export --format parquet --out data/parquet

# Download + journals + load in one step (for scheduled runs).
pubmed2db --data-dir data update

# Report what's been downloaded, loaded, and is ready to export (read-only).
pubmed2db --data-dir data status
```

`--data-dir data` is the default, so omitting it gives the same result.
The DuckDB database is stored as `<data-dir>/pubmed.duckdb`; override with `--db`.

The steps are independent and incremental (re-running `download` revalidates
existing files rather than refetching them), and each checks its prerequisites
against the database's own state: `load` errors if nothing has been downloaded,
and `export` errors if nothing has been loaded, warns if some downloaded files
have not been loaded yet, and warns if the journal dimension is empty (journal
names would be blank). When in doubt, `pubmed2db update` runs the whole pipeline
(`download → journals → load`) in order.

Use `--limit N` on `download`/`update` to fetch only the newest N files when testing.

> **Note:** The file layout under `data/` differs from Babel's PubMed download,
> so the two cannot share a download cache at this time.

## Notes

- We reuse `pubmed-downloader` **as-is** for downloading and XML parsing, but parse
  the NLM journal-overview file ourselves: that library's
  `catalog.process_journal_overview()` (≤ 0.0.14) requires `start_year`/`end_year`
  fields that the real `J_Entrez.txt` does not contain, so it raises on live data.
- This tool is intended to eventually replace the PubMed download in
  [Babel](https://github.com/NCATSTranslator/Babel) (`createcompendia/publications.py`).

See [`CLAUDE.md`](./CLAUDE.md) for architecture and design decisions, and
[`FUTURE.md`](./FUTURE.md) for known limitations and planned work.

## Development

```bash
uv sync --extra dev
uv run pytest
```

Tests gzip the readable XML fixtures under `tests/fixtures/` into temporary
`pubmedNNnNNNN.xml.gz` files; scratch downloads and databases go under `./data`
(gitignored).
