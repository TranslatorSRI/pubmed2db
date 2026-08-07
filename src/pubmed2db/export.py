"""Export the latest version of every abstract to JSON or Parquet.

JSON export follows the NCATS Translator DocumentMetadataAPI field names (which
differ from the PubMed names used in the database) and emits empty strings, not
nulls, for absent values. Parquet export keeps PubMed's own field names.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import duckdb

from .util import current_rss_gib, eta_str, fmt_duration, peak_rss_gib

#: Shard filename stem; DuckDB appends the writer-thread index and the extension.
_SHARD_STEM = "pubmed_metadata_"

logger = logging.getLogger(__name__)

#: Minimum gap between progress log lines, so large exports don't spam the log.
#: A full corpus export runs ~15 min, so a minute between lines still gives a
#: dozen-odd data points to watch RSS and the rate on.
_PROGRESS_INTERVAL_S = 60.0

#: Child tables whose rows belong to a specific article version (pmid, source_file).
_VERSIONED_CHILDREN = (
    "abstract_text",
    "author",
    "author_affiliation",
    "mesh_heading",
    "mesh_qualifier",
    "publication_type",
    "grant_",
    "article_id",
    "history",
)

#: Dimension/bookkeeping tables exported as-is.
_OTHER_TABLES = ("journal", "journal_issn", "source_file", "deleted_pmid")

#: PubMed ``ArticleId/@IdType`` -> CURIE prefix for the JSON ``identifiers``
#: field. The casing matches Babel's ``src/prefixes.py`` (``DOI = "doi"``,
#: ``PMC = "PMC"``, ``PMID = "PMID"``) so our CURIEs join against the Babel
#: publication compendium; ``DOI:`` would not. PubMed's ``pmc`` values already
#: start with ``PMC``, hence the doubled ``PMC:PMC1234567``. Values are emitted
#: verbatim, so consumers must match case-insensitively (DOIs are
#: case-insensitive per spec and PubMed is not consistent).
ID_PREFIXES = {"doi": "doi", "pmc": "PMC"}


#: Month abbreviations, frozen rather than taken from ``calendar.month_abbr``:
#: that is ``strftime('%b')`` under ``LC_TIME``, so any dependency calling
#: ``locale.setlocale`` would silently localize a spec-defined output field.
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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
    if raw.isdigit():
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
#: load-bearing: it is what stops ``"1999-2000"`` (a year range, no month at all)
#: from yielding ``"-2000"``.
_MEDLINE_MONTH_RE = re.compile(r"^\s*\d{4}\s+(.+?)\s*$")


def _month_from_medline_date(raw: str | None) -> str:
    """The month/season part of a free-text ``MedlineDate``, or ``""``.

    The month-side sibling of :func:`_year_from_medline_date`: ``"1994 Sep-Dec"``
    -> ``"Sep-Dec"``, ``"1998 Spring"`` -> ``"Spring"``. Shapes with no month to
    recover ("1999-2000", "n.d.", "Spring 1998") give ``""`` rather than a guess.
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
    parts = ((year or "").strip(), normalize_month(month), (day or "").strip())
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
    key = f"upper(substr(trim({expr}), 1, 1)) || lower(substr(trim({expr}), 2, 2))"
    return f"""CASE
        WHEN trim(COALESCE({expr}, '')) = '' THEN ''
        WHEN regexp_full_match(trim({expr}), '[0-9]+')
            THEN COALESCE(list_extract({_MONTHS_SQL}, TRY_CAST(trim({expr}) AS BIGINT)), '')
        WHEN list_contains({_MONTHS_SQL}, {key}) AND regexp_full_match(trim({expr}), '\\p{{L}}+')
            THEN {key}
        ELSE trim({expr})
    END"""


#: ``pub_month``'s logic as SQL: the raw ``Month``/``Season`` column, falling back
#: to the month half of a free-text ``MedlineDate``. ``validate`` calls the
#: *Python* twin to normalize the efetch side, so
#: ``test_pub_month_sql_matches_python`` pins the two together.
_MEDLINE_MONTH_SQL = (
    f"regexp_extract(COALESCE(la.medline_date, ''), '{_MEDLINE_MONTH_RE.pattern}', 1)"
)
_PUB_MONTH_SQL = (
    f"COALESCE(NULLIF({_normalize_month_sql('la.pub_month')}, ''), "
    f"{_normalize_month_sql(_MEDLINE_MONTH_SQL)})"
)

#: ``pub_date``'s logic as SQL. Note ``_normalize_month_sql`` is applied to
#: ``la.pub_month`` *alone*, not to ``_PUB_MONTH_SQL``: the latter already folds
#: the ``MedlineDate`` back in, which would double-count it on the branch that
#: only runs when there is no ``MedlineDate``. ``concat_ws`` skips NULLs, which
#: is what the ``NULLIF(..., '')`` wrappers are for — an absent month must not
#: leave a double space behind.
_PUB_DATE_SQL = f"""CASE
        WHEN trim(COALESCE(la.medline_date, '')) <> '' THEN trim(la.medline_date)
        ELSE trim(concat_ws(' ',
            NULLIF(trim(COALESCE(la.pub_year, '')), ''),
            NULLIF({_normalize_month_sql('la.pub_month')}, ''),
            NULLIF(trim(COALESCE(la.pub_day, '')), '')))
    END"""

#: ``_year_from_medline_date``'s fallback, same pattern, same pinning test
#: (``test_pub_year_sql_matches_year_from_medline_date``). ``regexp_extract``
#: returns ``''`` when nothing matches, which is already the spec's value.
_PUB_YEAR_SQL = (
    "COALESCE(NULLIF(la.pub_year, ''), "
    f"regexp_extract(COALESCE(la.medline_date, ''), '{_MEDLINE_YEAR_RE.pattern}', 1))"
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
    ("journal_abbrev", "COALESCE(j.abbreviation_iso, j.abbreviation_medline, '')"),
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
_LATEST_METADATA_SQL = """
WITH abs AS (
    SELECT pmid, source_file, string_agg(text, ' ' ORDER BY seq) AS abstract
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
        CASE id_type WHEN 'doi' THEN 'doi:' ELSE 'PMC:' END || id_value
    ))) AS identifiers
    FROM article_id ai
    WHERE id_type IN ('doi', 'pmc')
      -- Same reason as `abs` above: restrict before aggregating, so the
      -- group-by does not span the whole version history.
      AND EXISTS (
          SELECT 1 FROM _latest_snapshot la
          WHERE la.pmid = ai.pmid AND la.source_file = ai.source_file
      )
    GROUP BY pmid, source_file
)
SELECT
    PROJECTION
FROM _latest_snapshot la
LEFT JOIN journal j ON la.nlm_catalog_id = j.nlm_catalog_id
LEFT JOIN abs ON abs.pmid = la.pmid AND abs.source_file = la.source_file
LEFT JOIN ids ON ids.pmid = la.pmid AND ids.source_file = la.source_file
""".replace(
    "PROJECTION",
    ",\n    ".join(f'{expr} AS "{name}"' for name, expr in _JSON_FIELDS),
)


def _shard_paths(out_dir: Path) -> list[Path]:
    """The shard files this export owns, sorted — its output and its leftovers."""
    return sorted(
        p for p in out_dir.glob(f"{_SHARD_STEM}*")
        if p.name.endswith((".ndjson", ".ndjson.gz"))
    )


def _log_writing_progress(out_dir: Path, start: float) -> None:
    """Log how much shard output exists so far, and current RSS."""
    paths = _shard_paths(out_dir)
    written = sum(p.stat().st_size for p in paths)
    elapsed = time.monotonic() - start
    current = current_rss_gib()
    logger.info(
        "writing: %.1f GiB across %d shard(s) · %.0f MiB/s · elapsed %s · RSS %s",
        written / 1024**3, len(paths),
        written / 1024**2 / elapsed if elapsed else 0.0,
        fmt_duration(elapsed),
        "n/a" if current is None else f"{current:.1f} GiB",
    )


def _log_while_writing(out_dir: Path, stop: threading.Event, start: float) -> None:
    """Log progress every ``_PROGRESS_INTERVAL_S`` until ``stop`` is set.

    The whole export is now a single ``COPY``, so there are no batches to log
    between — but this is the job that gets OOM-killed, and a silent process is
    exactly what left the last run's ``--mem`` a guess. DuckDB releases the GIL
    while the statement runs, so this thread does get scheduled.

    ponytail: bytes and RSS, no ETA — the total output size is not known until
    it has been written. If a run ever needs one, split the COPY into PMID
    ranges and count rows per range.
    """
    while not stop.wait(_PROGRESS_INTERVAL_S):
        _log_writing_progress(out_dir, start)


def export_json(
    con: duckdb.DuckDBPyConnection,
    out_dir: str | Path,
    *,
    shards: int | None = None,
    gzip_output: bool = False,
) -> list[Path]:
    """Export the latest version of every abstract as sharded NDJSON.

    Each line is one document keyed by ``PMID:<id>`` using DocumentMetadataAPI
    field names. With ``gzip_output=True`` each shard is compressed as it is
    written (one pass, no separate re-read of the finished file), and the
    output is still line-readable via ``zcat``. Returns the list of files
    written.

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

    # Materialize the latest-version snapshot once: `latest_article` is a window
    # function over the full `article` table, and both the count and the export
    # query below would otherwise recompute it.
    con.execute("CREATE OR REPLACE TEMP TABLE _latest_snapshot AS SELECT * FROM latest_article")
    total = con.execute("SELECT count(*) FROM _latest_snapshot").fetchone()[0]
    logger.info(
        "starting JSON export: %d document(s) to %s in %s%s",
        total, f"at most {shards} shard(s)" if shards else "one shard per thread",
        out_dir, " (gzip)" if gzip_output else "",
    )

    # DuckDB appends to a per-thread output directory rather than clearing it,
    # so a previous run's shards would survive this one and be read back as if
    # they were part of it. Only files matching our own naming are removed.
    for stale in _shard_paths(out_dir):
        stale.unlink()

    suffix = "ndjson.gz" if gzip_output else "ndjson"
    options = [
        "FORMAT JSON", "PER_THREAD_OUTPUT true", "OVERWRITE_OR_IGNORE true",
        f"FILENAME_PATTERN '{_SHARD_STEM}{{i}}'", f"FILE_EXTENSION '{suffix}'",
    ]
    if gzip_output:
        options.append("COMPRESSION 'gzip'")
    # Escape single quotes so a path containing one doesn't break out of the
    # quoted string literal (same reason as _copy_parquet).
    escaped_dir = out_dir.as_posix().replace("'", "''")
    copy_sql = f"COPY ({_LATEST_METADATA_SQL}) TO '{escaped_dir}' ({', '.join(options)})"

    start = time.monotonic()
    stop = threading.Event()
    heartbeat = threading.Thread(target=_log_while_writing, args=(out_dir, stop, start))
    heartbeat.start()
    # `shards` is the thread cap for this statement only: one file per writer
    # thread is what PER_THREAD_OUTPUT gives us, so asking for N shards is
    # asking N threads to write. Restore whatever the connection had after.
    previous_threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
    try:
        if shards is not None:
            con.execute(f"SET threads = {int(shards)}")
        con.execute(copy_sql)
    finally:
        stop.set()
        heartbeat.join()
        if shards is not None:
            con.execute(f"SET threads = {int(previous_threads)}")

    paths = _shard_paths(out_dir)
    logger.info(
        "exported %d documents to %d shard(s) in %s in %s (peak RSS %.1f GiB)",
        total, len(paths), out_dir, fmt_duration(time.monotonic() - start), peak_rss_gib(),
    )
    return paths


def _copy_parquet(con: duckdb.DuckDBPyConnection, query: str, path: Path) -> None:
    # Escape single quotes so a path containing one (e.g. an apostrophe in a
    # directory name) doesn't break out of the quoted string literal.
    escaped_path = path.as_posix().replace("'", "''")
    con.execute(f"COPY ({query}) TO '{escaped_path}' (FORMAT PARQUET)")


def export_parquet(
    con: duckdb.DuckDBPyConnection,
    out_dir: str | Path,
    *,
    latest: bool = True,
) -> list[Path]:
    """Export the database to one Parquet file per table.

    With ``latest=True`` (default) only the latest non-deleted version of each
    article and its children are exported; with ``latest=False`` the full
    version history is exported. Returns the list of files written.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    tables = 1 + len(_VERSIONED_CHILDREN) + len(_OTHER_TABLES)
    logger.info(
        "starting Parquet export: %d table(s) (%s) to %s",
        tables, "latest version" if latest else "full history", out_dir,
    )
    run_start = time.monotonic()

    def _progress(done: int) -> None:
        remaining = tables - done
        elapsed = time.monotonic() - run_start
        eta = eta_str(elapsed, done, remaining)
        logger.info("progress: %d/%d tables, ~%s remaining", done, tables, eta)

    if latest:
        # Materialize once: `latest_article` is a window function over the full
        # `article` table, and it would otherwise be recomputed for the article
        # export itself plus once per child table's EXISTS below.
        con.execute("CREATE OR REPLACE TEMP TABLE _latest_snapshot AS SELECT * FROM latest_article")
        article_query = "SELECT * FROM _latest_snapshot"
    else:
        article_query = "SELECT * FROM article"
    article_path = out_dir / "article.parquet"
    _copy_parquet(con, article_query, article_path)
    written.append(article_path)
    _progress(len(written))

    for table in _VERSIONED_CHILDREN:
        if latest:
            query = (
                f"SELECT c.* FROM {table} c "
                "WHERE EXISTS (SELECT 1 FROM _latest_snapshot la "
                "WHERE la.pmid = c.pmid AND la.source_file = c.source_file)"
            )
        else:
            query = f"SELECT * FROM {table}"
        path = out_dir / f"{table}.parquet"
        _copy_parquet(con, query, path)
        written.append(path)
        _progress(len(written))

    # Dimension / bookkeeping tables are exported in full regardless of `latest`.
    for table in _OTHER_TABLES:
        path = out_dir / f"{table}.parquet"
        _copy_parquet(con, f"SELECT * FROM {table}", path)
        written.append(path)
        _progress(len(written))

    logger.info(
        "exported %d Parquet file(s) to %s (peak RSS %.1f GiB)",
        len(written), out_dir, peak_rss_gib(),
    )
    return written
