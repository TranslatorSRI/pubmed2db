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
_OTHER_TABLES = ("journal", "journal_issn", "source_file", "deleted_pmid", "pipeline_run")

#: PubMed ``ArticleId/@IdType`` -> CURIE prefix for the JSON ``identifiers``
#: field. ``doi`` is lowercase to match Babel's ``src/prefixes.py``, so those
#: CURIEs join against the Babel publication compendium; ``DOI:`` would not.
#: PubMed's ``pmc`` values already start with ``PMC``, hence the doubled
#: ``PMCID:PMC1234567``. Values are emitted verbatim, so consumers must match
#: case-insensitively (DOIs are case-insensitive per spec and PubMed is not
#: consistent).
#:
#: **The PMCID prefix is not settled.** Babel's ``prefixes.py`` says ``PMC``,
#: this export says ``PMCID``, and neither the Core Components spec nor the
#: DocumentMetadataAPI README carries a PMC example to arbitrate — the
#: production endpoint does not resolve PMCIDs in either form. ``PMCID`` is our
#: bet on where that lands. Tracked in issue #33, to be settled alongside
#: NCATSTranslator/Babel#1044.
ID_PREFIXES = {"doi": "doi", "pmc": "PMCID"}

#: The same mapping as SQL, derived rather than restated: `validate` imports
#: `ID_PREFIXES` to rebuild the CURIEs it expects, so a hand-written `CASE` here
#: would let a new id type or a casing fix reach the validator without reaching
#: the export, and every sampled record would be reported as a mismatch against
#: a correct export. No `ELSE` branch: an unlisted type cannot pass the `WHERE`.
_ID_CURIE_SQL = "CASE id_type " + " ".join(
    f"WHEN '{t}' THEN '{prefix}:'" for t, prefix in ID_PREFIXES.items()
) + " END"
_ID_TYPES_SQL = ", ".join(f"'{t}'" for t in ID_PREFIXES)


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
    """Leading 4-digit year of a free-text ``MedlineDate``, or ``""``.

    Records whose ``PubDate`` is a range or a season carry no ``<Year>`` element
    — PubMed puts the whole thing in ``<MedlineDate>`` ("1978 Jul-Aug", "1998
    Spring", "1998 Dec-1999 Jan", "1999-2000"). We store that verbatim for
    fidelity, which left ``pub_year`` blank in the export for every such record.
    The leading year is unambiguous in all of those shapes, so it is safe to
    recover; ``pub_day`` stays empty, since a range has no single day. The month
    half is recovered separately by :func:`_month_from_medline_date`.
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


#: **Not bare ``trim()``.** SQL ``trim(x)`` strips spaces and nothing else,
#: where Python's ``.strip()`` strips all whitespace — so a month carrying a tab
#: or a newline gave ``Mar`` from the Python twin and ``''`` from the SQL.
#: ``parse._raw_pubdate`` stores ``findtext("Month")`` verbatim, so such a
#: record exported blank *and* was reported as a ``pub_month`` mismatch against
#: efetch, which is a CORE_FIELDS error. ``_WS`` is the character set Python
#: strips, and every ``trim`` below names it.
_WS = " \t\n\r\f\v"

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
    key = f"upper(substr(trim({expr}, E'{_WS}'), 1, 1)) || lower(substr(trim({expr}, E'{_WS}'), 2, 2))"
    return f"""CASE
        WHEN trim(COALESCE({expr}, ''), E'{_WS}') = '' THEN ''
        WHEN regexp_full_match(trim({expr}, E'{_WS}'), '[0-9]+')
            THEN COALESCE(list_extract({_MONTHS_SQL}, TRY_CAST(trim({expr}, E'{_WS}') AS BIGINT)), '')
        WHEN list_contains({_MONTHS_SQL}, {key}) AND regexp_full_match(trim({expr}, E'{_WS}'), '\\p{{L}}+')
            THEN {key}
        ELSE trim({expr}, E'{_WS}')
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


def _shard_bytes(paths: list[Path]) -> int:
    """Total size of the shards that still exist, skipping any that vanish.

    DuckDB is writing this directory while we read it, so a path can disappear
    between the glob and the stat. That is a missing data point, not a reason to
    take the heartbeat down.
    """
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _log_writing_progress(out_dir: Path, start: float) -> None:
    """Log how much shard output exists so far, and current RSS."""
    paths = _shard_paths(out_dir)
    written = _shard_bytes(paths)
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

    Bytes and RSS, no ETA — the total output size is not known until it has been
    written. If a run ever needs one, split the COPY into PMID ranges and count
    rows per range.

    Nothing here is allowed to escape. An exception would kill this thread and
    leave a 20-minute export completely silent, which is the failure the
    heartbeat exists to prevent; logging the reason and carrying on is strictly
    better than that.
    """
    while not stop.wait(_PROGRESS_INTERVAL_S):
        try:
            _log_writing_progress(out_dir, start)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("could not log export progress: %s", exc)


def export_json(
    con: duckdb.DuckDBPyConnection,
    out_dir: str | Path,
    *,
    shards: int | None = None,
    gzip_output: bool = True,
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

    # Clock the whole export, not just the COPY. Materializing the snapshot is a
    # window function over the entire `article` table -- the reason export memory
    # scales with the database rather than the input file -- and with the sort
    # gone it is plausibly the dominant phase. slurm/README.md tells operators to
    # size --time from the line this feeds, so it must not start after the
    # expensive part.
    start = time.monotonic()

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
        "FORMAT JSON", "PER_THREAD_OUTPUT true", "OVERWRITE_OR_IGNORE true",
        f"FILENAME_PATTERN '{_SHARD_STEM}{{i}}'", f"FILE_EXTENSION '{suffix}'",
    ]
    if gzip_output:
        options.append("COMPRESSION 'gzip'")
    # Escape single quotes so a path containing one doesn't break out of the
    # quoted string literal (same reason as _copy_parquet).
    escaped_dir = out_dir.as_posix().replace("'", "''")
    copy_sql = f"COPY ({_LATEST_METADATA_SQL}) TO '{escaped_dir}' ({', '.join(options)})"

    # `shards` is the thread cap for this statement only: one file per writer
    # thread is what PER_THREAD_OUTPUT gives us, so asking for N shards is
    # asking N threads to write. Restore whatever the connection had after.
    #
    # Read *before* the heartbeat starts: the thread is non-daemon and only
    # stops when `stop` is set in the finally below, so a statement that raises
    # between start() and the try would hang the interpreter instead of
    # reporting the error.
    previous_threads = con.execute("SELECT current_setting('threads')").fetchone()[0]

    stop = threading.Event()
    heartbeat = threading.Thread(target=_log_while_writing, args=(out_dir, stop, start))
    heartbeat.start()
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

    # A re-export overwrites its own fixed set of file names, so the only file
    # that can survive is one whose table left the schema — `reference_citation`
    # is exactly that case. Left in place it reads as part of this export to
    # anything globbing the directory. Same sweep the JSON export does.
    keep = {f"{t}.parquet" for t in ("article", *_VERSIONED_CHILDREN, *_OTHER_TABLES)}
    stale = [path for path in out_dir.glob("*.parquet") if path.name not in keep]
    for path in stale:
        path.unlink()
    if stale:
        logger.info(
            "removed %d Parquet file(s) for table(s) no longer in the schema: %s",
            len(stale), ", ".join(sorted(path.stem for path in stale)),
        )

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
