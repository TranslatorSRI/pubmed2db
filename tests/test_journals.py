"""Tests for the NLM journal-overview parser and journal loading."""

from __future__ import annotations

from pathlib import Path

import pubmed_downloader.catalog as catalog

# Captured at import time, before conftest's autouse fixture rebinds the module
# attribute to keep the suite offline: the test below is the test *of* this
# function, so it needs the real one.
from pubmed2db.load import _serfile_urls as real_serfile_urls

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_journal_overview():
    from pubmed2db.load import _parse_journal_overview

    records = list(_parse_journal_overview(FIXTURES / "J_Entrez_sample.txt"))
    by_nlm = {rec["nlm_catalog_id"]: (rec, issns) for rec, issns in records}

    # The record with no NlmId is skipped (we join on nlm_catalog_id).
    assert set(by_nlm) == {"7708172", "0410462"}

    nature, nature_issns = by_nlm["0410462"]
    assert nature["title"] == "Nature"
    assert nature["abbreviation_iso"] == "Nature"
    assert ("0028-0836", "Print") in nature_issns
    assert ("1476-4687", "Electronic") in nature_issns


def test_load_journals(con, monkeypatch):
    from pubmed2db.load import load_journals

    monkeypatch.setattr(
        catalog, "ensure_journal_overview", lambda **_: FIXTURES / "J_Entrez_sample.txt"
    )
    n = load_journals(con)
    assert n == 2

    row = con.execute(
        "SELECT title, abbreviation_iso FROM journal WHERE nlm_catalog_id = '0410462'"
    ).fetchone()
    assert row == ("Nature", "Nature")
    assert con.execute(
        "SELECT count(*) FROM journal_issn WHERE nlm_catalog_id = '0410462'"
    ).fetchone()[0] == 2


def test_load_journals_keeps_data_when_overview_is_empty(con, monkeypatch, tmp_path):
    """A truncated download / error page must not wipe (or crash on) the tables."""
    from pubmed2db.load import load_journals

    monkeypatch.setattr(
        catalog, "ensure_journal_overview", lambda **_: FIXTURES / "J_Entrez_sample.txt"
    )
    assert load_journals(con) == 2

    empty = tmp_path / "J_Entrez_error.txt"
    empty.write_text("<html>Service temporarily unavailable</html>\n")
    monkeypatch.setattr(catalog, "ensure_journal_overview", lambda **_: empty)

    assert load_journals(con) == 0
    assert con.execute("SELECT count(*) FROM journal").fetchone()[0] == 2


def test_load_journals_always_refetches(con, monkeypatch):
    """pystow's ensure() skips a file that already exists, so without force=True
    the journal dimension freezes at whatever the very first run downloaded --
    while `status` keeps reporting a fresh refresh."""
    from pubmed2db.load import load_journals

    calls = []

    def fake_ensure(**kwargs):
        calls.append(kwargs)
        return FIXTURES / "J_Entrez_sample.txt"

    monkeypatch.setattr(catalog, "ensure_journal_overview", fake_ensure)
    load_journals(con)
    assert calls == [{"force": True}]


def test_catalog_year_rejects_marc_wildcards():
    """~1,600 baseline years are MARC's `19uu` / `uuuu` placeholders, and
    `int()` raises on those -- this is the one that fires on real data."""
    from pubmed2db.load import _catalog_year

    assert _catalog_year("1869") == 1869
    for wildcard in ("19uu", "199u", "uuuu", "", None, "186", "18690"):
        assert _catalog_year(wildcard) is None


def test_parse_serfile_years():
    from pubmed2db.load import _parse_serfile

    years = _parse_serfile([FIXTURES / "serfile_sample.xml"])

    # 9999 is "still publishing", not a year: it must not reach end_year.
    assert years["0410462"] == (1869, None, True)
    # A wildcard start year degrades to NULL; a real end year means ceased.
    assert years["7708172"] == (None, 1983, False)
    # No end year at all is unknown, which is not the same as ceased.
    assert years["9999999"] == (2001, None, None)


def test_parse_serfile_later_files_win():
    """The baseline is a snapshot; the monthly deltas amend it, so order matters."""
    from pubmed2db.load import _parse_serfile

    paths = [FIXTURES / "serfile_sample.xml", FIXTURES / "serfile_update_sample.xml"]
    assert _parse_serfile(paths)["0410462"] == (1869, 2025, False)
    assert _parse_serfile(list(reversed(paths)))["0410462"] == (1869, None, True)


def test_serfile_urls_picks_the_newest_baseline_and_skips_marcxml(monkeypatch):
    """Upstream's ensure_serfile_catalog() skips serfilebase*, leaving only the
    ~1 MB monthly deltas; the ~150k records we need are in the baseline."""
    import pubmed2db.load as load_mod

    listing = """
      <a href="serfilebase.2025.xml">serfilebase.2025.xml</a>
      <a href="serfilebase.2026.xml">serfilebase.2026.xml</a>
      <a href="serfilebase.2026.marcxml.xml">serfilebase.2026.marcxml.xml</a>
      <a href="serfile.20251101.xml">serfile.20251101.xml</a>
      <a href="serfile.20260301.xml">serfile.20260301.xml</a>
      <a href="serfile.20260201.xml">serfile.20260201.xml</a>
      <a href="serfile.20260301.marcxml.xml">serfile.20260301.marcxml.xml</a>
    """
    monkeypatch.setattr(
        load_mod.requests, "get", lambda *a, **k: type("R", (), {"text": listing})()
    )

    assert [u.rsplit("/", 1)[1] for u in real_serfile_urls()] == [
        "serfilebase.2026.xml",  # newest baseline, not the 2025 one
        "serfile.20260201.xml",  # updates oldest-first; 2025's delta is dropped
        "serfile.20260301.xml",
    ]


def test_load_journals_takes_years_from_the_catalog_and_titles_from_j_entrez(con, monkeypatch):
    """The whole point of not switching sources: J_Entrez owns the row set and
    the title (it is PubMed's own rendering), the catalog only fills the years.
    See docs/journal-catalog.md."""
    from pubmed2db.load import load_journals

    monkeypatch.setattr(
        catalog, "ensure_journal_overview", lambda **_: FIXTURES / "J_Entrez_sample.txt"
    )
    monkeypatch.setattr(
        "pubmed2db.load._ensure_serfile", lambda: [FIXTURES / "serfile_sample.xml"]
    )
    assert load_journals(con) == 2

    # Title stays J_Entrez's ("Nature"), not the catalog's ("Nature.").
    assert con.execute(
        "SELECT title, start_year, end_year, active FROM journal"
        " WHERE nlm_catalog_id = '0410462'"
    ).fetchone() == ("Nature", 1869, None, True)

    # A catalog-only journal is not inserted; J_Entrez defines the row set.
    assert con.execute(
        "SELECT count(*) FROM journal WHERE nlm_catalog_id = '9999999'"
    ).fetchone()[0] == 0


def test_load_journals_keeps_journals_the_catalog_does_not_cover(con, monkeypatch, tmp_path):
    """~2% of J_Entrez journals (proceedings volumes, mostly) have no catalog
    record. They keep NULL years rather than being dropped."""
    from pubmed2db.load import load_journals

    monkeypatch.setattr(
        catalog, "ensure_journal_overview", lambda **_: FIXTURES / "J_Entrez_sample.txt"
    )
    empty = tmp_path / "empty_serfile.xml"
    empty.write_text("<?xml version='1.0'?><NLMCatalogRecordSet/>")
    monkeypatch.setattr("pubmed2db.load._ensure_serfile", lambda: [empty])

    assert load_journals(con) == 2
    assert con.execute(
        "SELECT title, start_year, end_year, active FROM journal"
        " WHERE nlm_catalog_id = '0410462'"
    ).fetchone() == ("Nature", None, None, None)


def test_load_journals_survives_an_unreachable_catalog(con, monkeypatch):
    """The years are an enrichment; the export reads title/abbrev. A ~450 MB
    download failing must not cost us the dimension."""
    from pubmed2db.load import load_journals

    monkeypatch.setattr(
        catalog, "ensure_journal_overview", lambda **_: FIXTURES / "J_Entrez_sample.txt"
    )

    def boom():
        raise OSError("ftp.nlm.nih.gov unreachable")

    monkeypatch.setattr("pubmed2db.load._ensure_serfile", boom)

    assert load_journals(con) == 2
    assert con.execute(
        "SELECT start_year FROM journal WHERE nlm_catalog_id = '0410462'"
    ).fetchone() == (None,)


def _fake_serfile_module(monkeypatch, tmp_path, downloads):
    """Point _ensure_serfile at tmp_path, recording every ensure() call."""
    import pubmed2db.load as load_mod

    class FakeModule:
        def join(self, *, name):
            return tmp_path / name

        def ensure(self, *, url, force):
            name = url.rsplit("/", 1)[1]
            downloads.append((name, force))
            (tmp_path / name).write_text("<NLMCatalogRecordSet/>")
            return tmp_path / name

    monkeypatch.setattr(load_mod.pystow, "module", lambda *a, **k: FakeModule())
    monkeypatch.setattr(load_mod, "_serfile_urls", lambda: ["https://x/serfile.20260901.xml"])


def test_ensure_serfile_skips_a_file_whose_validator_has_not_moved(monkeypatch, tmp_path):
    """The baseline is ~450 MB; an unchanged file must cost one HEAD, not a refetch."""
    import pubmed2db.load as load_mod

    downloads = []
    _fake_serfile_module(monkeypatch, tmp_path, downloads)
    monkeypatch.setattr(load_mod, "_serfile_validator", lambda url: '"abc123"')

    load_mod._ensure_serfile()
    assert downloads == [("serfile.20260901.xml", False)]  # first run: no sidecar yet
    assert (tmp_path / "serfile.20260901.xml.etag").read_text() == '"abc123"'

    downloads.clear()
    load_mod._ensure_serfile()
    assert downloads == [("serfile.20260901.xml", False)]  # unchanged: not forced


def test_ensure_serfile_refetches_a_republished_file(monkeypatch, tmp_path):
    """pystow's ensure() skips by name, so without this a republished file would
    keep its stale bytes indefinitely. NLM publishes no .md5 for these, so the
    server's ETag is the validator we track."""
    import pubmed2db.load as load_mod

    downloads = []
    _fake_serfile_module(monkeypatch, tmp_path, downloads)

    monkeypatch.setattr(load_mod, "_serfile_validator", lambda url: '"old"')
    load_mod._ensure_serfile()

    downloads.clear()
    monkeypatch.setattr(load_mod, "_serfile_validator", lambda url: '"new"')
    load_mod._ensure_serfile()

    assert downloads == [("serfile.20260901.xml", True)]  # forced
    assert (tmp_path / "serfile.20260901.xml.etag").read_text() == '"new"'


def test_ensure_serfile_keeps_the_cached_copy_when_the_head_fails(monkeypatch, tmp_path):
    """A HEAD failing is not a reason to re-download ~450 MB, nor to give up."""
    import pubmed2db.load as load_mod

    downloads = []
    _fake_serfile_module(monkeypatch, tmp_path, downloads)
    monkeypatch.setattr(load_mod, "_serfile_validator", lambda url: '"abc"')
    load_mod._ensure_serfile()

    downloads.clear()
    monkeypatch.setattr(load_mod, "_serfile_validator", lambda url: None)
    assert load_mod._ensure_serfile() == [tmp_path / "serfile.20260901.xml"]
    assert downloads == [("serfile.20260901.xml", False)]
    # The stale-but-unverifiable sidecar is left alone, so the next successful
    # HEAD still compares against something real.
    assert (tmp_path / "serfile.20260901.xml.etag").read_text() == '"abc"'


def test_serfile_validator_falls_back_and_survives_network_trouble(monkeypatch):
    import requests

    import pubmed2db.load as load_mod

    class Response:
        def __init__(self, headers):
            self.headers = headers

        def raise_for_status(self):
            pass

    monkeypatch.setattr(load_mod.requests, "head", lambda *a, **k: Response({"ETag": '"e"'}))
    assert load_mod._serfile_validator("https://x") == '"e"'

    monkeypatch.setattr(
        load_mod.requests, "head", lambda *a, **k: Response({"Last-Modified": "Tue, 01 Sep 2026"})
    )
    assert load_mod._serfile_validator("https://x") == "Tue, 01 Sep 2026"

    def boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(load_mod.requests, "head", boom)
    assert load_mod._serfile_validator("https://x") is None
