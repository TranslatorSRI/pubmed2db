"""End-to-end CLI tests that avoid the network."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys

from click.testing import CliRunner

from pubmed2db.cli import main
from tests.conftest import SAMPLE_JOURNALS


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the CLI in a fresh subprocess.

    `pubmed_downloader`'s pystow module paths are fixed at import time from
    ``PYSTOW_HOME``; running in-process via `CliRunner` would pick up whatever
    value another already-imported test set first. A subprocess gives each
    invocation its own fresh import, matching a real invocation.
    """
    return subprocess.run(
        [sys.executable, "-m", "pubmed2db.cli", *args],
        capture_output=True,
        text=True,
    )


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


def test_cli_export_warns_on_wrong_format_flag(tmp_path, gz_fixture):
    from pubmed2db.db import connect

    db_path = tmp_path / "cli.duckdb"
    con = connect(db_path)
    _build_db(con, gz_fixture)
    con.close()

    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "export", "--format", "parquet",
         "--out", str(tmp_path / "out"), "--shards", "4"],
    )
    assert result.exit_code == 0, result.output
    assert "--shards only applies to --format json" in result.output
    # An unmentioned flag left at its default stays silent.
    assert "--latest" not in result.output


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


def test_cli_status_reports_pipeline_state(tmp_path, gz_fixture):
    """`status` runs read-only and reports each step's state."""
    from pubmed2db.db import connect

    db_path = tmp_path / "cli.duckdb"
    con = connect(db_path)
    _build_db(con, gz_fixture)
    con.close()

    result = CliRunner().invoke(main, ["--db", str(db_path), "status"])
    assert result.exit_code == 0, result.output
    assert "Download:" in result.output
    assert "Load:" in result.output
    assert "latest document(s)" in result.output
    assert "Export:    ready" in result.output


def test_cli_status_flags_a_second_baseline_year(tmp_path, gz_fixture):
    """A new baseline year stores every PMID twice; `status` should say so."""
    from pubmed2db.db import connect, register_source_file

    db_path = tmp_path / "cli.duckdb"
    con = connect(db_path)
    _build_db(con, gz_fixture)
    register_source_file(con, "pubmed25n0001.xml.gz", kind="baseline")
    con.close()

    result = CliRunner().invoke(main, ["--db", str(db_path), "status"])
    assert "baseline years" not in result.output

    con = connect(db_path)
    register_source_file(con, "pubmed26n0001.xml.gz", kind="baseline")
    con.close()

    result = CliRunner().invoke(main, ["--db", str(db_path), "status"])
    assert "2 baseline years present (2025, 2026)" in result.output
    assert "only 2026 is exported" in result.output


def test_record_run_roundtrip(tmp_path):
    """`record_run` stamps a step and `last_run` reads it back."""
    from pubmed2db.db import connect, record_run
    from pubmed2db.status import last_run

    con = connect(tmp_path / "rr.duckdb")
    assert last_run(con, "journals") is None
    record_run(con, "journals")
    first = last_run(con, "journals")
    assert first is not None
    record_run(con, "journals")  # upsert, not duplicate
    assert last_run(con, "journals") >= first
    assert con.execute("SELECT count(*) FROM pipeline_run").fetchone()[0] == 1


def test_connect_applies_duckdb_tuning(tmp_path):
    """`connect` passes `threads`/`temp_directory` through to DuckDB."""
    from pubmed2db.db import connect

    spill = tmp_path / "spill"
    con = connect(tmp_path / "tuned.duckdb", threads=2, temp_directory=spill)
    try:
        assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 2
        assert con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(spill)
    finally:
        con.close()


def test_cli_threads_and_temp_dir_options(tmp_path, gz_fixture):
    """The group-level `--threads`/`--temp-dir` options reach the connection."""
    from pubmed2db.db import connect

    db_path = tmp_path / "cli.duckdb"
    con = connect(db_path)
    _build_db(con, gz_fixture)
    con.close()

    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "--threads", "2", "--temp-dir", str(tmp_path / "spill"), "status"],
    )
    assert result.exit_code == 0, result.output
    assert "Export:    ready" in result.output


def test_cli_rejects_a_non_positive_limit(tmp_path):
    """`--limit 0` must not fall through to a full-corpus download."""
    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "cli.duckdb"), "download", "--limit", "0"]
    )
    assert result.exit_code != 0
    assert "--limit" in result.output


def test_cli_load_scans_download_directory(staged_download):
    """`load` with no explicit files finds them via its pystow directory scan."""
    db_path = staged_download / "cli.duckdb"
    result = _run_cli("--data-dir", str(staged_download), "--db", str(db_path), "load")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Loaded 2 file(s); 2 local file(s) checked." in result.stdout

    from pubmed2db.db import connect

    con = connect(db_path)
    try:
        assert con.execute("SELECT count(*) FROM article").fetchone()[0] > 0
    finally:
        con.close()


def test_cli_export_json_gzip(staged_download):
    """`export --gzip` writes compressed shards that still round-trip as NDJSON."""
    db_path = staged_download / "cli.duckdb"
    load_result = _run_cli("--data-dir", str(staged_download), "--db", str(db_path), "load")
    assert load_result.returncode == 0, load_result.stdout + load_result.stderr

    out_dir = staged_download / "out"
    result = _run_cli(
        "--db", str(db_path), "export", "--format", "json", "--out", str(out_dir), "--gzip"
    )
    assert result.returncode == 0, result.stdout + result.stderr

    paths = list(out_dir.glob("*.ndjson.gz"))
    assert paths
    with gzip.open(paths[0], "rt") as handle:
        docs = [json.loads(line) for line in handle]
    assert {doc["id"] for doc in docs} == {"PMID:1001", "PMID:1003"}


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


def test_cli_rejects_a_non_positive_shard_count(tmp_path):
    """`--shards 0` is a usage error, not a traceback out of export_json."""
    result = CliRunner().invoke(
        main,
        [
            "--db", str(tmp_path / "cli.duckdb"),
            "export", "--format", "json", "--out", str(tmp_path / "out"),
            "--shards", "0",
        ],
    )
    assert result.exit_code != 0
    assert "--shards" in result.output


def test_cli_update_survives_a_failed_journal_refresh(staged_download, monkeypatch):
    """A journal-overview outage must not throw away a completed download and
    skip the load -- `update` is documented for scheduled runs."""
    from pathlib import Path

    from pubmed2db import cli, download, load

    files = [
        (path, "baseline" if path.parent.name == "baseline" else "update")
        for path in Path(staged_download).glob("pubmed/*/*.xml.gz")
    ]
    monkeypatch.setattr(cli, "_local_files", lambda: files)
    monkeypatch.setattr(download, "sync", lambda con, **kwargs: [])

    def boom(con):
        raise RuntimeError("NLM Catalog unreachable")

    monkeypatch.setattr(load, "load_journals", boom)

    db_path = staged_download / "cli.duckdb"
    result = CliRunner().invoke(main, ["--db", str(db_path), "update"])
    assert result.exit_code == 0, result.output
    assert "Journal refresh failed" in result.output

    from pubmed2db.db import connect

    con = connect(db_path)
    assert con.execute("SELECT count(*) FROM article").fetchone()[0] > 0
    con.close()
