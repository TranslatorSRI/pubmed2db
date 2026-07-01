# CLAUDE.md — orientation for this repo

pubmed2db downloads PubMed abstracts, loads them into a DuckDB database keeping
**full version history**, and exports the **latest** version of every abstract to
JSON (DocumentMetadataAPI field names, for Node Annotator / ElasticSearch) and to
Parquet (PubMed field names, for downloadable queries).

**Project goal:** once mature, replace the PubMed download in Babel
(`createcompendia/publications.py` in NCATSTranslator/Babel) with this tool.

## Architecture / data flow

`download → load → export`, wired through a `click` CLI.

| File | Responsibility |
| --- | --- |
| `src/pubmed2db/db.py` | DuckDB connection, schema init, `source_file` registry, `parse_file_name` (filename → chronological `file_order_key`). |
| `src/pubmed2db/schema.sql` | Normalized tables (PubMed field names) + `latest_article` view. |
| `src/pubmed2db/download.py` | Reuses `pubmed_downloader` to fetch baseline/update files; adds `.md5` sidecar tracking. |
| `src/pubmed2db/parse.py` | Self-driven XML iteration: calls cthoyt's `_extract_article` per record, plus raw `PubDate` + `DeleteCitation`. |
| `src/pubmed2db/load.py` | Loads parsed files (full history, provenance-tagged), `latest`/delete logic, journal dimension. |
| `src/pubmed2db/export.py` | JSON (spec fields, empty-string-not-null) + Parquet export. |
| `src/pubmed2db/validate.py` | Post-export sanity checks over a NDJSON directory: structure, coverage (Entrez + DB denominators), sampled Entrez field comparison, deletion confirmation; emits a gated JSON report. |
| `src/pubmed2db/status.py` | Pipeline-readiness checks derived from DB state (drives the CLI's prerequisite errors/warnings). |
| `src/pubmed2db/cli.py` | `download`, `journals`, `load`, `export`, `update`, `status`, `validate`. |

## Key design decisions (and why)

- **Reuse [`cthoyt/pubmed-downloader`](https://github.com/cthoyt/pubmed-downloader) as-is, no upstream changes.**
  It handles bulk download + the rich `Article` data model. We do not use its
  `iterate_process_*`/JSONL cache — DuckDB is our store.
- **We drive the XML iteration ourselves** (`parse.py`) rather than using cthoyt's
  process pipeline, because we need two things it drops: the **raw `PubDate`
  components** (so `MedlineDate`-only/partial dates keep full fidelity instead of
  being collapsed to a `datetime.date`) and **`<DeleteCitation>`** PMIDs (needed
  for latest-version selection).
- **DB uses PubMed's own field names**; the DocumentMetadataAPI names
  (`journal_name`, `journal_abbrev`, `pub_month` as 3-letter abbrev, …) are
  applied **only** in the JSON export, with empty strings for missing values.
- **Full version history**, not upsert: every file's rows are tagged with
  `source_file` + `file_order_key`; `latest_article` selects the newest
  non-deleted version per PMID. `file_order_key = year_yy * 1_000_000 + file_number`
  reproduces PubMed's chronological ordering (baseline before updates; year prefix
  dominates).
- **MD5 is low-priority** (HTTP downloads are reliable and PubMed files are
  immutable): we store the published checksum and reload a file only if it is new
  or its checksum changed (`load.needs_load` compares `downloaded_at > processed_at`).
- **Journal names** come from the NLM Catalog journal-overview file, joined on
  `nlm_catalog_id` — see the known-issue below.
- **Step ordering is enforced from DB state, not a run-flag.** The steps are
  independent CLI commands; their ordering is guarded by checks derived from
  ground truth (`status.py`): `load` errors if nothing is downloaded, and `export`
  errors if no articles are loaded, warns if files are downloaded-but-unloaded,
  and warns if the `journal` table is empty (names would be blank). Deriving from
  the `source_file` watermarks + table contents means a check can't disagree with
  the data. `load` does **not** load journals — `journals` is its own step;
  `update` chains `download → journals → load` for the happy path. The read-only
  `status` command reports the same signals (`status.summarize`); the only
  recorded timestamp is `journals`' last refresh (`pipeline_run`), since it leaves
  no other trace — download/load recency stays derived from `source_file`.
- **Columnar bulk load.** `load._insert_batch` registers each file's rows as an
  Arrow table and inserts them via `INSERT ... SELECT`, not row-by-row
  `executemany` (which ran at ~2.5k rows/s and made load ~20 min/file). This is
  ~25–90× faster (~5–6 s/file). The load logs peak RSS per file for Slurm sizing;
  see `slurm/README.md` and `scripts/benchmark_load.py`.
- **`validate` reads the export, not the DB, as its ground truth.** It takes a
  directory of NDJSON shards (what actually shipped) and cross-checks them; the
  DuckDB database and a previous report are *optional* inputs that sharpen the
  coverage/deletion checks and are left blank in the report when absent. Every
  network call funnels through `validate._eutils` (one monkeypatchable seam, so
  tests stay offline). The report leads with `errors`/`warnings` arrays that are
  empty on a clean run; the process exits non-zero on errors so it can gate an
  HPC pipeline. Structural checks and the API field comparison deliberately
  reuse `export._document`/`month_to_abbrev` so "expected" is defined the same
  way the exporter defines it.

## Known upstream issue — journal parsing

`pubmed_downloader.catalog.process_journal_overview()` (≤ 0.0.14) **raises** on the
real `J_Entrez.txt` data: its `Journal` model requires `start_year`/`end_year`,
which that file does not provide. We therefore **parse the overview file ourselves**
in `load._parse_journal_overview` (reusing only `ensure_journal_overview()` for the
download). Revisit if a newer `pubmed-downloader` makes those fields optional. See
`FUTURE.md`.

## Development

```bash
uv sync --extra dev
uv run pytest          # 34 tests, no network
```

Tests gzip the readable XML fixtures under `tests/fixtures/` into temp
`pubmedNNnNNNN.xml.gz` files. Scratch downloads and databases go under `./data`
(gitignored). The CLI sets `PYSTOW_HOME` to `--data-dir` (default `data/`), so
`pubmed_downloader` keeps its cache under `data/pubmed_downloader/`. Use
`--limit N` on `download`/`update` to fetch only the newest N files when testing
against the live server.
