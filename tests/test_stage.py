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
    shard_dir,
    stage_file,
    stage_files,
    sweep,
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
def two_files(gz_fixture):
    return [(gz_fixture("pubmed25n0001"), "baseline"), (gz_fixture("pubmed25n0002"), "update")]


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


def test_sweep_prunes_shards_for_files_no_longer_on_disk(two_files, tmp_path):
    stage_dir = tmp_path / "stage"
    stage_files(two_files, stage_dir=stage_dir, workers=1)
    bad = stage_dir / ("pubmed25n0002" + BAD)
    shutil.copytree(shard_dir(stage_dir, "pubmed25n0002.xml.gz"), bad)
    removed = sweep(stage_dir, keep={"pubmed25n0001"})
    assert removed == ["pubmed25n0002"]
    assert shard_dir(stage_dir, "pubmed25n0001.xml.gz").is_dir() and bad.is_dir()


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
