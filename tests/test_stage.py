"""The staged load path: parallel parse to Parquet shards, then bulk insert.

The one test that matters most is the equivalence: a file loaded from its
shard must give row for row what loading it directly gives, since the two
sinks share `load.build_batches` and differ only in the INSERT's FROM clause.
"""

from __future__ import annotations

import json
import logging
import os
import shutil

import pytest

from pubmed2db.db import connect
from pubmed2db.load import _VERSIONED_TABLES, load_file, load_files, load_staged
from pubmed2db.stage import (
    BAD,
    MANIFEST,
    PART,
    Manifest,
    Shard,
    find_shard,
    find_shards,
    prune,
    shard_dir,
    stage_file,
    stage_files,
)


def _tables(con) -> dict[str, list[tuple]]:
    """Every versioned table's rows, sorted, with `loaded_at` left out."""
    out = {}
    for table in _VERSIONED_TABLES:
        exclude = " EXCLUDE (loaded_at)" if table == "article" else ""
        out[table] = sorted(
            con.execute(f"SELECT *{exclude} FROM {table}").fetchall(), key=lambda r: str(r)
        )
    return out


def _registry(con) -> list[tuple]:
    return con.execute(
        "SELECT file_name, kind, n_articles, n_deletions, n_failed, n_book_records, "
        "processed_at IS NOT NULL FROM source_file ORDER BY file_name"
    ).fetchall()


@pytest.fixture
def two_files(write_pubmed_gz):
    return [
        (write_pubmed_gz("pubmed25n0001"), "baseline"),
        (write_pubmed_gz("pubmed25n0002"), "update"),
    ]


def test_stage_file_writes_every_table_and_a_manifest(two_files, tmp_path):
    path, kind = two_files[0]
    manifest = stage_file(path, kind, tmp_path / "stage")

    shard = shard_dir(tmp_path / "stage", path.name)
    assert shard.is_dir() and not shard.with_name(shard.name + PART).exists()
    assert {p.name for p in shard.iterdir()} == {f"{t}.parquet" for t in _VERSIONED_TABLES} | {
        MANIFEST
    }
    assert manifest.file_name == path.name and manifest.kind == "baseline"
    assert manifest.n_articles == 3 and manifest.n_deletions == 0
    assert manifest.rows["article"] == 3 and manifest.rows["deleted_pmid"] == 0
    assert Manifest.read(shard / MANIFEST) == manifest
    assert isinstance(find_shard(tmp_path / "stage", path), Shard)


def test_stage_then_load_matches_a_direct_load(two_files, tmp_path):
    """Row for row, table for table, registry included."""
    direct = connect(tmp_path / "direct.duckdb")
    for path, kind in two_files:
        load_file(direct, path, kind=kind)

    staged_con = connect(tmp_path / "staged.duckdb")
    n, failed = stage_files(two_files, stage_dir=tmp_path / "stage", workers=1)
    assert (n, failed) == (2, [])
    shards = find_shards(tmp_path / "stage", [p for p, _ in two_files])
    loaded, failed = load_files(staged_con, two_files, shards=shards)
    assert (loaded, failed) == (2, [])

    assert _tables(staged_con) == _tables(direct)
    assert _registry(staged_con) == _registry(direct)
    assert (
        staged_con.execute("SELECT count(*) FROM latest_article").fetchone()[0]
        == (direct.execute("SELECT count(*) FROM latest_article").fetchone()[0])
    )
    # The shards are gone once their transaction committed.
    assert not any(shard_dir(tmp_path / "stage", p.name).exists() for p, _ in two_files)
    assert staged_con.execute(
        "SELECT value FROM pubmed2db_metadata WHERE key = 'last_loaded_at'"
    ).fetchone()


def test_a_group_of_shards_is_one_transaction(two_files, tmp_path, con, monkeypatch):
    """A failure in the middle of a group leaves nothing behind, then the
    group is retried file by file, and the shard at fault is set aside."""
    from pubmed2db import load as load_module

    stage_files(two_files, stage_dir=tmp_path / "stage", workers=1)
    shards = find_shards(tmp_path / "stage", [p for p, _ in two_files])
    bad = shards["pubmed25n0002.xml.gz"]
    (bad.path / "author.parquet").write_bytes(b"not parquet")

    loaded, failed = load_staged(con, list(shards.values()), files_per_txn=2)
    assert (loaded, failed) == (1, ["pubmed25n0002.xml.gz"])
    assert con.execute("SELECT DISTINCT source_file FROM article").fetchall() == [
        ("pubmed25n0001.xml.gz",)
    ]
    assert _registry(con) == [("pubmed25n0001.xml.gz", "baseline", 3, 0, 0, 0, True)]
    assert not bad.path.exists() and bad.path.with_name(bad.path.name + BAD).is_dir()
    assert not shards["pubmed25n0001.xml.gz"].path.exists()

    # And a failure after the inserts (the registry upsert) rolls the rows back too.
    stage_files(two_files, stage_dir=tmp_path / "stage", workers=1, force=True)
    shards = find_shards(tmp_path / "stage", [p for p, _ in two_files])
    monkeypatch.setattr(load_module, "_upsert_source_file", lambda *a, **k: 1 / 0)
    loaded, failed = load_staged(con, [shards["pubmed25n0002.xml.gz"]], files_per_txn=1)
    assert loaded == 0 and failed == ["pubmed25n0002.xml.gz"]
    assert (
        con.execute(
            "SELECT count(*) FROM article WHERE source_file = 'pubmed25n0002.xml.gz'"
        ).fetchone()[0]
        == 0
    )


@pytest.fixture
def fresh_identity():
    """`code_identity` is cached per process; clear it around a test that fakes it."""
    from pubmed2db import stage as stage_module

    stage_module.code_identity.cache_clear()
    yield stage_module
    stage_module.code_identity.cache_clear()


def test_the_code_identity_is_asked_of_git_once(fresh_identity, monkeypatch):
    calls = []
    monkeypatch.setattr(fresh_identity, "code_version", lambda: calls.append(1) or "v1.3-4-gabc")
    assert fresh_identity.code_identity() == "v1.3-4-gabc"
    assert fresh_identity.code_identity() == "v1.3-4-gabc"
    assert len(calls) == 1


def test_two_dirty_states_of_one_commit_are_different_code(fresh_identity, monkeypatch, tmp_path):
    """`git describe --dirty` reads the same for any uncommitted edit, so a
    dirty identity carries a hash of the package source as well."""
    package = tmp_path / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "parse.py").write_text("one parse\n")
    (tmp_path / "uv.lock").write_text("pins\n")
    before = fresh_identity._source_digest(package)
    (package / "parse.py").write_text("another parse\n")
    after = fresh_identity._source_digest(package)
    assert before != after
    (package / "notes.md").write_text("not code\n")
    assert fresh_identity._source_digest(package) == after
    (tmp_path / "uv.lock").write_text("other pins\n")
    assert fresh_identity._source_digest(package) != after

    monkeypatch.setattr(fresh_identity, "code_version", lambda: "v1.3-4-gabc-dirty")
    monkeypatch.setattr(fresh_identity, "_source_digest", lambda: "1a2b3c")
    assert fresh_identity.code_identity() == "v1.3-4-gabc-dirty+1a2b3c"


def test_a_shard_from_another_dirty_state_is_stale(
    two_files, tmp_path, fresh_identity, monkeypatch
):
    monkeypatch.setattr(fresh_identity, "code_version", lambda: "v1.3-4-gabc-dirty")
    digest = iter(["before", "after"])
    monkeypatch.setattr(fresh_identity, "_source_digest", lambda: next(digest))
    path, kind = two_files[0]
    stage_file(path, kind, tmp_path / "stage")
    assert isinstance(find_shard(tmp_path / "stage", path), Shard)
    fresh_identity.code_identity.cache_clear()  # a new process, after another edit
    reason = find_shard(tmp_path / "stage", path)
    assert reason == "staged by v1.3-4-gabc-dirty+before, this is v1.3-4-gabc-dirty+after"


def test_a_key_value_folder_above_the_stage_is_not_a_column(two_files, tmp_path, con):
    """DuckDB reads a `key=value` path component as a Hive partition by
    default, which would add a column and fail every positional INSERT."""
    stage_dir = tmp_path / "run=2026" / "stage"
    stage_files(two_files, stage_dir=stage_dir, workers=1)
    shards = find_shards(stage_dir, [p for p, _ in two_files])
    assert load_staged(con, list(shards.values())) == (2, [])


def _staged(two_files, tmp_path):
    stage_files(two_files, stage_dir=tmp_path / "stage", workers=1)
    return find_shards(tmp_path / "stage", [p for p, _ in two_files])


def test_a_database_failure_sets_no_shard_aside(two_files, tmp_path, con, monkeypatch):
    """One `(bad allocation)` used to condemn every remaining shard to `.bad`."""
    import duckdb

    from pubmed2db import load as load_module

    shards = _staged(two_files, tmp_path)

    def out_of_memory(*args, **kwargs):
        raise duckdb.OutOfMemoryException("Failed to allocate block (bad allocation)")

    monkeypatch.setattr(load_module, "_insert_from", out_of_memory)
    with pytest.raises(load_module.StagedLoadAborted, match="bad allocation"):
        load_staged(con, list(shards.values()), files_per_txn=1)
    assert all(s.path.is_dir() for s in shards.values())
    assert not list((tmp_path / "stage").glob("*" + BAD))


def test_a_connection_that_stops_answering_sets_no_shard_aside(two_files, tmp_path, con):
    from pubmed2db import load as load_module

    shards = _staged(two_files, tmp_path)
    con.close()  # any later statement raises, the `SELECT 1` probe included
    with pytest.raises(load_module.StagedLoadAborted, match="stopped answering"):
        load_staged(con, list(shards.values()), files_per_txn=2)
    assert all(s.path.is_dir() for s in shards.values())


def test_a_group_that_fails_file_by_file_sets_no_shard_aside(two_files, tmp_path, con):
    """Every file of a group refused on its own is the machine (a full disk
    raises the IOException a missing Parquet file does), not N bad shards."""
    from pubmed2db import load as load_module

    shards = _staged(two_files, tmp_path)
    for shard in shards.values():
        (shard.path / "author.parquet").unlink()
    with pytest.raises(load_module.StagedLoadAborted, match="all 2 files"):
        load_staged(con, list(shards.values()), files_per_txn=2)
    assert all(s.path.is_dir() for s in shards.values())
    assert con.execute("SELECT count(*) FROM article").fetchone()[0] == 0


def test_a_shard_that_cannot_be_moved_aside_is_still_reported(
    two_files, tmp_path, con, monkeypatch
):
    from pathlib import Path

    shards = _staged(two_files, tmp_path)
    bad = shards["pubmed25n0002.xml.gz"]
    (bad.path / "author.parquet").write_bytes(b"not parquet")

    def refuse(self, target):
        raise PermissionError("read-only stage directory")

    monkeypatch.setattr(Path, "rename", refuse)
    loaded, failed = load_staged(con, list(shards.values()), files_per_txn=2)
    assert (loaded, failed) == (1, ["pubmed25n0002.xml.gz"])
    assert bad.path.is_dir()


def test_stage_skips_what_is_fresh_and_restages_what_changed(two_files, tmp_path, caplog):
    stage_dir = tmp_path / "stage"
    assert stage_files(two_files, stage_dir=stage_dir, workers=1)[0] == 2
    assert stage_files(two_files, stage_dir=stage_dir, workers=1)[0] == 0
    assert stage_files(two_files, stage_dir=stage_dir, workers=1, force=True)[0] == 2

    # A re-downloaded file (new size or mtime) is not served from the old shard.
    path, _ = two_files[0]
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert find_shard(stage_dir, path) == "XML changed since it was staged"
    assert stage_files(two_files, stage_dir=stage_dir, workers=1)[0] == 1

    # A shard from other code is stale: `load --force` must apply this parse.
    manifest_path = shard_dir(stage_dir, path.name) / MANIFEST
    data = json.loads(manifest_path.read_text())
    data["code_version"] = "v0.0-other"
    manifest_path.write_text(json.dumps(data))
    reason = find_shard(stage_dir, path)
    assert isinstance(reason, str) and reason.startswith("staged by v0.0-other")
    with caplog.at_level(logging.INFO, logger="pubmed2db.stage"):
        assert path.name not in find_shards(stage_dir, [path])
    assert "not usable: staged by v0.0-other" in caplog.text

    # A missing or unreadable manifest is not a shard either.
    manifest_path.write_text("{")
    assert find_shard(stage_dir, path) == "unreadable manifest"
    manifest_path.unlink()
    assert find_shard(stage_dir, path) == "unreadable manifest"
    assert find_shard(stage_dir, tmp_path / "pubmed25n0099.xml.gz") == "not staged"


def test_stage_skips_files_the_database_has_loaded(two_files, tmp_path, con):
    load_file(con, two_files[0][0], kind="baseline")
    n, _ = stage_files(two_files, stage_dir=tmp_path / "stage", workers=1, con=con)
    assert n == 1
    assert not shard_dir(tmp_path / "stage", "pubmed25n0001.xml.gz").exists()
    assert shard_dir(tmp_path / "stage", "pubmed25n0002.xml.gz").is_dir()


def test_a_shard_for_an_up_to_date_file_is_left_alone(two_files, tmp_path, con):
    """`load` decides what to insert by the registry, exactly as for XML."""
    stage_files(two_files, stage_dir=tmp_path / "stage", workers=1)
    shards = find_shards(tmp_path / "stage", [p for p, _ in two_files])
    assert load_files(con, two_files, shards=shards)[0] == 2
    stage_files(two_files, stage_dir=tmp_path / "stage", workers=1, force=True)
    shards = find_shards(tmp_path / "stage", [p for p, _ in two_files])
    assert load_files(con, two_files, shards=shards) == (0, [])
    assert all(s.path.is_dir() for s in shards.values())
    # Reloading through the stage is idempotent.
    before = _tables(con)
    assert load_files(con, two_files, shards=shards, force=True)[0] == 2
    assert _tables(con) == before


def test_partial_shards_are_swept_and_a_bad_parse_is_skipped(two_files, tmp_path, caplog):
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    leftover = stage_dir / ("pubmed25n0001" + PART)
    leftover.mkdir()
    (leftover / "article.parquet").write_bytes(b"")
    broken = tmp_path / "pubmed25n0003.xml.gz"
    broken.write_bytes(b"\x1f\x8b garbage")
    files = [*two_files, (broken, "update"), (tmp_path / "notes.txt", "update")]

    with caplog.at_level(logging.INFO, logger="pubmed2db.stage"):
        n, failed = stage_files(files, stage_dir=stage_dir, workers=1)
    assert (n, failed) == (2, ["pubmed25n0003.xml.gz"])
    assert not leftover.exists()
    assert "ignoring notes.txt" in caplog.text
    assert "failed to stage pubmed25n0003.xml.gz" in caplog.text
    progress = [m for m in caplog.messages if m.startswith("progress:")]
    assert len(progress) == 3 and progress[-1].endswith("GiB)")
    assert "s/file" in progress[0] and "files this run" in progress[0]


def test_prune_removes_the_shards_no_load_will_insert(two_files, tmp_path, con):
    """Shards `stage --force` cut for loaded files, and shards whose XML is
    gone, used to stay until the database was rebuilt."""
    stage_dir = tmp_path / "stage"
    load_file(con, two_files[0][0], kind="baseline")  # n0001 is up to date
    stage_files(two_files, stage_dir=stage_dir, workers=1, force=True)
    gone = tmp_path / "pubmed25n0003.xml.gz"
    shutil.copy(two_files[1][0], gone)
    stage_files([(gone, "update")], stage_dir=stage_dir, workers=1)
    gone.unlink()
    bad = stage_dir / ("pubmed25n0004" + BAD)
    part = stage_dir / ("pubmed25n0005" + PART)
    bad.mkdir()
    part.mkdir()

    removed = prune(stage_dir, [p for p, _ in two_files], con)
    assert removed == ["pubmed25n0001", "pubmed25n0003"]
    assert shard_dir(stage_dir, "pubmed25n0002.xml.gz").is_dir()  # still to load
    assert bad.is_dir() and part.is_dir()


def test_two_workers_really_run_in_parallel(two_files, tmp_path):
    """One real spawn-context pool run, so the worker entry point and its
    logging setup are exercised the way the cluster will run them."""
    n, failed = stage_files(two_files, stage_dir=tmp_path / "stage", workers=2)
    assert (n, failed) == (2, [])
    for path, _ in two_files:
        assert isinstance(find_shard(tmp_path / "stage", path), Shard)


def test_shard_parquet_keeps_the_schema_types(two_files, tmp_path):
    import pyarrow.parquet as pq

    from pubmed2db.load import table_schemas

    stage_file(*two_files[0], tmp_path / "stage")
    shard = find_shard(tmp_path / "stage", two_files[0][0])
    assert isinstance(shard, Shard)
    for table, schema in table_schemas().items():
        assert pq.read_schema(shard.parquet(table)).equals(schema), table


def _killed_on_the_first_file(job):
    """A pool worker the OOM killer takes on pubmed25n0001; module-level, so a
    spawned worker can unpickle it by name."""
    import signal

    from pubmed2db.stage import _stage_worker

    if job[0].endswith("pubmed25n0001.xml.gz"):
        os.kill(os.getpid(), signal.SIGKILL)
    return _stage_worker(job)


def test_a_killed_worker_fails_its_file_and_the_rest_carry_on(two_files, tmp_path, monkeypatch):
    """`multiprocessing.Pool` waited forever for a SIGKILLed worker's task, so
    an OOM kill hung `stage` until Slurm's time limit. Run in a daemon thread
    so a regression fails here instead of hanging the suite."""
    import threading

    from pubmed2db import stage as stage_module

    monkeypatch.setattr(stage_module, "_stage_worker", _killed_on_the_first_file)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            stage_files(two_files, stage_dir=tmp_path / "stage", workers=1)
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=120)
    assert not thread.is_alive(), "stage_files hung on a killed worker"
    assert result == [(1, ["pubmed25n0001.xml.gz"])]
    assert isinstance(find_shard(tmp_path / "stage", two_files[1][0]), Shard)
    assert find_shard(tmp_path / "stage", two_files[0][0]) == "not staged"
