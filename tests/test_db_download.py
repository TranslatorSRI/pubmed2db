"""Tests for filename parsing, the registry, and MD5 helpers."""

from __future__ import annotations

import pytest


def test_parse_file_name_orders_chronologically():
    from pubmed2db.db import parse_file_name

    _, _, baseline = parse_file_name("pubmed25n0001.xml.gz")
    _, _, update = parse_file_name("pubmed25n1300.xml.gz")
    _, _, next_year = parse_file_name("pubmed26n0001.xml.gz")
    # Within a year updates sort after baseline; the year prefix dominates.
    assert baseline < update < next_year


def test_parse_file_name_rejects_bad_names():
    from pubmed2db.db import parse_file_name

    with pytest.raises(ValueError):
        parse_file_name("not-a-pubmed-file.txt")


def test_register_source_file_upserts(con):
    from pubmed2db.db import register_source_file

    register_source_file(con, "pubmed25n0001.xml.gz", kind="baseline", published_md5="aaa")
    register_source_file(con, "pubmed25n0001.xml.gz", kind="baseline", published_md5="bbb")
    published_md5, file_order_key = con.execute(
        "SELECT published_md5, file_order_key FROM source_file WHERE file_name = ?",
        ["pubmed25n0001.xml.gz"],
    ).fetchone()
    assert published_md5 == "bbb"
    assert file_order_key == 25_000_001


@pytest.mark.parametrize(
    "text,expected",
    [
        ("MD5(pubmed25n0001.xml.gz)= 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
        ("0123456789ABCDEF0123456789ABCDEF  pubmed25n0001.xml.gz", "0123456789abcdef0123456789abcdef"),
        ("no checksum here", None),
    ],
)
def test_parse_md5_text(text, expected):
    from pubmed2db.download import parse_md5_text

    assert parse_md5_text(text) == expected


def test_file_md5(tmp_path):
    import hashlib

    from pubmed2db.download import file_md5

    path = tmp_path / "blob.bin"
    payload = b"pubmed2db test payload"
    path.write_bytes(payload)
    assert file_md5(path) == hashlib.md5(payload).hexdigest()
