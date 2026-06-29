"""Pipeline readiness checks derived from the database's own state.

The steps (``download → journals``, then ``load``, then ``export``) are connected
not by a separate "step X ran" flag but by inspecting ground truth: the
``source_file`` registry's ``downloaded_at``/``processed_at`` watermarks and
whether the data tables are populated. Deriving readiness this way means a check
can never disagree with the data it guards (e.g. claim a load happened after the
tables were wiped). The CLI turns these into actionable errors/warnings.
"""

from __future__ import annotations

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
