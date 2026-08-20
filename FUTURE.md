# Future work & known limitations

Tracked follow-ups for pubmed2db. This is the initial implementation; the items
below are deliberately deferred.

When an item here gets its own issue, link the issue number in the entry (`(#30)`).
When that issue closes, **delete the entry** — the issue is the record from then
on, and a copy left here rots into a contradiction of the code.

## Integrations / goals

- **Replace Babel's PubMed downloader.** Wire this tool into
  `NCATSTranslator/Babel` `src/createcompendia/publications.py` so Babel reuses
  these downloads/exports instead of its own wget+TSV pipeline. Sharing the
  download cache works via `PYSTOW_HOME`.
- **Confirm the Node Annotator JSON contract — and do it before the next
  production export.** We emit sharded NDJSON, one document per line keyed
  `PMID:<id>` with the DocumentMetadataAPI fields. Verify this matches what Node
  Annotator's ElasticSearch ingest expects (id field, shard sizing, gzip).
  **Two things changed under the ingest's feet** and neither can be checked from
  this repo, because the consumer lives outside it:
  - **Shards are gzipped by default now.** `data/json/pubmed_metadata_*.ndjson`
    becomes `*.ndjson.gz`. `validate` handles both (`find_shards` matches either
    extension), which is the *only* consumer this repo can speak for. If the
    ingest globs `*.ndjson` or does not decompress, it reads **nothing** — an
    empty ingest, not an error, which is the worst shape for a failure to take.
    `--no-gzip` restores the old artifact if the answer is no.
  - **`pub_month` can carry a year** ([#43](https://github.com/TranslatorSRI/pubmed2db/issues/43)).
    A cross-year `MedlineDate` exports `"pub_month": "Dec-1999 Jan"`
    (PMID:10188493, ~1 in 5,773 records), verbatim as PubMed wrote it. The
    verbatim `pub_date` field (#17) gives consumers a clean string to render
    from, but does not by itself answer whether the odd `pub_month` is
    acceptable alongside it. Answering "blank it instead" is a one-line gate in
    the export — and one that has to be made *before* a whole-corpus run.
  - **Shard names lost their zero padding.** `pubmed_metadata_00000.ndjson`
    became `pubmed_metadata_0.ndjson.gz`: DuckDB's `PER_THREAD_OUTPUT` names
    files from `FILENAME_PATTERN '{i}'`, which emits a bare index. Anything
    globbing `pubmed_metadata_*` is unaffected; anything matching the padded
    form, or relying on the files sorting lexically past nine, is not.

## Validation (`validate`)

- **Richer deletion status** (#30). Deletion confirmation currently treats
  "efetch returns nothing" as deleted. Entrez `esummary` distinguishes deleted
  vs. merged/moved records; use it to label merges explicitly rather than
  surfacing them as "still live → review manually".
- **Semantic field tolerance** (#31). Journal name/abbrev come from the NLM
  Catalog dimension, not the article XML, so they are compared as *soft*
  (warning-only) fields; revisit if a stricter journal cross-check is wanted.

## Upstream dependency (`cthoyt/pubmed-downloader`)

The dependency is pinned `<0.1` because we call private APIs (`_extract_article`,
`_ensure_urls`); re-test before raising the ceiling.

- **Revert the custom journal parser** in `load._parse_journal_overview` once
  `pubmed_downloader.catalog.process_journal_overview()` no longer requires
  `start_year`/`end_year` (broken in ≤ 0.0.14 — those fields aren't in
  `J_Entrez.txt`, filed at https://github.com/cthoyt/pubmed-downloader/pull/16).
  Then we can go back to using the library's `Journal` model directly. See
  `load._parse_journal_overview` for what we do instead.
- **`cites_pubmed_ids` never matches, but we no longer care.** `_extract_article`
  searches `medline_citation.findall(".//ReferenceList/Reference")`, but PubMed
  nests `<ReferenceList>` under `<PubmedData>`, so `Article.cites_pubmed_ids` is
  **always empty** on real data. We dropped the citation graph rather than fix it
  (see below), so this affects nothing we store — but it is still a real upstream
  bug, and `parse._cited_pmids` plus `test_cited_pmids_is_parked_but_works` keep a
  working counter-example for whenever it gets reported.
- **Revert `parse._article_ids`** once upstream stops over-collecting. It gathers
  `pubmed_data.findall(".//ArticleIdList/ArticleId")`, and that `.//` descends into
  `<ReferenceList>`, so every *cited* reference's DOI/PMID is attributed to the
  citing article — silently wrong rows in `article_id`, proportional to reference
  count. We use the direct `PubmedData/ArticleIdList/ArticleId` path (and drop the
  redundant `pubmed` self-ID). `test_article_ids_exclude_reference_ids` pins it.
- **The citation graph is not stored — decided, not deferred.** `reference_citation`
  was removed: one real article carries ~444 references, so at corpus scale it
  would have been the largest table in the database, and no consumer wants it.
  `parse._cited_pmids` is parked (uncalled) with re-enabling instructions in its
  docstring, and `test_no_reference_citation_table` keeps it from creeping back.
  A database built before the removal keeps a stale, populated table; `DROP TABLE
  reference_citation` clears it.

### TODO: investigate the two reference bugs before reporting them upstream (#34)

Nothing has been filed against `cthoyt/pubmed-downloader` for either, and nothing
should be until the open items below are answered. (The journal-model fix is the
exception: already filed as https://github.com/cthoyt/pubmed-downloader/pull/16.)

Real-data evidence gathered on the `add-doi-and-pmcids` branch, which found the
`article_id` bug independently:

- [x] **`article_id` contamination is real and large.** PMID:41136637 alone
  contributed 426 cited references' DOIs alongside its own.
- [x] **`reference_citation` really is always empty.** 0 rows from 14,201 real
  articles whose records carry hundreds of references between them — so the
  failure is total, not a partial-match edge case.

Still open:

- [ ] **Confirm `<ReferenceList>` placement across release years.** The 14,201-article
  sample shows the current layout puts it under `<PubmedData>`; verify it was never
  under `<MedlineCitation>` in older baselines. If both placements occur
  historically, upstream's selector isn't simply wrong and the report changes shape.
- [ ] **Audit upstream's other `.//` selectors for the same over-reach.** Partly
  done — auditing found `cites_pubmed_ids` as the second instance. Still unchecked:
  `pubmed_data.findall(".//History/PubMedPubDate")` (looks safe only because
  `<Reference>` has no `<History>` — confirm), and the abstract/MeSH/author
  selectors. There may be one report to file, not two.
- [ ] **Check which versions are affected.** We only tested 0.0.14. Establish the
  range before claiming one in a report.
- [ ] **Validate `_article_ids` against real data**, not just the fixture: records
  carrying unusual `ArticleId` types, and any whitespace or empty values it would
  silently drop. (`_cited_pmids` needs this only if the citation graph is ever
  revived.)
- [ ] **Then decide: report upstream, or keep the workaround local.** Only after
  the above. Also worth deciding at that point whether our other additions (raw
  `PubDate` components, `DeleteCitation` handling) are worth contributing.

### TODO: existing databases carry the bad rows

`article_id` rows written before the fix are still wrong in any database loaded
with the previous code, and that is not corrected by a normal incremental run —
`needs_load` only re-parses files whose checksum moved. Such a database also
still has a populated `reference_citation` table, which nothing will clear.

- [x] **Rebuild from scratch, don't `load --force`.** Decided and written into
  the README's "Re-running after a gap". `load --force` applies a parsing change
  to the whole corpus, but `schema.sql` only ever adds — `CREATE TABLE IF NOT
  EXISTS` plus `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` — so a table dropped
  from the schema keeps its rows through any number of forced reloads, and
  `reference_citation` is exactly that case. Both cost one full `load`; only the
  rebuild is guaranteed to leave the shape the schema describes.

## Scale & performance (full PubMed is ~38M articles, ~1500+ files)

- **Loader throughput — done (serial).** `load.load_parsed` now inserts each
  file's rows columnar via Arrow `INSERT ... SELECT` (~200k rows/s) instead of
  row-by-row `executemany` (~2.5k rows/s), cutting per-file load from ~20 min to
  ~5–6 s. Peak RSS is logged per file (~0.8 GiB for a 5k-article file); see
  `slurm/README.md` and `scripts/benchmark_load.py`.
- **Loader parallelism — deferred.** Serial load of a full baseline is now ~2–3 h,
  likely fine. To go faster, parallelize across files: since DuckDB is
  single-writer, have parallel Slurm tasks each parse one XML → write per-file
  **Parquet shards** (no shared writer), then a single step does
  `INSERT INTO t SELECT * FROM read_parquet('shards/*')`. Bigger rearchitecture;
  do it only if 2–3 h serial becomes a bottleneck.
- **`latest_article` view** runs a window over the entire `article` table on every
  read. At full scale, consider an index on `article(pmid, file_order_key)` or
  materializing the latest set into a table before export.
- **JSON export throughput — done.** The export was a single Python loop calling
  `json.dumps` per row behind a full `ORDER BY la.pmid`; the sort delayed the
  first written row by ~3 minutes of an 18-minute run and was the job's
  peak-memory event, and the loop then serialized 40.9M documents on one core
  while the other seven idled. DuckDB now writes the NDJSON itself
  (`COPY ... FORMAT JSON`, one file per writer thread) with no sort: 3x faster
  end-to-end on a 2M-document benchmark (112.9s → 35.5s), byte-identical record
  sets. Closes issue #8. **Still to record from a cluster run:** the new peak
  RSS, which decides whether `--mem=256G` can come down.
- **No indexes** are created on the big per-version tables yet (kept lean for bulk
  load). Add them if interactive querying of the DB becomes a use case.

## Data fidelity

- **`MedlineDate` ranges — `pub_year` and `pub_month` done, `pub_day` blank by
  design.** Records whose `PubDate` is a season or a range carry no `<Year>`;
  PubMed puts the whole thing in `<MedlineDate>` ("1998 Spring", "1978 Jul-Aug",
  "1998 Dec-1999 Jan"). The export recovers the leading 4-digit year via
  `export._year_from_medline_date`, which a full-corpus `validate` run showed was
  the single largest source of export/Entrez disagreement (18 of 20 sampled
  mismatches). Confirmed against the archival XML rather than inferred:
  `pubmed26n0005.xml.gz` holds PMID 152567 as
  `<MedlineDate>1978 Jul-Aug</MedlineDate>` with no `<Year>` element, and **3,625
  of that file's 30,000 records (12%)** are the same shape — 127 distinct date
  strings, every one of which yields a year, none disagreeing with any other
  4-digit run in the string. `pub_month` now takes the rest of that string
  verbatim (issue #14, `export._month_from_medline_date`), since the spec's own
  PMID:8000234 example expects `"Sep-Dec"`. `pub_day` stays empty — a range has
  no single day, and the spec example leaves it blank too.
  **Still open:** how common `<Season>` actually is in the *archival* files. The
  parser now reads it (into the `pub_month` column), but every observed case so
  far has been the `<MedlineDate>` form in the baseline and the `<Season>`
  rendering only from efetch. `zgrep -c "<Season>"` over a baseline file would
  settle it; if the answer is zero, that half of the change is pure insurance.
- **The PMCID CURIE prefix is not settled** ([#33](https://github.com/TranslatorSRI/pubmed2db/issues/33)).
  We emit `PMCID:PMC1234567`. Babel's `src/prefixes.py` says `PMC`, and neither
  the Core Components specification
  ([CCWG#15](https://github.com/NCATSTranslator/Core-Components-Working-Group/issues/15))
  nor the DocumentMetadataAPI README carries a PMC example to arbitrate; the
  production endpoint resolves PMCIDs under neither form. To be settled
  alongside [Babel#1044](https://github.com/NCATSTranslator/Babel/issues/1044) —
  a change is a one-line edit to `export.ID_PREFIXES` plus a re-export, since
  `identifiers` is derived at export and never stored.
- **`ELocationID` DOIs are not read** (#35). The exported `identifiers` come from
  `PubmedData/ArticleIdList` only — the same place Babel reads, and the
  authoritative one. A DOI can also appear as
  `Article/ELocationID[@EIdType="doi"]`, normally as a duplicate; parse it as a
  fallback if records ever turn up with the latter but not the former.
- **Grounding is off.** We call `pubmed_downloader`'s parser with `ground=False`
  (no MeSH/ROR/ORCID lookups), so `author_affiliation.ror` etc. are unpopulated.
  The library's `[process]` extra (pyobo/orcid-downloader) could enable grounding
  if needed — at significant dependency + runtime cost.

## Misc

- **MD5 verification — done.** `download --verify` now hashes only files that are
  new or whose published checksum moved, so re-syncing an unchanged baseline costs
  no local I/O and verification can stay on by default. It no longer detects
  corruption that appears *after* a successful download; if that ever matters, add
  an explicit `--reverify` sweep rather than re-hashing on every run.
- **DeleteCitation re-add edge case** is handled (delete then later re-add) by the
  `latest_article` view comparing max delete order vs the latest article order, but
  is only covered by synthetic fixtures — worth confirming against real data if it
  ever occurs.
