"""Shared pytest fixtures.

Committed fixtures live as readable ``.xml`` files under ``tests/fixtures``; we
gzip them into properly named ``pubmedNNnNNNN.xml.gz`` files in a temp dir so the
real ``.xml.gz`` code path (and the filename -> order-key parsing) is exercised.
"""

from __future__ import annotations

import gzip
import shutil
from functools import partial
from pathlib import Path

import pytest

from pubmed2db.db import connect

FIXTURES = Path(__file__).parent / "fixtures"

#: A small NLM Catalog journal dimension matching the fixture articles.
SAMPLE_JOURNALS = [
    ("0410462", "Nature", "Nature", "Nature", 1869, None, True),
    ("9999991", "Journal of Examples", "J Examples", "J. Examples", 1990, None, True),
]


def gzip_fixture(stem: str, dst_dir: Path) -> Path:
    """Gzip the readable fixture ``<stem>.xml`` into ``<dst_dir>/<stem>.xml.gz``."""
    dst = dst_dir / f"{stem}.xml.gz"
    with (FIXTURES / f"{stem}.xml").open("rb") as fin, gzip.open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return dst


@pytest.fixture
def gz_fixture(tmp_path):
    """Return a factory that gzips a fixture ``.xml`` into ``<stem>.xml.gz``."""
    return partial(gzip_fixture, dst_dir=tmp_path)


@pytest.fixture
def con(tmp_path):
    """A fresh DuckDB connection with the schema initialized."""
    connection = connect(tmp_path / "test.duckdb")
    yield connection
    connection.close()


@pytest.fixture
def loaded_con(con, gz_fixture):
    """A connection with both fixtures loaded and the journal dimension seeded."""
    from pubmed2db.load import load_file

    load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")
    load_file(con, gz_fixture("pubmed25n0002"), kind="update")
    con.executemany("INSERT INTO journal VALUES (?,?,?,?,?,?,?)", SAMPLE_JOURNALS)
    return con


@pytest.fixture
def loaded_db(tmp_path, loaded_con) -> Path:
    """Path to a database built like `loaded_con`, with the file lock released
    so the CLI (which opens the file itself) can be pointed at it."""
    loaded_con.close()
    return tmp_path / "test.duckdb"


@pytest.fixture
def staged_download(tmp_path):
    """A ``--data-dir`` with fixtures staged as if `pubmed2db download` ran.

    Lays fixture files out under ``<data-dir>/pubmed/{baseline,updates}`` (the
    pystow layout `pubmed_downloader` uses), so CLI tests can exercise `load`'s
    real directory scan (`download.local_files`) instead of loading files
    directly.
    Returns the data-dir path.
    """
    data_dir = tmp_path / "data"
    baseline_dir = data_dir / "pubmed" / "baseline"
    updates_dir = data_dir / "pubmed" / "updates"
    baseline_dir.mkdir(parents=True)
    updates_dir.mkdir(parents=True)

    gzip_fixture("pubmed25n0001", baseline_dir)
    gzip_fixture("pubmed25n0002", updates_dir)
    return data_dir
