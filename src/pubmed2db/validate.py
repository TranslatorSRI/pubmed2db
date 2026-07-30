"""Validate a directory of exported NDJSON shards produced by ``export``.

The ``validate`` command answers "does this export make sense?" after a run on
the HPC server, and writes a human-readable, gated JSON report that can be
archived alongside the export. It runs four checks, split into an **offline
phase** (fast, deterministic, no network) and an **online phase** (sampled
Entrez eutils cross-checks):

1. **structure** — every line parses as JSON and matches the exporter's 10-field
   record shape (:func:`pubmed2db.export._document`); PMIDs are unique.
2. **coverage** — how much of PubMed we exported, against *two* denominators:
   the live Entrez total (portable) and the local ``latest_article`` count
   (authoritative — a shortfall means the export silently dropped rows). Drift
   vs. a previous report is flagged.
3. **field_validation** — a seeded random sample of records is re-fetched from
   Entrez ``efetch`` and compared field-by-field (fuzzy for the abstract).
4. **deletions** — a sample of PMIDs the database (or a previous report) marks
   dropped are confirmed absent from the export and, via the API, from PubMed.

The report leads with top-level ``errors``/``warnings`` arrays that are
conspicuously empty on a clean run; every actionable finding is duplicated there
(with a ``see`` pointer into ``checks``) so a human — and the exit code — can
judge the export at a glance. Optional inputs (the database, a previous report,
the network) are used when available and leave their sections blank otherwise.
"""

from __future__ import annotations

import calendar
import difflib
import gzip
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

from .export import _document, month_to_abbrev
from .util import fmt_duration, peak_rss_gib

logger = logging.getLogger(__name__)

#: NCBI E-utilities base URL.
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

#: Exactly the keys :func:`pubmed2db.export._document` emits, derived from the
#: exporter itself so the two cannot drift apart. The placeholder row only has
#: to be the right arity — ``_document`` names the keys, not the values.
EXPECTED_FIELDS = frozenset(_document((0,) + (None,) * 9))

#: Fields compared strictly against Entrez; a high mismatch rate here is an error.
CORE_FIELDS = ("article_title", "volume", "issue", "pub_year", "pub_month", "pub_day")

#: Fields sourced from the NLM Catalog dimension, not the article XML, so a
#: mismatch vs. efetch is informational (the two sources can legitimately differ).
SOFT_FIELDS = ("journal_name", "journal_abbrev")

_ID_RE = re.compile(r"^PMID:(\d+)$")

#: Valid ``pub_month`` values: the 3-letter abbreviations, or empty.
_VALID_MONTHS = frozenset(m for m in calendar.month_abbr if m) | {""}

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

# --------------------------------------------------------------------------- #
# Report accumulator
# --------------------------------------------------------------------------- #


@dataclass
class Report:
    """Accumulates findings and per-check detail, and renders the JSON report."""

    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def error(self, code: str, message: str, *, count: int, see: str) -> None:
        self.errors.append({"code": code, "message": message, "count": count, "see": see})

    def warning(self, code: str, message: str, *, count: int, see: str) -> None:
        self.warnings.append({"code": code, "message": message, "count": count, "see": see})

    def skip(self, what: str) -> None:
        self.skipped_checks.append(what)

    @property
    def status(self) -> str:
        if self.errors:
            return "fail"
        if self.warnings:
            return "warn"
        return "pass"


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

    for shard_index, path in enumerate(shards):
        rng = random.Random(seed + shard_index)
        reservoir: list[tuple[int, dict]] = []
        seen_in_shard = 0

        with _open_text(path) as handle:
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

    if result.records_total == 0:
        report.error(
            "no_records", "No records found in the export directory.", count=0,
            see="checks.structure",
        )
    if malformed:
        report.error(
            "malformed_json", "Lines that could not be parsed as JSON.",
            count=len(malformed), see="checks.structure.malformed",
        )
    if missing_fields:
        report.error(
            "missing_fields", "Records missing one or more required fields.",
            count=sum(missing_fields.values()), see="checks.structure.missing_fields",
        )
    if null_values:
        report.error(
            "null_values", "Fields with a null value (export should use empty strings).",
            count=len(null_values), see="checks.structure.null_values",
        )
    if invalid_ids:
        report.error(
            "invalid_ids", "Records whose id is not of the form PMID:<digits>.",
            count=len(invalid_ids), see="checks.structure.invalid_ids",
        )
    if duplicates:
        report.error(
            "duplicate_pmids", "PMIDs appearing in more than one record.",
            count=len(duplicates), see="checks.structure.duplicate_pmids",
        )
    if extra_fields:
        report.warning(
            "extra_fields", "Records carrying unexpected extra fields.",
            count=sum(extra_fields.values()), see="checks.structure.extra_fields",
        )
    if invalid_months:
        report.warning(
            "invalid_months", "Records whose pub_month is not a 3-letter abbrev or empty.",
            count=len(invalid_months), see="checks.structure.invalid_months",
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
            abstract = " ".join(
                _normalize("".join(node.itertext()))
                for node in article.findall("Abstract/AbstractText")
            )
            docs[pmid] = {
                "id": f"PMID:{pmid}",
                "journal_name": _text(article, "Journal/Title"),
                "journal_abbrev": _text(article, "Journal/ISOAbbreviation"),
                "article_title": _text(article, "ArticleTitle"),
                "volume": _text(journal, "Volume") if journal is not None else "",
                "issue": _text(journal, "Issue") if journal is not None else "",
                "pub_year": _text(pub, "Year") if pub is not None else "",
                "pub_month": month_to_abbrev(
                    pub.findtext("Month") if pub is not None else None
                ),
                "pub_day": _text(pub, "Day") if pub is not None else "",
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

    if online:
        try:
            total = entrez_total(api_key=api_key, email=email)
            coverage["entrez_total"] = total
            pct = exported_count / total if total else None
            coverage["pct_of_entrez"] = pct
            if pct is not None and not (entrez_low <= pct <= entrez_high):
                report.warning(
                    "coverage_out_of_band",
                    f"Exported {pct:.3%} of PubMed, outside the expected "
                    f"[{entrez_low:.2%}, {entrez_high:.2%}] band.",
                    count=1, see="checks.coverage",
                )
        except Exception as exc:  # network/parse failure degrades to a warning
            logger.warning("could not fetch Entrez total: %s", exc)
            report.warning(
                "entrez_unreachable", f"Could not fetch the Entrez total: {exc}",
                count=1, see="checks.coverage",
            )
    else:
        report.skip("coverage.entrez (offline)")

    if con is not None:
        db_latest = con.execute("SELECT count(*) FROM latest_article").fetchone()[0]
        coverage["db_latest_count"] = db_latest
        coverage["pct_of_db"] = exported_count / db_latest if db_latest else None
        if db_latest:
            shortfall = (db_latest - exported_count) / db_latest
            if shortfall > _DB_SHORTFALL_RATE:
                report.error(
                    "export_shortfall",
                    f"Export has {exported_count} records vs. {db_latest} in the "
                    f"database ({shortfall:.2%} short) — rows were dropped.",
                    count=db_latest - exported_count, see="checks.coverage",
                )
            elif exported_count != db_latest:
                report.warning(
                    "export_db_mismatch",
                    f"Export has {exported_count} records vs. {db_latest} in the "
                    "database.",
                    count=abs(db_latest - exported_count), see="checks.coverage",
                )
    else:
        report.skip("coverage.database (not available)")

    if previous is not None:
        prev_cov = previous.get("checks", {}).get("coverage", {})
        coverage["previous"] = {
            "exported_count": prev_cov.get("exported_count"),
            "pct_of_entrez": prev_cov.get("pct_of_entrez"),
        }
        prev_pct = prev_cov.get("pct_of_entrez")
        cur_pct = coverage["pct_of_entrez"]
        if prev_pct and cur_pct and abs(cur_pct - prev_pct) / prev_pct > _DRIFT_REL:
            report.warning(
                "coverage_drift",
                f"Coverage moved from {prev_pct:.3%} to {cur_pct:.3%} vs. the "
                "previous report.",
                count=1, see="checks.coverage.previous",
            )
    else:
        report.skip("coverage.previous_report (not provided)")

    report.checks["coverage"] = coverage


# --------------------------------------------------------------------------- #
# Check 3: field validation
# --------------------------------------------------------------------------- #


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
    if not online:
        report.skip("field_validation (offline)")
        report.checks["field_validation"] = None
        return
    if not sample:
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
    report.checks["field_validation"] = {
        "sampled": len(pmids),
        "checked": checked,
        "core_mismatch_rate": round(rate, 4),
        "mismatches": _capped(mismatches),
        "soft_mismatches": _capped(soft_mismatches),
        "missing_from_api": _capped(missing_from_api),
        "abstract_similarity": {
            "min": round(min(similarities), 3) if similarities else None,
            "mean": round(sum(similarities) / len(similarities), 3) if similarities else None,
        },
    }

    if missing_from_api:
        report.error(
            "sampled_pmid_absent",
            "Sampled PMIDs present in the export but not returned by PubMed.",
            count=len(missing_from_api), see="checks.field_validation.missing_from_api",
        )
    if rate > _FIELD_MISMATCH_RATE:
        report.error(
            "field_mismatch_rate",
            f"{rate:.1%} of sampled core-field comparisons disagreed with Entrez.",
            count=core_mismatch, see="checks.field_validation.mismatches",
        )
    elif core_mismatch:
        report.warning(
            "field_mismatches", "Some sampled records disagreed with Entrez.",
            count=core_mismatch, see="checks.field_validation.mismatches",
        )
    if soft_mismatches:
        report.warning(
            "journal_mismatches",
            "Sampled journal name/abbrev differs from Entrez (different source).",
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
    if con is None:
        report.skip("deletions (no database available)")
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
        except Exception as exc:
            logger.warning("could not confirm deletions via API: %s", exc)
            unknown = list(candidates)
            report.warning(
                "deletion_check_unreachable",
                f"Could not confirm dropped PMIDs via the API: {exc}",
                count=len(candidates), see="checks.deletions",
            )
    else:
        unknown = list(candidates)
        report.skip("deletions.api_confirmation (offline)")

    report.checks["deletions"] = {
        "source": source,
        "candidates_checked": len(candidates),
        "present_in_export": _capped(present_in_export),
        "confirmed_deleted": _capped(confirmed_deleted),
        "still_live": _capped(still_live),
        "unknown": _capped(unknown),
    }

    if present_in_export:
        report.error(
            "deleted_pmid_exported",
            "PMIDs the database marks deleted still appear in the export.",
            count=len(present_in_export), see="checks.deletions.present_in_export",
        )
    if still_live:
        report.warning(
            "deleted_pmid_still_live",
            "PMIDs marked dropped are still served by PubMed (possible merge) — "
            "review manually.",
            count=len(still_live), see="checks.deletions.still_live",
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
    if previous_manifest is None:
        report.skip("drops_since_previous (no --previous-manifest)")
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

    if unexplained and con is not None:
        report.error(
            "unexplained_drops",
            "PMIDs present in the previous export are missing from this one "
            "without a recorded deletion.",
            count=len(unexplained),
            see="checks.drops_since_previous.unexplained",
        )
    elif unexplained:
        report.warning(
            "drops_unattributed",
            "PMIDs present in the previous export are missing from this one; "
            "no database was available to confirm they were deleted.",
            count=len(unexplained),
            see="checks.drops_since_previous.unexplained",
        )


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

    previous = None
    if previous_report is not None:
        previous = json.loads(Path(previous_report).read_text())

    if not shards:
        report.error(
            "no_shards", f"No .ndjson shards found in {export_dir}.", count=0,
            see="inputs",
        )

    structure = check_structure(shards, report, sample_size=sample_size, seed=seed)

    check_coverage(
        report, exported_count=structure.records_total, con=con, online=online,
        api_key=api_key, email=email, previous=previous,
        entrez_low=entrez_low, entrez_high=entrez_high,
    )
    check_fields(
        report, structure.sample, online=online, api_key=api_key, email=email,
        abstract_threshold=abstract_threshold,
    )
    check_deletions(
        report, all_pmids=structure.all_pmids, con=con,
        online=online, api_key=api_key, email=email, drop_sample=drop_sample, seed=seed,
    )
    check_drops_since(
        report, all_pmids=structure.all_pmids,
        previous_manifest=previous_manifest, con=con,
    )

    if manifest_out is not None:
        write_manifest(structure.all_pmids, manifest_out)

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
        },
        "skipped_checks": report.skipped_checks,
        "checks": report.checks,
    }


def write_report(report: dict, out_path: Path) -> None:
    """Write the report as pretty-printed JSON."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


def format_summary(report: dict) -> str:
    """Render a stdout summary: quiet on success, loud on findings."""
    status = report["status"]
    banner = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[status]
    struct = report["checks"].get("structure", {})
    lines = [
        f"Validation {banner}: {struct.get('records_total', 0)} record(s) across "
        f"{len(report['inputs']['shards'])} shard(s).",
    ]
    for kind in ("errors", "warnings"):
        for item in report[kind]:
            marker = "ERROR" if kind == "errors" else "warn "
            lines.append(f"  [{marker}] {item['message']} ({item['count']}; {item['see']})")
    return "\n".join(lines)
