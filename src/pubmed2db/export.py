"""Export the latest version of every abstract to sharded NDJSON.

The export follows the NCATS Translator DocumentMetadataAPI field names (which
differ from the PubMed names used in the database) and emits empty strings, not
nulls, for absent values.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import datamodel
from .util import dir_size_gib, fmt_duration, memory_summary, peak_rss_gib, size_bytes

#: Shard filename stem; DuckDB appends the writer-thread index and the extension.
_SHARD_STEM = "pubmed_metadata_"

#: Written beside every export: what it is, what it was built from, and a
#: checksum per file. See :func:`write_export_metadata`.
METADATA_FILE = "metadata.json"

#: The same checksums in ``sha256sum``'s own format, so a consumer can verify a
#: download with ``sha256sum -c SHA256SUMS`` and no JSON parser.
CHECKSUMS_FILE = "SHA256SUMS"

logger = logging.getLogger(__name__)

#: Minimum gap between progress log lines, so large exports don't spam the log.
#: A full corpus export runs ~15 min, so a minute between lines still gives a
#: dozen-odd data points to watch RSS and the rate on.
_PROGRESS_INTERVAL_S = 60.0

#: PubMed ``ArticleId/@IdType`` -> CURIE prefix for the JSON ``identifiers``
#: field. ``doi`` is lowercase to match Babel's ``src/prefixes.py``, so those
#: CURIEs join against the Babel publication compendium; ``DOI:`` would not.
#: PubMed's ``pmc`` values already start with ``PMC``, hence the doubled
#: ``PMC:PMC1234567``. Values are emitted verbatim, so consumers must match
#: case-insensitively (DOIs are case-insensitive per spec and PubMed is not
#: consistent).
#:
#: **``PMC`` is interim: we follow Babel until Translator decides** (issue #33).
#: Babel's ``prefixes.py`` says ``PMC``, and its published compendia spell every
#: PMC ID ``PMC:PMC…`` (2026jul22's ``Publication.txt.00``: 134 ``PMC:``, no
#: ``PMCID:``), so following it is what makes these CURIEs join. Biolink
#: registers ``PMC`` on ``biolink:Publication`` but ``PMCID`` on
#: ``biolink:JournalArticle``; the ruling belongs to
#: https://github.com/biolink/biolink-model/issues/1366 and
#: https://github.com/NCATSTranslator/Babel/issues/1044. If they settle on
#: ``PMCID``, change this one value and re-export -- ``identifiers`` is never
#: stored, so no reload -- and ``tests/test_export.py`` names every doc example
#: still spelled the old way. After data model 1.0 ships, that is a major bump
#: affecting only the NDJSON.
ID_PREFIXES = {"doi": "doi", "pmc": "PMC"}

#: CURIE prefix for a publication type's MeSH UI (``MESH:D016454``), Biolink's
#: casing. One place, for the same reason as :data:`ID_PREFIXES`: `validate`
#: rebuilds the efetch side's ids from it.
MESH_PREFIX = "MESH"

#: The same mapping as SQL, derived rather than restated: `validate` imports
#: `ID_PREFIXES` to rebuild the CURIEs it expects, so a hand-written `CASE` here
#: would let a new id type or a casing fix reach the validator without reaching
#: the export, and every sampled record would be reported as a mismatch against
#: a correct export. No `ELSE` branch: an unlisted type cannot pass the `WHERE`.
_ID_CURIE_SQL = (
    "CASE id_type "
    + " ".join(f"WHEN '{t}' THEN '{prefix}:'" for t, prefix in ID_PREFIXES.items())
    + " END"
)
_ID_TYPES_SQL = ", ".join(f"'{t}'" for t in ID_PREFIXES)


#: Month abbreviations, frozen rather than taken from ``calendar.month_abbr``:
#: that is ``strftime('%b')`` under ``LC_TIME``, so any dependency calling
#: ``locale.setlocale`` would silently localize a spec-defined output field.
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def normalize_month(raw: str | None) -> str:
    """Normalize a raw PubMed month, passing through what isn't a month name.

    A month *name* is folded to a capitalized 3-letter abbreviation, per the
    spec's prose: ``"3"``/``"03"`` (numeric), ``"Mar"``/``"March"``/``"Sept"``/
    ``"sep"`` (textual). Anything else — a season (``"Spring"``) or a range
    (``"Sep-Dec"``) — is returned **verbatim**, because the spec's own worked
    example for PMID:8000234 emits ``"pub_month": "Sep-Dec"`` (issue #14). Only
    an uninterpretable *number* (``"13"``) becomes ``""``.

    The ``isalpha()`` guard is what makes the two cases distinguishable: without
    it the 3-character prefix match silently truncates ``"Sep-Dec"`` to
    ``"Sep"``, which is what this function used to do.
    """
    if not raw:
        return ""
    raw = raw.strip()
    # `raw.isascii() and raw.isdigit()`, not `isdigit()` alone, for two reasons.
    # `"²".isdigit()` is True while `int("²")` raises, and this function now sees
    # unconstrained MedlineDate CDATA rather than only a `<Month>` element, so
    # that ValueError would escape `efetch_documents` and abort a whole
    # validation run. And the SQL twin tests `[0-9]+`, which is ASCII-only:
    # `"١٢"` is `isdigit()` *and* `isdecimal()`, so Python alone would answer
    # `Dec` where the export passes `١٢` through verbatim.
    if raw.isascii() and raw.isdigit():
        month = int(raw)
        return _MONTH_ABBR[month - 1] if 1 <= month <= 12 else ""
    key = raw[:3].capitalize()
    return key if key in _MONTH_ABBR and raw.isalpha() else raw


_MEDLINE_YEAR_RE = re.compile(r"^\s*(\d{4})")


def _year_from_medline_date(raw: str | None) -> str:
    """**Leading** 4-digit year of a free-text ``MedlineDate``, or ``""``.

    Records whose ``PubDate`` is a range or a season carry no ``<Year>`` element
    — PubMed puts the whole thing in ``<MedlineDate>`` ("1978 Jul-Aug", "1998
    Spring", "1998 Dec-1999 Jan", "1999-2000"). We store that verbatim for
    fidelity, which left ``pub_year`` blank in the export for every such record.
    ``pub_day`` stays empty, since a range has no single day. The month half is
    recovered separately by :func:`_month_from_medline_date`.

    **Why the leading year, when a range spans two?**

    ==========================  ========  ===============================
    ``MedlineDate``             we emit   NCBI ``esummary.sortpubdate``
    ==========================  ========  ===============================
    ``"1994 Sep-Dec"``          1994      ``1994/09/01``
    ``"1998 Dec-1999 Jan"``     1998      ``1998/01/01``
    ``"1997 Dec-1998 Jan"``     1997      ``1997/01/01``
    ``"1987-1988"``             1987      ``1987/01/01``
    ==========================  ========  ===============================

    There is a real argument for the **trailing** year: "1998 Dec-1999 Jan"
    describes an issue that mostly reached readers in January 1999, so 1999 is
    arguably when it was published. We chose 1998 anyway, because agreeing with
    PubMed matters more than being semantically nicer in isolation:

    1. NCBI's own ``sortpubdate`` takes the leading year in every cross-year
       shape (right-hand column above) — a consumer joining our ``pub_year``
       against anything derived from Entrez would disagree with it otherwise.
    2. Every *other* shape already uses the leading year, and it is the only
       year present in most of them. A trailing-year rule would have to fire
       only when two years appear, making one rare shape behave unlike the rest.
    3. Cross-year ranges are ~0.07% of PubMed (4 of 5,773 sampled records, and
       three of those were bare "1987-1988" year ranges), so the inconsistency
       would buy very little and cost a special case forever.

    The fidelity escape hatch is :func:`pub_date`, which carries the whole
    string verbatim — a consumer that needs "1999" can see it there.
    """
    if not raw:
        return ""
    match = _MEDLINE_YEAR_RE.match(raw)
    return match.group(1) if match else ""


#: Whatever follows the leading year of a ``MedlineDate``. The **whitespace** is
#: load-bearing twice over.
#:
#: It is what stops ``"1999-2000"`` (a year range, no month at all) from
#: yielding ``"-2000"``. And it has to mean the *same thing* in both engines:
#: DuckDB's RE2 reads ``\s`` as ``[\t\n\f\r ]``, excluding ``\v`` and every
#: non-ASCII space, where Python's ``re`` includes both. Left as ``\s`` on both
#: sides, ``"1998\xa0Spring"`` gave ``Spring`` from the Python twin and ``''``
#: from the SQL — and since ``validate._text`` collapses whitespace before the
#: efetch side reaches :func:`pub_month`, efetch always renders ``Spring``, so
#: the disagreement surfaced as a ``pub_month`` mismatch. That is a CORE_FIELDS
#: error: the same gating failure the ``_WS`` note below describes for
#: ``<Month>``. :data:`_MEDLINE_SEP_SQL` is the RE2 class that tracks Python's
#: ``\s``.
_MEDLINE_MONTH_RE = re.compile(r"^\s*\d{4}\s+(.+?)\s*$")
_MEDLINE_SEP_SQL = r"[\s\p{Z}\x0B]"
_MEDLINE_MONTH_PATTERN_SQL = (
    rf"^{_MEDLINE_SEP_SQL}*\d{{4}}{_MEDLINE_SEP_SQL}+(.+?){_MEDLINE_SEP_SQL}*$"
)


def _month_from_medline_date(raw: str | None) -> str:
    """The month/season part of a free-text ``MedlineDate``, or ``""``.

    The month-side sibling of :func:`_year_from_medline_date`: ``"1994 Sep-Dec"``
    -> ``"Sep-Dec"``, ``"1998 Spring"`` -> ``"Spring"``. Shapes with no month to
    recover ("1999-2000", "n.d.", "Spring 1998") give ``""`` rather than a guess.

    **Verbatim includes the shapes that look wrong.** ``"1998 Dec-1999 Jan"``
    exports ``pub_month`` as ``"Dec-1999 Jan"`` — a month field carrying a year —
    and that is deliberate: it is what PubMed wrote and what its API renders
    back, so the export mirrors it rather than inventing a tidier ``"Dec-Jan"``
    that no source says. ``validate._VALID_MONTHS`` is a closed set that
    excludes it *on purpose*, so such records stay visible as ``month-format``
    warnings instead of being blessed. Export and checker disagreeing here is
    the design, not a bug — this has been raised as one once already.

    The wart is real, and it is answered by a *different* field rather than by
    tidying this one: PR #17 adds a verbatim ``pub_date`` carrying PubMed's own
    string on every record (``"1998 Dec-1999 Jan"``), the way ``esummary``'s
    ``pubdate`` does. Consumers rendering a citation read that; the three parsed
    fields stay parsed conveniences, and no arrangement of them can represent a
    cross-year range anyway. Whether DocumentMetadataAPI consumers are content
    to receive ``"pub_month": "Dec-1999 Jan"`` alongside it is the one part
    still open, and is tracked in issue #43. If the answer comes back "blank it
    instead", the change is here: gate this fallback on the closed set
    ``validate._VALID_MONTHS`` already accepts, so cross-year shapes return
    ``""`` while ``pub_date`` keeps the whole string.
    """
    if not raw:
        return ""
    match = _MEDLINE_MONTH_RE.match(raw)
    return match.group(1) if match else ""


def pub_month(month: str | None, medline_date: str | None = None) -> str:
    """The exported ``pub_month``, from a raw ``Month``/``Season`` + ``MedlineDate``.

    The Python twin of :data:`_PUB_MONTH_SQL`, pinned to it by
    ``test_pub_month_sql_matches_python``. ``validate`` calls this to normalize
    the efetch side, so a record the export normalizes doesn't read as a
    mismatch against PubMed.
    """
    return normalize_month(month) or normalize_month(_month_from_medline_date(medline_date))


#: **Never bare ``trim()``, and never a fixed character list either.** SQL
#: ``trim(x)`` strips spaces and nothing else, where Python's ``.strip()`` strips
#: all whitespace — so a month carrying a tab gave ``Mar`` from the Python twin
#: and ``''`` from the SQL. ``parse._raw_pubdate`` stores ``findtext("Month")``
#: verbatim, so such a record exported blank *and* was reported as a
#: ``pub_month`` mismatch against efetch, which is a CORE_FIELDS error.
#:
#: The first fix for that named an explicit ASCII set, which closed half the
#: gap and left the other half open: ``.strip()`` is Unicode-aware, so a
#: leading ``\xa0`` or a trailing ``\u2003`` still split the twins. In
#: :data:`_PUB_DATE_SQL` that could flip a *branch* rather than pad a string — a
#: ``medline_date`` of one non-breaking space is truthy to an ASCII trim and
#: falsy to ``.strip()``, so one side emitted the whitespace verbatim while the
#: other assembled the date from its parts.
#:
#: So this is a regex strip over the same class :data:`_MEDLINE_SEP_SQL` uses,
#: and it is the *only* way whitespace is removed below. Verified equal to
#: ``str.strip()`` over the shapes in ``tests/test_export.py``.
_STRIP_CLASS_SQL = r"[\s\p{Z}\x0B]"


def _strip_sql(expr: str) -> str:
    """``str.strip()`` as SQL, over an arbitrary expression."""
    return f"regexp_replace({expr}, '^{_STRIP_CLASS_SQL}+|{_STRIP_CLASS_SQL}+$', '', 'g')"


def normalize_day(raw: str | None) -> str:
    """Strip a leading zero from a numeric day, as NCBI ``esummary`` does.

    PubMed's archival XML zero-pads: PMID:35504184 is
    ``<Year>2022</Year><Month>Aug</Month><Day>01</Day>``, and ``esummary``
    renders that record's ``pubdate`` as ``"2022 Aug 1"``. Emitting
    ``"2022 Aug 01"`` would break the parity :func:`pub_date` exists for, and
    would show up as a ``pub_date`` mismatch on every zero-padded record —
    which is most days below the tenth, not an edge case.

    Only an ASCII digit string is touched, and the ASCII test is the same one
    :func:`normalize_month` uses: ``<Day>`` is nominally numeric but nothing
    enforces it, and a value we cannot read is passed through rather than
    mangled.
    """
    if not raw:
        return ""
    raw = raw.strip()
    if raw.isascii() and raw.isdigit():
        return str(int(raw))
    return raw


def pub_date(
    year: str | None,
    month: str | None,
    day: str | None,
    medline_date: str | None,
) -> str:
    """PubMed's own publication-date string, verbatim where it exists.

    ``pub_year``/``pub_month``/``pub_day`` are best-effort *parsed* fields and
    cannot represent every ``PubDate``: a cross-year range ("1998 Dec-1999 Jan",
    issue #14) has no single month, and splitting it puts a year inside
    ``pub_month``. This field is the fidelity guarantee instead — one string that
    always reproduces what PubMed says, on every record, so a consumer rendering
    a citation never has to reassemble one. Sorting and filtering still use
    ``pub_year``.

    Deliberately identical to NCBI ``esummary``'s ``pubdate``, which solves the
    same problem the same way::

        "1998 Dec-1999 Jan"  (MedlineDate)   -> "1998 Dec-1999 Jan"
        1994 + <Season>Sep-Dec               -> "1994 Sep-Dec"
        2019 + Mar + 15                      -> "2019 Mar 15"
        2019 (year only)                     -> "2019"

    Note the second and first lines converge: PubMed serves the *same* record as
    ``<Year>+<Season>`` from efetch and as a bare ``<MedlineDate>`` in the
    baseline, and both must produce one string — otherwise ``validate`` reads
    every such record as a mismatch. The month is normalized on the assembled
    path for the same reason, and so the output matches NCBI's "2019 Mar 15".

    The Python twin of :data:`_PUB_DATE_SQL`, pinned by
    ``test_pub_date_sql_matches_python``.
    """
    if medline_date and medline_date.strip():
        return medline_date.strip()
    # A day without a month is dropped, not floated up next to the year:
    # `("2019", "13", "15")` would otherwise render "2019 15", which reads as a
    # date to the consumer this field exists for and is not one. `normalize_month`
    # blanks an out-of-range number, and `_MONTH_INPUTS` shows those do reach
    # here. NCBI does the same thing in `sortpubdate` — an unusable component
    # costs precision rather than producing a plausible wrong answer.
    normalized_month = normalize_month(month)
    parts = [(year or "").strip(), normalized_month]
    if normalized_month:
        parts.append(normalize_day(day))
    return " ".join(part for part in parts if part)


_MONTHS_SQL = "[" + ", ".join(f"'{m}'" for m in _MONTH_ABBR) + "]"


def _normalize_month_sql(expr: str) -> str:
    """``normalize_month``'s logic as SQL over an arbitrary month expression.

    Built from the same ``_MONTH_ABBR`` tuple so the abbreviations cannot drift,
    and generated rather than written out because it is applied to *two* sources
    (the column and the ``MedlineDate`` remainder). The shape mirrors the Python:
    numeric months index the list (out-of-range gives NULL, hence the COALESCE),
    textual ones match on their capitalized 3-letter prefix so "March" and "Sept"
    both land, and everything else falls through verbatim.

    ``\\p{L}+`` rather than ``[A-Za-z]+``: Python's ``str.isalpha()`` is
    Unicode-aware, and a non-ASCII spelling must not make the twins disagree.
    """
    stripped = _strip_sql(f"COALESCE({expr}, '')")
    key = f"upper(substr({stripped}, 1, 1)) || lower(substr({stripped}, 2, 2))"
    return f"""CASE
        WHEN {stripped} = '' THEN ''
        WHEN regexp_full_match({stripped}, '[0-9]+')
            THEN COALESCE(list_extract({_MONTHS_SQL}, TRY_CAST({stripped} AS BIGINT)), '')
        WHEN list_contains({_MONTHS_SQL}, {key}) AND regexp_full_match({stripped}, '\\p{{L}}+')
            THEN {key}
        ELSE {stripped}
    END"""


#: ``pub_month``'s logic as SQL: the raw ``Month``/``Season`` column, falling back
#: to the month half of a free-text ``MedlineDate``. ``validate`` calls the
#: *Python* twin to normalize the efetch side, so
#: ``test_pub_month_sql_matches_python`` pins the two together.
_MEDLINE_MONTH_SQL = (
    f"regexp_extract(COALESCE(la.medline_date, ''), '{_MEDLINE_MONTH_PATTERN_SQL}', 1)"
)
_PUB_MONTH_SQL = (
    f"COALESCE(NULLIF({_normalize_month_sql('la.pub_month')}, ''), "
    f"{_normalize_month_sql(_MEDLINE_MONTH_SQL)})"
)

#: The month for the assembled branch. ``_normalize_month_sql('la.pub_month')``
#: rather than :data:`_PUB_MONTH_SQL`, which would fold the ``MedlineDate`` back
#: in — *not* because that would double-count anything (the two are provably
#: equal on this branch, since it only runs when the ``MedlineDate`` is blank
#: and a blank one contributes ``''``), but because naming the column directly
#: says what this branch means and skips a ``regexp_extract`` that can only ever
#: return ``''`` here. An earlier comment claimed a double-count hazard; there
#: is none, and chasing it would waste a reader's time.
_MONTH_FOR_DATE_SQL = _normalize_month_sql("la.pub_month")

#: NULL, not ``''``, when the month blanked — ``concat_ws`` skips NULLs, so the
#: day disappears rather than sitting beside the year and reading as a date.
#: ``normalize_day`` as SQL: a leading zero comes off an ASCII numeric day, so
#: ``<Day>01</Day>`` renders ``1`` the way ``esummary`` does. ``TRY_CAST`` is the
#: ASCII-digit test and the strip in one — it returns NULL for anything that is
#: not a plain integer, which is exactly the "pass it through" case.
_STRIPPED_DAY_SQL = _strip_sql("COALESCE(la.pub_day, '')")
_NORMALIZED_DAY_SQL = (
    f"CASE WHEN regexp_full_match({_STRIPPED_DAY_SQL}, '[0-9]+') "
    f"THEN CAST(CAST({_STRIPPED_DAY_SQL} AS BIGINT) AS VARCHAR) "
    f"ELSE {_STRIPPED_DAY_SQL} END"
)
_DAY_FOR_DATE_SQL = (
    f"CASE WHEN {_MONTH_FOR_DATE_SQL} = '' THEN NULL ELSE NULLIF({_NORMALIZED_DAY_SQL}, '') END"
)

#: ``pub_date``'s logic as SQL. ``concat_ws`` skips NULLs, which is what the
#: ``NULLIF(..., '')`` wrappers are for — an absent month must not leave a
#: double space behind.
#:
#: Whitespace goes through :func:`_strip_sql`, never ``trim``. It matters more
#: here than anywhere else: this expression *branches* on whether the
#: ``medline_date`` is blank, so a value of one non-breaking space that an
#: ASCII trim calls non-empty and ``.strip()`` calls empty does not pad a
#: string — it makes the two implementations emit entirely different dates.
_STRIPPED_YEAR_SQL = _strip_sql("COALESCE(la.pub_year, '')")
_ASSEMBLED_DATE_SQL = _strip_sql(
    "concat_ws(' ', "
    f"NULLIF({_STRIPPED_YEAR_SQL}, ''), "
    f"NULLIF({_MONTH_FOR_DATE_SQL}, ''), "
    f"{_DAY_FOR_DATE_SQL})"
)
_PUB_DATE_SQL = f"""CASE
        WHEN {_strip_sql("COALESCE(la.medline_date, '')")} <> ''
            THEN {_strip_sql("la.medline_date")}
        ELSE {_ASSEMBLED_DATE_SQL}
    END"""

#: ``_year_from_medline_date``'s fallback, same pattern, same pinning test
#: (``test_pub_year_sql_matches_year_from_medline_date``). ``regexp_extract``
#: returns ``''`` when nothing matches, which is already the spec's value.
#: The leading-whitespace class has to be the RE2 one here too, for the same
#: reason :data:`_MEDLINE_SEP_SQL` does: Python's ``^\s*`` skips a leading
#: ``\xa0`` and RE2's does not, so such a record recovered its year in
#: ``validate`` and exported blank.
_MEDLINE_YEAR_PATTERN_SQL = rf"^{_STRIP_CLASS_SQL}*(\d{{4}})"
_PUB_YEAR_SQL = (
    "COALESCE(NULLIF(la.pub_year, ''), "
    f"regexp_extract(COALESCE(la.medline_date, ''), '{_MEDLINE_YEAR_PATTERN_SQL}', 1))"
)

#: The JSON record: output field name -> SQL expression, in emitted order.
#:
#: This is the **single definition** of what a document contains. The export is
#: one ``COPY ... (FORMAT JSON)`` built from this list rather than a Python loop
#: over rows, so there is no second place where the DocumentMetadataAPI names,
#: the empty-string-not-null rule, or the field order is written down;
#: ``validate`` imports :data:`JSON_FIELDS` instead of restating them.
_JSON_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "'PMID:' || la.pmid"),
    # The LEFT JOIN yields NULL, not an empty list, for a PMID with neither a
    # DOI nor a PMCID; such a record still gets its own PMID CURIE.
    ("identifiers", "list_prepend('PMID:' || la.pmid, COALESCE(ids.identifiers, []::VARCHAR[]))"),
    ("journal_name", "COALESCE(j.title, '')"),
    # J_Entrez.txt's IsoAbbr equalled MedAbbr in 42,056 of 42,056 journals, so
    # data model 1.0 keeps only the one column (#48).
    ("journal_abbrev", "COALESCE(j.abbreviation_medline, '')"),
    ("article_title", "COALESCE(la.article_title, '')"),
    ("volume", "COALESCE(la.volume, '')"),
    ("issue", "COALESCE(la.issue, '')"),
    # Falls back to the year inside a free-text MedlineDate, which is the only
    # place these records carry one. See _year_from_medline_date.
    ("pub_year", _PUB_YEAR_SQL),
    # Approximate months are passed through, not blanked: the spec's own example
    # for PMID:8000234 emits "Sep-Dec" (issue #14). See normalize_month.
    ("pub_month", _PUB_MONTH_SQL),
    ("pub_day", "COALESCE(la.pub_day, '')"),
    # PubMed's own date string, verbatim -- the three fields above are parsed
    # conveniences and cannot represent a cross-year range. See pub_date.
    ("pub_date", _PUB_DATE_SQL),
    # [{"id": "MESH:D016454", "name": "Review"}, ...] in PubMed's order, which
    # is esummary's `pubtype` order. Our own field, like `identifiers`; a
    # record with none gets [], never null.
    (
        "publication_types",
        "COALESCE(pts.publication_types, []::STRUCT(id VARCHAR, name VARCHAR)[])",
    ),
    ("abstract", "COALESCE(abs.abstract, '')"),
)

#: The exported field names, in order — an external contract with Node Annotator
#: / ElasticSearch, and what ``validate`` checks every record against.
JSON_FIELDS = tuple(name for name, _ in _JSON_FIELDS)

#: Abstract section ``label``s ("BACKGROUND", "METHODS", ...) are deliberately
#: dropped: the consumer is a full-text search index, which wants prose, not
#: headings.
#:
#: Joining `article_id` on (pmid, source_file) — the same key `abs` uses —
#: confines the identifiers to the article's *latest* version, so a DOI or PMCID
#: that only ever appeared on a superseded version does not leak into the export.
#:
#: **No ORDER BY.** Sorting 40.9M rows by PMID cost ~3 minutes before the first
#: row could be written and was the export's peak-memory event (issue #8); shard
#: membership no longer depends on scan order, since each writer thread owns a
#: file. Nothing downstream consumes the order — the ingest is an ElasticSearch
#: bulk load, and `validate` sorts its own PMID manifest.
_LATEST_METADATA_SQL = f"""
WITH abs AS (
    SELECT pmid, source_file, string_agg(text, ' ' ORDER BY position) AS abstract
    FROM abstract_text a
    -- Restrict to the latest versions *before* aggregating: without this the
    -- group-by spans the entire version history and throws the superseded
    -- rows away in the join below, which dominates the export's peak RSS.
    WHERE EXISTS (
        SELECT 1 FROM _latest_snapshot la
        WHERE la.pmid = a.pmid AND la.source_file = a.source_file
    )
    GROUP BY pmid, source_file
),
ids AS (
    SELECT pmid, source_file, list_sort(list_distinct(list(
        {_ID_CURIE_SQL} || id_value
    ))) AS identifiers
    FROM article_id ai
    WHERE id_type IN ({_ID_TYPES_SQL})
      -- Same reason as `abs` above: restrict before aggregating, so the
      -- group-by does not span the whole version history.
      AND EXISTS (
          SELECT 1 FROM _latest_snapshot la
          WHERE la.pmid = ai.pmid AND la.source_file = ai.source_file
      )
    GROUP BY pmid, source_file
),
pts AS (
    SELECT pmid, source_file, list(
        {{'id': '{MESH_PREFIX}:' || type_ui, 'name': COALESCE(type_name, '')}}
        ORDER BY position
    ) AS publication_types
    FROM publication_type pt
    WHERE EXISTS (
        SELECT 1 FROM _latest_snapshot la
        WHERE la.pmid = pt.pmid AND la.source_file = pt.source_file
    )
    GROUP BY pmid, source_file
)
SELECT
    PROJECTION
FROM _latest_snapshot la
LEFT JOIN journal j ON la.nlm_catalog_id = j.nlm_catalog_id
LEFT JOIN abs ON abs.pmid = la.pmid AND abs.source_file = la.source_file
LEFT JOIN ids ON ids.pmid = la.pmid AND ids.source_file = la.source_file
LEFT JOIN pts ON pts.pmid = la.pmid AND pts.source_file = la.source_file
""".replace(
    "PROJECTION",
    ",\n    ".join(f'{expr} AS "{name}"' for name, expr in _JSON_FIELDS),
)


def _remove_finished_mark(out_dir: Path, overwrite: bool) -> None:
    """Delete a previous export's ``metadata.json`` and ``SHA256SUMS`` before writing.

    ``metadata.json`` is the "export finished" mark. Left in place, an export
    that dies after removing the old files would still look finished, with
    checksums for files that are gone or have been replaced. Remove the mark
    first, so a directory without one is one whose export never completed.

    It is also why a finished export is refused unless ``overwrite``: this
    export is about to delete that one's files, the only local copy of its
    build until ``scripts/archive_build.py`` has moved it (#24). A partial
    export, with no mark, is swept without asking. The check is here rather
    than in ``04-export.sbatch`` so every caller gets it.
    """
    if not overwrite and (out_dir / METADATA_FILE).exists():
        raise FileExistsError(
            f"{out_dir} holds a finished export ({METADATA_FILE} exists). "
            "Archive it first (scripts/archive_build.py), "
            "or pass --overwrite (EXPORT_OVERWRITE=1 under Slurm) to replace it."
        )
    for name in (METADATA_FILE, CHECKSUMS_FILE):
        (out_dir / name).unlink(missing_ok=True)


def _shard_paths(out_dir: Path) -> list[Path]:
    """The shard files this export owns, sorted — its output and its leftovers."""
    return sorted(
        p for p in out_dir.glob(f"{_SHARD_STEM}*") if p.name.endswith((".ndjson", ".ndjson.gz"))
    )


def _log_writing_progress(out_dir: Path, start: float, temp_dir: str | None = None) -> None:
    """Log how much shard output exists so far, memory, and DuckDB's spill size.

    RSS alone looked healthy on the run that failed (#42): the node refused a
    reservation at 68 GiB RSS under strict overcommit, so VSZ and the node's
    commit charge are logged beside it, and the spill directory's size shows
    when the buffer pool is full enough to be writing to disk.
    """
    paths = _shard_paths(out_dir)
    written = size_bytes(paths)
    elapsed = time.monotonic() - start
    logger.info(
        "writing: %.1f GiB across %d shard(s) · %.0f MiB/s · elapsed %s · %s · spill %.1f GiB",
        written / 1024**3,
        len(paths),
        written / 1024**2 / elapsed if elapsed else 0.0,
        fmt_duration(elapsed),
        memory_summary(),
        dir_size_gib(temp_dir),
    )


def _log_while_writing(
    out_dir: Path, stop: threading.Event, start: float, temp_dir: str | None = None
) -> None:
    """Log progress every ``_PROGRESS_INTERVAL_S`` until ``stop`` is set.

    The whole export is now a single ``COPY``, so there are no batches to log
    between — but this is the job that gets OOM-killed, and a silent process is
    exactly what left the last run's ``--mem`` a guess. DuckDB releases the GIL
    while the statement runs, so this thread does get scheduled.

    Bytes and memory, no ETA — the total output size is not known until it has been
    written. If a run ever needs one, split the COPY into PMID ranges and count
    rows per range.

    Nothing here is allowed to escape. An exception would kill this thread and
    leave a 20-minute export completely silent, which is the failure the
    heartbeat exists to prevent; logging the reason and carrying on is strictly
    better than that.
    """
    while not stop.wait(_PROGRESS_INTERVAL_S):
        try:
            _log_writing_progress(out_dir, start, temp_dir)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("could not log export progress: %s", exc)


#: Settings for the export's two corpus-sized statements. Insertion order is
#: free to drop: the query has no ORDER BY, each writer thread already owns its
#: own file, and nothing downstream reads the order (see _LATEST_METADATA_SQL).
#: Keeping it makes DuckDB buffer rows to emit them in scan order, and its own
#: out-of-memory message suggests turning it off. What it saves at corpus scale
#: is unmeasured: it was on in both the export refused on 2026-11-09 and the
#: one that finished, which differed only in the hold below (#42).
_UNORDERED = {"preserve_insertion_order": False}

#: Stop DuckDB telling jemalloc to hand memory back to the kernel mid-export
#: (#42). By default it flushes after any task that peaked above 128 MiB and
#: after any single deallocation above 512 MiB, both of which the snapshot's
#: hash tables and a spill trigger. On ht1's nodes (vm.overcommit_memory=2)
#: jemalloc returns memory by remapping it PROT_NONE, which splits mappings,
#: and the suspect for two exports refused by mmap at ~68 GiB RSS is the
#: process running out of them (vm.max_map_count). Held instead, the freed
#: memory stays mapped for jemalloc to reuse. 04-export.sbatch turns off
#: jemalloc's own time-based release to match (EXPORT_JE_MALLOC_CONF). With
#: both, the 2026-11-09 export finished, but RSS plateaued ~55 GiB above
#: memory_limit, which does not count held memory: --mem has to cover it. The
#: suspect is still unconfirmed, and #80 plans the runs that would settle it;
#: docs/Memory.md, "A refused allocation", has the figures.
_HOLD_ALLOCATOR = {
    "allocator_flush_threshold": "1TB",
    "allocator_bulk_deallocation_flush_threshold": "1TB",
}

#: Everything the snapshot and the COPY run under.
_HEAVY_STATEMENT_SETTINGS = {**_UNORDERED, **_HOLD_ALLOCATOR}


def _sql_setting_value(value: bool | int | str) -> str:
    """Render a setting's value for ``SET``: booleans lower-case, strings quoted."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


@contextlib.contextmanager
def _duckdb_settings(con: duckdb.DuckDBPyConnection, **values: bool | int | str):
    """``SET`` each setting for the duration of the block, then restore it.

    Restores on an error too, so a failed export does not leave the connection
    single-threaded or unordered for whatever the caller runs next. Every
    setting is attempted even if one fails to restore, which after a refused
    allocation it can. A failed restore is logged and, when the block itself
    raised, does not replace that error: the caller is owed the out-of-memory
    error, not whichever ``SET`` tripped over it.
    """
    names = list(values)
    previous = con.execute(
        "SELECT " + ", ".join(f"current_setting('{name}')" for name in names)
    ).fetchone()
    succeeded = False
    try:
        for name, value in values.items():
            con.execute(f"SET {name} = {_sql_setting_value(value)}")
        yield
        succeeded = True
    finally:
        failed = None
        for name, value in zip(names, previous, strict=True):
            try:
                con.execute(f"SET {name} = {_sql_setting_value(value)}")
            except duckdb.Error as exc:
                logger.warning("could not restore %s to %r: %s", name, value, exc)
                failed = failed or exc
        if succeeded and failed is not None:
            raise failed


@contextlib.contextmanager
def _logging_out_of_memory(temp_dir: str | None):
    """Log the memory figures when DuckDB runs out, then let the error propagate.

    The heartbeat samples once a minute, so without this the last figures
    before a failure can be a minute stale (#42). Nest it *inside*
    ``_duckdb_settings``, so it logs before the settings are restored rather
    than after. These are read after DuckDB
    has unwound the statement, so its spill files may already be gone and RSS
    may have fallen; VSZ, the node's commit charge and the mapping count fall
    less readily, and are the figures that explain a ``(bad allocation)``.
    """
    try:
        yield
    except duckdb.OutOfMemoryException as exc:
        refused = "bad allocation" in str(exc)
        logger.error(
            "DuckDB ran out of memory: %s · spill %.1f GiB · peak RSS %.1f GiB%s",
            memory_summary(),
            dir_size_gib(temp_dir),
            peak_rss_gib(),
            (
                ". '(bad allocation)' is mmap failing, not DuckDB's memory_limit: "
                "compare the node commit with its limit and the maps with "
                "vm.max_map_count (docs/Memory.md, 'A refused allocation')"
            )
            if refused
            else "",
        )
        raise


def export_json(
    con: duckdb.DuckDBPyConnection,
    out_dir: str | Path,
    *,
    shards: int | None = None,
    gzip_output: bool = True,
    overwrite: bool = False,
) -> list[Path]:
    """Export the latest version of every abstract as sharded NDJSON.

    Each line is one document keyed by ``PMID:<id>`` using DocumentMetadataAPI
    field names. Shards are gzipped as they are written (one pass, no separate
    re-read of the finished file) unless ``gzip_output=False``: NDJSON
    compresses ~4-5x, which is the difference between shipping ~52 GiB and
    ~12 GiB of a full corpus, and `validate` accepts either form without being
    told which. The output stays line-readable via ``zcat``. Returns the list of
    files written.

    DuckDB writes the JSON itself (``COPY ... (FORMAT JSON)``), one file per
    writer thread, rather than Python serializing row by row — the serialization
    then runs in C++ across every thread instead of on one core, which is where
    this export used to spend ~80% of its wall time. ``shards`` caps the number
    of writer threads, and so the number of files; the default is DuckDB's own
    thread count (see the group-level ``--threads``). A small dataset can be
    written by fewer threads than asked for, so it is a **maximum**, not a
    guarantee.
    """
    if shards is not None and shards < 1:
        raise ValueError("shards must be >= 1")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _remove_finished_mark(out_dir, overwrite)

    # Clock the whole export, not just the COPY: slurm/README.md tells operators
    # to size --time from the line this feeds. The snapshot below is cheap (17s
    # of a 15m full-corpus run on ht1, 2026-11-09); the COPY's join and
    # string_agg are the expensive part.
    start = time.monotonic()

    # Read *before* the heartbeat starts: the thread is non-daemon and only
    # stops when `stop` is set in the finally below, so a statement that raises
    # between start() and the try would hang the interpreter instead of
    # reporting the error.
    temp_dir = con.execute("SELECT current_setting('temp_directory')").fetchone()[0]

    # Materialize the latest-version snapshot once: `latest_article` is a window
    # function over the full `article` table, and both the count and the export
    # query below would otherwise recompute it.
    with _duckdb_settings(con, **_HEAVY_STATEMENT_SETTINGS), _logging_out_of_memory(temp_dir):
        con.execute("CREATE OR REPLACE TEMP TABLE _latest_snapshot AS SELECT * FROM latest_article")
    total = con.execute("SELECT count(*) FROM _latest_snapshot").fetchone()[0]
    logger.info(
        "starting JSON export: %d document(s) to %s in %s%s",
        total,
        f"at most {shards} shard(s)" if shards else "one shard per thread",
        out_dir,
        " (gzip)" if gzip_output else "",
    )

    # DuckDB appends to a per-thread output directory rather than clearing it,
    # so a previous run's shards would survive this one and be read back as if
    # they were part of it. Only files matching our own naming are removed --
    # and the count is logged, because silently deleting someone's export
    # directory is not something to do without a line in the log.
    stale = _shard_paths(out_dir)
    for path in stale:
        path.unlink()
    if stale:
        logger.info("removed %d shard file(s) from a previous export", len(stale))

    suffix = "ndjson.gz" if gzip_output else "ndjson"
    options = [
        "FORMAT JSON",
        "PER_THREAD_OUTPUT true",
        "OVERWRITE_OR_IGNORE true",
        f"FILENAME_PATTERN '{_SHARD_STEM}{{i}}'",
        f"FILE_EXTENSION '{suffix}'",
    ]
    if gzip_output:
        options.append("COMPRESSION 'gzip'")
    # Escape single quotes so a path containing one (an apostrophe in a
    # directory name) doesn't break out of the quoted string literal.
    escaped_dir = out_dir.as_posix().replace("'", "''")
    copy_sql = f"COPY ({_LATEST_METADATA_SQL}) TO '{escaped_dir}' ({', '.join(options)})"

    stop = threading.Event()
    heartbeat = threading.Thread(target=_log_while_writing, args=(out_dir, stop, start, temp_dir))
    heartbeat.start()
    # `shards` is the thread cap for the COPY only: one file per writer thread
    # is what PER_THREAD_OUTPUT gives us, so asking for N shards is asking N
    # threads to write.
    copy_settings = dict(_HEAVY_STATEMENT_SETTINGS)
    if shards is not None:
        copy_settings["threads"] = int(shards)
    try:
        with _duckdb_settings(con, **copy_settings), _logging_out_of_memory(temp_dir):
            con.execute(copy_sql)
    finally:
        stop.set()
        heartbeat.join()

    paths = _shard_paths(out_dir)
    write_export_metadata(con, out_dir, paths, records=total)
    logger.info(
        "exported %d documents to %d shard(s) in %s in %s (peak RSS %.1f GiB)",
        total,
        len(paths),
        out_dir,
        fmt_duration(time.monotonic() - start),
        peak_rss_gib(),
    )
    return paths


def file_sha256(path: Path) -> str:
    """SHA-256 of a file, as hex."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_export_metadata(
    con: duckdb.DuckDBPyConnection,
    out_dir: Path,
    files: list[Path],
    *,
    records: int,
) -> Path:
    """Write ``metadata.json`` and ``SHA256SUMS``: the export's version, provenance and checksums.

    An export travels without its database, so this is where a consumer learns
    which data model it follows (``data_model_version``; check the major), what
    corpus it holds (``corpus``: the cutoff file and when NCBI posted it, #57),
    how many current articles it holds (``records``), and each file's size and
    SHA-256. ``validate``
    checks the directory against it. ``SHA256SUMS`` repeats the hashes for
    ``sha256sum -c``. Both are written last, after every file they describe,
    and ``metadata.json`` after ``SHA256SUMS``, so its presence means the export
    finished.

    The hashes are for *identity*, not damage. A gzip shard that is truncated or
    corrupted already fails to decompress: DuckDB writes each shard as one gzip
    member, so a cut-off file raises ``EOFError`` and a flipped bit fails the
    CRC (measured, 2026-10-19). What only a hash catches is a shard that is
    intact but is not the one this export wrote, such as a leftover from an
    earlier build, which Babel's docs warn about. A hash also guards a
    ``--no-gzip`` export, which has no CRC at all.
    """
    from .status import corpus_provenance

    hashes = {path.name: file_sha256(path) for path in sorted(files)}
    (out_dir / CHECKSUMS_FILE).write_text(
        "".join(f"{digest}  {name}\n" for name, digest in hashes.items())
    )
    content = {
        "data_model_version": datamodel.DATA_MODEL_VERSION,
        "format": "ndjson",
        "records": records,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generated_by": datamodel.code_version(),
        "database": datamodel.metadata(con),
        "corpus": corpus_provenance(con),
        "files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": hashes[path.name]}
            for path in sorted(files)
        ],
    }
    path = out_dir / METADATA_FILE
    path.write_text(json.dumps(content, indent=2) + "\n")
    return path
