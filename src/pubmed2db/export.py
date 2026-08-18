"""Export the latest version of every abstract to JSON or Parquet.

JSON export follows the NCATS Translator DocumentMetadataAPI field names (which
differ from the PubMed names used in the database) and emits empty strings, not
nulls, for absent values. Parquet export keeps PubMed's own field names.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from pathlib import Path

import duckdb

from .load import _VERSIONED_TABLES
from .util import eta_str, peak_rss_gib

logger = logging.getLogger(__name__)

#: Rows pulled from DuckDB per fetch during the JSON export.
_FETCH_BATCH = 5000

#: Minimum gap between progress log lines, so large exports don't spam the log.
_PROGRESS_INTERVAL_S = 10.0

#: Child tables whose rows belong to a specific article version (pmid, source_file):
#: everything the loader writes per version except `article` itself (exported from
#: the latest-version snapshot) and `deleted_pmid` (exported in full below).
_VERSIONED_CHILDREN = tuple(
    t for t in _VERSIONED_TABLES if t not in ("article", "deleted_pmid")
)

#: Dimension/bookkeeping tables exported as-is.
_OTHER_TABLES = ("journal", "journal_issn", "source_file", "deleted_pmid", "pipeline_run")


#: Month abbreviations, frozen rather than taken from ``calendar.month_abbr``:
#: that is ``strftime('%b')`` under ``LC_TIME``, so any dependency calling
#: ``locale.setlocale`` would silently localize a spec-defined output field.
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_to_abbrev(raw: str | None) -> str:
    """Normalize a raw PubMed month to a capitalized 3-letter abbreviation.

    Accepts ``"3"``/``"03"`` (numeric), ``"Mar"``/``"March"``/``"Sept"``
    (textual), or ``None``; returns ``""`` when it cannot be interpreted.
    """
    if not raw:
        return ""
    raw = raw.strip()
    if raw.isdigit():
        month = int(raw)
        return _MONTH_ABBR[month - 1] if 1 <= month <= 12 else ""
    key = raw[:3].capitalize()
    return key if key in _MONTH_ABBR else ""


def _s(value: object) -> str:
    """Render a value as a string, mapping ``None`` to ``""`` per the spec."""
    return "" if value is None else str(value)


_MEDLINE_YEAR_RE = re.compile(r"^\s*(\d{4})")


def _year_from_medline_date(raw: str | None) -> str:
    """Leading 4-digit year of a free-text ``MedlineDate``, or ``""``.

    Records whose ``PubDate`` is a range or a season carry no ``<Year>`` element
    — PubMed puts the whole thing in ``<MedlineDate>`` ("1978 Jul-Aug", "1998
    Spring", "1998 Dec-1999 Jan", "1999-2000"). We store that verbatim for
    fidelity, which left ``pub_year`` blank in the export for every such record.
    The leading year is unambiguous in all of those shapes, so it is safe to
    recover; the month is not (a range has no single month), so ``pub_month``
    and ``pub_day`` stay empty rather than gaining a value we invented.
    """
    if not raw:
        return ""
    match = _MEDLINE_YEAR_RE.match(raw)
    return match.group(1) if match else ""


#: Section ``label``s ("BACKGROUND", "METHODS", ...) are deliberately dropped:
#: the consumer is a full-text search index, which wants prose, not headings.
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
)
SELECT
    la.pmid,
    j.title                AS journal_name,
    COALESCE(j.abbreviation_iso, j.abbreviation_medline) AS journal_abbrev,
    la.article_title,
    la.volume,
    la.issue,
    la.pub_year,
    la.pub_month,
    la.pub_day,
    la.medline_date,
    abs.abstract
FROM _latest_snapshot la
LEFT JOIN journal j ON la.nlm_catalog_id = j.nlm_catalog_id
LEFT JOIN abs ON abs.pmid = la.pmid AND abs.source_file = la.source_file
ORDER BY la.pmid
"""


def _document(row: tuple) -> dict[str, str]:
    (
        pmid, journal_name, journal_abbrev, title, volume, issue,
        year, month, day, medline_date, abstract,
    ) = row
    return {
        "id": f"PMID:{pmid}",
        "journal_name": _s(journal_name),
        "journal_abbrev": _s(journal_abbrev),
        "article_title": _s(title),
        "volume": _s(volume),
        "issue": _s(issue),
        # Falls back to the year inside a free-text MedlineDate, which is the
        # only place these records carry one. See _year_from_medline_date.
        "pub_year": _s(year) or _year_from_medline_date(medline_date),
        "pub_month": month_to_abbrev(month),
        "pub_day": _s(day),
        "abstract": _s(abstract),
    }


def export_json(
    con: duckdb.DuckDBPyConnection,
    out_dir: str | Path,
    *,
    shards: int = 1,
    gzip_output: bool = False,
) -> list[Path]:
    """Export the latest version of every abstract as sharded NDJSON.

    Each line is one document keyed by ``PMID:<id>`` using DocumentMetadataAPI
    field names. With ``gzip_output=True`` each shard is compressed as it is
    written (one pass, no separate re-read of the finished file), and the
    output is still line-readable via ``zcat``. Returns the list of files
    written.
    """
    if shards < 1:
        raise ValueError("shards must be >= 1")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Materialize the latest-version snapshot once: `latest_article` is a window
    # function over the full `article` table, and both the count and the export
    # query below would otherwise recompute it.
    con.execute("CREATE OR REPLACE TEMP TABLE _latest_snapshot AS SELECT * FROM latest_article")
    total = con.execute("SELECT count(*) FROM _latest_snapshot").fetchone()[0]
    logger.info(
        "starting JSON export: %d document(s) to %d shard(s) in %s%s",
        total, shards, out_dir, " (gzip)" if gzip_output else "",
    )

    # Only open as many shard files as there are documents to distribute, so a
    # dataset smaller than `shards` doesn't leave extra empty files on disk that
    # this function's own return value never mentions.
    active_shards = min(shards, total) if total else 1
    suffix = ".ndjson.gz" if gzip_output else ".ndjson"
    paths = [out_dir / f"pubmed_metadata_{i:05d}{suffix}" for i in range(active_shards)]

    # Re-exporting into the same directory with fewer shards, or with the other
    # --gzip setting, would otherwise leave earlier shards behind: a consumer
    # globbing the directory then reads a mix of two exports.
    keep = set(paths)
    stale = [p for p in out_dir.glob("pubmed_metadata_*.ndjson*") if p not in keep]
    for path in stale:
        path.unlink()
    if stale:
        logger.info("removed %d shard file(s) from a previous export", len(stale))

    opener = gzip.open if gzip_output else open
    handles = [opener(path, "wt", encoding="utf-8") for path in paths]
    run_start = time.monotonic()
    last_log = run_start
    try:
        cur = con.execute(_LATEST_METADATA_SQL)
        index = 0
        while True:
            rows = cur.fetchmany(_FETCH_BATCH)
            if not rows:
                break
            for row in rows:
                handles[index % active_shards].write(
                    json.dumps(_document(row), ensure_ascii=False) + "\n"
                )
                index += 1

            now = time.monotonic()
            if now - last_log >= _PROGRESS_INTERVAL_S and total:
                elapsed = now - run_start
                remaining = total - index
                eta = eta_str(elapsed, index, remaining)
                logger.info(
                    "progress: %d/%d documents (%.1f%%), ~%s remaining",
                    index, total, 100 * index / total, eta,
                )
                last_log = now
    finally:
        for handle in handles:
            handle.close()

    logger.info(
        "exported %d documents to %d shard(s) in %s (peak RSS %.1f GiB)",
        index, len(paths), out_dir, peak_rss_gib(),
    )
    return paths


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

    # A re-export overwrites its own fixed set of file names, so the only file
    # that can survive is one whose table left the schema — `reference_citation`
    # is exactly that case. Left in place it reads as part of this export to
    # anything globbing the directory. Same sweep the JSON export does.
    keep = {f"{t}.parquet" for t in ("article", *_VERSIONED_CHILDREN, *_OTHER_TABLES)}
    stale = [p for p in out_dir.glob("*.parquet") if p.name not in keep]
    for path in stale:
        path.unlink()
    if stale:
        logger.info(
            "removed %d Parquet file(s) for table(s) no longer in the schema: %s",
            len(stale), ", ".join(sorted(p.stem for p in stale)),
        )

    def _progress(path: Path) -> None:
        logger.info("wrote %s (%d/%d)", path.name, len(written), tables)

    if latest:
        # Materialize once: `latest_article` is a window function over the full
        # `article` table, and it would otherwise be recomputed for the article
        # export itself plus once per child table's EXISTS below.
        con.execute("CREATE OR REPLACE TEMP TABLE _latest_snapshot AS SELECT * FROM latest_article")
        article_query = "SELECT * FROM _latest_snapshot"
    else:
        article_query = "SELECT * FROM article"
    article_path = out_dir / "article.parquet"
    con.sql(article_query).write_parquet(str(article_path))
    written.append(article_path)
    _progress(article_path)

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
        con.sql(query).write_parquet(str(path))
        written.append(path)
        _progress(path)

    # Dimension / bookkeeping tables are exported in full regardless of `latest`.
    for table in _OTHER_TABLES:
        path = out_dir / f"{table}.parquet"
        con.sql(f"SELECT * FROM {table}").write_parquet(str(path))
        written.append(path)
        _progress(path)

    logger.info(
        "exported %d Parquet file(s) to %s (peak RSS %.1f GiB)",
        len(written), out_dir, peak_rss_gib(),
    )
    return written
