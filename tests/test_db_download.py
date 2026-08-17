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


_GOOD_MD5 = "0123456789abcdef0123456789abcdef"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


def _sync_kind(con, monkeypatch, tmp_path, *, urls, body, registry=None, limit=None):
    """Run download._sync_kind against a fake listing/server."""
    from pubmed2db import download

    blob = tmp_path / "blob.xml.gz"
    blob.write_bytes(b"")
    monkeypatch.setattr(download, "MD5_DIR", tmp_path / "md5")
    monkeypatch.setattr(download, "_ensure_urls", lambda *a, **k: urls)
    monkeypatch.setattr(download.requests, "get", lambda *a, **k: _FakeResponse(body))
    ensure_module = type("M", (), {"ensure": staticmethod(lambda *, url: blob)})()

    return download._sync_kind(
        con,
        kind="update",
        base_url="https://example.invalid/updatefiles/",
        list_cache=tmp_path / "listing.html",
        ensure_module=ensure_module,
        registry=registry if registry is not None else {},
        limit=limit,
        verify=False,
    )


def test_sync_limit_takes_the_newest_files(con, monkeypatch, tmp_path):
    """`--limit N` fetches the newest N: _ensure_urls sorts the listing newest-first."""
    urls = [
        f"https://example.invalid/updatefiles/pubmed25n{n:04d}.xml.gz"
        for n in (1279, 1278, 1277, 3, 2, 1)
    ]
    _sync_kind(con, monkeypatch, tmp_path, urls=urls, body=f"MD5(x)= {_GOOD_MD5}", limit=3)

    names = [
        r[0] for r in con.execute("SELECT file_name FROM source_file ORDER BY file_name").fetchall()
    ]
    assert names == [
        "pubmed25n1277.xml.gz",
        "pubmed25n1278.xml.gz",
        "pubmed25n1279.xml.gz",
    ]


def test_unusable_md5_sidecar_keeps_prior_checksum(con, monkeypatch, tmp_path):
    """An error page served with HTTP 200 must not wipe the stored checksum --
    that would flag every known file as changed and re-parse the whole corpus."""
    from pubmed2db.db import register_source_file

    file_name = "pubmed25n0001.xml.gz"
    register_source_file(con, file_name, kind="update", published_md5=_GOOD_MD5)
    before = con.execute(
        "SELECT downloaded_at FROM source_file WHERE file_name = ?", [file_name]
    ).fetchone()[0]

    _sync_kind(
        con,
        monkeypatch,
        tmp_path,
        urls=[f"https://example.invalid/updatefiles/{file_name}"],
        body="<html>Service temporarily unavailable</html>",
        registry={file_name: _GOOD_MD5},
    )

    md5, downloaded_at = con.execute(
        "SELECT published_md5, downloaded_at FROM source_file WHERE file_name = ?", [file_name]
    ).fetchone()
    assert md5 == _GOOD_MD5
    assert downloaded_at == before  # not spuriously flagged as re-downloaded


def test_file_md5(tmp_path):
    import hashlib

    from pubmed2db.download import file_md5

    path = tmp_path / "blob.bin"
    payload = b"pubmed2db test payload"
    path.write_bytes(payload)
    assert file_md5(path) == hashlib.md5(payload).hexdigest()
