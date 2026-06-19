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
