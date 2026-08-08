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
| `src/pubmed2db/parse.py` | Self-driven XML iteration: calls cthoyt's `_extract_article` per record, plus raw `PubDate`, `DeleteCitation`, and article IDs. |
| `src/pubmed2db/load.py` | Loads parsed files (full history, provenance-tagged), `latest`/delete logic, journal dimension. |
| `src/pubmed2db/export.py` | JSON (spec fields, empty-string-not-null) + Parquet export. |
| `src/pubmed2db/validate.py` | Post-export sanity checks over a NDJSON directory: structure, coverage (Entrez + DB denominators), sampled Entrez field comparison, deletion confirmation; emits a gated JSON report. |
| `src/pubmed2db/status.py` | Pipeline-readiness checks derived from DB state (drives the CLI's prerequisite errors/warnings). |
| `src/pubmed2db/util.py` | Shared helpers: progress/ETA formatting, duration formatting, peak-RSS reporting. |
| `src/pubmed2db/cli.py` | `download`, `journals`, `load`, `export`, `update`, `status`, `validate`; group-level `--threads`/`--memory-limit`/`--temp-dir` are applied by `_connect`. |

## Key design decisions (and why)

- **Reuse [`cthoyt/pubmed-downloader`](https://github.com/cthoyt/pubmed-downloader) as-is, no upstream changes.**
  It handles bulk download + the rich `Article` data model. We do not use its
  `iterate_process_*`/JSONL cache — DuckDB is our store.
- **No citation graph.** `reference_citation` was removed: one real article
  carries ~444 references, so at corpus scale it would have been the largest
  table here, for data no consumer wanted. `parse._cited_pmids` is parked
  (uncalled) with re-enabling instructions in its docstring.
- **We drive the XML iteration ourselves** (`parse.py`) rather than using cthoyt's
  process pipeline, because we need things it drops or gets wrong: the **raw
  `PubDate` components** (so `MedlineDate`-only/partial dates keep full fidelity
  instead of being collapsed to a `datetime.date`), **`<DeleteCitation>`** PMIDs
  (needed for latest-version selection), and **cited PMIDs + article IDs** (see the
  upstream issues below).
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
- **Two memory numbers mislead, and both have bitten.** DuckDB sizes `threads`
  from the machine's core count and `memory_limit` from its *physical RAM* — it
  cannot see a Slurm cgroup, so on a cluster node both default far above the
  allocation and a long load caches its way into an OOM kill. And
  `util.peak_rss_gib` is `ru_maxrss`, a high-water mark that only ever rises, so
  a climbing per-file "peak" is not evidence of a leak; `current_rss_gib` is the
  one that can fall. Before diagnosing loader memory, read
  `slurm/README.md` → "Running `load`: how much memory?".
- **DuckDB writes the JSON, Python does not.** `export_json` is one
  `COPY (...) TO <dir> (FORMAT JSON, PER_THREAD_OUTPUT true)`, not a
  `fetchmany` loop calling `json.dumps` per row: the old loop spent ~80% of the
  export's wall time serializing on a single core while the other seven idled
  (measured 17.7k docs/s; the same corpus through `COPY` runs at 56.2k). Two
  consequences worth knowing before editing:
  - **`_JSON_FIELDS` is the record definition** — output name → SQL expression,
    in emitted order — so the DocumentMetadataAPI names, the
    empty-string-not-null rule and the field order live in exactly one place,
    and `validate` imports `JSON_FIELDS` from it. `pub_month` and
    `_year_from_medline_date` still exist as Python, because `validate` needs
    them for the efetch side; `_PUB_MONTH_SQL`/`_PUB_YEAR_SQL` are their SQL
    twins and `test_pub_month_sql_matches_python` /
    `test_pub_year_sql_matches_year_from_medline_date` pin the pairs together
    over the edge cases (`"0"`, `"Sept"`, out-of-range, whitespace, non-ASCII).
    A divergence there makes every normalized record read as a PubMed mismatch.
    The month test iterates the **cross product** of month × MedlineDate inputs,
    because `_PUB_MONTH_SQL` falls through from one source to the other.
  - **`--shards N` is now a *maximum*, not a count.** One file per writer
    thread is what `PER_THREAD_OUTPUT` gives, so `shards` caps the COPY's
    thread count (restored afterwards) and a small dataset can use fewer.
    DuckDB *appends* to that directory rather than clearing it, so
    `export_json` deletes its own `pubmed_metadata_*` files first — otherwise a
    shorter run leaves a previous run's shards to be read as current.
    `PARTITION_BY (pmid % shards)` would restore an exact count, but **DuckDB
    ≤ 1.5.4 rejects `PARTITION_BY` for `FORMAT JSON`** (`Binder Error: Unknown
    option`), so don't reach for it without checking again first.
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
  blank for these records, as the spec example has it. Note the split: the
  `MedlineDate` half is export-only and needs no reload, the `<Season>` half only
  takes effect for files loaded after the change.
- **`pub_date` is the fidelity guarantee; the three parsed date fields are
  conveniences.** No arrangement of `pub_year`/`pub_month`/`pub_day` represents a
  cross-year `MedlineDate` — `"1998 Dec-1999 Jan"` (PMID:10188493) splits into a
  `pub_month` holding a year. So the export ships a twelfth field carrying
  PubMed's own string verbatim on **every** record, exactly as NCBI `esummary`'s
  `pubdate` does. Consumers rendering a citation read `pub_date`; consumers
  sorting or filtering read `pub_year`; neither parses the other. `_PUB_DATE_SQL`
  takes `medline_date` whole when present, else assembles
  `year + normalize_month(month) + day` — and note it calls
  `_normalize_month_sql('la.pub_month')`, **not** `_PUB_MONTH_SQL`, which would
  fold the `MedlineDate` back into a branch that only runs when there isn't one.
  The two renderings PubMed serves must converge on one string (efetch's
  `<Year>1994</Year><Season>Sep-Dec</Season>` and the baseline's
  `<MedlineDate>1994 Sep-Dec</MedlineDate>` both give `"1994 Sep-Dec"`); that is
  what lets `validate` compare the field at all, and
  `test_pub_date_matches_ncbi_pubdate` pins it to esummary's real output.
  `pub_year` takes the **leading** year of a range, against the semantic argument
  for the trailing one — see `_year_from_medline_date`'s docstring, which records
  why and the evidence (NCBI's `sortpubdate` agrees; cross-year ranges are ~0.07%
  of PubMed).
- **The JSON export does not sort (issue #8).** `ORDER BY la.pmid` materialized
  all 40.9M rows before the first could be written — ~3 minutes of a 18-minute
  run, and the export's peak-memory event. Shard membership no longer depends on
  scan order (each thread owns a file), and nothing downstream consumes the
  order: the ingest is an ElasticSearch bulk load and `validate` sorts its own
  PMID manifest.
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
  tests stay offline). The process exits non-zero on errors so it can gate an HPC
  pipeline.
- **A `validate` mismatch is not evidence about what we parsed.** efetch output is
  a *rendering*, not the archival XML: PubMed serves PMID 152567 as
  `<Year>1978</Year><Season>Jul-Aug</Season>`, while the baseline file it was
  loaded from holds `<MedlineDate>1978 Jul-Aug</MedlineDate>` and no `<Year>` at
  all. Diagnosing a field mismatch from efetch alone will therefore point at the
  wrong layer. Download the baseline file containing the PMID and read the raw
  element before changing any parsing or export code.
  **`esummary` is a third rendering, and the useful one for design questions.**
  Where efetch re-serializes the XML, `esummary` shows NCBI's *own* normalization
  decisions — `pubdate` ("1998 Dec-1999 Jan") and `sortpubdate` ("1998/01/01")
  settled both the shape and the leading-year rule for `export.pub_date` faster
  than any amount of arguing from the spec. Reach for it when the question is
  "what should we emit?" rather than "what did we parse?".
- **Measure PubMed by sampling PMIDs, not by downloading a baseline file.**
  "How common is this shape?" is answerable in seconds: draw random integers from
  `1..40_900_000`, hand 300 at a time to `esummary` (non-existent PMIDs simply
  drop out of the response), and tally. ~6,000 sampled records took under a
  minute and gave the numbers that decided two design calls this session — that
  cross-year `MedlineDate` ranges are ~0.07% of the corpus (so a general
  `pub_date` beat a special case), and that single-year ranges are ~7%
  (so passing them through mattered). A 30 MB baseline download answers one file;
  this answers the corpus, and needs no disk.
- **Every check is recorded, not just the failures.** `Report.record` appends a
  `Check` (name, expectation, status, observed) for passing checks too, and
  `errors`/`warnings`/`skipped_checks` are *projections* of that list rather than
  separately maintained arrays — so they cannot drift from it, and stdout can
  enumerate what was verified rather than only what broke. `format_summary` is a
  pure renderer over the report dict, which means anything printed is provably in
  the archived `validation_report.json`. `skip` (evidence obtainable — pass a
  flag, go online) and `n/a` (nothing to evidence) are deliberately distinct, so
  `skipped_checks` stays an actionable to-do list. "Expected" is always defined by the exporter itself, never
  restated: the field comparison imports `pub_month`, `_year_from_medline_date`
  *and* `_MONTH_ABBR` from `export`, so any normalization the export applies is
  applied to the efetch side too — otherwise every record the export
  normalizes reads as a mismatch. (`_MONTH_ABBR` rather than
  `calendar.month_abbr`, which is `LC_TIME`-dependent: under a non-English
  locale the whole `month-format` check would have warned on every record.)
  `EXPECTED_FIELDS` is `frozenset(export.JSON_FIELDS)`
  — the exporter's own field list, which its `COPY` projection is built from, so
  the record shape here cannot drift from what shipped.
  `test_expected_fields_matches_spec` additionally locks the twelve field names,
  since they are an external contract with Node Annotator / ElasticSearch.
  `identifiers` is the one list-valued field, so `check_fields` compares it as a
  set rather than through the string path, and `export.ID_PREFIXES` is the single
  place CURIE casing is written down.
- **PMID-set drift needs a sidecar, not a bigger report.** The report stores
  counts, never millions of PMIDs, so `--manifest` writes a sorted gzipped
  `pmids.txt.gz` from the set the structure check already holds and
  `--previous-manifest` diffs against it. A drop the `deleted_pmid` table
  explains is expected; an unexplained one is an **error** (records lost, not
  retired), downgraded to a warning when no database is available to attribute
  it. This catches same-count exports whose contents silently changed.

## Known upstream issues (`pubmed-downloader` ≤ 0.0.14)

Three bugs we work around; all tracked in `FUTURE.md` with a pinning test each, so
they fail loudly once upstream fixes them. The dependency is pinned `<0.1` because
we also call private APIs (`_extract_article`, `_ensure_urls`).

Every extraction in `_extract_article` uses a `.//` descendant search, and two of
them hit the wrong subtree. **Check the scope of any field you take from that
parser before trusting it** — both bugs are silent, and both corrupted data we
had already loaded.

- **Journal parsing raises.** `catalog.process_journal_overview()`'s `Journal` model
  requires `start_year`/`end_year`, which the real `J_Entrez.txt` does not provide.
  We **parse the overview file ourselves** in `load._parse_journal_overview`
  (reusing only `ensure_journal_overview()` for the download).
- **References are never found.** `_extract_article` looks for
  `.//ReferenceList/Reference` under `MedlineCitation`, but PubMed nests
  `<ReferenceList>` under `<PubmedData>`, a *sibling* — so `Article.cites_pubmed_ids`
  is always empty on real data (0 rows across 14,201 real articles, from a file
  whose records carry 444 references). Harmless for us now, since we don't store
  the citation graph; `parse._cited_pmids` keeps a working extraction, uncalled.
- **Article IDs are over-collected.** `pubmed_data.findall(".//ArticleIdList/ArticleId")`
  descends into that same `<ReferenceList>`, attributing every *cited* reference's
  DOI/PMID to the citing article — one real record (PMID:41136637) contributed 426
  foreign DOIs. `parse._article_ids` uses the direct
  `PubmedData/ArticleIdList/ArticleId` path instead (the path Babel reads).

Both silently corrupted `article_id` and left `reference_citation` empty from the
first load, so **any database built before these fixes needs rebuilding** — see
`FUTURE.md` for why `load --force` alone is not enough.

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
