# AGENTS.md — orientation for this repo

pubmed2db downloads PubMed abstracts, loads them into a DuckDB database keeping
**full version history**, and exports the **latest** version of every abstract to
JSON (DocumentMetadataAPI field names, for Node Annotator / ElasticSearch) and to
Parquet (PubMed field names, for downloadable queries).

Everything a user needs — commands, options, operational notes, the year-boundary
rebuild — is in [`README.md`](./README.md); deferred work and known limitations
are in [`FUTURE.md`](./FUTURE.md). This file is the map plus the handful of
decisions you cannot see from the code.

## Layout

`download → load → export`, wired through a `click` CLI. Each module's docstring
explains its part; this table is only a map.

| File | Responsibility |
| --- | --- |
| `src/pubmed2db/db.py` | Connection, schema init, `source_file` registry, `parse_file_name`. |
| `src/pubmed2db/schema.sql` | Normalized tables + `latest_article` view. **Read its header before editing.** |
| `src/pubmed2db/download.py` | Fetches baseline/update files; adds `.md5` sidecar tracking. |
| `src/pubmed2db/parse.py` | Self-driven XML iteration over each file. |
| `src/pubmed2db/load.py` | Loads parsed files (full history, provenance-tagged), delete logic, journal dimension. |
| `src/pubmed2db/export.py` | JSON + Parquet export. |
| `src/pubmed2db/status.py` | Pipeline-readiness checks derived from DB state. |
| `src/pubmed2db/cli.py` | `download`, `journals`, `load`, `export`, `update`, `status`. |

## Decisions that are not visible from the code

- **Reuse [`pubmed-downloader`](https://github.com/cthoyt/pubmed-downloader)
  as-is, no upstream changes.** We take its bulk download and its `Article` data
  model, but not its `iterate_process_*`/JSONL cache — DuckDB is our store, and
  `parse.py` drives the XML itself for the fields that pipeline drops (its
  docstring says which). Pinned `<0.1` because we call private APIs
  (`_extract_article`, `_ensure_urls`).
- **No citation graph, and that is a decision, not a gap.** One real article
  carries ~444 references, which would make it the largest table here for data no
  consumer wants. `parse._cited_pmids` is parked (uncalled) with re-enabling
  instructions in its docstring; `test_no_reference_citation_table` stops it
  creeping back.
- **Three upstream bugs are worked around**, each written up in `FUTURE.md` and
  pinned by a test, so they fail loudly once upstream fixes them. Two upstream
  *behaviours* are also easy to assume backwards (`_ensure_urls` sorts
  newest-first, so `--limit N` takes the head; `ensure()` skips by file name, so a
  republished file keeps stale bytes); both are pinned by tests in
  `tests/test_db_download.py`, whose docstrings say what they hold in place.

## Development

```bash
uv sync --extra dev
uv run pytest          # no network needed
```

Tests gzip the readable XML fixtures under `tests/fixtures/` into temp
`pubmedNNnNNNN.xml.gz` files. Scratch downloads and databases go under `./data`
(gitignored); the CLI points `PYSTOW_HOME` at `--data-dir` so
`pubmed_downloader` caches under `data/pubmed/{baseline,updates}/`.
