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
    assert article.journal.nlm_catalog_id == "0410462"


def test_article_ids_exclude_reference_ids(gz_fixture):
    from pubmed2db.parse import parse_file

    parsed = parse_file(gz_fixture("pubmed25n0001"))
    article = next(pa for pa in parsed.articles if pa.pubmed == 1001)

    # The article's own IDs only -- not the DOI of the reference it cites, and
    # not its own PMID (already the `pmid` column).
    assert set(article.article_ids) == {
        ("doi", "10.1038/example1001"),
        ("pmc", "PMC1234567"),
    }
    # Upstream's `.//ArticleIdList/ArticleId` descends into ReferenceList and
    # picks up the cited reference's DOI. When this stops holding, upstream has
    # fixed it and _article_ids can go. See FUTURE.md.
    assert ("doi", "10.1000/nopmid") in {(x.prefix, x.identifier) for x in article.article.xrefs}


def test_cited_pmids_is_parked_but_works(gz_fixture):
    """`_cited_pmids` is not wired into parse_file -- the citation graph is not
    stored. Kept working so re-enabling it is a one-line change, and because it
    is the counter-example to the upstream bug tracked in FUTURE.md.
    """
    import gzip

    from lxml import etree

    from pubmed2db.parse import _cited_pmids, parse_file

    with gzip.open(gz_fixture("pubmed25n0001"), "rb") as handle:
        root = etree.parse(handle).getroot()
    element = next(
        e for e in root.findall("PubmedArticle")
        if e.findtext("MedlineCitation/PMID") == "1001"
    )
    # Deduplicated; the DOI-only reference contributes nothing.
    assert _cited_pmids(element) == [9001]

    # Nothing populates a cited_pmids field any more...
    parsed = parse_file(gz_fixture("pubmed25n0001"))
    article = next(pa for pa in parsed.articles if pa.pubmed == 1001)
    assert not hasattr(article, "cited_pmids")
    # ...and upstream still finds nothing, because it searches MedlineCitation
    # for a ReferenceList that PubMed nests under PubmedData. When this starts
    # failing, upstream has fixed it. See FUTURE.md.
    assert article.article.cites_pubmed_ids == []


def test_collects_delete_citation(gz_fixture):
    from pubmed2db.parse import parse_file

    parsed = parse_file(gz_fixture("pubmed25n0002"))
    assert parsed.deleted_pmids == [1002]
    assert [pa.pubmed for pa in parsed.articles] == [1001]
    assert parsed.articles[0].pmid_version == 2


def test_articles_dropped_by_the_extractor_are_counted(gz_fixture, monkeypatch):
    """Upstream returns None (rather than raising) for e.g. an empty
    <ArticleTitle>; those must show up in n_failed, not vanish silently."""
    from pubmed2db import parse

    monkeypatch.setattr(parse, "_extract_article", lambda *a, **k: None)
    parsed = parse.parse_file(gz_fixture("pubmed25n0001"))

    assert parsed.articles == []
    assert parsed.n_failed > 0
