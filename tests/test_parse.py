"""Tests for the self-driven XML parser."""

from __future__ import annotations


def test_parses_all_articles_and_raw_dates(gz_fixture):
    from pubmed2db.parse import parse_file

    parsed = parse_file(gz_fixture("pubmed25n0001"))
    by_pmid = {pa.pubmed: pa for pa in parsed.articles}
    assert set(by_pmid) == {1001, 1002, 1003}

    # Full date preserved verbatim (month kept as "Mar", not collapsed to a number).
    full = by_pmid[1001]
    assert (full.pub_year, full.pub_month, full.pub_day) == ("2020", "Mar", "15")
    assert full.medline_date is None
    assert full.pmid_version == 1

    # Year-only date: month/day absent (None), not defaulted to January/1.
    year_only = by_pmid[1002]
    assert (year_only.pub_year, year_only.pub_month, year_only.pub_day) == ("2019", None, None)

    # MedlineDate-only article keeps the free-text date and has no Year.
    medline = by_pmid[1003]
    assert medline.pub_year is None
    assert medline.medline_date == "1998 Spring"


def test_extracts_rich_fields(gz_fixture):
    from pubmed2db.parse import parse_file

    parsed = parse_file(gz_fixture("pubmed25n0001"))
    article = next(pa.article for pa in parsed.articles if pa.pubmed == 1001)

    assert [(t.label, t.text) for t in article.abstract] == [
        ("BACKGROUND", "First section of the abstract."),
        ("RESULTS", "Second section of the abstract."),
    ]
    assert {(h.mesh_id, h.major) for h in article.headings} == {("D000818", False), ("D006801", True)}
    assert {(x.prefix, x.identifier) for x in article.xrefs} == {
        ("doi", "10.1038/example1001"),
        ("pmc", "PMC1234567"),
    }
    assert article.journal.nlm_catalog_id == "0410462"


def test_collects_delete_citation(gz_fixture):
    from pubmed2db.parse import parse_file

    parsed = parse_file(gz_fixture("pubmed25n0002"))
    assert parsed.deleted_pmids == [1002]
    assert [pa.pubmed for pa in parsed.articles] == [1001]
    assert parsed.articles[0].pmid_version == 2
