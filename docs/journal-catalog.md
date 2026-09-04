# Where the journal metadata comes from

`pubmed2db`'s `journal` dimension is built from **two** NLM sources, and the split
is deliberate:

| Column | Source | Why |
| --- | --- | --- |
| `title`, `abbreviation_medline`, `abbreviation_iso` | `J_Entrez.txt` | It **is** PubMed's own rendering — `esummary.fulljournalname` byte for byte. The NLM Catalog cannot reproduce it. |
| `start_year`, `end_year`, `active` | serfile (NLM serial catalog) | `J_Entrez.txt` does not carry them at all. |

This document records the measurements behind that, because the obvious move —
"the NLM Catalog is richer, use it for everything" — is wrong in a way that is
invisible until you compare against PubMed's APIs. It was prompted by
[pubmed-downloader#16](https://github.com/cthoyt/pubmed-downloader/pull/16), where
the maintainer of the library we depend on wrote:

> The file you reference is missing lots of information, which is why the
> implementation in the package gets the information from the SERFILES instead

He is right that `J_Entrez.txt` is thin. The finding here is that the information
it is *missing* is not the information we export, and the two fields we do export
exist in PubMed's form only in `J_Entrez.txt`.

**Populations used throughout:** `J_Entrez.txt` 42,056 journals;
`serfilebase.2026.xml` 150,942 catalog records (449 MB); overlap 41,154.
Measured 2026-09-04. Numbers are full-population unless a paragraph says
otherwise — two findings are samples, and both are flagged.

## The candidate sources

| Source | What it is | Size |
| --- | --- | --- |
| [`J_Entrez.txt`](https://ftp.ncbi.nlm.nih.gov/pubmed/J_Entrez.txt) | NCBI's list of journals in Entrez, `key: value` blocks | 10 MB |
| [serfile](https://ftp.nlm.nih.gov/projects/serfilelease/) | NLM Catalog, serials only, NLMXML | 449 MB baseline + ~1 MB/month |
| [CatfilePlus](https://ftp.nlm.nih.gov/projects/catpluslease/) | NLM Catalog, everything (books too) | 5.08 GB baseline, MARCXML |

serfile and CatfilePlus share the `NLMCatalogRecordSet` DTD and the same
`NlmUniqueID` key, which is also `article.nlm_catalog_id` in our schema, so all
three join cleanly.

## Finding 1 — `J_Entrez.txt` is PubMed's own rendering

On the 8,259 journals (20% of the overlap) whose J_Entrez and serfile titles
differ after normalizing whitespace and the trailing period:

| PubMed API | field | matches J_Entrez | matches serfile |
| --- | --- | --- | --- |
| `esummary` | `fulljournalname` | **27 / 27** | 0 / 27 |
| `efetch` | `<Journal><Title>` | **22 / 23** | 0 / 23 |

> **Caveat — this is a sample**, not the corpus: 50 journals drawn at random from
> the 8,259 disagreeing, one article fetched per journal. The single
> non-J_Entrez case matched *neither* source (`Foot & ankle surgery` — a real
> title change, not a rendering difference).

```
esummary PMID 42564453  fulljournalname = 'Microbial cell (Graz, Austria)'
   J_Entrez = 'Microbial cell (Graz, Austria)'      serfile = 'Microbial cell.'
esummary PMID 42641187  fulljournalname = 'Cirugia y cirujanos'
   J_Entrez = 'Cirugia y cirujanos'                 serfile = 'Cirugía y cirujanos.'
esummary PMID 40808748  fulljournalname = 'Ethos (Berkeley, Calif.)'
   J_Entrez = 'Ethos (Berkeley, Calif.)'            serfile = 'Ethos.'
```

The three disagreement classes, counted over all 41,154 overlapping journals:

- **3,747 diacritics.** NCBI ASCII-folds; the catalog does not.
  serfile `Cirugía y cirujanos.` → PubMed `Cirugia y cirujanos`
- **1,554 corporate suffix.** J_Entrez and PubMed append the collective author.
  `Academy of management review. Academy of Management`
- **~2,958 qualifier disambiguators.** PubMed distinguishes same-named journals.
  `Ethos (Berkeley, Calif.)`, `Methods in molecular biology (Clifton, N.J.)`

The clinching detail is that NCBI's *typos* ride along identically:
`Zeitschrift fur wissenschartliche Zoologie` (for *wissenschaftliche*) appears in
`esummary`, `J_Entrez.txt` and serfile alike. The two files hold the same
underlying string; `J_Entrez.txt` is that string after NCBI's normalization, and
the normalization is exactly what PubMed serves.

## Finding 2 — the catalog holds the Entrez title, but we cannot rebuild it reliably

The disambiguated form is in the record, as
`TitleAlternate[@TitleType="Uniform"]` (5/5 on the qualifier cases above). So a
reconstruction is tempting. The best rule tried — take `Uniform` if present else
`TitleMain`, strip the trailing period, ASCII-fold, optionally append
`AuthorList/Author/CollectiveName` — reaches:

**99.22% (40,834 / 41,154), full population.**

The residual 320 need NCBI's own rules, none of which are in the catalog:

- a transliteration table that plain NFKD folding does not implement —
  `Eksperimentalʹnai︠a︡ onkologii︠a︡.` → `Eksperimental'naia onkologiia`
  (combining double-breve dropped, `ʹ` → `'`)
- truncation at ~250 characters
- punctuation normalization, `" : "` → `": "`

Reimplementing that to chase 0.78% would be fragile against any change NCBI makes.
Taking the field from `J_Entrez.txt` is exact and free.

## Finding 3 — what serfile actually adds

| Field | Verdict | Evidence (full population, overlap of 41,154) |
| --- | --- | --- |
| `PublicationFirstYear` | **take** | present for 41,118 (99.9%) — but see the wildcard note: 96.0% of loaded journals end up with a *parseable* start year |
| `PublicationEndYear` | **take** | present for 40,618 (98.7%); `9999` = ongoing for 27,687 (67.3%) |
| `active` (derived) | **take** | `end == "9999"` → true; absent (536, 1.3%) → NULL |
| ISSNs | skip | filtered to `ValidYN="Y"`: 51,108 vs J_Entrez's 51,128 — **99.3% identical sets** (+143 / −156) |
| `ISSNLinking` | skip | 86% coverage, but `article.issn_linking` already carries it per-article from the XML |
| publisher, country, language, journal MeSH, homepage | skip | nothing consumes them |

So the enrichment is **three columns**. It is worth being blunt about that: an
earlier draft of this analysis claimed serfile added ~4,300 ISSNs, which was an
artifact of counting `ValidYN="N"` records — cancelled and incorrect ISSNs that
NLM records precisely so they can be recognized as wrong. Filter on `ValidYN`
before comparing ISSN sets.

> **Trap.** Roughly 1,600 year values are MARC wildcards — `19uu`, `199u`,
> `uuuu` — and `int()` raises on them. Only `\d{4}` is a year. Pinned by
> `test_catalog_year_rejects_marc_wildcards`. They are also why *presence* and
> *usability* differ: 99.9% of overlapping journals have a `PublicationFirstYear`
> element, but a real load yields **40,384 / 42,056 (96.0%)** non-NULL
> `start_year`. Quote the second number when checking a run.

The 4,763 `ValidYN="N"` ISSNs are the one genuinely new thing we are leaving on
the table. They would be useful as *resolver aliases* (an old citation carrying a
cancelled ISSN could still be matched), but exposing them needs a `valid` column
on `journal_issn` so a consumer cannot mistake them for current. Deferred, not
overlooked.

## Finding 4 — coverage, and why CatfilePlus is not worth it

902 of 42,056 J_Entrez journals (2.1%) have no record in the 2026 **baseline**.
They are conference-proceedings volumes and monographic series — cataloged as
monographs, not serials, so they fall outside serfile's scope by definition.

**The monthly deltas recover 429 of those 902 (47.6%)**, measured on a real load,
leaving 473 (1.1% of the dimension) with NULL years. That is the concrete reason
`_serfile_urls()` fetches the year's updates and not just the baseline — and it
halves whatever CatfilePlus could add.

- **CatfilePlus covers 867 of the 902** (full population, via batched
  `esearch db=nlmcatalog`); the other 35 are in PubMed with no catalog record at
  all. But the deltas already recover 429 of them, so CatfilePlus's real marginal
  gain over what we ship is at most ~438 journals — about 1% of the dimension,
  and only for the year columns.
- **But it does not fix the title question**: CatfilePlus `TitleMain` is the same
  catalog title. Checked directly — `Current protocols in cytometry` and
  `Oral History Association newsletter.` come back in catalog form, and no
  `TitleAlternate` carries the Entrez rendering for them.
- **And it costs 11×**: the 2026 baseline is 5.08 GB across four parts,
  **MARCXML only**. The last NLMXML baseline is 2023, so an NLMXML path means
  `catplusbase*of4.2023.xml` plus ~33 monthly deltas (the monthly
  `catplus.YYYYMMDD.xml` files *are* still NLMXML).
- **And every one of them is already in the dimension** with the correct title,
  from `J_Entrez.txt`, which we keep. The 473 the deltas do not reach lose only
  their publication years.

Article-volume impact, for scale: **~1,100 articles** across all 902, so roughly
**~580** across the 473 that actually end up with NULL years.

> **Caveat — extrapolated.** 60 of the 902 sampled via `esearch "<MedAbbr>"[ta]`
> → 73 articles, scaled to 902 and then prorated to 473. `[ta]` may undercount
> proceedings volumes, so read this as an order of magnitude (~0.001% of 40.9M),
> not a count.

**Conclusion: serfile gives us everything we would take from CatfilePlus, at a
sixth of the download and without a MARCXML parser.** Do not redo this analysis.

## Finding 5 — notes on `pubmed_downloader.catalog` (v0.0.14)

Three reasons `load.py` does its own serfile download and parse rather than
calling upstream, recorded here so the choice is not mistaken for
not-invented-here:

- **`ensure_serfile_catalog()` skips the baseline.** `catalog.py:734-740` passes
  `skip_prefix="serfilebase"`, so it only ever fetches the ~80 monthly
  `serfile.YYYYMMDD.xml` deltas (~0.5–1 MB each, Dec 2019 onward) — records
  *changed* since then, not the catalog. The ~150k records live in the 449 MB
  baseline it excludes.
- **The per-file cache is dead code.** `catalog.py:661` reads
  `if cache_path.is_file() and not force_process and False:` — the `and False`
  means the `.json.gz` it writes is never read back.
- **`process_catalog()` is unusable for us regardless.** It hard-imports `pyobo`
  and `orcid_downloader.lexical` and builds three grounders before parsing
  anything; both are in the `[process]` extra, and grounding is explicitly out of
  scope for this pipeline.

`CatalogRecord` also has no ISO abbreviation field, which is moot for us for a
separate measured reason: in `J_Entrez.txt`, `IsoAbbr` equals `MedAbbr` in
**42,056 / 42,056** records, so `export.py`'s
`COALESCE(j.abbreviation_iso, j.abbreviation_medline, '')` is a distinction
without a difference. `journal.abbreviation_iso` could be dropped — that is a
Parquet schema change for downstream consumers, so it is its own decision.

### Why `process_journal_overview()` is still worth fixing upstream

It cannot return a single record in ≤ 0.0.14: `Journal.start_year` and
`end_year` are annotated `int | None` but given no default, so pydantic makes
them required, and `J_Entrez.txt` has no such key. Every other field the file
cannot supply already defaults (`abbreviation_medline`, `abbreviation_iso`,
`synonyms`, `active`), which is what marks those two as an oversight rather than
a statement that the overview file is the wrong input.

It is tempting to read Finding 1 as "the catalog is the real source, so this
function is a vestige — delete it." The finding says the opposite. `J_Entrez.txt`
is **not** a subset of the catalog: `fulljournalname` and `IsoAbbr` exist nowhere
in `CatalogRecord`, and no reconstruction from the catalog reaches them (Finding
2). So `process_journal_overview()` is the only library path to two fields the
catalog cannot supply; it can *never* supply publication years, because its input
has none; and therefore those years must be optional. That is precisely the
load-then-enrich shape this pipeline uses —
[pubmed-downloader#16](https://github.com/cthoyt/pubmed-downloader/pull/16) is
what would let a library user build a `Journal` from the overview file and fill
the years from the catalog afterwards.

One caveat if it lands and we adopt it: `Journal.active` defaults to `True`,
which is wrong for the 13,012 journals serfile marks ceased, so `active` would
still have to be set from the catalog rather than taken from the model.

## How to redo any of this

Per `AGENTS.md`'s sampling note, none of this needs a PubMed baseline download:

1. `J_Entrez.txt` is 10 MB; parse the `key: value` blocks (`load._parse_journal_overview`).
2. `serfilebase.YYYY.xml` is 449 MB (a full year's downloads land closer to
   **890 MB** — NLM posts occasional bulk re-releases as monthly deltas; the 2026
   May and June files are 169 MB and 249 MB, not the ~1 MB the others are);
   `lxml.etree.iterparse(tag="NLMCatalogRecord")` streams it in **5.3 s at 50 MiB
   peak RSS** (measured, duckdb-free), so hold both in a dict and compare.
3. Compare titles against **`esummary`'s `fulljournalname`**, not `efetch`. Per
   `AGENTS.md`, esummary shows NCBI's own normalization decisions, which is the
   question here; and it batches, so 30 PMIDs cost one request.
4. Normalize whitespace and the trailing period before comparing, or the trailing
   `.` on every `TitleMain` swamps the real differences.

## Keeping the cached catalog fresh

NLM publishes **no `.md5` sidecars** for serfile — checked directly, and there are
none in the directory listing — so `download.py`'s checksum machinery, which
relies on NCBI publishing `<file>.xml.gz.md5`, does not transfer. What the server
does offer is a stable `ETag` (and `Last-Modified`), so each cached file gets an
`.etag` sidecar and a HEAD per run decides whether it moved.

That matters because pystow's `ensure()` skips by file *name* — the hazard
`AGENTS.md` already records for upstream's `ensure()`. Without a validator, a
republished `serfile.YYYYMMDD.xml` would keep its stale bytes indefinitely, and
re-fetching ~890 MB every run to avoid that is obviously out. A failed HEAD
returns `None` and leaves the cached copy alone, so a network blip costs nothing.

## Verifying a real run

```
uv run pubmed2db --data-dir data journals   # ~70 s, ~890 MB of downloads
```

Expected on the 2026 data, and what each number means if it moves:

| Check | Value |
| --- | --- |
| journals loaded | 42,056 — the J_Entrez row count; the catalog must not change it |
| non-NULL `start_year` | 40,384 (96.0%) — the shortfall is MARC wildcards plus the 473 uncovered |
| `active` true / false / NULL | 28,035 / 13,012 / 1,009 |
| `Ethos (Berkeley, Calif.)` (`9877005`) | title keeps the Entrez disambiguator, `start_year` 1973, `active` true |

That last row is the whole point of the two-source split in one assertion: the
title is J_Entrez's, the years are the catalog's.
