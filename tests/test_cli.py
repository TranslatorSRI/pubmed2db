"""End-to-end CLI tests that avoid the network."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys

import pytest
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

    # No --gzip flag: shards are compressed by default.
    docs = {}
    for path in out_dir.glob("*.ndjson.gz"):
        with gzip.open(path, "rt") as handle:
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
    """`connect` passes `threads`/`memory_limit`/`temp_directory` to DuckDB."""
    from pubmed2db.db import connect

    spill = tmp_path / "spill"
    con = connect(
        tmp_path / "tuned.duckdb", threads=2, temp_directory=spill, memory_limit="1GB"
    )
    try:
        assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 2
        assert con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(spill)
        # DuckDB reports the limit back in its own units ("953.6 MiB" for 1GB).
        assert _as_bytes(_setting(con, "memory_limit")) == pytest.approx(10**9, rel=0.01)
    finally:
        con.close()


def _setting(con, name: str) -> str:
    return con.execute(f"SELECT current_setting('{name}')").fetchone()[0]


def _as_bytes(value: str) -> float:
    """Parse DuckDB's '953.6 MiB' / '12.7 GiB' back into a number."""
    number, unit = value.split()
    return float(number) * {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[unit]


def test_connect_memory_limit_is_below_machine_default(tmp_path):
    """An explicit limit must actually shrink the default, not sit alongside it.

    Left alone DuckDB sets this from the *machine's* physical RAM, which on a
    cluster node is far above the Slurm --mem the process really has -- the
    reason a long load's memory climbs until it is OOM-killed.
    """
    from pubmed2db.db import connect

    default_con = connect(tmp_path / "default.duckdb")
    capped_con = connect(tmp_path / "capped.duckdb", memory_limit="1GB")
    try:
        default = _as_bytes(_setting(default_con, "memory_limit"))
        capped = _as_bytes(_setting(capped_con, "memory_limit"))
        assert capped < default
        assert capped == pytest.approx(10**9, rel=0.01)
    finally:
        default_con.close()
        capped_con.close()


def test_cli_threads_and_temp_dir_options(tmp_path, gz_fixture):
    """The group-level DuckDB tuning options reach the connection."""
    from pubmed2db.db import connect

    db_path = tmp_path / "cli.duckdb"
    con = connect(db_path)
    _build_db(con, gz_fixture)
    con.close()

    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "--threads", "2", "--memory-limit", "1GB",
         "--temp-dir", str(tmp_path / "spill"), "status"],
    )
    assert result.exit_code == 0, result.output
    assert "Export:    ready" in result.output


def test_cli_load_scans_download_directory(staged_download):
    """`load` with no explicit files finds them via its pystow directory scan."""
    db_path = staged_download / "cli.duckdb"
    result = _run_cli("--data-dir", str(staged_download), "--db", str(db_path), "load")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Loaded 2 of 2 file(s)." in result.stdout

    from pubmed2db.db import connect

    con = connect(db_path)
    try:
        assert con.execute("SELECT count(*) FROM article").fetchone()[0] > 0
    finally:
        con.close()


def test_cli_export_json_no_gzip(staged_download):
    """`export --no-gzip` opts out of the default compression, writing plain NDJSON."""
    db_path = staged_download / "cli.duckdb"
    load_result = _run_cli("--data-dir", str(staged_download), "--db", str(db_path), "load")
    assert load_result.returncode == 0, load_result.stdout + load_result.stderr

    out_dir = staged_download / "out"
    result = _run_cli(
        "--db", str(db_path), "export", "--format", "json", "--out", str(out_dir), "--no-gzip"
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert not list(out_dir.glob("*.ndjson.gz"))
    paths = list(out_dir.glob("*.ndjson"))
    assert paths
    docs = [json.loads(line) for path in paths for line in path.open()]
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
    assert list(out_dir.glob("*.ndjson.gz"))  # export still happened


def test_cli_export_then_validate_needs_no_flags(staged_download):
    """The default export is gzipped, and `validate` must read it as-is.

    Compression is only a safe default if the checker downstream does not have
    to be told about it -- so this runs the two commands exactly as the docs
    do, with no --gzip on one side and nothing on the other.
    """
    db_path = staged_download / "cli.duckdb"
    assert _run_cli("--data-dir", str(staged_download), "--db", str(db_path), "load").returncode == 0

    out_dir = staged_download / "out"
    export = _run_cli("--db", str(db_path), "export", "--format", "json", "--out", str(out_dir))
    assert export.returncode == 0, export.stdout + export.stderr
    assert list(out_dir.glob("*.ndjson.gz"))

    validate = _run_cli("--db", str(db_path), "validate", str(out_dir), "--offline")
    assert validate.returncode == 0, validate.stdout + validate.stderr
    assert "Validation PASS" in validate.stdout
    assert "2 record(s)" in validate.stdout
