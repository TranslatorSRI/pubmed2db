# AGENTS.md — orientation for this repo

pubmed2db downloads PubMed abstracts, loads them into a DuckDB database keeping
**full version history**, and exports the **latest** version of every abstract to
JSON (DocumentMetadataAPI field names, for Node Annotator / ElasticSearch) and to
Parquet (PubMed field names, for downloadable queries).

Everything a user needs — commands, options, operational notes, the year-boundary
rebuild — is in [`README.md`](./README.md); deferred work and known limitations
are in [`FUTURE.md`](./FUTURE.md); how a release and its data build are cut is in
[`docs/Releases.md`](./docs/Releases.md); what the outputs *mean*, for the
people who consume them, is in [`docs/data-model/`](./docs/data-model/README.md). This
file is the map plus the handful of decisions you cannot see from the code.

## Layout

`download → load → export`, wired through a `click` CLI. Each module's docstring
explains its part; this table is only a map.

| File | Responsibility |
| --- | --- |
| `src/pubmed2db/db.py` | Connection, schema init, `source_file` registry, `parse_file_name`. |
| `src/pubmed2db/schema.sql` | Normalized tables, `latest_*` views, and the `COMMENT ON` data dictionary. **Read its header before editing.** |
| `src/pubmed2db/datamodel.py` | `DATA_MODEL_VERSION`, the `pubmed2db_metadata` table, the version check on open, and the shape/doc generators. |
| `src/pubmed2db/download.py` | Fetches baseline/update files; adds `.md5` sidecar tracking. |
| `src/pubmed2db/parse.py` | Self-driven XML iteration over each file. |
| `src/pubmed2db/load.py` | Loads parsed files (full history, provenance-tagged), delete logic, journal dimension. |
| `src/pubmed2db/export.py` | JSON (one DuckDB `COPY`, gzipped by default) + Parquet export. |
| `src/pubmed2db/validate.py` | Post-export checks over a directory of NDJSON shards; emits a gated JSON report. |
| `src/pubmed2db/status.py` | Pipeline-readiness checks derived from DB state. |
| `src/pubmed2db/util.py` | Shared helpers for the long steps: progress/ETA, durations, peak RSS. |
| `src/pubmed2db/cli.py` | `download`, `journals`, `load`, `export`, `update`, `status`, `validate`. |
| `slurm/` | One `sbatch` script per pipeline step, `config.sh`, and `submit.sh`. See [`slurm/README.md`](./slurm/README.md). |
| `docs/` | Longer write-ups, named in CamelCase (`JournalCatalog.md`). `data-model/` is the exception: the data model's contract (`README.md`), its `Changelog.md` and its generated `Reference.md`. |

## Decisions that are not visible from the code

- **The outputs follow a versioned data model, separate from the code
  version.** The code version (`v1.2`) says how the code changed;
  `datamodel.DATA_MODEL_VERSION` (1.0.0, standard semver) says how consumers
  are affected, and `docs/Releases.md` defines both. It covers the database,
  the Parquet export and the NDJSON record together. One number, because the
  NDJSON fields are SQL over the tables, so documenting one documents the other.
  Changing the shape fails `tests/test_data_model.py` until you bump it:
  - **minor** only appends (a column at a table's end, a table, a view, an
    NDJSON field);
  - **major** for anything else, including a change in what a value *means*
    (what NULL stands for, an ordering's base) with no change to the shape.
    No test catches that one; ask whether a consumer must read anything
    differently.
  - **Patch** is not used. The version is the contract, not the contents:
    new PubMed or MeSH data never bumps it, and neither does a fix that
    changes values without changing how to read them. That goes in the code's
    release notes, with whether it needs a reload.

  Then write `tests/data_model/<MAJOR.MINOR>.json` with
  `python -m pubmed2db.datamodel --golden`; there is one file per
  `MAJOR.MINOR`, because a patch cannot change the shape. Every bump also
  needs an entry in `docs/data-model/Changelog.md` saying, per
  output, what consumers must do; a test checks that it exists. That per-output
  table is the deliberate alternative to an extra version level for "breaks
  some consumers but not others". **Never edit an existing version's
  golden file:** `test_minor_versions_only_add` compares consecutive files, and
  rewriting one erases the evidence it checks. Things that are easy to get
  wrong:
  - **Tiers live in the comments.** Each table comment starts with `Stable.`,
    `Provenance.` or `Internal.`; `datamodel.describe` reads the tier from
    there. Only internal tables (`pipeline_run`) are outside the contract.
  - **The comments are the data dictionary, and the doc is generated from
    them.** `docs/data-model/Reference.md` is `datamodel.render_reference`;
    edit the `COMMENT ON` in `schema.sql`, then run
    `python -m pubmed2db.datamodel --docs docs/data-model/Reference.md`. A test
    fails on a stale block, so don't hand-edit it. It and the changelog are
    separate from the README so the contract prose stays short enough to read
    whole; the three link each other by absolute URL on `main`, and
    `test_links_between_the_data_model_docs_resolve` checks those links.
  - **Pre-1.0 databases are refused, not migrated.** `check_compatible` runs
    *before* `schema.sql`, so a refused file is untouched. That is why the old
    `ALTER TABLE` migrations, and the NULL-`type_name` export warning they
    needed, are gone. From 1.0 on, the "only ever adds" rule in `schema.sql`'s
    header is back in force for minor versions.
  - **Timestamps are `TIMESTAMPTZ`, which DuckDB's Python client cannot fetch
    without `pytz`.** That is the only reason `pytz` is a dependency. The
    pre-1.0 naive `TIMESTAMP`s stored the loading machine's local wall-clock
    time: `now()` and a bound UTC datetime are both cast through the session's
    time zone.
  - **The exports don't read the `latest_*` views.** Each view re-evaluates
    `latest_article`, a window over every version, so the exports materialize
    one snapshot and filter against it. A test holds the views to exactly the
    exports' rows.
  - **`metadata.json` is written last**, after every file it checksums, and
    `validate` fails on any shard that differs from it. **The SHA-256s are for
    identity, not damage**, so don't justify them, or drop them, on integrity
    grounds. gzip already catches damage: DuckDB writes each shard as one gzip
    member, so truncation anywhere raises `EOFError` and a flipped bit fails
    the CRC (measured 2026-10-19). Only a hash catches an intact shard that
    isn't the one this export wrote (a leftover from another build) or damage
    to a `--no-gzip` export.
  - **`metadata.json` is also the "finished" mark the between-runs scripts
    key on.** `export` refuses to run while one exists in its output
    directory unless given `--overwrite` (it deletes the previous shards
    first, #24; the check is in `export.py`, not the Slurm script, so a
    hand-run export gets it too, and `submit.sh` repeats it before submitting
    so an `all` chain does not run its load first), and
    `scripts/archive_build.py` moves the directory to `data/builds/<build>/`
    named from its `corpus.build_name`. Two things there are deliberate:
    `data/manifests/` is copied and never moved, because the dated files are
    the chain every `validate` diffs against; and `scripts/fresh_database.py`
    *moves* a superseded baseline year out of `data/pubmed/{baseline,updates}`
    rather than deleting it, because `load` globs those two directories
    (registered or not) and the operator keeps the XML. `source_file.csv` in
    each build is the only surviving record of the MD5 NCBI published per
    file; no sidecars are kept on disk.
  - **`validate` asks PubMed about drops the database cannot explain**
    (`check_drops_since`), because a database built fresh from a new baseline
    never loaded the update files that carried the deletions published before
    it. Judged by `deleted_pmid` alone, the first run after every rebuild
    failed, wrote no manifest, and so failed again the next time. A drop PubMed
    still serves is the error; one it no longer serves is the
    `drops_retired_upstream` warning, **but only on a database created after
    the previous manifest** (`pubmed2db_metadata.created_at` against the
    write time in the manifest's gzip header, which a `cp -r` of the data
    directory keeps and the file's mtime does not) **and with no gap in its
    files** (`status.load_gaps`):
    a skipped update file's deletions look exactly like the ones published
    before the baseline. On a continuing database the same drop means an update
    file was missed, and stays a `missed_deletions` error; there PubMed's
    answer only names the failure, so one batch is sampled
    (`_DROP_DIAGNOSE_SAMPLE`) rather than all of them. The lookup is
    `pmids_served`, on esummary rather than efetch, because esummary names
    every ID it was asked about (a missing one with `"error"`): efetch's
    recovering parser skips what it cannot read, which made a degraded
    response read as "retired". On a clean rebuild it is capped at
    `_DROP_CONFIRM_LIMIT`, and a run over the cap fails, so the cap has to sit
    well above a year's deletions or the chain never recovers.

- **Reuse [`pubmed-downloader`](https://github.com/cthoyt/pubmed-downloader)
  as-is, no upstream changes.** We take its file listings, its cache layout and
  its `Article` data model, but not its transfers (see below) or its
  `iterate_process_*`/JSONL cache — DuckDB is our store, and
  `parse.py` drives the XML itself for the fields that pipeline drops (its
  docstring says which). Pinned `<0.1` because we call private APIs
  (`_extract_article`, `_ensure_urls`).
- **The journal dimension reads two NLM sources, and swapping to one loses
  data.** `J_Entrez.txt` gives `title`/`abbreviation_*`; NLM's serial catalog
  (serfile) gives `start_year`/`end_year`/`active`. The tempting simplification —
  "the catalog is richer, use it for everything" — is wrong: `J_Entrez.txt`'s
  `JournalTitle` **is** `esummary.fulljournalname`, byte for byte, and the
  catalog's `TitleMain` differs for 20% of journals (diacritics, corporate
  suffixes, `(Baltimore, Md.)` disambiguators). Reconstructing the Entrez form
  from the catalog tops out at 99.22%. CatfilePlus was evaluated and rejected: it
  covers 867 of the 902 journals the serfile *baseline* lacks, but the monthly
  deltas already recover 429 of those, so its marginal gain is ~1% of the
  dimension — for 11× the download, MARCXML-only since 2024, and the same catalog
  titles. All of it, with the
  sample sizes and the redo recipe, is in
  [`docs/JournalCatalog.md`](./docs/JournalCatalog.md) — written to be
  shareable outside this repo, which is also why it is not restated here. Three
  traps it records: ~1,600 catalog years are MARC wildcards (`19uu`, `uuuu`) that
  `int()` raises on; those wildcards split two ways for `active` (`uuuu` is
  *unknown*, `19uu` is *ceased* at an uncertain date); and ISSN comparisons are
  meaningless unless filtered to `ValidYN="Y"`.
- **No citation graph, and that is a decision, not a gap.** One real article
  carries ~444 references, which would make it the largest table here for data no
  consumer wants. `parse._cited_pmids` is parked (uncalled) with re-enabling
  instructions in its docstring; `test_no_reference_citation_table` stops it
  creeping back.
- **`identifiers` is derived at export, never stored.** DOIs and PMCIDs already
  land in the normalized `article_id` table on every load, so the JSON export
  aggregates them there (`export._LATEST_METADATA_SQL`'s `ids` CTE says how, and
  `export.ID_PREFIXES` is the single place the Babel-compatible CURIE casing is
  written down, including the interim `PMC` prefix for PMCIDs, which follows
  Babel until biolink-model#1366 and Babel#1044 rule; issue #33 tracks it).
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
- **`publication_types` comes from our own parse, not upstream's
  `type_mesh_ids`.** Upstream keeps only the MeSH UI and *sorts* it, losing the
  name ("Review" is what consumers filter on) and PubMed's order. That order is
  not alphabetical and equals `esummary`'s `pubtype` (8/8 sampled multi-type
  records), so `publication_type.position` is what makes the export match NCBI.
  `parse._publication_types` reads the exact path, and `validate` reuses it for
  the efetch side, so the two cannot diverge. One thing to keep:
  - **The field is SOFT, under its own check.** NLM revises types when MEDLINE
    indexing completes, so live efetch can be ahead of our last update file.
    `validate._SOFT_GROUPS` gives every soft reason its own check and warning
    code. Add a new soft field there, not to an existing group: a single shared
    check reported every `pub_date` disagreement as `journal_mismatches` from
    the day `pub_date` became soft.
- **MeSH labels are PubMed's, per record, and never reconciled.** Every MeSH
  UI/name pair (`mesh_heading`, `mesh_qualifier`, `publication_type`) is read
  from the record's own XML and stored against `(pmid, source_file)`. Nothing
  looks names up in the MeSH vocabulary, and there is no dimension keyed by
  UI, so one UI can carry different names in different records, or in
  different versions of the same record. This is deliberate: matching the
  PubMed downloads exactly comes first. The database is expected to be deleted
  and rebuilt from a fresh baseline once or twice a year (README, "Prefer a
  fresh database at the year boundary"), and that re-issue resets any drift.
  Don't add a MeSH descriptor table to "fix" the names.
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
    restore an exact count, but **DuckDB still rejects `PARTITION_BY` for
    `FORMAT JSON`** (`Binder Error: Unknown option for COPY ... TO ...
    (FORMAT JSON): "partition_by"`) — re-checked on 1.5.5, the version
    `pyproject.toml` now floors. Don't reach for it without checking again.
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
- **The cgroup measurements below were taken on duckdb 1.5.4 and have not been
  re-taken.** The dependency floor is now 1.5.5, and nothing about the readings
  is version-fragile in principle — but they are measurements, and the rule that
  produced them is *measure it, don't reason about it*. Re-run the one-line
  probe on the first cluster job rather than assuming they carried over (#42 is
  the run that will have the chance).
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
  element before changing any parsing or export code. The stronger case also
  exists: efetch serves values the archival XML never had — an `<Issue>` of `1`,
  a whole `<Abstract>`, a DOI in neither `ArticleIdList` nor `ELocationID` —
  which was half of the 2026sep26 sample's core mismatches (#59). A blank on
  our side against a value on efetch's is therefore not evidence we dropped it.
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
  single record. Two download
  *behaviours* are also easy to assume backwards (`_ensure_urls` sorts
  newest-first, so `--limit N` takes the head; a cached file is skipped by name,
  so a republished one keeps stale bytes unless its checksum moved); both are
  pinned by tests in `tests/test_db_download.py`, whose docstrings say what they
  hold in place.
- **Nothing is fetched with pystow's `ensure()`, directly or through a
  pubmed-downloader helper.** Its urllib backend sets no timeout, and a stalled
  NLM connection hung a live `journals` run with the socket still open; its
  requests backend never checks the status. The PubMed files, `J_Entrez.txt`
  and the serial catalog all go through `util.download_file` (timeout,
  `raise_for_status`, `.part` then rename, bytes written undecoded), while
  pubmed-downloader and pystow still supply the listings and the cache paths.
  `test_nothing_is_fetched_with_pystow_ensure` scans the package for any
  `ensure`/`ensure_*` call, because the download fakes only catch one on the
  module they replace. A file that fails is skipped and the sync carries on,
  then `download` exits non-zero naming it; a SIGKILL skips `download_file`'s
  cleanup, so `download` and the catalog prune each sweep stale `.part` files.

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
