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

```bash
# Download the baseline + update files (MD5-checked, incremental).
pubmed2db --db pubmed.duckdb download

# Refresh the journal dimension from the NLM Catalog.
pubmed2db --db pubmed.duckdb journals

# Parse downloaded files into the database (full history).
pubmed2db --db pubmed.duckdb load

# Export the latest abstract of every PMID as sharded NDJSON (DocumentMetadataAPI fields).
pubmed2db --db pubmed.duckdb export --format json --out ./json_out --shards 16

# Export the database to Parquet (latest version per table, or --all for full history).
pubmed2db --db pubmed.duckdb export --format parquet --out ./parquet_out

# Download + journals + load in one step (for scheduled runs).
pubmed2db --db pubmed.duckdb update
```

Downloaded files are cached by `pubmed-downloader` via [pystow](https://github.com/cthoyt/pystow);
set `PYSTOW_HOME` to control where (and to share the cache with a Babel run). Use
`--limit N` on `download`/`update` to fetch only the newest N files when testing.

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
