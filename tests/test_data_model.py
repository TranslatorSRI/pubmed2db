"""The data model contract: its version, its shape, and what it promises.

`docs/data-model.md` is the consumer-facing description; these tests are what
stops the database drifting away from it. A failure here usually means the
shape changed — which is fine, but only together with a version bump.
"""

from __future__ import annotations

import duckdb
import pytest

from pubmed2db import datamodel
from pubmed2db.datamodel import DATA_MODEL_VERSION, IncompatibleDatabase
from pubmed2db.db import connect


# --- version -----------------------------------------------------------------


def test_new_database_records_the_version_and_its_creator(con):
    meta = datamodel.metadata(con)
    assert meta["data_model_version"] == DATA_MODEL_VERSION
    assert meta["created_at"]
    assert meta["created_by"] == datamodel.code_version()
    assert "last_loaded_by" not in meta


def test_reopening_keeps_the_creation_stamp(tmp_path):
    path = tmp_path / "db.duckdb"
    first = connect(path)
    created = datamodel.metadata(first)["created_at"]
    first.execute(
        "UPDATE pubmed2db_metadata SET value = '2000-01-01T00:00:00+00:00' WHERE key = 'created_at'"
    )
    first.close()
    second = connect(path)
    try:
        assert datamodel.metadata(second)["created_at"] == "2000-01-01T00:00:00+00:00" != created
    finally:
        second.close()


def test_load_records_which_code_loaded(con, gz_fixture):
    from pubmed2db.load import load_files

    load_files(con, [(gz_fixture("pubmed25n0001"), "baseline")])
    meta = datamodel.metadata(con)
    assert meta["last_loaded_by"] == datamodel.code_version()
    assert meta["last_loaded_at"]


def _old_database(path, *statements):
    raw = duckdb.connect(str(path))
    for statement in statements:
        raw.execute(statement)
    raw.close()


def test_a_pre_1_0_database_is_refused_and_left_untouched(tmp_path):
    """Every database built before 1.0 lacks the metadata table. 1.0 renamed and
    dropped columns, which `CREATE TABLE IF NOT EXISTS` cannot do, so opening one
    must fail loudly rather than half-migrate it."""
    path = tmp_path / "old.duckdb"
    _old_database(path, "CREATE TABLE grant_ (pmid BIGINT, source_file TEXT)")

    with pytest.raises(IncompatibleDatabase, match="predates pubmed2db data model"):
        connect(path)

    raw = duckdb.connect(str(path))
    try:
        assert [r[0] for r in raw.execute("SHOW TABLES").fetchall()] == ["grant_"]
    finally:
        raw.close()


@pytest.mark.parametrize(
    ("stored", "message"),
    [("0.9", "rebuild the database"), ("2.0", "rebuild the database"), ("1.99", "newer than")],
)
def test_an_incompatible_version_is_refused(tmp_path, stored, message):
    path = tmp_path / "db.duckdb"
    connect(path).close()
    raw = duckdb.connect(str(path))
    raw.execute(
        "UPDATE pubmed2db_metadata SET value = ? WHERE key = 'data_model_version'", [stored]
    )
    raw.close()

    with pytest.raises(IncompatibleDatabase, match=message):
        connect(path)


def test_an_older_minor_version_is_raised_to_ours(tmp_path, monkeypatch):
    """The additive case: schema.sql adds the minor version's columns, and the
    stored version follows. Simulated by running code one minor ahead."""
    path = tmp_path / "db.duckdb"
    connect(path).close()
    major, minor = DATA_MODEL_VERSION.split(".")
    newer = f"{major}.{int(minor) + 1}"
    monkeypatch.setattr(datamodel, "DATA_MODEL_VERSION", newer)

    con = connect(path)
    try:
        assert datamodel.stored_version(con) == newer
    finally:
        con.close()


def test_cli_explains_an_incompatible_database(tmp_path):
    from click.testing import CliRunner

    from pubmed2db.cli import main

    path = tmp_path / "old.duckdb"
    _old_database(path, "CREATE TABLE article (pmid BIGINT)")
    result = CliRunner().invoke(main, ["--db", str(path), "status"])
    assert result.exit_code == 1
    assert "predates pubmed2db data model" in result.output
    assert "Traceback" not in result.output


def test_code_version_ignores_a_repository_it_is_not_in(tmp_path, monkeypatch):
    """An installed copy inside someone else's checkout must not report that
    checkout's commit as ours."""
    from pubmed2db import __version__

    fake_module = tmp_path / "site-packages" / "pubmed2db" / "datamodel.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.touch()
    monkeypatch.setattr(datamodel, "__file__", str(fake_module))
    assert datamodel.code_version() == __version__
