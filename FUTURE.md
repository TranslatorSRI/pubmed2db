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

## Validation (`validate`)

- **Richer deletion status.** Deletion confirmation currently treats "efetch
  returns nothing" as deleted. Entrez `esummary` distinguishes deleted vs.
  merged/moved records; use it to label merges explicitly rather than surfacing
  them as "still live → review manually".
- **Semantic field tolerance.** Journal name/abbrev come from the NLM Catalog
  dimension, not the article XML, so they are compared as *soft* (warning-only)
  fields; revisit if a stricter journal cross-check is wanted.

## Upstream dependency (`cthoyt/pubmed-downloader`)

- **Revert the custom journal parser** in `load._parse_journal_overview` once
  `pubmed_downloader.catalog.process_journal_overview()` no longer requires
  `start_year`/`end_year` (broken in ≤ 0.0.14 — those fields aren't in
  `J_Entrez.txt`). Then we can go back to using the library's `Journal` model
  directly. See `CLAUDE.md`.
- **Report the `xrefs` bug upstream.** `_extract_article` uses
  `.//ArticleIdList/ArticleId` under `PubmedData`, which also matches
  `ReferenceList/Reference/ArticleIdList` and so attributes every cited
  reference's DOI/PMCID to the citing article. Worked around in `parse._xrefs`;
  the one-character fix upstream is anchoring the path to the direct child.
- Consider whether any of our additions (raw `PubDate` components,
  `DeleteCitation` handling) are worth contributing upstream after all.

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
- **No indexes** are created on the big per-version tables yet (kept lean for bulk
  load). Add them if interactive querying of the DB becomes a use case.

## Data fidelity

- **`MedlineDate` ranges** (e.g. "1998 Spring", "1998 Dec-1999 Jan") currently
  yield empty `pub_year`/`pub_month`/`pub_day` in the JSON export. Could parse a
  leading 4-digit year out of `medline_date` to populate `pub_year`.
- **`ELocationID` DOIs are not read.** The exported `identifiers` come from
  `PubmedData/ArticleIdList` only — the same place Babel reads, and the
  authoritative one. A DOI can also appear as
  `Article/ELocationID[@EIdType="doi"]`, normally as a duplicate; parse it as a
  fallback if records ever turn up with the latter but not the former.
- **Grounding is off.** We call `pubmed_downloader`'s parser with `ground=False`
  (no MeSH/ROR/ORCID lookups), so `author_affiliation.ror` etc. are unpopulated.
  The library's `[process]` extra (pyobo/orcid-downloader) could enable grounding
  if needed — at significant dependency + runtime cost.

## Misc

- **MD5 verification** (`download --verify`) recomputes the local digest for every
  file. Since PubMed files are immutable, this rarely catches anything; consider
  defaulting it off for speed on large syncs.
- **DeleteCitation re-add edge case** is handled (delete then later re-add) by the
  `latest_article` view comparing max delete order vs the latest article order, but
  is only covered by synthetic fixtures — worth confirming against real data if it
  ever occurs.
