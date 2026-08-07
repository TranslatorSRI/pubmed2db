"""Validate a directory of exported NDJSON shards produced by ``export``.

The ``validate`` command answers "does this export make sense?" after a run on
the HPC server, and writes a human-readable, gated JSON report that can be
archived alongside the export. It runs four checks, split into an **offline
phase** (fast, deterministic, no network) and an **online phase** (sampled
Entrez eutils cross-checks):

1. **structure** — every line parses as JSON and matches the exporter's 12-field
   record shape (:data:`pubmed2db.export.JSON_FIELDS`); PMIDs are unique.
2. **coverage** — how much of PubMed we exported, against *two* denominators:
   the live Entrez total (portable) and the local ``latest_article`` count
   (authoritative — a shortfall means the export silently dropped rows). Drift
   vs. a previous report is flagged.
3. **field_validation** — a seeded random sample of records is re-fetched from
   Entrez ``efetch`` and compared field-by-field (fuzzy for the abstract).
4. **deletions** — a sample of PMIDs the database (or a previous report) marks
   dropped are confirmed absent from the export and, via the API, from PubMed.

Every check — passing, failing, skipped or not-applicable — is recorded as a
:class:`Check` via :meth:`Report.record`, and the report's ``errors``,
``warnings`` and ``skipped_checks`` arrays are **projections** of that one list.
That is what lets stdout enumerate what was *verified* rather than only what went
wrong, and it means the arrays cannot drift from the checks they summarise.

:func:`format_summary` renders stdout purely from the report dict, so anything a
human reads there is provably in the archived JSON too. Optional inputs (the
database, a previous report, the network) are used when available and leave their
sections skipped otherwise.
"""

from __future__ import annotations

import difflib
import gzip
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import requests
from lxml import etree

from .export import (
    _MONTH_ABBR,
    ID_PREFIXES,
    JSON_FIELDS,
    _year_from_medline_date,
    pub_date,
    pub_month,
)
from .util import current_rss_gib, eta_str, fmt_duration, peak_rss_gib

logger = logging.getLogger(__name__)

#: NCBI E-utilities base URL.
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

#: Exactly the fields the exporter emits, taken from the exporter's own field
#: list (:data:`pubmed2db.export.JSON_FIELDS`, which the export's ``COPY``
#: projection is built from) so the record shape here cannot drift from what
#: shipped. ``test_expected_fields_matches_spec`` locks the names themselves,
#: since they are an external contract with Node Annotator / ElasticSearch.
EXPECTED_FIELDS = frozenset(JSON_FIELDS)

#: Fields compared strictly against Entrez; a high mismatch rate here is an error.
CORE_FIELDS = ("article_title", "volume", "issue",
               "pub_year", "pub_month", "pub_day", "pub_date")

#: Fields sourced from the NLM Catalog dimension, not the article XML, so a
#: mismatch vs. efetch is informational (the two sources can legitimately differ).
SOFT_FIELDS = ("journal_name", "journal_abbrev")

_ID_RE = re.compile(r"^PMID:(\d+)$")

#: Seasons PubMed uses in ``<Season>`` / ``<MedlineDate>``.
_SEASONS = ("Spring", "Summer", "Autumn", "Fall", "Winter")

#: Valid ``pub_month`` values: empty, a month abbreviation, a season, or a range
#: of two of those ("Sep-Dec", "Jul-Aug"). The export passes approximate months
#: through verbatim (see ``export.normalize_month``), so this can no longer be
#: just the 12 abbreviations — but it stays a closed set, so the odder shapes a
#: ``MedlineDate`` can produce ("Dec-1999 Jan") remain visible as warnings
#: instead of being blessed.
#:
#: Built from ``export._MONTH_ABBR``, not ``calendar.month_abbr``: the latter is
#: ``strftime('%b')`` under ``LC_TIME``, which is exactly what the exporter froze
#: its own tuple to avoid — under a non-English locale every record here would
#: otherwise trip the ``month-format`` warning.
_MONTH_TOKENS = _MONTH_ABBR + _SEASONS
_VALID_MONTHS = frozenset(
    {""} | set(_MONTH_TOKENS) | {f"{a}-{b}" for a in _MONTH_TOKENS for b in _MONTH_TOKENS}
)

#: How many records to include verbatim in an example list before truncating.
_MAX_EXAMPLES = 20

#: efetch accepts many IDs per request; batch to stay well under URL limits.
_EFETCH_BATCH = 20

#: A sampled record whose core fields mismatch above this fraction is an error.
_FIELD_MISMATCH_RATE = 0.20

#: Relative drift in coverage vs. a previous report that trips a warning.
_DRIFT_REL = 0.10

#: Fractional shortfall of export vs. the DB latest count that is an error.
_DB_SHORTFALL_RATE = 0.01

#: Minimum gap between progress log lines while reading the shards, matching the
#: export's cadence: enough points to watch RSS climb, few enough to read.
_PROGRESS_INTERVAL_S = 60.0

# --------------------------------------------------------------------------- #
# Report accumulator
# --------------------------------------------------------------------------- #


#: Check outcomes. ``skip`` and ``n/a`` are deliberately distinct: ``skip`` is an
#: *absence of evidence* the reviewer could remove (pass a flag, go online), and
#: is therefore a to-do list; ``n/a`` means there was nothing to evidence in the
#: first place (no deleted PMIDs to sample, an empty record sample) and needs no
#: action. Collapsing them would put permanent noise in ``skipped_checks``.
PASS, FAIL, WARN, SKIP, NA = "pass", "fail", "warn", "skip", "n/a"


@dataclass
class Check:
    """One named expectation, its verdict, and what was actually observed.

    ``code`` is the stable machine identifier a finding is reported under, and is
    what :attr:`Report.errors` / :attr:`Report.warnings` project. A non-passing
    check with **no** code is display-only: its finding is already carried by
    another check, and re-reporting it would double-count.
    """

    name: str
    section: str
    expectation: str
    status: str
    observed: str = ""
    code: str | None = None
    message: str = ""
    count: int = 0
    see: str | None = None
    detail: dict = field(default_factory=dict)

    def as_finding(self) -> dict:
        return {
            "code": self.code, "message": self.message,
            "count": self.count, "see": self.see,
        }

    def as_dict(self) -> dict:
        return {
            "name": self.name, "section": self.section,
            "expectation": self.expectation, "status": self.status,
            "observed": self.observed, "code": self.code,
            "count": self.count, "see": self.see, "detail": self.detail,
        }


@dataclass
class Report:
    """Accumulates named checks, and renders the JSON report.

    Every check — passing or not — goes through :meth:`record`, so the report can
    enumerate what was *verified*, not just what went wrong. ``errors``,
    ``warnings`` and ``skipped_checks`` are **projections** of that one list
    rather than separately maintained arrays, so they cannot disagree with it.
    """

    checks_run: list[Check] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def record(
        self,
        name: str,
        section: str,
        expectation: str,
        status: str,
        observed: str = "",
        *,
        code: str | None = None,
        message: str = "",
        count: int = 0,
        see: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self.checks_run.append(
            Check(
                name=name, section=section, expectation=expectation, status=status,
                observed=observed, code=code, message=message, count=count, see=see,
                detail=detail or {},
            )
        )

    def _findings(self, status: str) -> list[dict]:
        return [c.as_finding() for c in self.checks_run if c.status == status and c.code]

    @property
    def errors(self) -> list[dict]:
        return self._findings(FAIL)

    @property
    def warnings(self) -> list[dict]:
        return self._findings(WARN)

    @property
    def skipped_checks(self) -> list[str]:
        return [
            f"{c.section}.{c.name} ({c.observed})"
            for c in self.checks_run
            if c.status == SKIP
        ]

    @property
    def status(self) -> str:
        if self.errors:
            return FAIL
        if self.warnings:
            return WARN
        return PASS


def _capped(items: list) -> list:
    """Cap an example list so the report stays readable on pathological inputs."""
    return items[:_MAX_EXAMPLES]


# --------------------------------------------------------------------------- #
# Reading shards
# --------------------------------------------------------------------------- #


def find_shards(export_dir: Path) -> list[Path]:
    """Return the exported NDJSON shards in a directory, sorted by name."""
    return sorted(
        p for p in export_dir.iterdir() if p.name.endswith((".ndjson", ".ndjson.gz"))
    )


def _open_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _normalize(value: str) -> str:
    """Collapse whitespace for tolerant field comparison."""
    return " ".join(value.split())


# --------------------------------------------------------------------------- #
# PMID manifest sidecar
# --------------------------------------------------------------------------- #


def write_manifest(pmids: set[int], out_path: Path) -> None:
    """Write a sorted, gzipped PMID manifest — one decimal PMID per line.

    The report deliberately stores counts rather than millions of PMIDs, so this
    sidecar is what makes a *set* comparison between two exports possible. It is
    written from the set the structure check already built, so it costs one sort
    and one write rather than another pass over the shards.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8") as handle:
        handle.writelines(f"{pmid}\n" for pmid in sorted(pmids))


def read_manifest(path: Path) -> set[int]:
    """Read a PMID manifest written by :func:`write_manifest`.

    Accepts gzipped or plain text, and tolerates blank lines so a hand-edited
    manifest still loads.
    """
    with _open_text(path) as handle:
        return {int(line) for line in handle if line.strip()}


# --------------------------------------------------------------------------- #
# Check 1: structure
# --------------------------------------------------------------------------- #


@dataclass
class StructureResult:
    records_total: int = 0
    records_by_shard: dict[str, int] = field(default_factory=dict)
    all_pmids: set[int] = field(default_factory=set)
    #: pmid -> exported document, for the seeded random sample only.
    sample: dict[int, dict] = field(default_factory=dict)


def check_structure(
    shards: list[Path], report: Report, *, sample_size: int, seed: int
) -> StructureResult:
    """Parse and structurally validate every record; reservoir-sample per shard.

    Uses per-shard reservoir sampling so we hold only ``sample_size`` documents
    per shard in memory rather than the whole export, while still collecting the
    full set of PMIDs (needed for duplicate detection and the deletion check).
    """
    result = StructureResult()
    malformed: list[dict] = []
    missing_fields: dict[str, int] = {}
    extra_fields: dict[str, int] = {}
    null_values: list[dict] = []
    invalid_ids: list[dict] = []
    invalid_months: list[dict] = []
    duplicates: dict[int, int] = {}

    # Progress is measured in *bytes of shard consumed*, the one denominator we
    # know before reading: the record total is what this pass is computing, and
    # counting shards alone would report nothing at all for the common
    # single-shard export. Hence the raw handle below — `raw.tell()` is the
    # compressed offset for a .gz shard, which is the scale `st_size` is in.
    total_bytes = sum(path.stat().st_size for path in shards)
    bytes_done = 0
    start = time.monotonic()
    last_log = start

    for shard_index, path in enumerate(shards):
        rng = random.Random(seed + shard_index)
        reservoir: list[tuple[int, dict]] = []
        seen_in_shard = 0

        with path.open("rb") as raw:
            handle = (
                gzip.open(raw, "rt", encoding="utf-8") if path.name.endswith(".gz")
                else io.TextIOWrapper(raw, encoding="utf-8")
            )
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError as exc:
                    malformed.append({"shard": path.name, "line": lineno, "error": str(exc)})
                    continue

                result.records_total += 1
                result.records_by_shard[path.name] = (
                    result.records_by_shard.get(path.name, 0) + 1
                )

                keys = set(doc)
                for missing in EXPECTED_FIELDS - keys:
                    missing_fields[missing] = missing_fields.get(missing, 0) + 1
                for extra in keys - EXPECTED_FIELDS:
                    extra_fields[extra] = extra_fields.get(extra, 0) + 1
                for key, value in doc.items():
                    if value is None:
                        null_values.append({"shard": path.name, "line": lineno, "field": key})

                match = _ID_RE.match(str(doc.get("id", "")))
                if match is None:
                    invalid_ids.append(
                        {"shard": path.name, "line": lineno, "id": doc.get("id")}
                    )
                    pmid = None
                else:
                    pmid = int(match.group(1))
                    if pmid in result.all_pmids:
                        duplicates[pmid] = duplicates.get(pmid, 1) + 1
                    result.all_pmids.add(pmid)

                if doc.get("pub_month") not in _VALID_MONTHS:
                    invalid_months.append(
                        {"shard": path.name, "line": lineno, "pub_month": doc.get("pub_month")}
                    )

                # Reservoir sampling (Algorithm R), keyed by valid PMID.
                if pmid is not None:
                    if len(reservoir) < sample_size:
                        reservoir.append((pmid, doc))
                    else:
                        j = rng.randint(0, seen_in_shard)
                        if j < sample_size:
                            reservoir[j] = (pmid, doc)
                    seen_in_shard += 1

                now = time.monotonic()
                if now - last_log >= _PROGRESS_INTERVAL_S and total_bytes:
                    elapsed = now - start
                    read = bytes_done + raw.tell()
                    current = current_rss_gib()
                    logger.info(
                        "progress: %s record(s), shard %d/%d, %.1f%% of %.1f GiB "
                        "read · elapsed %s · RSS %s · ~%s remaining",
                        f"{result.records_total:,}", shard_index + 1, len(shards),
                        100 * read / total_bytes, total_bytes / 1024**3,
                        fmt_duration(elapsed),
                        "n/a" if current is None else f"{current:.1f} GiB",
                        eta_str(elapsed, read, total_bytes - read),
                    )
                    last_log = now

        bytes_done += path.stat().st_size
        for pmid, doc in reservoir:
            result.sample[pmid] = doc

    result_dict = {
        "records_total": result.records_total,
        "records_by_shard": result.records_by_shard,
        "malformed": _capped(malformed),
        "missing_fields": missing_fields,
        "extra_fields": extra_fields,
        "null_values": _capped(null_values),
        "invalid_ids": _capped(invalid_ids),
        "invalid_months": _capped(invalid_months),
        "duplicate_pmids": _capped(sorted(duplicates)),
    }
    report.checks["structure"] = result_dict

    report.record(
        "records-present", "structure", "the export contains at least one record",
        FAIL if result.records_total == 0 else PASS,
        f"{result.records_total:,} record(s)",
        code="no_records", message="No records found in the export directory.",
        count=0, see="checks.structure",
    )

    #: (name, expectation, noun, findings, key in result_dict, code, message, severity)
    rows = (
        ("json-parse", "every line parses as JSON", "malformed line(s)",
         malformed, "malformed", "malformed_json",
         "Lines that could not be parsed as JSON.", FAIL),
        ("record-fields", "every record has all the required fields",
         "record(s) missing a field", missing_fields, "missing_fields", "missing_fields",
         "Records missing one or more required fields.", FAIL),
        ("no-nulls", 'absent values are "" and never null', "null field value(s)",
         null_values, "null_values", "null_values",
         "Fields with a null value (export should use empty strings).", FAIL),
        ("id-format", "every id looks like PMID:<digits>", "invalid id(s)",
         invalid_ids, "invalid_ids", "invalid_ids",
         "Records whose id is not of the form PMID:<digits>.", FAIL),
        ("pmid-unique", "no PMID is exported twice", "duplicate PMID(s)",
         duplicates, "duplicate_pmids", "duplicate_pmids",
         "PMIDs appearing in more than one record.", FAIL),
        ("no-extra-fields", "no record carries an unexpected field",
         "record(s) with extra fields", extra_fields, "extra_fields", "extra_fields",
         "Records carrying unexpected extra fields.", WARN),
        ("month-format", 'pub_month is a month, season, range of those, or ""',
         "invalid pub_month value(s)", invalid_months, "invalid_months", "invalid_months",
         "Records whose pub_month is not a month/season, a range of two, or empty.", WARN),
    )
    for name, expectation, noun, findings, key, code, message, severity in rows:
        # dict findings (field -> n) count occurrences; list findings count rows.
        count = sum(findings.values()) if isinstance(findings, dict) else len(findings)
        report.record(
            name, "structure", expectation,
            severity if findings else PASS, f"{count:,} {noun}",
            code=code, message=message, count=count,
            see=f"checks.structure.{key}",
        )
    return result


# --------------------------------------------------------------------------- #
# Entrez client
# --------------------------------------------------------------------------- #


class _RateLimiter:
    """Space out eutils requests: 3/s without a key, 10/s with one."""

    def __init__(self) -> None:
        self._next = 0.0

    def wait(self, *, has_key: bool) -> None:
        interval = 0.1 if has_key else 0.34
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(now, self._next) + interval


_RATE = _RateLimiter()

#: Status codes worth retrying (NCBI returns 429 when rate limits are exceeded).
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _eutils(
    endpoint: str,
    params: dict,
    *,
    api_key: str | None,
    email: str | None,
    timeout: float = 60.0,
    retries: int = 3,
) -> bytes:
    """GET an E-utilities endpoint, with rate limiting and retry.

    This is the single seam every network call goes through, so tests can
    ``monkeypatch`` it with canned responses.
    """
    query = dict(params)
    query.setdefault("tool", "pubmed2db")
    if api_key:
        query["api_key"] = api_key
    if email:
        query["email"] = email
    url = f"{EUTILS_BASE}/{endpoint}"

    last_exc: Exception | None = None
    for attempt in range(retries):
        _RATE.wait(has_key=bool(api_key))
        try:
            resp = requests.get(url, params=query, timeout=timeout)
            if resp.status_code in _RETRY_STATUS:
                raise requests.HTTPError(f"{resp.status_code} from {endpoint}")
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("eutils %s attempt %d failed: %s", endpoint, attempt + 1, exc)
            time.sleep(min(2**attempt, 10))
    raise RuntimeError(f"eutils {endpoint} failed after {retries} attempts") from last_exc


def entrez_total(*, api_key: str | None, email: str | None) -> int:
    """Total number of records in PubMed, via einfo."""
    raw = _eutils(
        "einfo.fcgi", {"db": "pubmed", "retmode": "json", "version": "2.0"},
        api_key=api_key, email=email,
    )
    dbinfo = json.loads(raw)["einforesult"]["dbinfo"]
    if isinstance(dbinfo, list):  # version 2.0 wraps dbinfo in a list
        dbinfo = dbinfo[0]
    return int(dbinfo["count"])


_XML_PARSER = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, recover=True)


def _text(element, path: str) -> str:
    node = element.find(path)
    if node is None:
        return ""
    return _normalize("".join(node.itertext()))


def _identifiers(article, pmid: int) -> list[str]:
    """Rebuild the exporter's ``identifiers`` CURIEs from an efetch record.

    Reads the same ``ArticleIdList`` the loader does and applies the same
    :data:`pubmed2db.export.ID_PREFIXES` casing, so a difference between this
    and the export is a real difference in the data, not in the formatting.
    """
    curies = [f"PMID:{pmid}"]
    for node in article.findall("PubmedData/ArticleIdList/ArticleId"):
        prefix = ID_PREFIXES.get(node.get("IdType", ""))
        if prefix and node.text and node.text.strip():
            curies.append(f"{prefix}:{node.text.strip()}")
    return curies


def efetch_documents(
    pmids: list[int], *, api_key: str | None, email: str | None
) -> dict[int, dict]:
    """Fetch and normalize PubMed records for ``pmids`` via efetch.

    Returns ``{pmid: document}`` using the same field semantics the exporter
    applies, so results compare directly to exported records. PMIDs absent from
    the response (deleted or non-existent) are simply missing from the result.
    """
    docs: dict[int, dict] = {}
    for start in range(0, len(pmids), _EFETCH_BATCH):
        batch = pmids[start : start + _EFETCH_BATCH]
        raw = _eutils(
            "efetch.fcgi",
            {"db": "pubmed", "id": ",".join(str(p) for p in batch), "retmode": "xml"},
            api_key=api_key, email=email,
        )
        root = etree.fromstring(raw, parser=_XML_PARSER)
        if root is None:
            continue
        for art in root.findall("PubmedArticle"):
            pmid_node = art.find("MedlineCitation/PMID")
            if pmid_node is None or not (pmid_node.text or "").strip().isdigit():
                continue
            pmid = int(pmid_node.text.strip())
            article = art.find("MedlineCitation/Article")
            if article is None:
                continue
            journal = article.find("Journal/JournalIssue")
            pub = article.find("Journal/JournalIssue/PubDate")
            # efetch renders a season/range record as <Year>+<Season> where the
            # archival XML has a bare <MedlineDate>, but it does not always do
            # so. Apply the exporter's own recovery to whichever form comes
            # back, or every such record reads as a mismatch against an export
            # that did recover the year.
            pub_year = _text(pub, "Year") if pub is not None else ""
            if not pub_year and pub is not None:
                pub_year = _year_from_medline_date(_text(pub, "MedlineDate"))
            abstract = " ".join(
                _normalize("".join(node.itertext()))
                for node in article.findall("Abstract/AbstractText")
            )
            docs[pmid] = {
                "id": f"PMID:{pmid}",
                # `art` (the PubmedArticle root), not `article`: ArticleIdList
                # lives under PubmedData, a sibling of MedlineCitation.
                "identifiers": _identifiers(art, pmid),
                "journal_name": _text(article, "Journal/Title"),
                "journal_abbrev": _text(article, "Journal/ISOAbbreviation"),
                "article_title": _text(article, "ArticleTitle"),
                "volume": _text(journal, "Volume") if journal is not None else "",
                "issue": _text(journal, "Issue") if journal is not None else "",
                "pub_year": pub_year,
                # Same reasoning as pub_year above, for the month: efetch may
                # return <Month>, <Season> or a bare <MedlineDate>, and the
                # export recovers a month from all three. Route efetch's side
                # through the exporter's own function or every approximate-date
                # record reads as a mismatch.
                "pub_month": pub_month(
                    (pub.findtext("Month") or pub.findtext("Season"))
                    if pub is not None else None,
                    _text(pub, "MedlineDate") if pub is not None else None,
                ),
                "pub_day": _text(pub, "Day") if pub is not None else "",
                # Assembled from whichever rendering came back, by the exporter's
                # own function. This is the strongest of the date comparisons:
                # efetch's <Year>+<Season> and the baseline's bare <MedlineDate>
                # describe the same record, so both sides must land on one string.
                "pub_date": pub_date(
                    _text(pub, "Year"),
                    (pub.findtext("Month") or pub.findtext("Season")),
                    _text(pub, "Day"),
                    _text(pub, "MedlineDate"),
                ) if pub is not None else "",
                "abstract": _normalize(abstract),
            }
    return docs


# --------------------------------------------------------------------------- #
# Check 2: coverage
# --------------------------------------------------------------------------- #


def check_coverage(
    report: Report,
    *,
    exported_count: int,
    con: duckdb.DuckDBPyConnection | None,
    online: bool,
    api_key: str | None,
    email: str | None,
    previous: dict | None,
    entrez_low: float,
    entrez_high: float,
) -> None:
    """Compare the exported record count to Entrez and the local DB.

    The default ``entrez_low``/``entrez_high`` band is calibrated against a real
    full-corpus run: the 2026-07-30 export held 40,901,984 documents against an
    Entrez total of 40,944,369, a ratio of 0.9990. The ±5% band therefore leaves
    room for the Entrez total growing between export and validation (PubMed adds
    roughly 4% a year) while still catching a materially short export. A partial
    export — anything downloaded with ``--limit`` — is legitimately far below the
    band and needs ``--entrez-low`` widened.
    """
    coverage: dict = {
        "exported_count": exported_count,
        "entrez_total": None,
        "pct_of_entrez": None,
        "db_latest_count": None,
        "pct_of_db": None,
        "previous": None,
    }

    band = f"within [{entrez_low:.0%}, {entrez_high:.0%}] of the live PubMed total"
    if online:
        try:
            total = entrez_total(api_key=api_key, email=email)
            coverage["entrez_total"] = total
            pct = exported_count / total if total else None
            coverage["pct_of_entrez"] = pct
            out_of_band = pct is not None and not (entrez_low <= pct <= entrez_high)
            report.record(
                "vs-entrez", "coverage", band,
                WARN if out_of_band else PASS,
                f"{exported_count:,} of {total:,}"
                + (f" ({pct:.3%})" if pct is not None else ""),
                code="coverage_out_of_band",
                message=(
                    f"Exported {pct:.3%} of PubMed, outside the expected "
                    f"[{entrez_low:.2%}, {entrez_high:.2%}] band."
                    if pct is not None else ""
                ),
                count=1, see="checks.coverage",
            )
        except Exception as exc:  # network/parse failure degrades to a warning
            logger.warning("could not fetch Entrez total: %s", exc)
            report.record(
                "vs-entrez", "coverage", band, WARN, f"Entrez unreachable: {exc}",
                code="entrez_unreachable",
                message=f"Could not fetch the Entrez total: {exc}",
                count=1, see="checks.coverage",
            )
    else:
        report.record("vs-entrez", "coverage", band, SKIP, "offline")

    expect_db = "matches the database's latest_article count"
    if con is not None:
        db_latest = con.execute("SELECT count(*) FROM latest_article").fetchone()[0]
        coverage["db_latest_count"] = db_latest
        coverage["pct_of_db"] = exported_count / db_latest if db_latest else None
        observed = f"{exported_count:,} of {db_latest:,}"
        if not db_latest:
            report.record("vs-database", "coverage", expect_db, NA,
                          "the database holds no current articles")
        else:
            shortfall = (db_latest - exported_count) / db_latest
            if shortfall > _DB_SHORTFALL_RATE:
                report.record(
                    "vs-database", "coverage", expect_db, FAIL,
                    f"{observed} ({shortfall:.2%} short)",
                    code="export_shortfall",
                    message=(
                        f"Export has {exported_count} records vs. {db_latest} in the "
                        f"database ({shortfall:.2%} short) — rows were dropped."
                    ),
                    count=db_latest - exported_count, see="checks.coverage",
                )
            elif exported_count != db_latest:
                report.record(
                    "vs-database", "coverage", expect_db, WARN,
                    f"{observed} (differs by {abs(db_latest - exported_count):,})",
                    code="export_db_mismatch",
                    message=(
                        f"Export has {exported_count} records vs. {db_latest} in the "
                        "database."
                    ),
                    count=abs(db_latest - exported_count), see="checks.coverage",
                )
            else:
                report.record("vs-database", "coverage", expect_db, PASS,
                              f"exact match ({exported_count:,})")
    else:
        report.record("vs-database", "coverage", expect_db, SKIP, "no database available")

    expect_prev = f"coverage within {_DRIFT_REL:.0%} of the previous report's"
    if previous is not None:
        prev_cov = previous.get("checks", {}).get("coverage", {})
        coverage["previous"] = {
            "exported_count": prev_cov.get("exported_count"),
            "pct_of_entrez": prev_cov.get("pct_of_entrez"),
        }
        prev_pct = prev_cov.get("pct_of_entrez")
        cur_pct = coverage["pct_of_entrez"]
        if not (prev_pct and cur_pct):
            report.record("vs-previous", "coverage", expect_prev, NA,
                          "no comparable coverage figure in the previous report")
        else:
            drifted = abs(cur_pct - prev_pct) / prev_pct > _DRIFT_REL
            report.record(
                "vs-previous", "coverage", expect_prev, WARN if drifted else PASS,
                f"{prev_pct:.3%} then, {cur_pct:.3%} now",
                code="coverage_drift",
                message=(
                    f"Coverage moved from {prev_pct:.3%} to {cur_pct:.3%} vs. the "
                    "previous report."
                ),
                count=1, see="checks.coverage.previous",
            )
    else:
        report.record("vs-previous", "coverage", expect_prev, SKIP,
                      "no --previous-report")

    report.checks["coverage"] = coverage


# --------------------------------------------------------------------------- #
# Check 3: field validation
# --------------------------------------------------------------------------- #


#: How a single field disagreement failed, worst first. The distinction that
#: matters downstream is ``values_differ`` (we exported something *wrong*) versus
#: everything else (we exported *nothing* where PubMed has something) — the first
#: is a correctness bug, the rest are completeness gaps.
MISMATCH_KINDS = ("values_differ", "low_similarity", "entrez_blank", "exported_blank")


def _mismatch_kind(mismatch: dict) -> str:
    if "similarity" in mismatch:
        return "low_similarity"
    exported, entrez = mismatch.get("exported"), mismatch.get("entrez")
    if not exported and entrez:
        return "exported_blank"
    if exported and not entrez:
        return "entrez_blank"
    return "values_differ"


def group_mismatches(mismatches: list[dict]) -> dict:
    """Tally field mismatches by field and by *kind*.

    The kind tally is what turns "20 records disagreed" into a decision: all
    ``exported_blank`` means the export shipped blanks where PubMed has values —
    incomplete, but nothing downstream will read a wrong value. Any
    ``values_differ`` means the opposite, and needs looking at.
    """
    by_field: dict[str, int] = {}
    by_kind: dict[str, int] = {kind: 0 for kind in MISMATCH_KINDS}
    examples: dict[str, dict] = {}
    for mismatch in mismatches:
        name = mismatch.get("field", "?")
        by_field[name] = by_field.get(name, 0) + 1
        kind = _mismatch_kind(mismatch)
        by_kind[kind] += 1
        examples.setdefault(kind, mismatch)
    return {
        "by_field": dict(sorted(by_field.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_kind": by_kind,
        # One worked example per kind, so a reader (or an LLM) can see the shape
        # of the disagreement without paging through the capped list.
        "examples": {kind: examples[kind] for kind in MISMATCH_KINDS if kind in examples},
    }


def check_fields(
    report: Report,
    sample: dict[int, dict],
    *,
    online: bool,
    api_key: str | None,
    email: str | None,
    abstract_threshold: float,
) -> None:
    """Compare a sample of exported records to Entrez efetch."""
    expectations = {
        "sample-fetched": "PubMed still serves every sampled record",
        "core-fields": f"<{_FIELD_MISMATCH_RATE:.0%} of compared fields differ from Entrez",
        "abstract": f"abstracts at least {abstract_threshold:.0%} similar to Entrez",
        "journal-soft": "journal name/abbrev match Entrez (advisory)",
    }
    if not online:
        for name, expectation in expectations.items():
            report.record(name, "field accuracy", expectation, SKIP, "offline")
        report.checks["field_validation"] = None
        return
    if not sample:
        for name, expectation in expectations.items():
            report.record(name, "field accuracy", expectation, NA, "no records sampled")
        report.checks["field_validation"] = {"sampled": 0, "checked": 0}
        return

    pmids = sorted(sample)
    fetched = efetch_documents(pmids, api_key=api_key, email=email)

    mismatches: list[dict] = []
    soft_mismatches: list[dict] = []
    missing_from_api: list[int] = []
    similarities: list[float] = []
    checked = 0
    core_comparisons = 0
    core_mismatch = 0

    for pmid in pmids:
        exported = sample[pmid]
        entrez = fetched.get(pmid)
        if entrez is None:
            missing_from_api.append(pmid)
            continue
        checked += 1

        for f in CORE_FIELDS:
            core_comparisons += 1
            if _normalize(str(exported.get(f, ""))) != entrez.get(f, ""):
                core_mismatch += 1
                mismatches.append(
                    {"pmid": pmid, "field": f, "exported": exported.get(f), "entrez": entrez.get(f)}
                )

        # `identifiers` is the one list-valued field, so it can't go through the
        # string comparison above. Order is irrelevant; membership is not. Note
        # this compares our newest *loaded* version against live PubMed, so a
        # DOI assigned after our last update file reads as a mismatch — the same
        # exposure the other core fields carry, absorbed by the rate threshold.
        core_comparisons += 1
        if set(exported.get("identifiers") or []) != set(entrez.get("identifiers") or []):
            core_mismatch += 1
            mismatches.append({
                "pmid": pmid, "field": "identifiers",
                "exported": exported.get("identifiers"),
                "entrez": entrez.get("identifiers"),
            })

        exp_abs = _normalize(str(exported.get("abstract", "")))
        ent_abs = entrez.get("abstract", "")
        if exp_abs or ent_abs:
            ratio = difflib.SequenceMatcher(None, exp_abs, ent_abs).ratio()
            similarities.append(ratio)
            core_comparisons += 1
            if ratio < abstract_threshold:
                core_mismatch += 1
                mismatches.append(
                    {"pmid": pmid, "field": "abstract", "similarity": round(ratio, 3)}
                )

        for f in SOFT_FIELDS:
            if _normalize(str(exported.get(f, ""))) != entrez.get(f, ""):
                soft_mismatches.append(
                    {"pmid": pmid, "field": f, "exported": exported.get(f), "entrez": entrez.get(f)}
                )

    rate = core_mismatch / core_comparisons if core_comparisons else 0.0
    grouped = group_mismatches(mismatches)
    min_similarity = round(min(similarities), 3) if similarities else None
    report.checks["field_validation"] = {
        "sampled": len(pmids),
        "checked": checked,
        # The rate's denominator, so a reader can audit it rather than infer it.
        "core_comparisons": core_comparisons,
        "core_mismatches": core_mismatch,
        "core_mismatch_rate": round(rate, 4),
        "mismatches": _capped(mismatches),
        "mismatches_by_field": grouped["by_field"],
        "mismatches_by_kind": grouped["by_kind"],
        "soft_mismatches": _capped(soft_mismatches),
        "missing_from_api": _capped(missing_from_api),
        "abstract_similarity": {
            "min": min_similarity,
            "mean": round(sum(similarities) / len(similarities), 3) if similarities else None,
        },
    }

    report.record(
        "sample-fetched", "field accuracy", expectations["sample-fetched"],
        FAIL if missing_from_api else PASS,
        f"{checked:,} of {len(pmids):,} sampled record(s) returned",
        code="sampled_pmid_absent",
        message="Sampled PMIDs present in the export but not returned by PubMed.",
        count=len(missing_from_api), see="checks.field_validation.missing_from_api",
    )

    observed = f"{core_mismatch:,} of {core_comparisons:,} comparison(s) differ ({rate:.2%})"
    if rate > _FIELD_MISMATCH_RATE:
        report.record(
            "core-fields", "field accuracy", expectations["core-fields"], FAIL, observed,
            code="field_mismatch_rate",
            message=f"{rate:.1%} of sampled core-field comparisons disagreed with Entrez.",
            count=core_mismatch, see="checks.field_validation.mismatches",
            detail=grouped,
        )
    else:
        report.record(
            "core-fields", "field accuracy", expectations["core-fields"],
            WARN if core_mismatch else PASS, observed,
            code="field_mismatches" if core_mismatch else None,
            message="Some sampled records disagreed with Entrez.",
            count=core_mismatch, see="checks.field_validation.mismatches",
            detail=grouped,
        )

    # No code: a low-similarity abstract is already counted in `core-fields`
    # above, so reporting it again would double-count the same finding.
    if min_similarity is None:
        report.record("abstract", "field accuracy", expectations["abstract"], NA,
                      "no sampled record had an abstract on either side")
    else:
        mean = sum(similarities) / len(similarities)
        report.record(
            "abstract", "field accuracy", expectations["abstract"],
            PASS if min_similarity >= abstract_threshold else WARN,
            f"min {min_similarity:.3f}, mean {mean:.3f} over {len(similarities):,} record(s)",
        )

    report.record(
        "journal-soft", "field accuracy", expectations["journal-soft"],
        WARN if soft_mismatches else PASS,
        f"{len(soft_mismatches):,} of {checked:,} record(s) differ",
        code="journal_mismatches",
        message="Sampled journal name/abbrev differs from Entrez (different source).",
        count=len(soft_mismatches), see="checks.field_validation.soft_mismatches",
    )


# --------------------------------------------------------------------------- #
# Check 4: deletions
# --------------------------------------------------------------------------- #


def check_deletions(
    report: Report,
    *,
    all_pmids: set[int],
    con: duckdb.DuckDBPyConnection | None,
    online: bool,
    api_key: str | None,
    email: str | None,
    drop_sample: int,
    seed: int,
) -> None:
    """Confirm that dropped PMIDs are absent from the export and from PubMed.

    The authoritative list of drops is the database's ``deleted_pmid`` table
    (restricted to PMIDs no later version reinstated). Without a database there
    is nothing reliable to enumerate, so the check is skipped — the field
    validation still flags any sampled record PubMed no longer serves.
    """
    rng = random.Random(seed)
    expect_absent = "DB-deleted PMIDs are absent from the export"
    expect_gone = "PubMed no longer serves those PMIDs either"
    if con is None:
        report.record("deleted-absent", "deletions", expect_absent, SKIP,
                      "no database available")
        report.record("deleted-gone", "deletions", expect_gone, SKIP,
                      "no database available")
        report.checks["deletions"] = {"source": None}
        return

    rows = con.execute(
        """
        SELECT DISTINCT d.pmid FROM deleted_pmid d
        WHERE NOT EXISTS (SELECT 1 FROM latest_article la WHERE la.pmid = d.pmid)
        """
    ).fetchall()
    pool = [r[0] for r in rows]
    if not pool:
        # Nothing to evidence rather than evidence withheld: the database simply
        # records no deletion that a later version did not reinstate.
        for name, expectation in (("deleted-absent", expect_absent),
                                  ("deleted-gone", expect_gone)):
            report.record(name, "deletions", expectation, NA,
                          "the database records no un-reinstated deletions")
        report.checks["deletions"] = {"source": "database", "candidates_checked": 0}
        return
    source = "database"
    candidates = rng.sample(pool, min(drop_sample, len(pool)))

    present_in_export = [p for p in candidates if p in all_pmids]

    confirmed_deleted: list[int] = []
    still_live: list[int] = []
    unknown: list[int] = []
    if online:
        try:
            fetched = efetch_documents(candidates, api_key=api_key, email=email)
            for pmid in candidates:
                (still_live if pmid in fetched else confirmed_deleted).append(pmid)
            report.record(
                "deleted-gone", "deletions", expect_gone,
                WARN if still_live else PASS,
                f"{len(confirmed_deleted):,} of {len(candidates):,} confirmed gone",
                code="deleted_pmid_still_live",
                message=(
                    "PMIDs marked dropped are still served by PubMed (possible "
                    "merge) — review manually."
                ),
                count=len(still_live), see="checks.deletions.still_live",
            )
        except Exception as exc:
            logger.warning("could not confirm deletions via API: %s", exc)
            unknown = list(candidates)
            report.record(
                "deleted-gone", "deletions", expect_gone, WARN,
                f"PubMed unreachable: {exc}",
                code="deletion_check_unreachable",
                message=f"Could not confirm dropped PMIDs via the API: {exc}",
                count=len(candidates), see="checks.deletions",
            )
    else:
        unknown = list(candidates)
        report.record("deleted-gone", "deletions", expect_gone, SKIP, "offline")

    report.checks["deletions"] = {
        "source": source,
        "candidates_checked": len(candidates),
        "present_in_export": _capped(present_in_export),
        "confirmed_deleted": _capped(confirmed_deleted),
        "still_live": _capped(still_live),
        "unknown": _capped(unknown),
    }

    report.record(
        "deleted-absent", "deletions", expect_absent,
        FAIL if present_in_export else PASS,
        f"{len(present_in_export):,} of {len(candidates):,} sampled still in the export",
        code="deleted_pmid_exported",
        message="PMIDs the database marks deleted still appear in the export.",
        count=len(present_in_export), see="checks.deletions.present_in_export",
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def check_drops_since(
    report: Report,
    *,
    all_pmids: set[int],
    previous_manifest: Path | None,
    con: duckdb.DuckDBPyConnection | None,
) -> None:
    """Diff this export's PMID set against a previous export's manifest.

    This is the check the coverage counts cannot make: two exports can hold the
    same number of records while a thousand PMIDs silently swapped out. A PMID
    the previous export shipped and this one does not is only legitimate if the
    database recorded a ``DeleteCitation`` for it, so an *unexplained* drop is an
    error — it means records vanished without PubMed asking for their removal.

    Without a database the drops cannot be attributed, so they are reported as a
    warning to review rather than silently accepted.
    """
    expectation = "no PMID vanished without a recorded deletion"
    if previous_manifest is None:
        report.record("pmid-set-diff", "drops since previous", expectation, SKIP,
                      "no --previous-manifest")
        return

    previous_pmids = read_manifest(previous_manifest)
    dropped = previous_pmids - all_pmids
    added = all_pmids - previous_pmids

    explained: list[int] = []
    unexplained = sorted(dropped)
    if dropped and con is not None:
        # Ask the DB which of the dropped PMIDs it actually marked deleted. Passed
        # as an Arrow-friendly temp view rather than a giant IN list.
        rows = con.execute(
            """
            SELECT DISTINCT d.pmid FROM deleted_pmid d
            WHERE d.pmid IN (SELECT * FROM UNNEST(?))
            """,
            [sorted(dropped)],
        ).fetchall()
        explained = sorted(r[0] for r in rows)
        unexplained = sorted(dropped - set(explained))

    report.checks["drops_since_previous"] = {
        "previous_manifest": str(previous_manifest),
        "previous_count": len(previous_pmids),
        "current_count": len(all_pmids),
        "dropped": len(dropped),
        "added": len(added),
        "explained_by_deletion": _capped(explained),
        "explained_by_deletion_count": len(explained),
        "unexplained": _capped(unexplained),
        "unexplained_count": len(unexplained),
        "added_examples": _capped(sorted(added)),
    }

    observed = (
        f"{len(dropped):,} dropped ({len(explained):,} explained by a recorded "
        f"deletion), {len(added):,} added"
    )
    if unexplained and con is not None:
        report.record(
            "pmid-set-diff", "drops since previous", expectation, FAIL, observed,
            code="unexplained_drops",
            message=(
                "PMIDs present in the previous export are missing from this one "
                "without a recorded deletion."
            ),
            count=len(unexplained), see="checks.drops_since_previous.unexplained",
        )
    elif unexplained:
        report.record(
            "pmid-set-diff", "drops since previous", expectation, WARN,
            f"{observed}; no database to attribute them",
            code="drops_unattributed",
            message=(
                "PMIDs present in the previous export are missing from this one; "
                "no database was available to confirm they were deleted."
            ),
            count=len(unexplained), see="checks.drops_since_previous.unexplained",
        )
    else:
        report.record("pmid-set-diff", "drops since previous", expectation, PASS, observed)


def run_validation(
    export_dir: Path,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    previous_report: Path | None = None,
    previous_manifest: Path | None = None,
    manifest_out: Path | None = None,
    sample_size: int = 15,
    drop_sample: int = 10,
    seed: int = 0,
    abstract_threshold: float = 0.90,
    online: bool = True,
    api_key: str | None = None,
    email: str | None = None,
    entrez_low: float = 0.95,
    entrez_high: float = 1.05,
) -> dict:
    """Run all checks and return the assembled report dict."""
    start = time.monotonic()
    shards = find_shards(export_dir)
    report = Report()

    # One start line, before any of the slow work: what is being validated, and
    # whether the two things a run is usually misconfigured on — the database
    # and the API key — were actually picked up. The key itself is never logged,
    # only that one was found; the report carries the same fact as `api_key_used`.
    logger.info(
        "starting validation: %d shard(s) in %s, %.1f GiB · database %s · %s",
        len(shards), export_dir,
        sum(path.stat().st_size for path in shards) / 1024**3,
        "available" if con is not None else "not available",
        "offline (no Entrez checks)" if not online
        else "online with an NCBI API key (10 req/s)" if api_key
        else "online without an NCBI API key (3 req/s; set NCBI_API_KEY for 10/s)",
    )

    previous = None
    if previous_report is not None:
        previous = json.loads(Path(previous_report).read_text())

    report.record(
        "shards-found", "structure", "the export directory contains NDJSON shards",
        FAIL if not shards else PASS, f"{len(shards)} shard(s)",
        code="no_shards", message=f"No .ndjson shards found in {export_dir}.",
        count=0, see="inputs",
    )

    # The phase lines cost nothing and are what tells you *where* a run is stuck:
    # reading the shards is CPU-bound and local, everything after it waits on
    # NCBI, and a hung eutils call otherwise looks identical to a slow read.
    logger.info("reading shards (structure check)...")
    structure = check_structure(shards, report, sample_size=sample_size, seed=seed)
    logger.info(
        "read %s record(s) in %s (peak RSS %.1f GiB)",
        f"{structure.records_total:,}", fmt_duration(time.monotonic() - start),
        peak_rss_gib(),
    )

    logger.info("checking coverage...")
    check_coverage(
        report, exported_count=structure.records_total, con=con, online=online,
        api_key=api_key, email=email, previous=previous,
        entrez_low=entrez_low, entrez_high=entrez_high,
    )
    logger.info("comparing %d sampled record(s) against Entrez...", len(structure.sample))
    check_fields(
        report, structure.sample, online=online, api_key=api_key, email=email,
        abstract_threshold=abstract_threshold,
    )
    logger.info("confirming deletions...")
    check_deletions(
        report, all_pmids=structure.all_pmids, con=con,
        online=online, api_key=api_key, email=email, drop_sample=drop_sample, seed=seed,
    )
    check_drops_since(
        report, all_pmids=structure.all_pmids,
        previous_manifest=previous_manifest, con=con,
    )

    if manifest_out is not None:
        logger.info("writing PMID manifest to %s...", manifest_out)
        write_manifest(structure.all_pmids, manifest_out)

    logger.info(
        "validation finished in %s (peak RSS %.1f GiB)",
        fmt_duration(time.monotonic() - start), peak_rss_gib(),
    )

    return {
        "status": report.status,
        "errors": report.errors,
        "warnings": report.warnings,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration": fmt_duration(time.monotonic() - start),
        "peak_rss_gib": round(peak_rss_gib(), 3),
        "inputs": {
            "export_dir": str(export_dir),
            "shards": [p.name for p in shards],
            "database": None if con is None else "available",
            "previous_report": None if previous_report is None else str(previous_report),
            "previous_manifest": None if previous_manifest is None else str(previous_manifest),
            "manifest_written": None if manifest_out is None else str(manifest_out),
            "online": online,
            "sample_size": sample_size,
            "drop_sample": drop_sample,
            "seed": seed,
            "api_key_used": bool(api_key),
            # The thresholds each check was judged against, so the report can be
            # re-read later without guessing what the run considered acceptable.
            "abstract_threshold": abstract_threshold,
            "entrez_low": entrez_low,
            "entrez_high": entrez_high,
        },
        "skipped_checks": report.skipped_checks,
        "checks_run": [c.as_dict() for c in report.checks_run],
        "checks": report.checks,
    }


def write_report(report: dict, out_path: Path) -> None:
    """Write the report as pretty-printed JSON."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


#: Coverage gaps that no check can discover for itself, because they are about
#: what the export *omits*. Everything else in the NOT CHECKED block is derived
#: from CORE_FIELDS/SOFT_FIELDS so it cannot fall out of date.
_NOT_CHECKED = (
    "identifiers other than the PMID (DOI, PMCID) are not part of this export",
    "MeSH terms, authors, affiliations and grants are stored in the DB, never exported",
    "records outside the sample are checked for structure only, never against Entrez",
)

#: Section order for the rendered report; anything else falls to OTHER CHECKS.
_SECTIONS = ("structure", "coverage", "field accuracy", "deletions", "drops since previous")

_MARKERS = {PASS: "pass", FAIL: "FAIL", WARN: "WARN", SKIP: "skip", NA: "n/a "}

#: Plain-English reading of each mismatch kind, worst first.
_KIND_READING = {
    "values_differ": "exported a different value (incorrect data)",
    "low_similarity": "abstract text diverged (possible truncation)",
    "entrez_blank": "exported a value Entrez does not have (extra data)",
    "exported_blank": "exported blank where Entrez has a value (missing data)",
}


def _rows(checks: list[dict], section: str) -> list[str]:
    """One line per check, with any detail block directly beneath its own row."""
    lines = []
    for check in (c for c in checks if c["section"] == section):
        marker = _MARKERS.get(check["status"], check["status"])
        lines.append(
            f"  [{marker}] {check['name']:<15} {check['expectation']:<44} "
            f"{check['observed']}".rstrip()
        )
        if check.get("detail"):
            lines += _mismatch_detail(check)
    return lines


def _mismatch_detail(check: dict) -> list[str]:
    """Explain a field-accuracy finding well enough to act on it."""
    detail = check.get("detail") or {}
    by_field, by_kind = detail.get("by_field", {}), detail.get("by_kind", {})
    if not by_field:
        return []

    shown, total = sum(by_field.values()), check["count"]
    suffix = f" ({shown} of {total} shown)" if total > shown else ""
    lines = [
        "         by field" + suffix + ": "
        + ", ".join(f"{n}x {name}" for name, n in by_field.items())
    ]
    # Print every kind that occurred, and always print values_differ even at
    # zero -- that zero is the whole answer to "is this safe to pass on?".
    for kind in MISMATCH_KINDS:
        n = by_kind.get(kind, 0)
        if n or kind == "values_differ":
            lines.append(f"         {n:>4} {_KIND_READING[kind]}")
    for example in (detail.get("examples") or {}).values():
        pmid, name = example.get("pmid"), example.get("field")
        if "similarity" in example:
            lines.append(f"         e.g. PMID:{pmid} {name} similarity {example['similarity']}")
        else:
            lines.append(
                f"         e.g. PMID:{pmid} {name} exported "
                f'"{_clip(example.get("exported"))}" vs. Entrez "{_clip(example.get("entrez"))}"'
            )
    if check["see"]:
        lines.append(f"         see {check['see']}")
    return lines


def _clip(value: object, limit: int = 40) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _not_checked(report: dict) -> list[str]:
    threshold = report["inputs"].get("abstract_threshold")
    derived = [
        f"compared strictly against Entrez: {', '.join(CORE_FIELDS)}",
        f"compared but never fails the run (NLM Catalog source): {', '.join(SOFT_FIELDS)}",
        "abstract compared by similarity ratio"
        + (f" (>= {threshold})" if threshold is not None else "")
        + ", not character-for-character",
    ]
    return [f"  - {item}" for item in derived + list(_NOT_CHECKED)]


def format_summary(report: dict) -> str:
    """Render stdout as a test report: what ran, what it expected, what it saw.

    A pure function of the report dict — it reads nothing that is not archived in
    ``validation_report.json``, so anything a human sees here is provably in the
    file too.
    """
    status = report["status"]
    banner = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}[status]
    inputs = report["inputs"]
    checks = report.get("checks_run", [])
    struct = report["checks"].get("structure", {})
    fv = report["checks"].get("field_validation") or {}

    online = inputs.get("online")
    entrez = "offline"
    if online:
        entrez = "online, no API key (3 req/s; set NCBI_API_KEY for 10/s)"
        if inputs.get("api_key_used"):
            entrez = "online with an API key (10 req/s)"
    lines = [
        f"Validation {banner}: {struct.get('records_total', 0):,} record(s) in "
        f"{len(inputs['shards'])} shard(s) of {inputs['export_dir']}",
        f"  database {inputs.get('database') or 'not available'} · Entrez {entrez}",
        f"  ran in {report.get('duration', '?')}, peak RSS "
        f"{report.get('peak_rss_gib', 0)} GiB",
    ]

    for section in _SECTIONS:
        rows = _rows(checks, section)
        if not rows:
            continue
        heading = section.upper()
        if section == "field accuracy" and fv.get("sampled"):
            heading += (
                f"  ({fv['sampled']:,} records sampled: {inputs.get('sample_size')}/shard "
                f"x {len(inputs['shards'])} shards, seed {inputs.get('seed')})"
            )
        lines += ["", heading, *rows]

    # Anything reported by a check the sections above did not render, so a future
    # check cannot silently vanish from stdout.
    rendered = {c["name"] for c in checks if c["section"] in _SECTIONS}
    orphans = [
        item for kind in ("errors", "warnings") for item in report[kind]
        if not any(c["code"] == item["code"] and c["name"] in rendered for c in checks)
    ]
    if orphans:
        lines += ["", "OTHER FINDINGS"]
        lines += [f"  {i['message']} ({i['count']}; {i['see']})" for i in orphans]

    lines += ["", "NOT CHECKED", *_not_checked(report)]
    lines += [
        "",
        f"Validation {banner}: {len(report['errors'])} error(s), "
        f"{len(report['warnings'])} warning(s).",
    ]
    return "\n".join(lines)
