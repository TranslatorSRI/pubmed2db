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


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


def _stub_sync(monkeypatch, tmp_path, urls, *, hashed):
    """Stub every network/filesystem seam `_sync_kind` reaches through.

    Records which URLs were fetched and which local files were MD5'd, which is
    all the two behaviours under test are about.
    """
    from pubmed2db import download as dl

    fetched: list[str] = []
    monkeypatch.setattr(dl, "_ensure_urls", lambda *a, **k: list(urls))
    monkeypatch.setattr(dl, "MD5_DIR", tmp_path / "md5")
    monkeypatch.setattr(dl, "_save_md5_sidecar", lambda *a, **k: None)
    monkeypatch.setattr(
        dl.requests, "get",
        lambda url, **k: (fetched.append(url), _FakeResponse("MD5(x)= " + "0" * 32))[1],
    )

    def fake_file_md5(path):
        hashed.append(path.name)
        return "0" * 32

    monkeypatch.setattr(dl, "file_md5", fake_file_md5)

    class _Module:
        base = tmp_path

        @staticmethod
        def ensure(url):
            p = tmp_path / url.rsplit("/", 1)[-1]
            p.write_bytes(b"x")
            return p

    return fetched, _Module


def test_limit_takes_the_newest_files(con, monkeypatch, tmp_path):
    """`--limit N` means the N *newest* files, i.e. the tail of the listing.

    The listing is chronological, so slicing from the front would hand back the
    oldest baseline files -- useless for testing update-file handling, which is
    what the flag is for.
    """
    from pubmed2db.download import _sync_kind

    urls = [f"https://example.org/pubmed25n{i:04d}.xml.gz" for i in (1, 2, 3, 4)]
    hashed: list[str] = []
    fetched, module = _stub_sync(monkeypatch, tmp_path, urls, hashed=hashed)

    _sync_kind(
        con, kind="baseline", base_url="u", list_cache=tmp_path / "c",
        ensure_module=module, registry={}, limit=2, verify=False,
    )
    assert [u.rsplit("/", 1)[-1] for u in fetched] == [
        "pubmed25n0003.xml.gz.md5", "pubmed25n0004.xml.gz.md5",
    ]


def test_verify_skips_files_whose_checksum_has_not_moved(con, monkeypatch, tmp_path):
    """Re-hashing an unchanged corpus costs tens of GiB of I/O and finds nothing.

    PubMed files are immutable, so verification only earns its keep on files that
    are new or whose published checksum changed.
    """
    from pubmed2db.download import _sync_kind

    urls = ["https://example.org/pubmed25n0001.xml.gz",
            "https://example.org/pubmed25n0002.xml.gz"]
    hashed: list[str] = []
    _, module = _stub_sync(monkeypatch, tmp_path, urls, hashed=hashed)

    # 0001 is already registered with the checksum the server reports; 0002 is new.
    registry = {"pubmed25n0001.xml.gz": "0" * 32}
    _sync_kind(
        con, kind="baseline", base_url="u", list_cache=tmp_path / "c",
        ensure_module=module, registry=registry, limit=None, verify=True,
    )
    assert hashed == ["pubmed25n0002.xml.gz"]
