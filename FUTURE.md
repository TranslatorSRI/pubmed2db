# Future work & known limitations

Tracked follow-ups for pubmed2db. This is the initial implementation; the items
below are deliberately deferred.

## Integrations / goals

- **Replace Babel's PubMed downloader.** Wire this tool into
  `NCATSTranslator/Babel` `src/createcompendia/publications.py` so Babel reuses
  these downloads/exports instead of its own wget+TSV pipeline. Sharing the
  download cache works via `PYSTOW_HOME`.
- **Confirm the Node Annotator JSON contract.** We currently emit sharded NDJSON,
  one document per line keyed `PMID:<id>` with the DocumentMetadataAPI fields.
  Verify this matches what Node Annotator's ElasticSearch ingest expects (id
  field, shard sizing, gzip?).

## Upstream dependency (`cthoyt/pubmed-downloader`)

The dependency is pinned `<0.1` because we call private APIs (`_extract_article`,
`_ensure_urls`); re-test before raising the ceiling.

- **Revert the custom journal parser** in `load._parse_journal_overview` once
  `pubmed_downloader.catalog.process_journal_overview()` no longer requires
  `start_year`/`end_year` (broken in ≤ 0.0.14 — those fields aren't in
  `J_Entrez.txt`, filed at https://github.com/cthoyt/pubmed-downloader/pull/16).
  Then we can go back to using the library's `Journal` model
  directly. See `CLAUDE.md`.
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

### TODO: investigate the two reference bugs before reporting them upstream

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

- [ ] Decide whether to re-load affected files with `load --force` (a full
  re-parse, roughly a baseline's worth of time) or to rebuild from scratch, and
  note the answer in the README's "Re-running after a gap" section. Rebuilding is
  the simpler answer: `load --force` fixes `article_id`'s rows but leaves the
  dropped `reference_citation` table sitting there, since `schema.sql` only ever
  adds tables.

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
- **JSON export's global sort** (`ORDER BY la.pmid`) sorts the whole latest set to
  keep round-robin sharding deterministic. Sharding on `pmid % shards` would remove
  it; measure first, since restricting the abstract aggregation to the latest
  snapshot already cut into the same peak. Tracked in issue #8.
- **No indexes** are created on the big per-version tables yet (kept lean for bulk
  load). Add them if interactive querying of the DB becomes a use case.

## Data fidelity

- **`MedlineDate` ranges** (e.g. "1998 Spring", "1998 Dec-1999 Jan") currently
  yield empty `pub_year`/`pub_month`/`pub_day` in the JSON export. Could parse a
  leading 4-digit year out of `medline_date` to populate `pub_year`.
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
