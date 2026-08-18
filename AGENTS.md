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
| `src/pubmed2db/parse.py` | Self-driven XML iteration: calls `pubmed_downloader`'s `_extract_article` per record, plus raw `PubDate`, `DeleteCitation`, and article IDs. |
| `src/pubmed2db/load.py` | Loads parsed files (full history, provenance-tagged), `latest`/delete logic, journal dimension. |
| `src/pubmed2db/export.py` | JSON (spec fields, empty-string-not-null) + Parquet export. |
| `src/pubmed2db/status.py` | Pipeline-readiness checks derived from DB state (drives the CLI's prerequisite errors/warnings). |
| `src/pubmed2db/cli.py` | `download`, `journals`, `load`, `export`, `update`, `status`; group-level `--threads`/`--temp-dir` are applied by `_connect`. |

## Key design decisions (and why)

- **Reuse [`cthoyt/pubmed-downloader`](https://github.com/cthoyt/pubmed-downloader) as-is, no upstream changes.**
  It handles bulk download + the rich `Article` data model. We do not use its
  `iterate_process_*`/JSONL cache — DuckDB is our store.
- **No citation graph.** `reference_citation` was removed: one real article
  carries ~444 references, so at corpus scale it would have been the largest
  table here, for data no consumer wanted. `parse._cited_pmids` is parked
  (uncalled) with re-enabling instructions in its docstring.
- **We drive the XML iteration ourselves** (`parse.py`) rather than using the
  `pubmed_downloader` process pipeline, because we need things it drops or
  gets wrong: the **raw `PubDate` components** (so `MedlineDate`-only/partial
  dates keep full fidelity instead of being collapsed to a `datetime.date`),
  **`<DeleteCitation>`** PMIDs (needed for latest-version selection), and
  **article IDs** (see the upstream issues below). Cited PMIDs are extractable
  the same way, but `parse._cited_pmids` is parked and never called — we store
  no citation graph.
- **DB uses PubMed's own field names**; the DocumentMetadataAPI names
  (`journal_name`, `journal_abbrev`, `pub_month` as 3-letter abbrev, …) are
  applied **only** in the JSON export, with empty strings for missing values.
- **Full version history**, not upsert: every file's rows are tagged with
  `source_file` + `file_order_key`; `latest_article` selects the newest
  non-deleted version per PMID. `file_order_key = year_yy * 1_000_000 + file_number`
  reproduces PubMed's chronological ordering (baseline before updates; year prefix
  dominates).
- **`schema.sql` only ever adds, and it runs on every connect.** Tables are
  `CREATE TABLE IF NOT EXISTS`, so a column added after the first release does
  **not** appear in an existing database — it needs its own
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` line right below the table (see
  `source_file.n_failed`). Nothing drops a table or column, which is why the
  README tells you to rebuild rather than `load --force` after a schema change:
  a removed table keeps its rows forever.
- **MD5 is low-priority** (HTTP downloads are reliable and PubMed files are
  immutable): we store the published checksum and reload a file only if it is new
  or its checksum changed (`load.needs_load` compares `downloaded_at > processed_at`).
  A new baseline year is therefore ~1,300 "new" files: a full re-parse that stores
  a second version of every PMID (correct, since `file_order_key` prefers the newer
  year, but it roughly doubles the DB) — see the README's "Re-running after a gap".
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

## Known upstream issues (`pubmed-downloader` ≤ 0.0.14)

Three bugs we work around; all tracked in `FUTURE.md` with a pinning test each, so
they fail loudly once upstream fixes them. The dependency is pinned `<0.1` because
we also call private APIs (`_extract_article`, `_ensure_urls`).

- **Journal parsing raises.** `catalog.process_journal_overview()`'s `Journal` model
  requires `start_year`/`end_year`, which the real `J_Entrez.txt` does not provide.
  We **parse the overview file ourselves** in `load._parse_journal_overview`
  (reusing only `ensure_journal_overview()` for the download).
- **References are never found.** `_extract_article` looks for
  `.//ReferenceList/Reference` under `MedlineCitation`, but PubMed nests
  `<ReferenceList>` under `<PubmedData>` — so `Article.cites_pubmed_ids` is always
  empty on real data. Harmless for us now, since we don't store the citation
  graph; `parse._cited_pmids` keeps a working extraction, uncalled.
- **Article IDs are over-collected.** `pubmed_data.findall(".//ArticleIdList/ArticleId")`
  descends into that same `<ReferenceList>`, attributing every *cited* reference's
  DOI/PMID to the citing article. `parse._article_ids` uses the direct
  `PubmedData/ArticleIdList/ArticleId` path instead.

Two upstream *behaviours* (not bugs) are easy to assume backwards; both are
pinned by tests in `tests/test_db_download.py`:

- **`_ensure_urls` sorts the listing newest-first**, so `--limit N` takes the
  head. It was once "fixed" to take the tail, which made `--limit` quietly fetch
  the oldest files.
- **`ensure()` skips by file name**, so a file republished under its old name
  keeps its stale bytes. `download._sync_kind` unlinks the local copy whenever a
  known published checksum moves, before calling `ensure()`.

## Development

```bash
uv sync --extra dev
uv run pytest          # no network needed
```

Tests gzip the readable XML fixtures under `tests/fixtures/` into temp
`pubmedNNnNNNN.xml.gz` files. Scratch downloads and databases go under `./data`
(gitignored). The CLI sets `PYSTOW_HOME` to `--data-dir` (default `data/`), so
`pubmed_downloader` keeps its cache under `data/pubmed/{baseline,updates}/`. Use
`--limit N` on `download`/`update` to fetch only the newest N files when testing
against the live server.
