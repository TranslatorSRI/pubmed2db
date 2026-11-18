"""Tests for the self-driven XML parser."""

from __future__ import annotations

import pytest


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


def test_season_is_read_as_the_month():
    """`<Season>` shares the month's slot -- the DTD makes the two exclusive.

    Inline XML rather than a fourth fixture article, which would ripple through
    the record counts asserted in test_export, test_cli and test_validate. This
    is the form efetch serves PMID:8000234 in (issue #14); dropping it exported
    an empty pub_month.
    """
    from lxml import etree

    from pubmed2db.parse import _raw_pubdate

    element = etree.fromstring(
        "<PubmedArticle><MedlineCitation><Article><Journal><JournalIssue>"
        "<PubDate><Year>1994</Year><Season>Sep-Dec</Season></PubDate>"
        "</JournalIssue></Journal></Article></MedlineCitation></PubmedArticle>"
    )
    assert _raw_pubdate(element) == ("1994", "Sep-Dec", None, None)


def test_extracts_rich_fields(gz_fixture):
    from pubmed2db.parse import parse_file

    parsed = parse_file(gz_fixture("pubmed25n0001"))
    article = next(pa.article for pa in parsed.articles if pa.pubmed == 1001)

    assert [(t.label, t.text) for t in article.abstract] == [
        ("BACKGROUND", "First section of the abstract."),
        ("RESULTS", "Second section of the abstract."),
    ]
    assert article.journal.nlm_catalog_id == "0410462"


def test_mesh_headings_are_read_from_the_xml(gz_fixture):
    """UI, PubMed's own name, major flag and qualifiers, in document order."""
    from pubmed2db.parse import MeshHeading, MeshQualifier, parse_file

    parsed = parse_file(gz_fixture("pubmed25n0001"))
    article = next(pa for pa in parsed.articles if pa.pubmed == 1001)
    assert article.mesh_headings == [
        MeshHeading("D000818", "Animals", False, []),
        MeshHeading("D006801", "Humans", True, [MeshQualifier("Q000235", "genetics", False)]),
    ]


def test_mesh_headings_keep_pubmeds_text_and_skip_a_descriptor_without_a_ui():
    """Neither shape occurs in 2026 files; both are where upstream's model moved.

    pubmed-downloader 0.1.0 drops a qualifier with no text and rewrites an ID
    taken from a MeshHeading URI; neither should reach our tables.
    """
    from lxml import etree

    from pubmed2db.parse import MeshHeading, MeshQualifier, _mesh_headings

    element = etree.fromstring(
        "<PubmedArticle><MedlineCitation><MeshHeadingList>"
        '<MeshHeading URI="https://id.nlm.nih.gov/mesh/D000820Q000453">'
        "<DescriptorName>Animal Diseases</DescriptorName></MeshHeading>"
        '<MeshHeading><DescriptorName UI="D006801">Humans</DescriptorName>'
        '<QualifierName UI="Q000235" MajorTopicYN="Y"></QualifierName></MeshHeading>'
        "</MeshHeadingList></MedlineCitation></PubmedArticle>"
    )
    assert _mesh_headings(element) == [
        MeshHeading("D006801", "Humans", False, [MeshQualifier("Q000235", None, True)]),
    ]


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


def test_publication_types_keep_names_and_pubmed_order(gz_fixture):
    """Upstream's `type_mesh_ids` sorts the UIs and drops the names; ours keeps
    both in document order, which is the order esummary's `pubtype` serves --
    here deliberately not alphabetical by name or by UI."""
    from pubmed2db.parse import parse_file

    parsed = parse_file(gz_fixture("pubmed25n0002"))
    article = next(pa for pa in parsed.articles if pa.pubmed == 1001)

    assert article.publication_types == [
        ("D016422", "Letter"),
        ("D013485", "Research Support, Non-U.S. Gov't"),
        ("D016420", "Comment"),
    ]
    assert article.article.type_mesh_ids == ["D013485", "D016420", "D016422"]  # upstream


def test_publication_types_skip_an_entry_without_a_ui():
    from lxml import etree

    from pubmed2db.parse import _publication_types

    element = etree.fromstring(
        "<PubmedArticle><MedlineCitation><Article><PublicationTypeList>"
        '<PublicationType UI="D016454"> Review </PublicationType>'
        "<PublicationType>Orphan</PublicationType>"
        "</PublicationTypeList></Article></MedlineCitation></PubmedArticle>"
    )
    assert _publication_types(element) == [("D016454", "Review")]


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
        e for e in root.findall("PubmedArticle") if e.findtext("MedlineCitation/PMID") == "1001"
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


def test_article_ids_exclude_cited_references(gz_fixture):
    """A cited reference's ArticleIdList must not become the article's own.

    Upstream's `_extract_article` searches `.//ArticleIdList/ArticleId` under
    `PubmedData`, which also matches `ReferenceList/Reference/ArticleIdList`;
    `parse._article_ids` anchors to the direct child instead. This covers the
    update file, where v2 carries a *different* PMCID than v1.
    """
    from pubmed2db.parse import parse_file

    parsed = parse_file(gz_fixture("pubmed25n0002"))
    article = next(pa for pa in parsed.articles if pa.pubmed == 1001)

    assert set(article.article_ids) == {
        ("doi", "10.1038/example1001"),
        ("pmc", "PMC7654321"),
        ("pii", "S0140-6736(20)30183-5"),
    }
    # The cited paper's identifiers are absent, and so is our own PMID.
    assert not any(value.startswith("10.9999/") for _, value in article.article_ids)
    assert not any(id_type == "pubmed" for id_type, _ in article.article_ids)


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


def test_book_records_are_counted_not_dropped_silently(gz_fixture, caplog, tmp_path):
    """PubmedBookArticle shares the file per the DTD; we parse journal citations
    only, so the skip has to be visible (see issue #27)."""
    import gzip

    from pubmed2db.parse import parse_file

    src = gz_fixture("pubmed25n0001")
    with gzip.open(src, "rt", encoding="utf-8") as handle:
        xml = handle.read()
    book = (
        "<PubmedBookArticle><BookDocument><PMID Version='1'>9001</PMID>"
        "<ArticleTitle>A chapter</ArticleTitle></BookDocument></PubmedBookArticle>"
    )
    dst = tmp_path / "pubmed25n0003.xml.gz"
    with gzip.open(dst, "wt", encoding="utf-8") as handle:
        handle.write(xml.replace("</PubmedArticleSet>", book + "</PubmedArticleSet>"))

    with caplog.at_level("WARNING"):
        parsed = parse_file(dst)

    assert 9001 not in {a.pubmed for a in parsed.articles}
    assert "skipped 1 PubmedBookArticle record(s)" in caplog.text


def test_deletions_and_articles_each_parse_without_the_other(gz_fixture, tmp_path):
    """Streaming reads the three record kinds in one pass, so a file holding a
    DeleteCitation and no articles (a real shape for an update file) must still
    yield its deletions, and one with articles and no DeleteCitation none."""
    import gzip

    from pubmed2db.parse import parse_file

    xml = (
        "<?xml version='1.0'?><PubmedArticleSet><DeleteCitation>"
        "<PMID Version='1'>1002</PMID><PMID Version='1'>1003</PMID>"
        "</DeleteCitation></PubmedArticleSet>"
    )
    dst = tmp_path / "pubmed25n0009.xml.gz"
    with gzip.open(dst, "wt", encoding="utf-8") as handle:
        handle.write(xml)

    parsed = parse_file(dst)
    assert parsed.articles == []
    assert parsed.deleted_pmids == [1002, 1003]
    assert parsed.n_failed == 0 and parsed.n_book_records == 0

    # pubmed25n0001 has articles and no <DeleteCitation>.
    articles_only = parse_file(gz_fixture("pubmed25n0001"))
    assert articles_only.articles
    assert articles_only.deleted_pmids == []


@pytest.mark.parametrize("name", ["renamed.xml.GZ", "renamed.tmp", "plain.xml"])
def test_compression_is_read_from_the_content_not_the_name(gz_fixture, tmp_path, name):
    """`etree.parse` let libxml2 sniff for gzip, so a gzipped file parsed under
    any name; `iterparse` on a file object decompresses nothing, and choosing by
    extension would fail such a file at byte 0. `open_xml` sniffs the magic
    bytes instead, so a renamed gzip file and a plain one both parse."""
    import gzip
    import shutil

    from pubmed2db.parse import parse_file

    src = gz_fixture("pubmed25n0001")
    dst = tmp_path / name
    if name.startswith("plain"):
        with gzip.open(src, "rb") as handle:
            dst.write_bytes(handle.read())
    else:
        shutil.copyfile(src, dst)

    assert [a.pubmed for a in parse_file(dst).articles] == [
        a.pubmed for a in parse_file(src).articles
    ]


def test_iter_records_yields_direct_children_of_the_root_only():
    """`iterparse`'s `tag=` matches at any depth, but `root.findall` (which the
    streaming replaced) matched the root's children only. A nested match must
    be neither yielded nor pruned: pruning its "earlier siblings" would delete
    the start of the enclosing record before that record was read. It is
    counted instead, so the caller can say so."""
    import io
    from collections import Counter

    from pubmed2db.parse import iter_records

    xml = b"<Set><R><a/><R><b/></R><c/></R><x/><R><d/></R></Set>"
    nested = Counter()
    records = iter_records(io.BytesIO(xml), "R", nested=nested)
    seen = [[child.tag for child in el] for el in records]
    assert seen == [["a", "R", "c"], ["d"]]
    assert nested == {"R": 1}


def test_iter_records_can_yield_nested_matches_for_a_recovered_parse():
    """With `recover=True`, an unclosed element in one record puts every later
    record inside it. `yield_nested` reads those too, and prunes nothing
    around them, so the enclosing record keeps its own children: a loop that
    pruned around them (as `main`'s serial-catalog parse did) read the nested
    records and lost the enclosing one's ID."""
    import io

    from pubmed2db.parse import iter_records

    xml = (
        b"<Set><R><id>1</id><X></R>"  # <X> is never closed
        b"<R><id>2</id></R><R><id>3</id></R></Set>"
    )
    strict = iter_records(io.BytesIO(xml), "R", recover=True)
    assert [el.findtext("id") for el in strict] == ["1"]
    recovered = iter_records(io.BytesIO(xml), "R", yield_nested=True, recover=True)
    assert [el.findtext("id") for el in recovered] == ["2", "3", "1"]


def test_unrecognized_top_level_records_are_counted_and_logged(tmp_path, caplog):
    """Only the three tags the DTD allows are read as records. Anything else
    directly under the root, before, between or after them, is skipped,
    counted by tag and named in a warning, not mistaken for a deletion; and
    the same tag nested inside a record is not counted at all."""
    import gzip
    import logging

    from pubmed2db.parse import parse_file

    xml = (
        "<?xml version='1.0'?><PubmedArticleSet>"
        "<!-- a comment is not a record -->"
        "<Mystery><PMID>7</PMID></Mystery>"
        "<DeleteCitation><PMID Version='1'>1002</PMID></DeleteCitation>"
        "<Mystery/><Other/>"
        "<DeleteCitation><PMID Version='1'>1003<Mystery/></PMID></DeleteCitation>"
        "<Mystery/>"
        "</PubmedArticleSet>"
    )
    dst = tmp_path / "pubmed25n0009.xml.gz"
    with gzip.open(dst, "wt", encoding="utf-8") as handle:
        handle.write(xml)

    with caplog.at_level(logging.WARNING, logger="pubmed2db.parse"):
        parsed = parse_file(dst)
    assert parsed.deleted_pmids == [1002, 1003]
    assert parsed.unrecognized_records == {"Mystery": 3, "Other": 1}
    assert "skipped 4 top-level record(s)" in caplog.text
    assert "Mystery ×3, Other ×1" in caplog.text


def test_nested_records_are_counted_and_logged(tmp_path, caplog):
    """A record inside another element is skipped (pruning around it would
    truncate the element holding it), but not silently: a wrapper PubMed
    might add would otherwise hide every record inside it."""
    import gzip
    import logging

    from pubmed2db.parse import parse_file

    xml = (
        "<?xml version='1.0'?><PubmedArticleSet>"
        "<Wrapper><DeleteCitation><PMID Version='1'>1001</PMID></DeleteCitation>"
        "<PubmedBookArticle/><PubmedBookArticle/></Wrapper>"
        "<DeleteCitation><PMID Version='1'>1002</PMID></DeleteCitation>"
        "</PubmedArticleSet>"
    )
    dst = tmp_path / "pubmed25n0009.xml.gz"
    with gzip.open(dst, "wt", encoding="utf-8") as handle:
        handle.write(xml)

    with caplog.at_level(logging.WARNING, logger="pubmed2db.parse"):
        parsed = parse_file(dst)
    assert parsed.deleted_pmids == [1002]
    assert parsed.n_book_records == 0
    assert parsed.nested_records == {"PubmedBookArticle": 2, "DeleteCitation": 1}
    assert parsed.unrecognized_records == {"Wrapper": 1}
    assert "skipped 3 record(s) nested inside another element" in caplog.text
    assert "PubmedBookArticle ×2, DeleteCitation ×1" in caplog.text


def test_a_known_file_has_no_unrecognized_records(gz_fixture, caplog):
    from pubmed2db.parse import parse_file

    for stem in ("pubmed25n0001", "pubmed25n0002"):
        parsed = parse_file(gz_fixture(stem))
        assert parsed.unrecognized_records == {}
        assert parsed.nested_records == {}
    assert "does not know" not in caplog.text
    assert "nested" not in caplog.text
