"""Tests for the NLM journal-overview parser and journal loading."""

from __future__ import annotations

from pathlib import Path

import pubmed_downloader.catalog as catalog

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
