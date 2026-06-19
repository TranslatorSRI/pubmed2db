"""Shared pytest fixtures.

Committed fixtures live as readable ``.xml`` files under ``tests/fixtures``; we
gzip them into properly named ``pubmedNNnNNNN.xml.gz`` files in a temp dir so the
real ``.xml.gz`` code path (and the filename -> order-key parsing) is exercised.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest

from pubmed2db.db import connect

FIXTURES = Path(__file__).parent / "fixtures"

#: A small NLM Catalog journal dimension matching the fixture articles.
SAMPLE_JOURNALS = [
    ("0410462", "Nature", "Nature", "Nature", 1869, None, True),
    ("9999991", "Journal of Examples", "J Examples", "J. Examples", 1990, None, True),
]


@pytest.fixture
def gz_fixture(tmp_path):
    """Return a factory that gzips a fixture ``.xml`` into ``<stem>.xml.gz``."""

    def _make(stem: str) -> Path:
        src = FIXTURES / f"{stem}.xml"
        dst = tmp_path / f"{stem}.xml.gz"
        with src.open("rb") as fin, gzip.open(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        return dst

    return _make


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
