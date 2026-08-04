"""Export the latest version of every abstract to JSON or Parquet.

JSON export follows the NCATS Translator DocumentMetadataAPI field names (which
differ from the PubMed names used in the database) and emits empty strings, not
nulls, for absent values. Parquet export keeps PubMed's own field names.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path

import duckdb

from .util import eta_str, peak_rss_gib

logger = logging.getLogger(__name__)

#: Minimum gap between progress log lines, so large exports don't spam the log.
_PROGRESS_INTERVAL_S = 10.0

#: Child tables whose rows belong to a specific article version (pmid, source_file).
_VERSIONED_CHILDREN = (
    "abstract_text",
    "author",
    "author_affiliation",
    "mesh_heading",
    "mesh_qualifier",
    "publication_type",
    "grant_",
    "reference_citation",
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


#: Abstract section ``label``s ("BACKGROUND", "METHODS", ...) are deliberately
#: dropped: the consumer is a full-text search index, which wants prose, not
#: headings.
#:
#: Joining `article_id` on (pmid, source_file) — the same key `abs` uses —
#: confines the identifiers to the article's *latest* version, so a DOI or PMCID
#: that only ever appeared on a superseded version does not leak into the export.
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
    la.pmid,
    j.title                AS journal_name,
    COALESCE(j.abbreviation_iso, j.abbreviation_medline) AS journal_abbrev,
    la.article_title,
    la.volume,
    la.issue,
    la.pub_year,
    la.pub_month,
    la.pub_day,
    abs.abstract,
    ids.identifiers
FROM _latest_snapshot la
LEFT JOIN journal j ON la.nlm_catalog_id = j.nlm_catalog_id
LEFT JOIN abs ON abs.pmid = la.pmid AND abs.source_file = la.source_file
LEFT JOIN ids ON ids.pmid = la.pmid AND ids.source_file = la.source_file
ORDER BY la.pmid
"""


def _document(row: tuple) -> dict[str, str | list[str]]:
    (
        pmid, journal_name, journal_abbrev, title, volume, issue,
        year, month, day, abstract, identifiers,
    ) = row
    return {
        "id": f"PMID:{pmid}",
        # The LEFT JOIN yields NULL, not an empty list, for a PMID with neither
        # a DOI nor a PMCID; such a record still gets its own PMID CURIE.
        "identifiers": [f"PMID:{pmid}", *(identifiers or [])],
        "journal_name": _s(journal_name),
        "journal_abbrev": _s(journal_abbrev),
        "article_title": _s(title),
        "volume": _s(volume),
        "issue": _s(issue),
        "pub_year": _s(year),
        "pub_month": month_to_abbrev(month),
        "pub_day": _s(day),
        "abstract": _s(abstract),
    }


def export_json(
    con: duckdb.DuckDBPyConnection,
    out_dir: str | Path,
    *,
    shards: int = 1,
    batch_size: int = 5000,
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
    opener = (lambda p: gzip.open(p, "wt", encoding="utf-8")) if gzip_output else (
        lambda p: p.open("w", encoding="utf-8")
    )
    handles = [opener(path) for path in paths]
    run_start = time.monotonic()
    last_log = run_start
    try:
        cur = con.execute(_LATEST_METADATA_SQL)
        index = 0
        while True:
            rows = cur.fetchmany(batch_size)
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
