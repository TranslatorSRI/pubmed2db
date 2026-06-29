"""End-to-end CLI tests that avoid the network."""

from __future__ import annotations

import json

from click.testing import CliRunner

from pubmed2db.cli import main
from tests.conftest import SAMPLE_JOURNALS


def _build_db(con, gz_fixture):
    from pubmed2db.load import load_file

    load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")
    load_file(con, gz_fixture("pubmed25n0002"), kind="update")
    con.executemany("INSERT INTO journal VALUES (?,?,?,?,?,?,?)", SAMPLE_JOURNALS)


def test_cli_export_json(tmp_path, gz_fixture):
    from pubmed2db.db import connect

    db_path = tmp_path / "cli.duckdb"
    con = connect(db_path)
    _build_db(con, gz_fixture)
    con.close()  # release the DuckDB file lock before the CLI opens it

    out_dir = tmp_path / "out"
    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "export", "--format", "json", "--out", str(out_dir)],
    )
    assert result.exit_code == 0, result.output

    docs = {}
    for path in out_dir.glob("*.ndjson"):
        with path.open() as handle:
            for line in handle:
                doc = json.loads(line)
                docs[doc["id"]] = doc
    assert set(docs) == {"PMID:1001", "PMID:1003"}
    assert docs["PMID:1001"]["pub_month"] == "Mar"


def test_cli_export_parquet(tmp_path, gz_fixture):
    from pubmed2db.db import connect

    db_path = tmp_path / "cli.duckdb"
    con = connect(db_path)
    _build_db(con, gz_fixture)
    con.close()

    out_dir = tmp_path / "pq"
    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "export", "--format", "parquet", "--out", str(out_dir), "--all"],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "article.parquet").exists()


def test_cli_load_without_download_errors(tmp_path):
    """`load` with nothing downloaded fails with a message pointing at download."""
    result = CliRunner().invoke(
        main,
        ["--data-dir", str(tmp_path), "--db", str(tmp_path / "cli.duckdb"), "load"],
    )
    assert result.exit_code != 0
    assert "download" in result.output.lower()


def test_cli_export_without_load_errors(tmp_path):
    """`export` before any load fails with a message pointing at load."""
    from pubmed2db.db import connect

    db_path = tmp_path / "cli.duckdb"
    connect(db_path).close()  # empty (schema only) database

    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "export", "--format", "json", "--out", str(tmp_path / "out")],
    )
    assert result.exit_code != 0
    assert "load" in result.output.lower()


def test_cli_export_warns_when_journals_missing(tmp_path, gz_fixture):
    """`export` proceeds but warns when the journal dimension is empty."""
    from pubmed2db.db import connect
    from pubmed2db.load import load_file

    db_path = tmp_path / "cli.duckdb"
    con = connect(db_path)
    load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")  # articles, no journals
    con.close()

    out_dir = tmp_path / "out"
    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "export", "--format", "json", "--out", str(out_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "journals" in result.output.lower()
    assert list(out_dir.glob("*.ndjson"))  # export still happened
