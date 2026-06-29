"""DuckDB connection, schema initialization, and the source-file registry."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import duckdb

#: Filenames look like ``pubmed25n1274.xml.gz`` -> (year_yy=25, file_number=1274).
_FILENAME_RE = re.compile(r"^pubmed(\d{2})n(\d+)\.xml\.gz$")

#: Multiplier so that (year_yy, file_number) sorts chronologically as one int.
_ORDER_MULTIPLIER = 1_000_000


def parse_file_name(file_name: str) -> tuple[int, int, int]:
    """Return ``(year_yy, file_number, file_order_key)`` for a PubMed XML file.

    ``file_order_key`` encodes PubMed's chronological order: within a release
    year baseline files have low numbers and updatefiles continue above them,
    and the two-digit year prefix increments across years.
    """
    match = _FILENAME_RE.match(file_name)
    if match is None:
        raise ValueError(f"not a PubMed XML filename: {file_name!r}")
    year_yy = int(match.group(1))
    file_number = int(match.group(2))
    return year_yy, file_number, year_yy * _ORDER_MULTIPLIER + file_number


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the DuckDB database and ensure the schema."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    init_schema(con)
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create all tables and views if they do not already exist."""
    schema_sql = resources.files("pubmed2db").joinpath("schema.sql").read_text()
    con.execute(schema_sql)


def register_source_file(
    con: duckdb.DuckDBPyConnection,
    file_name: str,
    *,
    kind: str,
    published_md5: str | None = None,
    downloaded_at: bool = True,
) -> None:
    """Insert or update a row in the ``source_file`` registry.

    Updates ``published_md5`` (and ``downloaded_at``) without disturbing
    ``processed_at``/``n_articles`` from any prior load.
    """
    year_yy, file_number, order_key = parse_file_name(file_name)
    ts = datetime.now(timezone.utc) if downloaded_at else None
    con.execute(
        """
        INSERT INTO source_file
            (file_name, kind, year_yy, file_number, file_order_key,
             published_md5, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (file_name) DO UPDATE SET
            published_md5 = excluded.published_md5,
            downloaded_at = COALESCE(excluded.downloaded_at, source_file.downloaded_at)
        """,
        [file_name, kind, year_yy, file_number, order_key, published_md5, ts],
    )


def record_run(con: duckdb.DuckDBPyConnection, step: str) -> None:
    """Stamp ``pipeline_run`` with the current time for ``step``.

    Used for steps whose recency is not otherwise recoverable from the data
    (currently just ``journals``); download/load recency comes from
    ``source_file`` timestamps instead.
    """
    con.execute(
        """
        INSERT INTO pipeline_run (step, last_run_at) VALUES (?, now())
        ON CONFLICT (step) DO UPDATE SET last_run_at = excluded.last_run_at
        """,
        [step],
    )


def get_registry(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """Return the ``source_file`` registry keyed by file name."""
    cols = [
        "file_name",
        "kind",
        "file_order_key",
        "published_md5",
        "processed_at",
        "n_articles",
        "n_deletions",
    ]
    rows = con.execute(f"SELECT {', '.join(cols)} FROM source_file").fetchall()
    return {row[0]: dict(zip(cols, row)) for row in rows}
