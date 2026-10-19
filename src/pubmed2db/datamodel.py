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


# --- the data model's shape, read back from a database --------------------------

#: The stability tiers a table or view's comment starts with, as ``"Stable."``.
#: `docs/data-model.md` defines them; only ``internal`` is outside the contract.
TIERS = ("stable", "provenance", "internal")


def describe(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Every table and view, with its tier, comment and columns, from the database itself.

    The comments are ``schema.sql``'s ``COMMENT ON`` statements, so this is the
    data dictionary as shipped. It is what `tests/test_data_model.py` pins and
    what :func:`render_reference` turns into the reference half of
    `docs/data-model.md`. Relations are sorted by name and columns are in
    table order.
    """
    relations = con.execute(
        """
        SELECT table_name, 'table', comment FROM duckdb_tables()
        WHERE database_name = current_database() AND schema_name = 'main' AND NOT temporary
        UNION ALL
        SELECT view_name, 'view', comment FROM duckdb_views()
        WHERE database_name = current_database() AND schema_name = 'main'
          AND NOT temporary AND NOT internal
        ORDER BY 1
        """
    ).fetchall()
    columns: dict[str, list[dict]] = {}
    for table, name, data_type, comment in con.execute(
        """
        SELECT table_name, column_name, data_type, comment FROM duckdb_columns()
        WHERE database_name = current_database() AND schema_name = 'main'
        ORDER BY table_name, column_index
        """
    ).fetchall():
        columns.setdefault(table, []).append(
            {"name": name, "type": data_type, "comment": comment or ""}
        )
    described = []
    for name, kind, comment in relations:
        comment = comment or ""
        first_word = comment.split(".", 1)[0].strip().lower()
        described.append({
            "name": name,
            "kind": kind,
            "tier": first_word if first_word in TIERS else None,
            "comment": comment,
            "columns": columns.get(name, []),
        })
    return described


def shape(con: duckdb.DuckDBPyConnection) -> dict:
    """The versioned part of :func:`describe`: names, kinds and column types.

    Internal relations are left out, since they may change in any release, and
    so are the comments, since rewording one changes no consumer's query.
    """
    from .export import JSON_FIELDS

    return {
        "data_model_version": DATA_MODEL_VERSION,
        "relations": {
            relation["name"]: {
                "kind": relation["kind"],
                "tier": relation["tier"],
                "columns": [f"{c['name']} {c['type']}" for c in relation["columns"]],
            }
            for relation in describe(con)
            if relation["tier"] != "internal"
        },
        "ndjson_fields": list(JSON_FIELDS),
    }


def only_adds(old: dict, new: dict) -> list[str]:
    """What ``new`` removes or changes from ``old``; empty when it only adds.

    The rule a minor version must follow, applied to two :func:`shape` dicts:
    every relation keeps its kind and tier, a table keeps its columns *in order*
    with new ones only appended (``load`` inserts by position, and a migrated
    database can only append), a view keeps every column it had, and the NDJSON
    record keeps every field.
    """
    problems = []
    for name, before in old["relations"].items():
        after = new["relations"].get(name)
        if after is None:
            problems.append(f"{name} was removed")
            continue
        for key in ("kind", "tier"):
            if after[key] != before[key]:
                problems.append(f"{name}'s {key} changed from {before[key]} to {after[key]}")
        if before["kind"] == "table":
            if after["columns"][: len(before["columns"])] != before["columns"]:
                problems.append(f"{name}'s existing columns changed (new ones may only be appended)")
        else:
            missing = [c for c in before["columns"] if c not in after["columns"]]
            if missing:
                problems.append(f"{name} lost or retyped columns {missing}")
    missing_fields = [f for f in old["ndjson_fields"] if f not in new["ndjson_fields"]]
    if missing_fields:
        problems.append(f"the NDJSON record lost fields {missing_fields}")
    return problems


#: The markers around the generated half of `docs/data-model.md`.
REFERENCE_BEGIN = "<!-- BEGIN GENERATED REFERENCE: python -m pubmed2db.datamodel --docs docs/data-model.md -->"
REFERENCE_END = "<!-- END GENERATED REFERENCE -->"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_reference(relations: list[dict]) -> str:
    """The column reference of `docs/data-model.md`, rendered from :func:`describe`.

    Generated rather than written so that the document and the comments shipped
    in the database cannot say different things: a test regenerates it and
    compares. Tables first in tier order, then the views in one list, since
    every ``latest_*`` view has its table's columns.
    """
    lines = [REFERENCE_BEGIN, ""]
    tables = [r for r in relations if r["kind"] == "table"]
    for tier in TIERS:
        for relation in (r for r in tables if r["tier"] == tier):
            lines += [
                f"### `{relation['name']}`",
                "",
                relation["comment"],
                "",
            ]
            if tier == "internal":
                continue
            lines += ["| Column | Type | Meaning |", "| --- | --- | --- |"]
            lines += [
                f"| `{c['name']}` | {c['type']} | {_cell(c['comment'])} |"
                for c in relation["columns"]
            ]
            lines.append("")
    lines += ["### Views", "", "| View | Meaning |", "| --- | --- |"]
    lines += [
        f"| `{r['name']}` | {_cell(r['comment'])} |"
        for r in relations if r["kind"] == "view"
    ]
    lines += ["", REFERENCE_END]
    return "\n".join(lines)


def replace_reference(document: str, reference: str) -> str:
    """``document`` with the text between the reference markers replaced."""
    start = document.index(REFERENCE_BEGIN)
    end = document.index(REFERENCE_END) + len(REFERENCE_END)
    return document[:start] + reference + document[end:]


def _fresh_database() -> duckdb.DuckDBPyConnection:
    """An empty in-memory database at this code's data model."""
    from .db import connect

    return connect(":memory:")


def main(argv: list[str] | None = None) -> None:
    """Regenerate the files that are derived from ``schema.sql``.

    ``--golden PATH`` writes this version's :func:`shape`, which
    `tests/test_data_model.py` compares every fresh database against; run it as
    part of a deliberate version bump, never to make a failing test pass.
    ``--docs PATH`` rewrites the generated column reference in
    `docs/data-model.md` after a comment in ``schema.sql`` changes.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m pubmed2db.datamodel", description=main.__doc__)
    parser.add_argument("--golden", type=Path, help="write the shape JSON for this version here")
    parser.add_argument(
        "--docs", type=Path, help="regenerate the column reference in this Markdown file"
    )
    args = parser.parse_args(argv)
    con = _fresh_database()
    try:
        if args.golden:
            args.golden.write_text(json.dumps(shape(con), indent=2) + "\n")
        if args.docs:
            reference = render_reference(describe(con))
            args.docs.write_text(replace_reference(args.docs.read_text(), reference))
    finally:
        con.close()


if __name__ == "__main__":
    main()
