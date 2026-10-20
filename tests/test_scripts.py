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
    result = run(ARCHIVE, "--data-dir", str(data_dir))
    assert result.returncode == 0, result.stderr
    csv = (data_dir / "builds" / "2026oct19" / "source_file.csv").read_text()
    assert csv.splitlines()[0].startswith("file_name,kind,")
    assert "pubmed26n0001.xml.gz,baseline" in csv and "abc123" in csv
    assert "pubmed.duckdb HOST:" in result.stdout
