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
| `src/pubmed2db/validate.py` | Post-export checks over a directory of NDJSON shards; emits a gated JSON report. |
| `src/pubmed2db/status.py` | Pipeline-readiness checks derived from DB state. |
| `src/pubmed2db/util.py` | Shared helpers for the long steps: progress/ETA, durations, peak RSS. |
| `src/pubmed2db/cli.py` | `download`, `journals`, `load`, `export`, `update`, `status`, `validate`. |

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
- **`identifiers` is derived at export, never stored.** DOIs and PMCIDs already
  land in the normalized `article_id` table on every load, so the JSON export
  aggregates them there (`export._LATEST_METADATA_SQL`'s `ids` CTE says how, and
  `export.ID_PREFIXES` is the single place the Babel-compatible CURIE casing is
  written down). A denormalized `article.identifiers` column would have
  duplicated the data *and* forced a full corpus reload to backfill it, which is
  the reason not to, and the reason is not visible from the query.
- **DuckDB reads the Slurm cgroup. Both of its sized defaults do — we guessed
  otherwise twice and were wrong twice.** Measured on duckdb 1.5.4:
  `memory_limit` is ~76% of `--mem` (6.1 GiB under `--mem=8G`, 47.3 GiB under
  `--mem=62G`; ~80% of physical RAM off a cluster) and `threads` is
  `--cpus-per-task` (2 under `--cpus-per-task=2` on a 64-core node — and via the
  cgroup's CPU quota, not an affinity mask, which read the full 64). **Do not
  reason from "DuckDB cannot see the allocation"; measure it.** Both claims
  reached the docs, the `--help` text and an issue before anyone ran the
  one-line probe in #36/#38 that disproves them. So the buffer pool is not what
  overruns the allocation; what its limit does not cover is, since the lxml
  tree, the parsed records and the Arrow batch share the same cgroup and the
  default already claims three quarters of it.
- **Two memory numbers mislead, and both have bitten.** `util.peak_rss_gib` is
  `ru_maxrss`, a high-water mark that only ever rises, so a climbing per-file
  "peak" is not evidence of a leak; `current_rss_gib` is the one that can fall —
  which is why the load and validate progress lines log both. Before diagnosing loader memory, read `slurm/README.md` → "Running
  `load`: how much memory?".
- **An efetch mismatch is not evidence about what we parsed.** `validate`
  compares the export against Entrez `efetch`, but efetch output is a
  *rendering*, not the archival XML: it serves PMID 152567 as
  `<Year>1978</Year><Season>Jul-Aug</Season>` where the baseline file it was
  loaded from holds a bare `<MedlineDate>1978 Jul-Aug</MedlineDate>` and no
  `<Year>` at all. Diagnosing a field mismatch from efetch alone points at the
  wrong layer — download the baseline file containing the PMID and read the raw
  element before changing any parsing or export code.
- **`validate.py` is one 1,400-line module on purpose.** Splitting it by check
  would add import edges without reducing what you must read: every check needs
  `Report.record` and the example accumulators, most need `efetch_documents`, and
  that coupling is what makes the report's "the arrays cannot drift from the
  checks" property hold. The banner comments run in execution order, which is the
  navigability a split would have bought. Two things would change the answer: a
  second consumer of the Entrez client (`_RateLimiter`/`_eutils`/
  `efetch_documents`, ~110 lines, the one cleanly separable seam), or
  `run_validation` growing past its 14 keyword arguments — the latter wants an
  options dataclass *within* the file, not a split.
- **Three upstream bugs are worked around**, each written up in `FUTURE.md` and
  pinned by a test, so they fail loudly once upstream fixes them. Two of them are
  the same mistake: every extraction in `_extract_article` uses a `.//`
  descendant search, and two of those reach into `<ReferenceList>`. **Check the
  scope of any field you take from that parser before trusting it** — both were
  silent, and one had already written 426 foreign DOIs into `article_id` from a
  single record. Two upstream
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
