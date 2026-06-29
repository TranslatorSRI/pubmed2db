"""Pipeline readiness checks derived from the database's own state.

The steps (``download → journals``, then ``load``, then ``export``) are connected
not by a separate "step X ran" flag but by inspecting ground truth: the
``source_file`` registry's ``downloaded_at``/``processed_at`` watermarks and
whether the data tables are populated. Deriving readiness this way means a check
can never disagree with the data it guards (e.g. claim a load happened after the
tables were wiped). The CLI turns these into actionable errors/warnings.
"""

from __future__ import annotations

from datetime import datetime

import duckdb


def articles_loaded(con: duckdb.DuckDBPyConnection) -> bool:
    """Whether any article version has been loaded (i.e. ``load`` has run)."""
    return bool(con.execute("SELECT EXISTS(SELECT 1 FROM article)").fetchone()[0])


def journals_loaded(con: duckdb.DuckDBPyConnection) -> bool:
    """Whether the journal dimension is populated (i.e. ``journals`` has run)."""
    return bool(con.execute("SELECT EXISTS(SELECT 1 FROM journal)").fetchone()[0])


def pending_file_count(con: duckdb.DuckDBPyConnection) -> int:
    """Number of downloaded files not yet loaded (or re-downloaded since loading).

    This is the registry-wide form of :func:`pubmed2db.load.needs_load`: a file is
    pending if it was downloaded but never processed, or downloaded again after
    its last load (a changed published MD5).
    """
    return con.execute(
        """
        SELECT count(*) FROM source_file
        WHERE downloaded_at IS NOT NULL
          AND (processed_at IS NULL OR downloaded_at > processed_at)
        """
    ).fetchone()[0]


def last_run(con: duckdb.DuckDBPyConnection, step: str) -> datetime | None:
    """When ``step`` was last recorded in ``pipeline_run`` (or ``None``)."""
    row = con.execute(
        "SELECT last_run_at FROM pipeline_run WHERE step = ?", [step]
    ).fetchone()
    return row[0] if row else None


def summarize(con: duckdb.DuckDBPyConnection) -> dict:
    """Collect a read-only snapshot of pipeline state for the ``status`` command.

    Download and load figures are derived from the ``source_file`` registry; the
    journal refresh time comes from ``pipeline_run`` (it has no other timestamp).
    """
    (
        known,
        downloaded,
        baseline,
        update,
        last_download,
        loaded_files,
        last_load,
    ) = con.execute(
        """
        SELECT
            count(*),
            count(*) FILTER (WHERE downloaded_at IS NOT NULL),
            count(*) FILTER (WHERE kind = 'baseline'),
            count(*) FILTER (WHERE kind = 'update'),
            max(downloaded_at),
            count(*) FILTER (WHERE processed_at IS NOT NULL),
            max(processed_at)
        FROM source_file
        """
    ).fetchone()

    return {
        "known_files": known,
        "downloaded_files": downloaded,
        "baseline_files": baseline,
        "update_files": update,
        "last_download": last_download,
        "loaded_files": loaded_files,
        "pending_files": pending_file_count(con),
        "last_load": last_load,
        "article_versions": con.execute("SELECT count(*) FROM article").fetchone()[0],
        "latest_documents": con.execute("SELECT count(*) FROM latest_article").fetchone()[0],
        "journals": con.execute("SELECT count(*) FROM journal").fetchone()[0],
        "journals_refreshed": last_run(con, "journals"),
        "articles_loaded": articles_loaded(con),
        "journals_loaded": journals_loaded(con),
    }
