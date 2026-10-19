"""The pubmed2db data model's version, and the metadata every database carries.

One version number covers every output — the DuckDB database, the Parquet export
(the same tables as files) and the NDJSON export (a projection of them) — so a
consumer of any of them pins one thing. It is **not** the code version: many
code releases can share a data model, and `docs/data-model.md` says what each
bump means. In short, a minor bump only *adds* (a table, a column, a view, an
NDJSON field); a major bump renames, removes or changes the meaning of one.

The version is written into the database when it is created, in the key/value
``pubmed2db_metadata`` table, and checked every time it is opened
(:func:`check_compatible`, called by :func:`pubmed2db.db.connect`). A database
from before data model 1.0 has no such table, and is refused rather than
migrated: 1.0 renamed and dropped columns, which ``schema.sql``'s
``CREATE ... IF NOT EXISTS`` cannot do to an existing file.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from . import __version__

#: The data model this code reads and writes. See the module docstring, and
#: `docs/data-model.md` for when to bump it.
DATA_MODEL_VERSION = "1.0"

#: The table holding :data:`DATA_MODEL_VERSION` and who built the database.
METADATA_TABLE = "pubmed2db_metadata"


class IncompatibleDatabase(Exception):
    """The database was written under a data model this code cannot open."""


def _major_minor(version: str) -> tuple[int, int]:
    major, _, minor = version.partition(".")
    return int(major), int(minor or 0)


def code_version() -> str:
    """The code that is running: ``git describe`` in a checkout, else ``__version__``.

    The same ``git describe`` the Slurm logs open with (``slurm/config.sh``'s
    ``run_commit``), so a database and the job log that built it name the same
    commit. Asked only of *this* package's checkout: an installed copy that
    happens to sit inside some other repository (a virtualenv in a Babel
    checkout, say) must not report that repository's commit.
    """
    repo = Path(__file__).resolve().parents[2]
    if (repo / ".git").exists():
        try:
            described = subprocess.run(
                ["git", "describe", "--always", "--dirty", "--match", "v*"],
                cwd=repo, capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            described = ""
        if described:
            return described
    return __version__


def _user_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = 'main' "
            "AND NOT temporary"
        ).fetchall()
    }


def stored_version(con: duckdb.DuckDBPyConnection) -> str | None:
    """The data model version recorded in the database, or ``None`` if there is none."""
    if METADATA_TABLE not in _user_tables(con):
        return None
    row = con.execute(
        f"SELECT value FROM {METADATA_TABLE} WHERE key = 'data_model_version'"
    ).fetchone()
    return row[0] if row else None


def check_compatible(con: duckdb.DuckDBPyConnection) -> None:
    """Raise :class:`IncompatibleDatabase` unless this code can use the database.

    Called *before* ``schema.sql`` runs, so a database that is refused is left
    exactly as it was. Accepted: an empty database (it is about to be created),
    or one at the same major version and a minor version no newer than ours —
    an older minor only lacks columns that ``schema.sql``'s additive migrations
    then add. A newer minor is refused too: ``load`` inserts ``BY POSITION``, so
    old code writing into a table that has grown a column would misalign rows.
    """
    stored = stored_version(con)
    if stored is None:
        if _user_tables(con):
            raise IncompatibleDatabase(
                "this database predates pubmed2db data model "
                f"{DATA_MODEL_VERSION} (it has no {METADATA_TABLE} table), and "
                "1.0 renamed and dropped columns that cannot be migrated in place. "
                "Rebuild it: move the file aside and run `pubmed2db load` "
                "(the downloaded files are reused). See the README's "
                "\"`load --force` or a fresh database?\"."
            )
        return
    if not re.fullmatch(r"\d+\.\d+", stored):
        raise IncompatibleDatabase(f"unrecognized data model version {stored!r}")
    ours = _major_minor(DATA_MODEL_VERSION)
    theirs = _major_minor(stored)
    if theirs[0] != ours[0]:
        raise IncompatibleDatabase(
            f"this database is data model {stored}; this pubmed2db reads and writes "
            f"{DATA_MODEL_VERSION}. A major version changes existing tables, so "
            "rebuild the database with this pubmed2db (or use the one that built it)."
        )
    if theirs > ours:
        raise IncompatibleDatabase(
            f"this database is data model {stored}, newer than this pubmed2db's "
            f"{DATA_MODEL_VERSION}; upgrade pubmed2db to write to it."
        )


def _set(con: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    con.execute(
        f"INSERT INTO {METADATA_TABLE} (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [key, value],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp(con: duckdb.DuckDBPyConnection) -> None:
    """Record the data model version, and who created the database if nobody has.

    Runs after ``schema.sql`` on every open. On a new database it writes all
    three creation keys; on an older-minor one it raises the stored version to
    ours, which is only correct because ``schema.sql`` has just added the
    columns that version introduced.
    """
    if stored_version(con) is None:
        _set(con, "created_at", _now())
        _set(con, "created_by", code_version())
    if stored_version(con) != DATA_MODEL_VERSION:
        _set(con, "data_model_version", DATA_MODEL_VERSION)


def record_load(con: duckdb.DuckDBPyConnection) -> None:
    """Note which code last loaded files into the database, and when.

    Not derivable from ``source_file``, which records *when* each file was
    loaded but not by which commit — and a database loaded by two releases is
    exactly the one whose provenance someone will want to check.
    """
    _set(con, "last_loaded_at", _now())
    _set(con, "last_loaded_by", code_version())


def metadata(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Every key in ``pubmed2db_metadata``, as a dict."""
    return dict(con.execute(f"SELECT key, value FROM {METADATA_TABLE}").fetchall())
