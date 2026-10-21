"""The between-runs helpers in scripts/: archive a build, prepare a fresh database.

Both move or delete tens of gigabytes of operator data, so they are run here as
the operator runs them (as subprocesses, by path) against a data directory laid
out the way the pipeline leaves one, and the assertions are on what is left.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = REPO_ROOT / "scripts" / "archive_build.py"
FRESH = REPO_ROOT / "scripts" / "fresh_database.py"


def run(script: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, input=stdin,
    )


def _touch_at(path: Path, when: float, content: str = "") -> None:
    path.write_text(content)
    os.utime(path, (when, when))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A data directory after download, load, export and validate have run.

    Times are explicit: the previous build's validation report predates this
    export's metadata.json, and this build's does not, which is how the script
    tells them apart.
    """
    now = time.time()
    data = tmp_path / "data"
    export = data / "json"
    export.mkdir(parents=True)
    for i in range(2):
        _touch_at(export / f"pubmed_metadata_{i}.ndjson.gz", now - 600, "shard")
    _touch_at(export / "SHA256SUMS", now - 500, "x  pubmed_metadata_0.ndjson.gz\n")
    _touch_at(export / "metadata.json", now - 500, json.dumps(
        {"records": 2, "corpus": {"build_name": "2026oct19", "cutoff_file": "pubmed26n1700.xml.gz"}}
    ))

    manifests = data / "manifests"
    manifests.mkdir()
    _touch_at(manifests / "pmids-20260926.txt.gz", now - 10_000, "old")
    _touch_at(manifests / "validation_report-20260926.json", now - 10_000, json.dumps(
        {"status": "warn", "inputs": {"export_dir": str(export),
                                     "manifest_written": str(manifests / "pmids-20260926.txt.gz")}}
    ))
    _touch_at(manifests / "pmids-20261020.txt.gz", now - 100, "new")
    _touch_at(manifests / "validation_report-20261020.json", now - 100, json.dumps(
        {"status": "warn", "inputs": {"export_dir": str(export),
                                     "manifest_written": str(manifests / "pmids-20261020.txt.gz")}}
    ))

    logs = data / "logs"
    logs.mkdir()
    _touch_at(logs / "pubmed2db-load-100.out", now - 20_000, "previous build")
    _touch_at(logs / "pubmed2db-load-200.out", now - 700, "this build")
    _touch_at(logs / "pubmed2db-export-201.out", now - 600, "this build")
    _touch_at(logs / "submit.log", now - 50, "all of them")

    previous = data / "builds" / "2026sep26" / "logs"
    previous.mkdir(parents=True)
    _touch_at(previous / "pubmed2db-load-100.out", now - 20_000, "previous build")
    return data


# --------------------------------------------------------------------------- #
# archive_build.py
# --------------------------------------------------------------------------- #


def test_archive_moves_the_export_and_gathers_its_evidence(data_dir: Path) -> None:
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert result.returncode == 0, result.stderr
    dest = data_dir / "builds" / "2026oct19"

    # Shards and metadata at the top level, as published on stars.
    assert not (data_dir / "json").exists()
    assert (dest / "pubmed_metadata_0.ndjson.gz").is_file()
    assert (dest / "metadata.json").is_file()
    assert (dest / "SHA256SUMS").is_file()

    # This build's report and manifest, copied; the chain stays where it was.
    assert (dest / "manifests" / "validation_report-20261020.json").is_file()
    assert (dest / "manifests" / "pmids-20261020.txt.gz").is_file()
    assert not (dest / "manifests" / "validation_report-20260926.json").exists()
    assert (data_dir / "manifests" / "pmids-20261020.txt.gz").is_file()
    assert (data_dir / "manifests" / "validation_report-20261020.json").is_file()

    # Only the logs newer than the last archived build's, and not submit.log.
    assert sorted(p.name for p in (dest / "logs").iterdir()) == [
        "pubmed2db-export-201.out", "pubmed2db-load-200.out",
    ]
    assert (data_dir / "logs" / "pubmed2db-load-200.out").is_file()

    assert "rsync -a" in result.stdout and "2026oct19" in result.stdout


def test_archive_refuses_an_unfinished_export(data_dir: Path) -> None:
    (data_dir / "json" / "metadata.json").unlink()
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert result.returncode == 1
    assert "metadata.json" in result.stderr
    assert (data_dir / "json" / "pubmed_metadata_0.ndjson.gz").is_file()


def test_archive_refuses_to_replace_an_archived_build(data_dir: Path) -> None:
    (data_dir / "builds" / "2026oct19").mkdir()
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert (data_dir / "json" / "metadata.json").is_file()


def test_archive_refuses_an_unvalidated_export_unless_told(data_dir: Path) -> None:
    (data_dir / "manifests" / "validation_report-20261020.json").unlink()
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert result.returncode == 1
    assert "validate" in result.stderr
    assert (data_dir / "json" / "metadata.json").is_file()

    result = run(ARCHIVE, "--data-dir", str(data_dir), "--without-validation")
    assert result.returncode == 0, result.stderr
    dest = data_dir / "builds" / "2026oct19"
    assert (dest / "metadata.json").is_file()
    assert not (dest / "manifests").exists()


def test_archive_refuses_a_failed_validation(data_dir: Path) -> None:
    report = data_dir / "manifests" / "validation_report-20261020.json"
    content = json.loads(report.read_text())
    content["status"] = "fail"
    report.write_text(json.dumps(content))
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert result.returncode == 1
    assert "failed validation" in result.stderr


def test_archive_needs_a_name_when_the_cutoff_was_never_synced(data_dir: Path) -> None:
    meta = data_dir / "json" / "metadata.json"
    when = meta.stat().st_mtime  # keep it older than the report
    _touch_at(meta, when, json.dumps({"corpus": {"build_name": None}}))
    assert run(ARCHIVE, "--data-dir", str(data_dir)).returncode == 1
    result = run(ARCHIVE, "--data-dir", str(data_dir), "--name", "2026octNN")
    assert result.returncode == 0, result.stderr
    assert (data_dir / "builds" / "2026octNN" / "metadata.json").is_file()


def test_archive_dumps_the_source_file_registry(data_dir: Path) -> None:
    """The MD5 NCBI published lives only in the database, which a rebuild wipes."""
    from contextlib import closing

    from pubmed2db.db import connect, register_source_file

    with closing(connect(data_dir / "pubmed.duckdb")) as con:
        register_source_file(con, "pubmed26n0001.xml.gz", kind="baseline",
                             published_md5="abc123")
        register_source_file(con, "pubmed26n1700.xml.gz", kind="update")
        # Synced after the export: not part of the build whose cutoff is 1700.
        register_source_file(con, "pubmed26n1701.xml.gz", kind="update")
        con.execute("UPDATE source_file SET processed_at = now()")
        # Downloaded but never loaded: before the cutoff, and still not in the build.
        register_source_file(con, "pubmed26n1650.xml.gz", kind="update")
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert result.returncode == 0, result.stderr
    csv = (data_dir / "builds" / "2026oct19" / "source_file.csv").read_text()
    assert csv.splitlines()[0].startswith("file_name,kind,")
    assert "pubmed26n0001.xml.gz,baseline" in csv and "abc123" in csv
    assert "pubmed26n1700.xml.gz" in csv and "pubmed26n1701.xml.gz" not in csv
    assert "pubmed26n1650.xml.gz" not in csv
    assert "left 1 file(s) after the cutoff" in result.stdout
    assert "left 1 file(s) the database never loaded" in result.stdout
    assert "pubmed.duckdb HOST:" in result.stdout


def test_archive_reads_reports_from_an_overridden_manifest_dir(data_dir: Path) -> None:
    """config.sh lets MANIFEST_DIR move; validate's reports go with it."""
    elsewhere = data_dir.parent / "elsewhere"
    (data_dir / "manifests").rename(elsewhere)
    result = subprocess.run(
        [sys.executable, str(ARCHIVE), "--data-dir", str(data_dir)],
        capture_output=True, text=True, env={**os.environ, "MANIFEST_DIR": str(elsewhere)},
    )
    assert result.returncode == 0, result.stderr
    assert (data_dir / "builds" / "2026oct19" / "manifests"
            / "validation_report-20261020.json").is_file()


def test_archive_says_when_it_finds_no_logs(data_dir: Path) -> None:
    """An sbatch file submitted by hand logs to data/logs, not $DATA_DIR/logs."""
    logs = data_dir.parent / "submitdir-logs"
    (data_dir / "logs").rename(logs)
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert "pass --logs-dir" in result.stdout
    assert not (data_dir / "builds" / "2026oct19" / "logs").exists()


def test_archive_takes_logs_from_logs_dir(data_dir: Path) -> None:
    logs = data_dir.parent / "submitdir-logs"
    (data_dir / "logs").rename(logs)
    result = run(ARCHIVE, "--data-dir", str(data_dir), "--logs-dir", str(logs))
    assert result.returncode == 0, result.stderr
    assert (data_dir / "builds" / "2026oct19" / "logs" / "pubmed2db-load-200.out").is_file()


def test_archive_recovers_from_a_run_that_died_before_the_move(data_dir: Path) -> None:
    """A staging directory left behind neither blocks a re-run nor hides its logs."""
    stale = data_dir / "builds" / ".2026oct19.incoming"
    (stale / "logs").mkdir(parents=True)
    _touch_at(stale / "logs" / "pubmed2db-export-201.out", time.time(), "staged")
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert result.returncode == 0, result.stderr
    dest = data_dir / "builds" / "2026oct19"
    assert not stale.exists()
    assert sorted(p.name for p in (dest / "logs").iterdir()) == [
        "pubmed2db-export-201.out", "pubmed2db-load-200.out",
    ]


# --------------------------------------------------------------------------- #
# fresh_database.py
# --------------------------------------------------------------------------- #


@pytest.fixture
def two_years(data_dir: Path) -> Path:
    """Two baseline years on disk, a database with a WAL, and no finished export."""
    (data_dir / "json" / "metadata.json").unlink()
    for kind in ("baseline", "updates"):
        d = data_dir / "pubmed" / kind
        d.mkdir(parents=True)
        for name in ("pubmed25n0001.xml.gz", "pubmed25n1500.xml.gz", "pubmed26n0001.xml.gz"):
            (d / name).write_text(name)
        (d / "stray.txt").write_text("not a pubmed file")
    (data_dir / "pubmed.duckdb").write_text("db")
    (data_dir / "pubmed.duckdb.wal").write_text("wal")
    (data_dir / "pubmed2db" / "serfile").mkdir(parents=True)
    (data_dir / "pubmed2db" / "serfile" / "serfilebase.2026.xml").write_text("catalog")
    return data_dir


def _listing(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_fresh_database_moves_the_old_year_and_deletes_the_database(two_years: Path) -> None:
    before = _listing(two_years)
    result = run(FRESH, "--data-dir", str(two_years), "--keep-year", "2026", "--yes")
    assert result.returncode == 0, result.stderr
    after = _listing(two_years)

    # Last year's files are kept, outside the two directories load globs.
    for kind in ("baseline", "updates"):
        assert f"pubmed-archive/2025/{kind}/pubmed25n0001.xml.gz" in after
        assert f"pubmed-archive/2025/{kind}/pubmed25n1500.xml.gz" in after
        assert f"pubmed/{kind}/pubmed25n0001.xml.gz" not in after
        assert f"pubmed/{kind}/pubmed26n0001.xml.gz" in after
        assert f"pubmed/{kind}/stray.txt" in after
    assert "pubmed.duckdb" not in after and "pubmed.duckdb.wal" not in after

    # Everything else is exactly as it was.
    untouched = {p for p in before if p.startswith(("manifests/", "logs/", "builds/", "pubmed2db/"))}
    assert untouched and untouched <= after
    assert "serial catalog unavailable" in result.stdout
    assert "drops_retired_upstream" in result.stdout


def test_fresh_database_refuses_while_a_finished_export_is_unarchived(two_years: Path) -> None:
    (two_years / "json" / "metadata.json").write_text("{}")
    before = _listing(two_years)
    result = run(FRESH, "--data-dir", str(two_years), "--keep-year", "2026", "--yes")
    assert result.returncode == 1
    assert "archive_build.py" in result.stderr
    assert _listing(two_years) == before


def test_fresh_database_asks_first(two_years: Path) -> None:
    before = _listing(two_years)
    result = run(FRESH, "--data-dir", str(two_years), "--keep-year", "2026", stdin="n\n")
    assert result.returncode == 0, result.stderr
    assert "move 4 file(s)" in result.stdout
    assert "Nothing changed." in result.stdout
    assert _listing(two_years) == before


def test_fresh_database_with_one_year_moves_nothing(two_years: Path) -> None:
    for kind in ("baseline", "updates"):
        for p in (two_years / "pubmed" / kind).glob("pubmed25n*"):
            p.unlink()
    result = run(FRESH, "--data-dir", str(two_years), "--keep-year", "2026", "--yes")
    assert result.returncode == 0, result.stderr
    assert "nothing to move" in result.stdout
    assert not (two_years / "pubmed-archive").exists()
    assert not (two_years / "pubmed.duckdb").exists()
    assert (two_years / "pubmed" / "baseline" / "pubmed26n0001.xml.gz").is_file()


def test_fresh_database_at_the_year_boundary_moves_the_only_year_on_disk(two_years: Path) -> None:
    """The README runs this *before* `submit.sh all` downloads the new baseline.

    So at the boundary the newest year on disk is the superseded one, and
    keeping it would load both years into the fresh database.
    """
    result = run(FRESH, "--data-dir", str(two_years), "--keep-year", "2027", "--yes")
    assert result.returncode == 0, result.stderr
    assert "not downloaded yet" in result.stdout
    for kind in ("baseline", "updates"):
        assert not list((two_years / "pubmed" / kind).glob("pubmed2*"))
        assert (two_years / "pubmed-archive" / "2026" / kind / "pubmed26n0001.xml.gz").is_file()


def test_fresh_database_reads_the_year_from_ncbi(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    import requests

    spec = importlib.util.spec_from_file_location("fresh_database", FRESH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Listing:
        text = '<a href="pubmed26n0001.xml.gz">x</a> <a href="pubmed26n1334.xml.gz.md5">'

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(requests, "get", lambda url, timeout: Listing())
    assert module.ncbi_baseline_year() == 26


def test_fresh_database_without_an_answer_refuses_cleanly(two_years: Path) -> None:
    """Under sbatch or cron stdin is empty: a refusal, not an EOFError traceback."""
    before = _listing(two_years)
    result = run(FRESH, "--data-dir", str(two_years), "--keep-year", "2026")
    assert result.returncode == 1
    assert "--yes" in result.stderr and "Traceback" not in result.stderr
    assert _listing(two_years) == before


def test_fresh_database_checks_an_overridden_export_dir(two_years: Path) -> None:
    elsewhere = two_years.parent / "exports"
    elsewhere.mkdir()
    (elsewhere / "metadata.json").write_text("{}")
    result = subprocess.run(
        [sys.executable, str(FRESH), "--data-dir", str(two_years), "--keep-year", "2026", "--yes"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "EXPORT_DIR": str(elsewhere)},
    )
    assert result.returncode == 1
    assert str(elsewhere) in result.stderr
    assert (two_years / "pubmed.duckdb").exists()


# --------------------------------------------------------------------------- #
# The docs that point at the README's "Between runs" section
# --------------------------------------------------------------------------- #


def test_the_between_runs_section_exists_where_the_docs_point() -> None:
    """Three files link the section by anchor; renaming the heading breaks all three silently."""
    heading = "### Between runs: what to keep, archive and delete"
    anchor = "#between-runs-what-to-keep-archive-and-delete"
    assert heading in (REPO_ROOT / "README.md").read_text()
    for name in ("README.md", "slurm/README.md", "docs/Releases.md"):
        assert anchor in (REPO_ROOT / name).read_text(), name
