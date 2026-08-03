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
| `src/pubmed2db/util.py` | Shared helpers: progress/ETA formatting, duration formatting, peak-RSS reporting. |
| `src/pubmed2db/cli.py` | `download`, `journals`, `load`, `export`, `update`, `status`, `validate`; group-level `--threads`/`--temp-dir` are applied by `_connect`. |

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
  A new baseline year is therefore ~1,300 "new" files: a full re-parse that stores
  a second version of every PMID (correct, since `file_order_key` prefers the newer
  year, but it roughly doubles the DB) — see the README's "Re-running after a gap".
- **Journal names** come from the NLM Catalog journal-overview file, joined on
  `nlm_catalog_id` — see the known-issue below.
- **`identifiers` is derived at export, not stored.** DOIs and PMCIDs already
  land in the normalized `article_id` table on every load, so the JSON export's
  `identifiers` array is an `ids` CTE over that table joined on
  `(pmid, source_file)` — the same key the abstract CTE uses, which is what
  keeps a superseded version's DOI out of the export. Denormalizing it into an
  `article` column would have duplicated the data and forced a full reload of
  the corpus to backfill. The CURIE prefixes (`PMID`, lowercase `doi`, `PMC`)
  are fixed by Babel's `src/prefixes.py`: `DOI:` or a de-doubled `PMC:6423490`
  would silently fail to join against the Babel compendium. Values keep PubMed's
  own case — consumers match case-insensitively. `export.ID_PREFIXES` is the one
  place that casing is written down; `validate` imports it rather than restating
  it, so a formatting difference can't masquerade as a data difference.
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
  HPC pipeline. "Expected" is always defined by the exporter itself, never
  restated: the field comparison imports `month_to_abbrev` from `export`, and
  `EXPECTED_FIELDS` is derived by calling `export._document` on a placeholder row
  so the record shape cannot drift out of sync. `test_expected_fields_matches_spec`
  additionally locks those eleven names, since they are an external contract with
  Node Annotator / ElasticSearch. `identifiers` is the one list-valued field, so
  `check_fields` compares it as a set rather than through the string path.
- **PMID-set drift needs a sidecar, not a bigger report.** The report stores
  counts, never millions of PMIDs, so `--manifest` writes a sorted gzipped
  `pmids.txt.gz` from the set the structure check already holds and
  `--previous-manifest` diffs against it. A drop the `deleted_pmid` table
  explains is expected; an unexplained one is an **error** (records lost, not
  retired), downgraded to a warning when no database is available to attribute
  it. This catches same-count exports whose contents silently changed.

## Known upstream issue — `_extract_article` XPaths are scoped loosely

Every extraction in `pubmed_downloader.api._extract_article` uses a `.//`
descendant search, and two of them hit the wrong subtree. **Check the scope of
any field you take from that parser before trusting it**; both bugs are silent.

1. **`xrefs` over-matches** — see below. Fixed in `parse._xrefs`.
2. **`cites_pubmed_ids` never matches.** It searches
   `medline_citation.findall(".//ReferenceList/Reference")`, but `ReferenceList`
   is a child of `PubmedData`, a *sibling* of `MedlineCitation` — so it returns
   nothing on real data and **`reference_citation` is always empty** (0 rows
   across 14,201 real articles from a file whose records carry 444 references).
   Not yet fixed; see `FUTURE.md`.

## Known upstream issue — xrefs pick up cited references

`_extract_article` builds `Article.xrefs` from
`pubmed_data.findall(".//ArticleIdList/ArticleId")`. `.//` matches at any depth,
and `PubmedData/ReferenceList/Reference` has an `ArticleIdList` of its own, so
**every cited reference's DOI/PMCID is attributed to the citing article** — one
real record contributed 426 foreign DOIs. `parse._xrefs` re-extracts them from
the direct child `PubmedData/ArticleIdList/ArticleId` (the path Babel uses) and
overwrites `article.xrefs` before the row is built.

This silently corrupted `article_id` from the first load, so **any database
built before this fix has a polluted `article_id` table** (the JSON export was
unaffected only because it did not yet read it — the Parquet export was). A
`load --force` over the corpus is required to clean it. `test_parse.py::test_xrefs_exclude_cited_references`
locks the behaviour. Worth reporting upstream.

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
