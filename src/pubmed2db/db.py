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


def connect(
    db_path: str | Path,
    *,
    threads: int | None = None,
    temp_directory: str | Path | None = None,
    memory_limit: str | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the DuckDB database and ensure the schema.

    ``threads`` caps DuckDB's thread pool, which otherwise sizes itself from the
    machine's core count and so oversubscribes a Slurm allocation smaller than
    the node. ``temp_directory`` is where DuckDB spills when a query exceeds its
    memory budget — worth pointing at local scratch for `export`.

    ``memory_limit`` (e.g. ``"48GB"``) caps DuckDB's buffer pool. It has the same
    problem ``threads`` does, and it matters more: left alone DuckDB sets the
    limit to ~80% of the *machine's* physical RAM, so on a large node it will
    happily cache its way past a much smaller ``--mem`` cgroup and be OOM-killed.
    Set it below your allocation on any long load.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {}
    if threads is not None:
        config["threads"] = threads
    if temp_directory is not None:
        config["temp_directory"] = str(temp_directory)
    if memory_limit is not None:
        config["memory_limit"] = memory_limit
    con = duckdb.connect(str(path), config=config)
    init_schema(con)
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create all tables and views if they do not already exist."""
    schema_sql = resources.files("pubmed2db").joinpath("schema.sql").read_text()
    con.execute(schema_sql)


#: Shared "downloaded but not (re)loaded" predicate: a file is pending if it was
#: downloaded but never processed, or downloaded again since its last load (a
#: changed published MD5). Used by :func:`pubmed2db.status.pending_file_count`
#: and :func:`pubmed2db.status.summarize`, and by
#: :func:`pubmed2db.load.needs_load`'s single-file check, so the rule can't
#: drift apart between its callers.
NEEDS_LOAD_SQL = (
    "downloaded_at IS NOT NULL AND (processed_at IS NULL OR downloaded_at > processed_at)"
)


def register_source_file(
    con: duckdb.DuckDBPyConnection,
    file_name: str,
    *,
    kind: str,
    published_md5: str | None = None,
    mark_downloaded: bool = True,
) -> None:
    """Insert or update a row in the ``source_file`` registry.

    Updates ``published_md5`` (and ``downloaded_at``) without disturbing
    ``processed_at``/``n_articles`` from any prior load.
    """
    year_yy, file_number, order_key = parse_file_name(file_name)
    ts = datetime.now(timezone.utc) if mark_downloaded else None
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
