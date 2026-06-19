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
