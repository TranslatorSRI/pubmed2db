"""Export the latest version of every abstract to JSON or Parquet.

JSON export follows the NCATS Translator DocumentMetadataAPI field names (which
differ from the PubMed names used in the database) and emits empty strings, not
nulls, for absent values. Parquet export keeps PubMed's own field names.
"""

from __future__ import annotations

import calendar
import json
import logging
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

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
        return calendar.month_abbr[month] if 1 <= month <= 12 else ""
    key = raw[:3].capitalize()
    return key if key in calendar.month_abbr else ""


def _s(value: object) -> str:
    """Render a value as a string, mapping ``None`` to ``""`` per the spec."""
    return "" if value is None else str(value)


_LATEST_METADATA_SQL = """
WITH abs AS (
    SELECT pmid, source_file, string_agg(text, ' ' ORDER BY seq) AS abstract
    FROM abstract_text
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
    abs.abstract
FROM latest_article la
LEFT JOIN journal j ON la.nlm_catalog_id = j.nlm_catalog_id
LEFT JOIN abs ON abs.pmid = la.pmid AND abs.source_file = la.source_file
ORDER BY la.pmid
"""


def _document(row: tuple) -> dict[str, str]:
    pmid, journal_name, journal_abbrev, title, volume, issue, year, month, day, abstract = row
    return {
        "id": f"PMID:{pmid}",
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
) -> list[Path]:
    """Export the latest version of every abstract as sharded NDJSON.

    Each line is one document keyed by ``PMID:<id>`` using DocumentMetadataAPI
    field names. Returns the list of files written.
    """
    if shards < 1:
        raise ValueError("shards must be >= 1")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = [out_dir / f"pubmed_metadata_{i:05d}.ndjson" for i in range(shards)]
    handles = [path.open("w", encoding="utf-8") for path in paths]
    try:
        cur = con.execute(_LATEST_METADATA_SQL)
        index = 0
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                handles[index % shards].write(
                    json.dumps(_document(row), ensure_ascii=False) + "\n"
                )
                index += 1
    finally:
        for handle in handles:
            handle.close()

    written = [p for p in paths if p.stat().st_size > 0] or paths[:1]
    logger.info("exported %d documents to %d shard(s) in %s", index, len(written), out_dir)
    return written


def _copy_parquet(con: duckdb.DuckDBPyConnection, query: str, path: Path) -> None:
    con.execute(f"COPY ({query}) TO '{path.as_posix()}' (FORMAT PARQUET)")


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

    if latest:
        article_query = "SELECT * FROM latest_article"
    else:
        article_query = "SELECT * FROM article"
    article_path = out_dir / "article.parquet"
    _copy_parquet(con, article_query, article_path)
    written.append(article_path)

    for table in _VERSIONED_CHILDREN:
        if latest:
            query = (
                f"SELECT c.* FROM {table} c "
                "WHERE EXISTS (SELECT 1 FROM latest_article la "
                "WHERE la.pmid = c.pmid AND la.source_file = c.source_file)"
            )
        else:
            query = f"SELECT * FROM {table}"
        path = out_dir / f"{table}.parquet"
        _copy_parquet(con, query, path)
        written.append(path)

    # Dimension / bookkeeping tables are exported in full regardless of `latest`.
    for table in _OTHER_TABLES:
        path = out_dir / f"{table}.parquet"
        _copy_parquet(con, f"SELECT * FROM {table}", path)
        written.append(path)

    logger.info("exported %d Parquet file(s) to %s", len(written), out_dir)
    return written
