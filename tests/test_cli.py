"""End-to-end CLI tests that avoid the network."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys

import pytest
from click.testing import CliRunner

from pubmed2db.cli import main


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


def test_cli_export_json(tmp_path, loaded_db):
    db_path = loaded_db

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


def test_cli_export_warns_on_wrong_format_flag(tmp_path, loaded_db):
    db_path = loaded_db

    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "export", "--format", "parquet",
         "--out", str(tmp_path / "out"), "--shards", "4"],
    )
    assert result.exit_code == 0, result.output
    assert "--shards only applies to --format json" in result.output
    # An unmentioned flag left at its default stays silent.
    assert "--latest" not in result.output


def test_cli_export_parquet(tmp_path, loaded_db):
    db_path = loaded_db

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


def test_cli_status_reports_pipeline_state(loaded_db):
    """`status` runs read-only and reports each step's state."""
    db_path = loaded_db

    result = CliRunner().invoke(main, ["--db", str(db_path), "status"])
    assert result.exit_code == 0, result.output
    assert "Download:" in result.output
    assert "Load:" in result.output
    assert "latest document(s)" in result.output
    assert "Export:    ready" in result.output


def test_cli_status_flags_a_second_baseline_year(loaded_db):
    """A new baseline year stores every PMID twice; `status` should say so."""
    from pubmed2db.db import connect, register_source_file

    db_path = loaded_db
    con = connect(db_path)
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


def test_cli_threads_and_temp_dir_options(tmp_path, loaded_db):
    """The group-level DuckDB tuning options reach the connection."""
    db_path = loaded_db

    result = CliRunner().invoke(
        main,
        ["--db", str(db_path), "--threads", "2", "--memory-limit", "1GB",
         "--temp-dir", str(tmp_path / "spill"), "status"],
    )
    assert result.exit_code == 0, result.output
    assert "Export:    ready" in result.output


def _setting(con, name: str) -> str:
    return con.execute(f"SELECT current_setting('{name}')").fetchone()[0]


#: DuckDB reports a size in whichever unit fits, and the *default* limit is
#: sized from the machine — on a large-memory cluster node that is TiB, so a
#: GiB-only ladder turns this test into a KeyError on exactly the hardware it
#: is written for.
_UNITS = {"bytes": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
          "TiB": 1024**4, "PiB": 1024**5}


def _as_bytes(value: str) -> float:
    """Parse DuckDB's '953.6 MiB' / '12.7 GiB' back into a number."""
    number, unit = value.split()
    return float(number) * _UNITS[unit]


def test_connect_memory_limit_is_below_the_default(tmp_path):
    """An explicit limit must actually shrink the default, not sit alongside it.

    The cap is derived from the observed default rather than hard-coded. DuckDB
    sizes that default from the cgroup (#36), so a fixed 1GB is only below it on
    a machine with enough memory -- in a small container the default itself can
    be under a gigabyte and the comparison inverts, failing a run in which
    memory_limit was applied perfectly correctly.
    """
    from pubmed2db.db import connect

    default_con = connect(tmp_path / "default.duckdb")
    try:
        default = _as_bytes(_setting(default_con, "memory_limit"))
    finally:
        default_con.close()

    wanted = default / 2
    capped_con = connect(tmp_path / "capped.duckdb", memory_limit=f"{int(wanted)}B")
    try:
        capped = _as_bytes(_setting(capped_con, "memory_limit"))
    finally:
        capped_con.close()

    # The property under test: the explicit limit *replaces* the default.
    assert capped < default
    # And lands near what was asked for -- a sanity band, not a round-trip.
    # DuckDB does not echo this setting back verbatim: it reports a 6.2 GiB
    # request as "6.1 GiB" and a 1GB one as "953.6 MiB", so an exact comparison
    # fails on the value the machine happens to hand it.
    assert capped == pytest.approx(wanted, rel=0.05)


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
    monkeypatch.setattr(download, "local_files", lambda: files)
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


#: The three group-level DuckDB knobs, and the environment variable each reads.
#: `show_envvar=True` is what puts these names in `--help`; --memory-limit
#: originally spelled its own out in prose instead, which is a second copy that
#: can drift from the `envvar=` argument beside it.
DUCKDB_KNOBS = [
    ("--threads", "PUBMED2DB_THREADS"),
    ("--temp-dir", "PUBMED2DB_DUCKDB_TEMP_DIR"),
    ("--memory-limit", "PUBMED2DB_DUCKDB_MEMORY_LIMIT"),
]


@pytest.mark.parametrize("option,envvar", DUCKDB_KNOBS)
def test_duckdb_knobs_advertise_their_env_var(option, envvar):
    """`--help` names each knob's environment variable, via click not prose."""
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    assert option in result.output
    # click wraps help to the terminal width, so "[env var:" and the name can
    # land on different lines. Collapse whitespace before matching.
    flattened = " ".join(result.output.split())
    # click renders these as "[env var: NAME; ...]" only when show_envvar is set.
    assert f"env var: {envvar}" in flattened


def test_env_var_names_are_written_down_once():
    """No knob repeats its env var in the help *text* as well as the marker.

    Two copies in one help string is what this replaced; the `[env var: ...]`
    marker click generates is the single source.
    """
    result = CliRunner().invoke(main, ["--help"])
    for _option, envvar in DUCKDB_KNOBS:
        assert result.output.count(envvar) == 1, (
            f"{envvar} appears more than once in --help; the click marker "
            "should be its only mention"
        )
