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
from collections import Counter
import gzip
import hashlib
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import duckdb
import requests
from lxml import etree

from .export import (
    ID_PREFIXES,
    JSON_FIELDS,
    METADATA_FILE,
    MESH_PREFIX,
    _MONTH_ABBR,
    file_sha256,
    normalize_day,
    normalize_month,
    _year_from_medline_date,
    pub_date,
    pub_month,
)
from .parse import _publication_types
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
#: (``article_title`` is compared through :func:`_normalize_title`, which folds
#: efetch's rendering of the title, not its wording.)
CORE_FIELDS = ("article_title", "volume", "issue",
               "pub_year", "pub_month", "pub_day")

class _SoftGroup(NamedTuple):
    """Soft fields that share a reason for being soft; see :data:`_SOFT_GROUPS`."""

    reason: str  # printed in the run summary
    fields: tuple[str, ...]
    check: str  # the report check's name
    expectation: str  # the check's expectation, short enough for the summary table
    code: str  # its warning code, and its examples' key under field_validation
    message: str


#: Fields compared and reported, but never able to fail the run. There are three
#: unrelated reasons a field ends up here, one per group:
#:
#: ``journal_name``/``journal_abbrev`` come from a **different source**: the
#: journal dimension, whose titles and abbreviations are NLM's ``J_Entrez.txt``
#: (not the serial catalog, which only supplies years), rather than the article
#: XML, so the two can legitimately differ — though ``JournalTitle`` is
#: byte-identical to what efetch serves, so they rarely do (#31).
#:
#: ``pub_date`` is **correlated with CORE_FIELDS**, and it is worth spelling out
#: because putting it in CORE_FIELDS looks obviously right. The *rendering*
#: half of the problem is handled elsewhere and is not why it sits here:
#: :func:`_normalize_date` folds the two spellings of one date before
#: comparing, so a baseline ``"1998 September"`` meeting a reconstructed
#: ``"1998 Sep"`` is not reported at all. What remains is that ``pub_date`` is
#: derived from the same three columns as ``pub_year``/``pub_month``/
#: ``pub_day``. Gating on it would let a single date disagreement — a genuine
#: one, in the data — contribute four mismatches instead of three, tightening
#: the FAIL threshold on exactly the records most likely to trip it. That is
#: the same reasoning that keeps the ``identifiers`` comparison advisory below,
#: and it is unaffected by how well the two sides are normalized.
#:
#: ``publication_types`` is **revised after publication**: NLM changes a
#: record's types when MEDLINE indexing completes, so live efetch can be ahead
#: of our last update file without anything being wrong with the load.
#:
#: Grouped by reason because the reasons are unrelated: the run summary prints
#: them separately, and each group is its own report check with its own warning
#: code and its own example list. Everything else is derived from this rather
#: than written twice — the summary line once gave the journal reason for every
#: soft field, and a single report check still called a ``pub_date``
#: disagreement a journal mismatch, both long after ``pub_date`` joined for an
#: entirely different reason. A new soft field with a new reason gets a new
#: group.
_SOFT_GROUPS: tuple[_SoftGroup, ...] = (
    _SoftGroup(
        "journal dimension, not the article XML", ("journal_name", "journal_abbrev"),
        "journal-soft", "journal name/abbrev match Entrez (advisory)",
        "journal_mismatches",
        "Sampled journal name/abbrev differs from Entrez (different source).",
    ),
    _SoftGroup(
        "derived from pub_year/pub_month/pub_day, already compared", ("pub_date",),
        "pub-date-soft", "pub_date matches Entrez (advisory)",
        "pub_date_mismatches",
        "Sampled pub_date differs from Entrez's rendering of the same record.",
    ),
    _SoftGroup(
        "indexing revises types after our last update file", ("publication_types",),
        "publication-types-soft", "publication types match Entrez (advisory)",
        "publication_type_mismatches",
        "Sampled publication types differ from Entrez "
        "(may have been revised by indexing since our last update).",
    ),
)

SOFT_FIELD_REASONS: dict[str, tuple[str, ...]] = {g.reason: g.fields for g in _SOFT_GROUPS}

SOFT_FIELDS = tuple(f for g in _SOFT_GROUPS for f in g.fields)

_ID_RE = re.compile(r"^PMID:(\d+)$")

#: A publication type's CURIE. Written out rather than built from
#: ``export.MESH_PREFIX`` on purpose: the efetch side *is* built from it, so a
#: casing slip there (``MeSH:``) would change both sides of the field comparison
#: together and pass. The prefix is a contract with Biolink, like the field
#: names ``test_expected_fields_matches_spec`` pins.
_PUBLICATION_TYPE_ID_RE = re.compile(r"^MESH:D\d+$")

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

#: esummary accepts many IDs per request, and `pmids_served` only needs to know
#: which exist, so it asks for far more per request than efetch does.
_ESUMMARY_BATCH = 200

#: Most unexplained drops `check_drops_since` will ask PubMed about. At
#: _ESUMMARY_BATCH per request that is 1,000 requests: ~100 s with an API key
#: (10/s), ~6 min without (3/s). The first run after a fresh-database rebuild
#: has the deletions PubMed published between the previous build and the new
#: baseline (not measured; set well above any plausible year's worth, since a
#: run over the limit fails and so cannot write the manifest that would end
#: the comparison). Far more than this means the previous manifest describes a
#: different corpus, which no amount of fetching will explain.
_DROP_CONFIRM_LIMIT = 200_000

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


class _Examples:
    """Count findings while keeping only the first ``_MAX_EXAMPLES`` of them.

    The report shows a capped list either way, so retaining every occurrence
    buys nothing — and a systematic defect on the ~41M-record corpus (one blank
    field per record, say) would otherwise cost gigabytes to describe a single
    bug, OOM-killing the validation of exactly the export that was broken.
    """

    def __init__(self) -> None:
        self.count = 0
        self.examples: list = []

    def append(self, item) -> None:
        self.count += 1
        if len(self.examples) < _MAX_EXAMPLES:
            self.examples.append(item)

    def __len__(self) -> int:
        return self.count


# --------------------------------------------------------------------------- #
# Reading shards
# --------------------------------------------------------------------------- #


def find_shards(export_dir: Path) -> list[Path]:
    """Return the exported NDJSON shards in a directory, sorted by name."""
    return sorted(
        p for p in export_dir.iterdir() if p.name.endswith((".ndjson", ".ndjson.gz"))
    )


def check_export_metadata(
    export_dir: Path, shards: list[Path], records_total: int, report: Report,
    sha256: dict[str, str] | None = None,
) -> dict | None:
    """Hold the directory to the ``metadata.json`` its export wrote; return that metadata.

    The export records each shard's SHA-256 and the record count, so a shard
    that is missing, extra, or replaced since — even by an intact one with the
    same record count — is caught here rather than by whoever downloads it.
    Damage is caught earlier and without this: a truncated or corrupted gzip
    shard fails ``shard-readable``. An export without the file (one from before
    data model 1.0) is a ``skip``: nothing to check against, not a failure.

    ``sha256`` is the hashes :func:`check_structure` took while it read the
    shards; a shard missing from it (one that read did not finish) is hashed here.
    """
    sha256 = sha256 or {}
    path = export_dir / METADATA_FILE
    if not path.is_file():
        report.record(
            "export-metadata", "structure", f"{METADATA_FILE} matches the shards", SKIP,
            f"no {METADATA_FILE} (an export from before data model 1.0)",
        )
        return None
    try:
        metadata = json.loads(path.read_text())
        listed = {f["name"]: f for f in metadata["files"]}
        expected_records = metadata["records"]
        # `bool` is an `int`; neither a string nor `true` is a record count.
        if not isinstance(expected_records, int) or isinstance(expected_records, bool):
            raise TypeError(f"records is {expected_records!r}, not an integer")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report.record(
            "export-metadata", "structure", f"{METADATA_FILE} matches the shards", FAIL,
            f"unreadable: {exc}", code="export_metadata_unreadable",
            message=f"{METADATA_FILE} could not be read.", count=1, see="inputs",
        )
        return None

    problems = []
    found = {p.name for p in shards}
    problems += [f"{name}: listed but missing" for name in sorted(set(listed) - found)]
    problems += [f"{name}: present but not listed" for name in sorted(found - set(listed))]
    for shard in shards:
        entry = listed.get(shard.name)
        if entry is None:
            continue
        digest = sha256.get(shard.name) or file_sha256(shard)
        if digest != entry.get("sha256"):
            problems.append(f"{shard.name}: checksum differs from {METADATA_FILE}")
    if records_total != expected_records:
        problems.append(f"{records_total:,} record(s) read, {expected_records:,} exported")
    report.record(
        "export-metadata", "structure", f"{METADATA_FILE} matches the shards",
        FAIL if problems else PASS,
        "; ".join(problems) or f"{len(listed)} file(s) and {expected_records:,} record(s) as listed",
        code="export_metadata_mismatch",
        message=f"The shards differ from what {METADATA_FILE} says the export wrote.",
        count=len(problems), see="checks_run",
        detail={"problems": _capped(problems)},
    )
    return metadata


def _shard_sizes(shards: list[Path]) -> dict[Path, int]:
    """Byte size of each shard, ``0`` for one that cannot be stat'd.

    `export` publishes in place (#24), so a re-run can replace or remove a shard
    between :func:`find_shards` and here. That is the half-written export this
    check exists to report, not a reason to abandon the run with a
    ``FileNotFoundError`` — the read itself then fails and records the shard as
    unreadable. Only the progress denominator is off, and only for that shard.
    """
    sizes = {}
    for path in shards:
        try:
            sizes[path] = path.stat().st_size
        except OSError:
            sizes[path] = 0
    return sizes


def _open_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _normalize(value: str) -> str:
    """Collapse whitespace for tolerant field comparison."""
    return " ".join(value.split())


#: Fields whose two sides can be *written* differently while describing the same
#: value, so they are compared through :func:`_normalize_date`.
#: ``article_title`` is the same idea through :func:`_normalize_title`.
_DATE_RENDERED_FIELDS = frozenset({"pub_date"})


def _valid_publication_types(types: object) -> bool:
    """A list of ``{"id": "MESH:D…", "name": str}`` and nothing else."""
    return isinstance(types, list) and all(
        isinstance(t, dict)
        and set(t) == {"id", "name"}
        and isinstance(t["id"], str) and _PUBLICATION_TYPE_ID_RE.match(t["id"])
        and isinstance(t["name"], str)
        for t in types
    )


def _type_ids(record: dict) -> list:
    """A record's publication-type ids, in order, ignoring the names."""
    return [t.get("id") for t in record.get("publication_types") or [] if isinstance(t, dict)]


def _has_unnamed_type(record: dict) -> bool:
    return any(
        isinstance(t, dict) and not t.get("name")
        for t in record.get("publication_types") or []
    )


def _soft_differs(field: str, exported: dict, entrez: dict) -> bool:
    """Whether a soft field disagrees with Entrez.

    A publication type exported without a name comes from a database loaded
    before names were parsed. ``check_structure`` reports that once, with its
    cause and its fix, so here such a record is compared on its ids alone:
    comparing the names too would put every sampled record of a stale
    database under ``publication_type_mismatches``, blamed on indexing.
    """
    if field == "publication_types" and _has_unnamed_type(exported):
        return _type_ids(exported) != _type_ids(entrez)
    return _compare_value(field, exported) != _compare_value(field, entrez)


def _compare_value(field: str, record: dict) -> str:
    """One side of a field comparison, normalized for rendering differences."""
    raw = record.get(field, "")
    if isinstance(raw, list):
        # `str()` of a list would compare Python reprs -- quoting and key order
        # included. Canonical JSON compares the values, in order: order is part
        # of `publication_types`, which ships PubMed's own.
        return json.dumps(raw, sort_keys=True, ensure_ascii=False)
    value = _normalize(str(raw))
    if field in _DATE_RENDERED_FIELDS:
        return _normalize_date(value)
    if field == "article_title":
        return _normalize_title(value)
    return value


def _normalize_date(value: str) -> str:
    """Collapse the *renderings* of a date string, for comparison only.

    ``pub_date`` is the archival string where PubMed supplies one, but efetch
    serves a re-serialization rather than that string, so the two sides can be
    written differently while describing the same date:

    - ``"1998 September"`` (a ``MedlineDate``, taken whole) against
      ``"1998 Sep"`` (assembled from ``<Year>`` + ``<Month>``, which the
      exporter's month normalization folds — as it must, or the
      ``<Year>+<Season>`` rendering would not converge with ``<MedlineDate>``).
    - A day either side of :func:`export.normalize_day`, if one side ever
      escapes it.

    Both are correct; neither is a data difference. Comparing the normalized
    forms keeps the report about the *data*, so a real drop or a wrong year is
    not buried under rendering noise. Nothing here reaches the export — this is
    a comparison-time view of two strings that both still ship verbatim.
    """
    parts = _normalize(value).split(" ")
    return " ".join(
        [parts[0]] if len(parts) == 1
        else [parts[0], normalize_month(parts[1])] if len(parts) == 2
        else [parts[0], normalize_month(parts[1]), normalize_day(parts[2])] + parts[3:]
    )


def _normalize_title(value: str) -> str:
    """Collapse efetch's *renderings* of an article title, for comparison only.

    The export ships the archival ``<ArticleTitle>`` verbatim; efetch re-renders
    it. All four title mismatches in the 2026-09-26 corpus sample were this,
    checked against the raw baseline files:

    - a terminal period added where the archival title has none
      (PMID:10205128 ``"…in health care "`` vs. ``"…in health care."``,
      PMID:36326224 ``"[…how?]"`` vs. ``"[…how?]."``);
    - the ``[…]`` marking a translated title dropped
      (PMID:5684047 ``"[Surgical results…]."`` vs. ``"Surgical results…."``);
    - an empty ``<ArticleTitle/>`` served as ``"[Not Available]."``
      (PMID:33511611).

    Fold all three on both sides; a change of wording still differs. The period
    comes off before the brackets because efetch writes ``[…].``. As with
    :func:`_normalize_date`, nothing here reaches the export.
    """
    value = _normalize(value).removesuffix(".")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return "" if value == "Not Available" else value


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
    #: shard name -> SHA-256 of its bytes, taken during the same read so the
    #: metadata check does not read every shard a second time. Absent for a
    #: shard the read did not finish.
    sha256: dict[str, str] = field(default_factory=dict)


class _HashingReader(io.RawIOBase):
    """Pass a binary file's bytes through, hashing them as they are read."""

    def __init__(self, raw) -> None:
        self._raw = raw
        self.sha256 = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        n = self._raw.readinto(buffer)
        if n:
            self.sha256.update(memoryview(buffer)[:n])
        return n


def check_structure(
    shards: list[Path], report: Report, *, sample_size: int, seed: int
) -> StructureResult:
    """Parse and structurally validate every record; reservoir-sample per shard.

    Uses per-shard reservoir sampling so we hold only ``sample_size`` documents
    per shard in memory rather than the whole export, while still collecting the
    full set of PMIDs (needed for duplicate detection and the deletion check).
    """
    result = StructureResult()
    malformed = _Examples()
    missing_fields: dict[str, int] = {}
    extra_fields: dict[str, int] = {}
    null_values = _Examples()
    invalid_ids = _Examples()
    invalid_months = _Examples()
    unreadable = _Examples()
    duplicates = _Examples()
    #: PMIDs already recorded in `duplicates`, so that one PMID exported 25
    #: times spends one of the 20 example slots rather than all of them, and
    #: `duplicates.count` stays a count of distinct offending PMIDs.
    duplicated: set[int] = set()
    #: CURIE prefix -> records carrying at least one identifier with it. A DOI
    #: rate that collapsed between two exports is invisible to every check here
    #: — nothing about one export in isolation says 96.7% is right and 6% is
    #: not — so record the rate and let a reader compare it to the last run.
    with_prefix: Counter[str] = Counter()
    invalid_types = _Examples()
    unnamed_types = _Examples()

    # Progress is measured in *bytes of shard consumed*, the one denominator we
    # know before reading: the record total is what this pass is computing, and
    # counting shards alone would report nothing at all for the common
    # single-shard export. Hence the raw handle below — `raw.tell()` is the
    # compressed offset for a .gz shard, which is the scale `st_size` is in.
    shard_bytes = _shard_sizes(shards)
    total_bytes = sum(shard_bytes.values())
    bytes_done = 0
    start = time.monotonic()
    last_log = start

    for shard_index, path in enumerate(shards):
        rng = random.Random(seed + shard_index)
        reservoir: list[tuple[int, dict]] = []
        seen_in_shard = 0

        # A shard truncated by a killed export (`export` writes in place, so that
        # is a real state on disk) raises EOFError from gzip mid-iteration, and a
        # corrupt header raises BadGzipFile. Report it as the structural fault it
        # is instead of dying on the one input this check exists to catch.
        try:
            # The raw binary handle rather than `_open_text`, so the progress
            # line below can call `raw.tell()`: for a .gz shard that is the
            # compressed offset, the scale `total_bytes` is measured in. The
            # bytes are hashed on their way through, for check_export_metadata.
            with path.open("rb", buffering=0) as raw:
                hashing = _HashingReader(raw)
                buffered = io.BufferedReader(hashing)
                with (
                    gzip.open(buffered, "rt", encoding="utf-8") if path.name.endswith(".gz")
                    else io.TextIOWrapper(buffered, encoding="utf-8")
                ) as handle:
                    for lineno, line in enumerate(handle, start=1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            doc = json.loads(line)
                        except json.JSONDecodeError as exc:
                            malformed.append(
                                {"shard": path.name, "line": lineno, "error": str(exc)}
                            )
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
                            if pmid in result.all_pmids and pmid not in duplicated:
                                duplicated.add(pmid)
                                duplicates.append(pmid)
                            result.all_pmids.add(pmid)

                        with_prefix.update(
                            {c.split(":", 1)[0] for c in doc.get("identifiers") or []}
                        )

                        # Absent or null is already `record-fields`/`no-nulls`.
                        types = doc.get("publication_types")
                        if types is not None:
                            if not _valid_publication_types(types):
                                invalid_types.append({
                                    "shard": path.name, "line": lineno,
                                    "publication_types": _clip(json.dumps(types), 200),
                                })
                            elif any(t["name"] == "" for t in types):
                                unnamed_types.append(
                                    {"shard": path.name, "line": lineno, "id": doc.get("id")}
                                )

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

                        # Reading the clock every record looks wasteful and is not:
                        # `time.monotonic()` is ~25 ns against ~2.8 µs of per-record
                        # work above (json.loads, the two set differences, the null
                        # scan, the id regex, the reservoir draw), so gating on a
                        # `records % N` counter instead saves ~1 s of a 7m 38s
                        # corpus read. It would also cost the property the line is
                        # for: progress is denominated in bytes, so a record-counted
                        # gate would space the lines by record size rather than by
                        # time, and these are meant to be a minute apart in a log.
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
                    # Whatever the decoder left unread (trailing padding) is
                    # still part of the file its checksum covers.
                    while hashing.read(1 << 20):
                        pass
                result.sha256[path.name] = hashing.sha256.hexdigest()
        except (OSError, EOFError) as exc:
            unreadable.append({"shard": path.name, "error": str(exc)})

        bytes_done += shard_bytes[path]
        for pmid, doc in reservoir:
            result.sample[pmid] = doc

    result_dict = {
        "records_total": result.records_total,
        "records_by_shard": result.records_by_shard,
        "malformed": malformed.examples,
        "missing_fields": missing_fields,
        "extra_fields": extra_fields,
        "null_values": null_values.examples,
        "invalid_ids": invalid_ids.examples,
        "invalid_months": invalid_months.examples,
        "unreadable_shards": unreadable.examples,
        "duplicate_pmids": duplicates.examples,
        "invalid_publication_types": invalid_types.examples,
        "unnamed_publication_types": unnamed_types.examples,
        "identifier_coverage": {
            prefix: {
                "records": with_prefix[prefix],
                "pct": round(100 * with_prefix[prefix] / result.records_total, 2)
                if result.records_total else 0.0,
            }
            for prefix in ID_PREFIXES.values()
        },
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
        ("shard-readable", "every shard can be read to its end",
         "unreadable shard(s)", unreadable, "unreadable_shards", "unreadable_shard",
         "Shards that could not be read (truncated or corrupt).", FAIL),
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
        ("pmid-unique", "no PMID is exported twice", "duplicated PMID(s)",
         duplicates, "duplicate_pmids", "duplicate_pmids",
         "PMIDs appearing in more than one record.", FAIL),
        ("publication-types", "publication_types is a list of {id: MESH:D…, name}",
         "invalid publication_types", invalid_types, "invalid_publication_types",
         "invalid_publication_types",
         "Records whose publication_types is not a list of {id, name} with a MESH: id.",
         FAIL),
        ("no-extra-fields", "no record carries an unexpected field",
         "record(s) with extra fields", extra_fields, "extra_fields", "extra_fields",
         "Records carrying unexpected extra fields.", WARN),
        ("month-format", 'pub_month is a month, season, range of those, or ""',
         "invalid pub_month value(s)", invalid_months, "invalid_months", "invalid_months",
         "Records whose pub_month is not a month/season, a range of two, or empty.", WARN),
        ("publication-type-names", "every publication type has a name",
         "record(s) with an unnamed type", unnamed_types, "unnamed_publication_types",
         "publication_type_names_missing",
         "Publication types exported without a name: the export came from a database "
         "loaded before names were parsed, which predates data model 1.0 and no longer "
         "opens. Rebuild the database, then export again.", WARN),
    )
    for name, expectation, noun, findings, key, code, message, severity in rows:
        # dict findings (field -> n) count occurrences; _Examples counts its own.
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
    made = 0
    for attempt in range(retries):
        made = attempt + 1
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
            # raise_for_status() also fires for 4xx, which no amount of retrying
            # fixes (a bad api_key would burn every attempt plus its backoff).
            # A connection error carries no response, so it stays retryable.
            status = getattr(exc.response, "status_code", None)
            if status is not None and status not in _RETRY_STATUS:
                break
            if attempt < retries - 1:
                time.sleep(min(2**attempt, 10))
    raise RuntimeError(f"eutils {endpoint} failed after {made} attempt(s)") from last_exc


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


def _curie_set(curies) -> set[str]:
    """Case-folded CURIEs, for comparing two renderings of the same identifiers."""
    return {c.casefold() for c in (curies or [])}


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
                # The loader's own parser, so both sides read the same exact
                # path and keep PubMed's order.
                "publication_types": [
                    {"id": f"{MESH_PREFIX}:{ui}", "name": name}
                    for ui, name in _publication_types(art)
                ],
                "abstract": _normalize(abstract),
            }
    return docs


def pmids_served(pmids: list[int], *, api_key: str | None, email: str | None) -> set[int]:
    """Which of ``pmids`` PubMed still serves, via esummary.

    esummary names every ID it was asked about: a served one with its summary,
    a missing one with ``"error"`` (live, 2026-10-20: PMID 99999999 comes back
    as ``{"uid": "99999999", "error": "cannot get document summary"}``). So a
    PMID the response does not mention at all is not evidence that PubMed
    dropped it, and raises instead: a degraded response (an error body, a
    truncated one) must not turn a lost record into a "retired upstream" one.
    efetch cannot make that distinction, which is why this is not
    :func:`efetch_documents`.
    """
    served: set[int] = set()
    for start in range(0, len(pmids), _ESUMMARY_BATCH):
        batch = pmids[start : start + _ESUMMARY_BATCH]
        raw = _eutils(
            "esummary.fcgi",
            {"db": "pubmed", "id": ",".join(str(p) for p in batch), "retmode": "json"},
            api_key=api_key, email=email,
        )
        try:
            result = json.loads(raw)["result"]
            entries = {int(uid): result[uid] for uid in result["uids"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"unusable esummary response: {raw[:200]!r}") from exc
        unanswered = set(batch) - entries.keys()
        if unanswered:
            raise RuntimeError(
                f"esummary did not answer for {len(unanswered)} PMID(s), "
                f"e.g. {min(unanswered)}"
            )
        served.update(p for p in batch if "error" not in entries[p])
    return served


def _rebuilt_since(con: duckdb.DuckDBPyConnection, manifest: Path) -> bool:
    """Whether the database was created after ``manifest`` was written.

    That is the fresh-database rebuild, the one case where a drop the database
    cannot explain may still be legitimate. False when the creation time is
    unknown: the strict answer.
    """
    from .datamodel import metadata

    created = metadata(con).get("created_at")
    if not created:
        return False
    return datetime.fromisoformat(created).timestamp() > manifest.stat().st_mtime


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
        **{g.check: g.expectation for g in _SOFT_GROUPS},
        "identifiers-soft": "DOIs and PMCIDs match Entrez (advisory)",
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
    try:
        fetched = efetch_documents(pmids, api_key=api_key, email=email)
    except Exception as exc:  # as the other online checks do: degrade, don't die
        # The report and the --manifest sidecar are both written after every
        # check, so letting this escape would throw away the offline structure
        # and coverage results too — the expensive part of an HPC run.
        logger.warning("could not fetch the sampled records: %s", exc)
        for name, expectation in expectations.items():
            report.record(
                name, "field accuracy", expectation, WARN,
                f"Entrez unreachable: {exc}",
                code="field_check_unreachable",
                message=f"Could not fetch the sampled records: {exc}",
                count=len(pmids), see="checks.field_validation",
            )
        report.checks["field_validation"] = {"sampled": len(pmids), "checked": 0}
        return

    mismatches: list[dict] = []
    #: One list per soft group, keyed by its code: a shared list capped at
    #: `_MAX_EXAMPLES` let one busy group (publication types, which indexing
    #: revises often) crowd out the examples another group's check points to.
    soft_mismatches: dict[str, list[dict]] = {g.code: [] for g in _SOFT_GROUPS}
    identifier_mismatches: list[dict] = []
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
            if _compare_value(f, exported) != _compare_value(f, entrez):
                core_mismatch += 1
                mismatches.append(
                    {"pmid": pmid, "field": f, "exported": exported.get(f), "entrez": entrez.get(f)}
                )

        # `identifiers` is the one list-valued field, so it can't go through the
        # string comparison above. Order is irrelevant; membership is not.
        #
        # Advisory, not part of the gated rate: this compares our newest
        # *loaded* version against live PubMed, so a PMCID assigned after our
        # last update file reads as a mismatch. That is the same exposure the
        # core fields carry, but it is not independent of them — an
        # ahead-of-print record already mismatching on volume/issue is exactly
        # the one that has since been assigned a PMCID, so counting it here too
        # would tighten the FAIL threshold on the records most likely to trip
        # it. Same reasoning as SOFT_FIELDS below: compared, reported, never
        # fatal.
        # Compared case-folded, reported verbatim: a DOI is case-insensitive by
        # specification and PubMed is not internally consistent about its own,
        # so a case-only change between our load and today's efetch is not a
        # difference in the data. The prefixes cannot drift — both sides build
        # them from ID_PREFIXES — so folding them too costs nothing.
        if _curie_set(exported.get("identifiers")) != _curie_set(entrez.get("identifiers")):
            identifier_mismatches.append({
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
                if exp_abs and ent_abs:
                    mismatches.append(
                        {"pmid": pmid, "field": "abstract", "similarity": round(ratio, 3)}
                    )
                else:
                    # One side is blank: that is missing or extra data, not
                    # text that diverged. Carry the two values, as CORE_FIELDS
                    # do, so `_mismatch_kind` reads it as exported_blank /
                    # entrez_blank rather than "possible truncation". (PMID
                    # 4825553: no <Abstract> in the baseline XML, efetch has one.)
                    mismatches.append({
                        "pmid": pmid, "field": "abstract",
                        "exported": exported.get("abstract"), "entrez": ent_abs,
                    })

        for group in _SOFT_GROUPS:
            for f in group.fields:
                if _soft_differs(f, exported, entrez):
                    soft_mismatches[group.code].append(
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
        **{code: _capped(found) for code, found in soft_mismatches.items()},
        "identifier_mismatches": _capped(identifier_mismatches),
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

    for group in _SOFT_GROUPS:
        differing = {m["pmid"] for m in soft_mismatches[group.code]}
        report.record(
            group.check, "field accuracy", expectations[group.check],
            WARN if differing else PASS,
            f"{len(differing):,} of {checked:,} record(s) differ",
            code=group.code, message=group.message,
            count=len(differing), see=f"checks.field_validation.{group.code}",
        )

    report.record(
        "identifiers-soft", "field accuracy", expectations["identifiers-soft"],
        WARN if identifier_mismatches else PASS,
        f"{len(identifier_mismatches):,} of {checked:,} record(s) differ",
        code="identifier_mismatches",
        message="Sampled DOIs/PMCIDs differ from Entrez (may be assigned since our last update).",
        count=len(identifier_mismatches),
        see="checks.field_validation.identifier_mismatches",
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
        -- Ordered because rng.sample() below indexes into this list: DuckDB does
        -- not promise a row order across parallel scans, so without it --seed
        -- would not reproduce the same sample.
        ORDER BY d.pmid
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
    online: bool,
    api_key: str | None,
    email: str | None,
) -> None:
    """Diff this export's PMID set against a previous export's manifest.

    This is the check the coverage counts cannot make: two exports can hold the
    same number of records while a thousand PMIDs silently swapped out. A PMID
    the previous export shipped and this one does not is legitimate if the
    database recorded a ``DeleteCitation`` for it, or if PubMed no longer serves
    it either. A drop PubMed *still* serves is an error: records vanished
    without PubMed asking for their removal.

    The second test exists for the fresh-database rebuild (README, "Prefer a
    fresh database at the year boundary"), and only applies to a database
    created after the previous manifest was written. A database built from the new
    baseline never loads the update files that carried the deletions published
    between the previous build and that baseline, so its ``deleted_pmid`` cannot
    explain them; nor can it explain a record PubMed dropped quietly, which the
    old additive database kept. Judged by the database alone every one of those
    was an error, the run failed, no manifest was written, and the next run
    diffed against the same old manifest and failed again. Asking PubMed is what
    makes the drop check usable across the one event it is most needed for.
    It only asks about drops the database cannot explain, and only up to
    :data:`_DROP_CONFIRM_LIMIT` of them. On a continuing database the same
    drop means an update file's ``DeleteCitation`` was never loaded, so it
    stays an error there even though PubMed agrees the record is gone.

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
        # Ask the DB which of the dropped PMIDs are retired: marked deleted and
        # not since re-added. Bound as a list via UNNEST rather than interpolated
        # into a giant IN clause.
        rows = con.execute(
            """
            SELECT DISTINCT d.pmid FROM deleted_pmid d
            WHERE d.pmid IN (SELECT * FROM UNNEST(?))
              -- A PMID deleted and later re-added is live again, so its absence
              -- from the export is not explained by that deletion. Same filter
              -- as check_deletions uses to build its candidate pool.
              AND NOT EXISTS (SELECT 1 FROM latest_article la WHERE la.pmid = d.pmid)
            """,
            [sorted(dropped)],
        ).fetchall()
        explained = sorted(r[0] for r in rows)
        unexplained = sorted(dropped - set(explained))

    # Then ask PubMed about what the database could not explain. Only with a
    # database: without one the whole set is unattributed and stays a warning.
    retired_upstream: list[int] = []
    confirmation: str | None = None
    rebuilt = con is not None and _rebuilt_since(con, previous_manifest)
    if unexplained and con is not None:
        if not online:
            confirmation = "offline"
        elif len(unexplained) > _DROP_CONFIRM_LIMIT:
            confirmation = "over_limit"
        else:
            try:
                served = pmids_served(unexplained, api_key=api_key, email=email)
                confirmation = "entrez"
                retired_upstream = [p for p in unexplained if p not in served]
                unexplained = [p for p in unexplained if p in served]
            except Exception as exc:
                logger.warning("could not confirm dropped PMIDs via API: %s", exc)
                confirmation = "unreachable"
    # PubMed agreeing is only an explanation after a rebuild; see the docstring.
    missed_deletions = [] if rebuilt else retired_upstream

    report.checks["drops_since_previous"] = {
        "previous_manifest": str(previous_manifest),
        "previous_count": len(previous_pmids),
        "current_count": len(all_pmids),
        "dropped": len(dropped),
        "added": len(added),
        "explained_by_deletion": _capped(explained),
        "explained_by_deletion_count": len(explained),
        "explained_by_pubmed": _capped(retired_upstream),
        "explained_by_pubmed_count": len(retired_upstream),
        "pubmed_confirmation": confirmation,
        "database_rebuilt_since": rebuilt,
        "unexplained": _capped(unexplained),
        "unexplained_count": len(unexplained),
        "added_examples": _capped(sorted(added)),
    }

    observed = (
        f"{len(dropped):,} dropped ({len(explained):,} explained by a recorded "
        f"deletion, {len(retired_upstream):,} no longer served by PubMed), "
        f"{len(added):,} added"
    )
    if unexplained and con is not None:
        why = {
            "offline": "; offline, so PubMed was not asked about them",
            "over_limit": (
                f"; more than {_DROP_CONFIRM_LIMIT:,}, so PubMed was not asked "
                "about them (does the previous manifest describe this corpus?)"
            ),
            "unreachable": "; PubMed was unreachable, so they could not be attributed",
            "entrez": "; PubMed still serves them",
        }[confirmation]
        next_step = {
            "offline": "; run validate online to ask PubMed whether it still serves them.",
            "over_limit": (
                "; too many to ask PubMed about. If the corpus really changed this "
                "much, review the list and start the chain again by running validate "
                "once by hand with --manifest and without --previous-manifest."
            ),
            "unreachable": "; PubMed could not be asked, so re-run validate once it is reachable.",
            "entrez": ", and PubMed still serves them.",
        }[confirmation]
        report.record(
            "pmid-set-diff", "drops since previous", expectation, FAIL, observed + why,
            code="unexplained_drops",
            message=(
                "PMIDs present in the previous export are missing from this one "
                "without a recorded deletion" + next_step
            ),
            count=len(unexplained), see="checks.drops_since_previous.unexplained",
        )
    elif missed_deletions:
        report.record(
            "pmid-set-diff", "drops since previous", expectation, FAIL,
            f"{observed}; the database predates the previous manifest",
            code="missed_deletions",
            message=(
                "PubMed no longer serves these PMIDs, but this database, which "
                "exported them last time, never loaded a DeleteCitation for them: "
                "an update file was skipped or failed to load. Check `status`."
            ),
            count=len(missed_deletions), see="checks.drops_since_previous.explained_by_pubmed",
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
    elif retired_upstream:
        report.record(
            "pmid-set-diff", "drops since previous", expectation, WARN, observed,
            code="drops_retired_upstream",
            message=(
                "PubMed no longer serves these PMIDs, and this database, created "
                "since the previous manifest, never loaded a DeleteCitation for "
                "them: expected on the first run after a fresh-database rebuild "
                "(the deletions were published before the new baseline)."
            ),
            count=len(retired_upstream), see="checks.drops_since_previous.explained_by_pubmed",
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
        sum(_shard_sizes(shards).values()) / 1024**3,
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

    export_metadata = check_export_metadata(
        export_dir, shards, structure.records_total, report, structure.sha256
    )

    logger.info("checking coverage...")
    check_coverage(
        report, exported_count=structure.records_total, con=con, online=online,
        api_key=api_key, email=email, previous=previous,
        entrez_low=entrez_low, entrez_high=entrez_high,
    )
    # `check_fields` returns immediately when offline, so announcing the
    # comparison unconditionally contradicts the start line two lines up.
    if online:
        logger.info(
            "comparing %d sampled record(s) against Entrez...", len(structure.sample)
        )
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
        online=online, api_key=api_key, email=email,
    )

    # A manifest is a *baseline*: the next run diffs its export against it and
    # reports what disappeared. Writing one from a failed run poisons that
    # comparison — a truncated or half-written export yields a short PMID set,
    # and the next run reports the recovered corpus as "N added" while the real
    # drops go unnoticed. Since 05-validate.sbatch passes --manifest on every
    # cluster run, that is now the default path rather than an explicit choice.
    #
    # WARN still writes, deliberately: the common warning is "Entrez was
    # unreachable", which says nothing about the PMID set the shard read built.
    # Only FAIL — no shards, unreadable shards, malformed records — means the
    # set itself is not to be trusted.
    manifest_written: Path | None = None
    if manifest_out is not None:
        if report.status == FAIL:
            logger.warning(
                "not writing the PMID manifest to %s: validation failed, so this "
                "export's PMID set would be an untrustworthy baseline for the "
                "next run's drop check",
                manifest_out,
            )
        else:
            logger.info("writing PMID manifest to %s...", manifest_out)
            write_manifest(structure.all_pmids, manifest_out)
            manifest_written = manifest_out

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
        # The data model the export follows, from its metadata.json. A consumer
        # checks the major. Never the database's: with no metadata.json the
        # shards may predate it (a pre-1.0 export beside a rebuilt database),
        # and borrowing its version would call them compatible.
        "data_model_version": (
            export_metadata.get("data_model_version") if export_metadata else None
        ),
        "inputs": {
            "export_dir": str(export_dir),
            "shards": [p.name for p in shards],
            "database": None if con is None else "available",
            "previous_report": None if previous_report is None else str(previous_report),
            "previous_manifest": None if previous_manifest is None else str(previous_manifest),
            "manifest_written": None if manifest_written is None else str(manifest_written),
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
    "identifiers other than the DOI, PMCID and PMID are exported but never compared",
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
    """One line per check, with any detail block directly beneath its own row.

    Columns are sized to the widest name and expectation across *all* checks,
    not this section's, so every section lines up with the others; fixed widths
    went ragged as soon as a check name outgrew them.
    """
    name_width = max((len(c["name"]) for c in checks), default=0)
    expectation_width = max((len(c["expectation"]) for c in checks), default=0)
    lines = []
    for check in (c for c in checks if c["section"] == section):
        marker = _MARKERS.get(check["status"], check["status"])
        lines.append(
            f"  [{marker}] {check['name']:<{name_width}} "
            f"{check['expectation']:<{expectation_width}} {check['observed']}".rstrip()
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
        "article_title compared after folding efetch's rendering (terminal "
        "period, [ ] around a translated title, [Not Available] for an empty title)",
        *(
            f"compared but never fails the run ({reason}): {', '.join(fields)}"
            for reason, fields in SOFT_FIELD_REASONS.items()
        ),
        "compared but never fails the run (assigned upstream after our last "
        "update file): identifiers",
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
        if section == "structure" and struct.get("identifier_coverage"):
            carried = ", ".join(
                f"{v['pct']:.1f}% {prefix}"
                for prefix, v in struct["identifier_coverage"].items()
            )
            heading += f"  ({carried})"
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
