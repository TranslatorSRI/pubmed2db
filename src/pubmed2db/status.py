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

#: Shared "downloaded but not (re)loaded" predicate: a file is pending if it was
#: downloaded but never processed, or downloaded again since its last load (a
#: changed published MD5). Used by both this module's pending_file_count() and
#: summarize(), and by :func:`pubmed2db.load.needs_load`'s single-file check, so
#: the rule can't drift apart between its callers.
NEEDS_LOAD_SQL = (
    "downloaded_at IS NOT NULL AND (processed_at IS NULL OR downloaded_at > processed_at)"
)


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
        f"SELECT count(*) FILTER (WHERE {NEEDS_LOAD_SQL}) FROM source_file"
    ).fetchone()[0]


def last_run(con: duckdb.DuckDBPyConnection, step: str) -> datetime | None:
    """When ``step`` was last recorded in ``pipeline_run`` (or ``None``)."""
    row = con.execute(
        "SELECT last_run_at FROM pipeline_run WHERE step = ?", [step]
    ).fetchone()
    return row[0] if row else None


def export_readiness(con: duckdb.DuckDBPyConnection) -> dict:
    """Whether ``export`` can run, and any warnings — shared by the ``export``
    and ``status`` commands so their verdicts can't drift apart.
    """
    if not articles_loaded(con):
        return {
            "blocked": True,
            "warnings": ["No articles loaded; run `pubmed2db load` first."],
        }
    warnings = []
    pending = pending_file_count(con)
    if pending:
        warnings.append(
            f"{pending} downloaded file(s) not yet loaded; "
            "run `pubmed2db load` to include them in the export."
        )
    if not journals_loaded(con):
        warnings.append(
            "journal table is empty, so journal names will be blank; "
            "run `pubmed2db journals` to populate them."
        )
    return {"blocked": False, "warnings": warnings}


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
            -- Filtered on downloaded_at so these read as a breakdown of the
            -- downloaded count they're printed next to, not of known files.
            count(*) FILTER (WHERE kind = 'baseline' AND downloaded_at IS NOT NULL),
            count(*) FILTER (WHERE kind = 'update' AND downloaded_at IS NOT NULL),
            max(downloaded_at),
            count(*) FILTER (WHERE processed_at IS NOT NULL),
            max(processed_at)
        FROM source_file
        """
    ).fetchone()
    pending_files = pending_file_count(con)

    article_versions, latest_documents, journals = con.execute(
        """
        SELECT
            (SELECT count(*) FROM article),
            (SELECT count(*) FROM latest_article),
            (SELECT count(*) FROM journal)
        """
    ).fetchone()

    return {
        "known_files": known,
        "downloaded_files": downloaded,
        "baseline_files": baseline,
        "update_files": update,
        "last_download": last_download,
        "loaded_files": loaded_files,
        "pending_files": pending_files,
        "last_load": last_load,
        "article_versions": article_versions,
        "latest_documents": latest_documents,
        "journals": journals,
        "journals_refreshed": last_run(con, "journals"),
        "articles_loaded": articles_loaded(con),
        "journals_loaded": journals_loaded(con),
    }
