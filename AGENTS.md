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
| `src/pubmed2db/export.py` | JSON (one DuckDB `COPY`, gzipped by default) + Parquet export. |
| `src/pubmed2db/validate.py` | Post-export checks over a directory of NDJSON shards; emits a gated JSON report. |
| `src/pubmed2db/status.py` | Pipeline-readiness checks derived from DB state. |
| `src/pubmed2db/util.py` | Shared helpers for the long steps: progress/ETA, durations, peak RSS. |
| `src/pubmed2db/cli.py` | `download`, `journals`, `load`, `export`, `update`, `status`, `validate`. |
| `slurm/` | One `sbatch` script per pipeline step, `config.sh`, and `submit.sh`. See [`slurm/README.md`](./slurm/README.md). |

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
  written down, including the unsettled `PMCID` casing that issue #33 tracks).
  A denormalized `article.identifiers` column would have duplicated the data
  *and* forced a full corpus reload to backfill it, which is the reason not to,
  and the reason is not visible from the query. `validate`'s `EXPECTED_FIELDS`
  is `frozenset(export.JSON_FIELDS)` for the same reason — the exporter's own
  list, which its `COPY` projection is built from, so the record shape checked
  cannot drift from the record shape shipped. The field comparison imports
  `pub_month`, `_year_from_medline_date` *and* `_MONTH_ABBR` from `export` on
  the same principle: any normalization the export applies is applied to the
  efetch side too. `_MONTH_ABBR` rather than `calendar.month_abbr`, which is
  `LC_TIME`-dependent — under a non-English locale the `month-format` check
  would have warned on every record.
- **DuckDB writes the JSON, Python does not.** `export_json` is one
  `COPY (...) TO <dir> (FORMAT JSON, PER_THREAD_OUTPUT true)`, not a `fetchmany`
  loop calling `json.dumps` per row: the old loop spent ~80% of the export's
  wall time serializing on a single core while the other seven idled (17.7k
  docs/s measured, against 56.2k for the same corpus through `COPY`). Three
  consequences before you edit it:
  - **`_JSON_FIELDS` is the record definition** — output name → SQL expression,
    in emitted order — so the DocumentMetadataAPI names, the
    empty-string-not-null rule and the field order live in one place, and
    `validate` imports `JSON_FIELDS` from it rather than restating the shape.
    `pub_month` and `_year_from_medline_date` survive as Python because
    `validate` needs them for the efetch side; `_PUB_MONTH_SQL`/`_PUB_YEAR_SQL`
    are their SQL twins, pinned together by `test_pub_month_sql_matches_python`
    and `test_pub_year_sql_matches_year_from_medline_date` over the edge cases
    (`"0"`, `"Sept"`, out of range, whitespace, non-ASCII). A divergence there
    makes every normalized record read as a PubMed mismatch. The month test
    iterates the **cross product** of month × MedlineDate inputs, because
    `_PUB_MONTH_SQL` falls through from one source to the other.
  - **`--shards N` is a *maximum*, not a count.** `PER_THREAD_OUTPUT` gives one
    file per writer thread, so `shards` caps the COPY's thread count (restored
    afterwards) and a small dataset can use fewer. DuckDB *appends* to that
    directory rather than clearing it, so `export_json` deletes its own
    `pubmed_metadata_*` files first — otherwise a shorter run leaves a previous
    run's shards to be read as current. `PARTITION_BY (pmid % shards)` would
    restore an exact count, but **DuckDB ≤ 1.5.4 rejects `PARTITION_BY` for
    `FORMAT JSON`** (`Binder Error: Unknown option`), so don't reach for it
    without checking again first.
  - **There is no `ORDER BY`, and that is deliberate (issue #8).** Sorting all
    40.9M rows by PMID materialized them before the first could be written —
    ~3 minutes of an 18-minute run, and the export's peak-memory event. Shard
    membership no longer depends on scan order (each thread owns a file), and
    nothing downstream consumes the order: the ingest is an ElasticSearch bulk
    load, and `validate` sorts its own PMID manifest.
- **`pub_month` passes approximate months through; only month *names* are
  normalized (issue #14).** The DocumentMetadataAPI spec contradicts itself: its
  prose says "capitalized three-letter abbreviations", but its own worked example
  for PMID:8000234 emits `"pub_month": "Sep-Dec"`. We follow the example.
  `export.normalize_month` folds a month name (`"03"`, `"March"`, `"Sept"`,
  `"sep"` → `"Mar"`/`"Sep"`) and returns everything else verbatim (`"Spring"`,
  `"Sep-Dec"`); only an out-of-range *number* becomes `""`. The `raw.isalpha()`
  guard is load-bearing — without it the 3-character prefix match silently
  truncated `"Sep-Dec"` to `"Sep"`. Two sources feed it, and PubMed uses all
  three renderings for the same record: `<Season>` shares the `pub_month` column
  with `<Month>` (the DTD makes them exclusive, so no schema column and no
  migration), and `_month_from_medline_date` recovers the text after a
  `MedlineDate`'s leading year — whose mandatory *whitespace* is what stops
  `"1999-2000"` (a year range, no month) yielding `"-2000"`. `pub_day` stays
  blank for these records, as the spec example has it. A cross-year
  `MedlineDate` therefore exports `pub_month` as `"Dec-1999 Jan"` — a month
  field carrying a year, deliberately, because it is what PubMed wrote and what
  its API renders back. `validate._VALID_MONTHS` excludes that shape on purpose,
  so those records stay visible as `month-format` warnings; the checker
  disagreeing with the export is the design here, not a bug. The wart is
  answered by a verbatim `pub_date` field (#17), not by inventing a tidier
  month — though whether consumers accept the odd `pub_month` beside it is
  still open (#43). Note the split: the
  `MedlineDate` half is export-only and needs no reload, the `<Season>` half only
  takes effect for files loaded after the change. Every `trim` in the generated
  SQL names `_WS`, the character set Python's `.strip()` removes — bare SQL
  `trim()` strips spaces alone, which made the twins disagree on a `<Month>`
  carrying a tab.
- **`pub_date` is the fidelity guarantee; the three parsed date fields are
  conveniences.** No arrangement of `pub_year`/`pub_month`/`pub_day` represents a
  cross-year `MedlineDate` — `"1998 Dec-1999 Jan"` (PMID:10188493) splits into a
  `pub_month` holding a year. So the export ships a twelfth field carrying
  PubMed's own string verbatim on **every** record, exactly as NCBI `esummary`'s
  `pubdate` does — including its unpadded day: the archival XML writes
  `<Day>01</Day>` where `esummary` renders `1` (PMID:35504184), so
  `normalize_day` strips the zero rather than letting most days below the tenth
  read as a mismatch. Consumers rendering a citation read `pub_date`; consumers
  sorting or filtering read `pub_year`; neither parses the other. `_PUB_DATE_SQL`
  takes `medline_date` whole when present, else assembles
  `year + normalize_month(month) + day`. It calls `_normalize_month_sql('la.pub_month')`
  rather than `_PUB_MONTH_SQL` for clarity and one fewer `regexp_extract`, **not**
  to avoid double-counting the `MedlineDate` — an earlier comment claimed that
  hazard and it does not exist, since the branch only runs when the
  `MedlineDate` is blank and a blank one contributes `''`, making the two
  expressions provably equal there.
  The two renderings PubMed serves must converge on one string (efetch's
  `<Year>1994</Year><Season>Sep-Dec</Season>` and the baseline's
  `<MedlineDate>1994 Sep-Dec</MedlineDate>` both give `"1994 Sep-Dec"`); that is
  what lets `validate` compare the field at all, and
  `test_pub_date_matches_ncbi_pubdate` pins it to esummary's real output.
  `pub_year` takes the **leading** year of a range, against the semantic argument
  for the trailing one — see `_year_from_medline_date`'s docstring, which records
  why and the evidence (NCBI's `sortpubdate` agrees; cross-year ranges are ~0.07%
  of PubMed).

  **Date comparison has two separate problems, fixed in two separate places.**
  *Rendering:* efetch serves a re-serialization rather than the archival string,
  so `efetch_documents` reconstructs one — normalizing the month, which is what
  makes the `<Year>+<Season>` rendering converge. A baseline
  `<MedlineDate>1998 September</MedlineDate>` therefore meets a reconstructed
  `"1998 Sep"`, both correct. `validate._normalize_date` folds the two spellings
  at comparison time, so it is not reported; nothing about the export changes.
  *Correlation:* `pub_date` is derived from the same three columns as
  `pub_year`/`pub_month`/`pub_day`, so gating on it would let one genuine date
  disagreement count four times instead of three, tightening the FAIL threshold
  on the records most likely to trip it. That is why it is a **SOFT** field, and
  normalization does not address it.
- **`esummary` is a third rendering, and the useful one for design questions.**
  Where efetch re-serializes the XML, `esummary` shows NCBI's *own* normalization
  decisions — `pubdate` ("1998 Dec-1999 Jan") and `sortpubdate` ("1998/01/01")
  settled both the shape and the leading-year rule for `export.pub_date` faster
  than any amount of arguing from the spec. Reach for it when the question is
  "what should we emit?" rather than "what did we parse?".
- **Measure PubMed by sampling PMIDs, not by downloading a baseline file.**
  "How common is this shape?" is answerable in seconds: draw random integers from
  `1..40_900_000`, hand 300 at a time to `esummary` (non-existent PMIDs simply
  drop out of the response), and tally. ~6,000 sampled records took under a
  minute and gave the numbers that decided two design calls — that cross-year
  `MedlineDate` ranges are ~0.07% of the corpus (so a general `pub_date` beat a
  special case), and that single-year ranges are ~7% (so passing them through
  mattered). A 30 MB baseline download answers one file; this answers the
  corpus, and needs no disk.
- **Gzip is the export's default, and `validate` must not need telling.** NDJSON
  compresses ~4-5x (~52 GiB of full-corpus shards down to ~12), DuckDB
  compresses each shard as it writes it, and `validate.find_shards` matches
  `.ndjson`/`.ndjson.gz` alike while `check_structure` reads through a raw handle
  so its byte-progress denominator stays the *compressed* size either way.
  `test_cli_export_then_validate_needs_no_flags` runs both commands with no
  flags, because a compressed default is only safe while the checker downstream
  stays flag-free.
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

- **The `#SBATCH` headers in `slurm/` are the only place an allocation is
  written down**, and `slurm/README.md` deliberately does not repeat them — it
  carries the *evidence* (dated run tables, peak RSS, the shard-read breakdown)
  and the reasoning instead. The two have different lifecycles: a new run
  appends a measurement, a changed cluster profile edits a header. Keeping both
  runnable meant two places to edit with nothing to catch a missed one;
  `test_readme_does_not_duplicate_the_allocations` holds the split. Don't
  "helpfully" restore a copy-pasteable `srun --mem=…` to the README.
- **`--dependency=afterok` does not cancel anything by default.** Slurm leaves a
  dependent whose dependency can never be satisfied *pending forever* with
  reason `DependencyNeverSatisfied`; it cancels only where the site set
  `kill_invalid_depend` in `slurm.conf`. `submit.sh` therefore passes
  `--kill-on-invalid-dep=yes` on every chained job, which is what makes its
  header's promise true on any site. In the same family: `sbatch --parsable`
  prints `jobid;clustername` on a multi-cluster site, so the id is truncated at
  the `;` before it reaches the next `--dependency`. Neither is observable off
  the cluster — both are pinned by tests in `tests/test_slurm_scripts.py`.
- **Compare manifest paths by base name, never as strings.** `MANIFEST_DIR` is
  environment-overridable, so `data/manifests/` and `data/manifests` both reach
  `05-validate.sbatch`; the second spelling makes `$manifest` and `find`'s
  output the same file but never string-equal, which silently let a same-day
  re-run pick today's manifest as its own baseline and report zero drops. The
  matching rule on the Python side is that a **`fail`** run writes no manifest
  at all (a short PMID set from a truncated export would poison the next run's
  drop check) while a **`warn`** run still does — the usual warning is "Entrez
  was unreachable", which says nothing about the set the shard read built.
- **Test the shell scripts by running them, not by reading them.** Every bug in
  `slurm/` so far was invisible on inspection and would have fired on the
  cluster, not locally: an empty array expanded under `set -u` is an error on
  bash 3.2 (and sat on a *fallback* path), and `:=` restored a default the
  config documented as disableable by setting it empty. `tests/test_slurm_scripts.py`
  runs them against a stub `uv` that echoes its arguments, which is cheap enough
  that new logic has no excuse. It also avoids `declare -A` so the scripts run
  on bash 3.2, which is what makes them testable on a macOS dev box at all.
  (One thing measured and found *not* to be a bug, so it is not re-litigated:
  `[[ cond ]] && arr+=(...)` under `set -e` is harmless mid-script — non-final
  commands of an AND-OR list are exempt. It bites only as the last statement of
  a script or function.)

## Development

```bash
uv sync --extra dev
uv run pytest          # no network needed
```

Tests gzip the readable XML fixtures under `tests/fixtures/` into temp
`pubmedNNnNNNN.xml.gz` files. Scratch downloads and databases go under `./data`
(gitignored); the CLI points `PYSTOW_HOME` at `--data-dir` so
`pubmed_downloader` caches under `data/pubmed/{baseline,updates}/`.
