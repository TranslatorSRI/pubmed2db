"""Parse PubMed XML files into structured records.

We drive the XML iteration ourselves (rather than using
``pubmed_downloader.iterate_process_*``) for two reasons:

1. We reuse cthoyt's :func:`pubmed_downloader.api._extract_article` for the rich
   record (authors, MeSH, grants, citations, history, ...), called with all
   grounders ``None`` so no heavy ``pyobo``/``orcid`` lookups happen.
2. In the same pass we capture three things cthoyt's pipeline drops: the *raw*
   ``PubDate`` components (so ``MedlineDate``-only and partial dates survive with
   full fidelity), ``<DeleteCitation>`` PMIDs (needed for latest-version
   selection), the cited PMIDs from ``<ReferenceList>`` (see
   :func:`_cited_pmids`), and the article's own IDs (see :func:`_article_ids`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from pubmed_downloader.api import Article, _extract_article

logger = logging.getLogger(__name__)


@dataclass
class ParsedArticle:
    """A cthoyt :class:`Article` plus the raw fields we extract ourselves."""

    article: Article
    pmid_version: int | None = None
    #: Raw ``PubDate`` children, preserved verbatim (e.g. month ``"Mar"`` or
    #: ``"03"``); ``medline_date`` holds free-text dates like ``"1998 Spring"``.
    pub_year: str | None = None
    pub_month: str | None = None
    pub_day: str | None = None
    medline_date: str | None = None
    #: PMIDs this article cites; see :func:`_cited_pmids` for why we don't use
    #: the upstream ``Article.cites_pubmed_ids``.
    cited_pmids: list[int] = field(default_factory=list)
    #: ``(id_type, id_value)`` for the article itself; see :func:`_article_ids`
    #: for why we don't use the upstream ``Article.xrefs``.
    article_ids: list[tuple[str, str]] = field(default_factory=list)

    @property
    def pubmed(self) -> int:
        """The PMID."""
        return self.article.pubmed


@dataclass
class ParsedFile:
    """The result of parsing one PubMed XML file."""

    articles: list[ParsedArticle] = field(default_factory=list)
    deleted_pmids: list[int] = field(default_factory=list)
    n_failed: int = 0


_PUBDATE_PATH = "MedlineCitation/Article/Journal/JournalIssue/PubDate"


def _raw_pubdate(element: etree._Element) -> tuple[str | None, ...]:
    pub_date = element.find(_PUBDATE_PATH)
    if pub_date is None:
        return (None, None, None, None)
    return (
        pub_date.findtext("Year"),
        pub_date.findtext("Month"),
        pub_date.findtext("Day"),
        pub_date.findtext("MedlineDate"),
    )


#: The article's *own* ``ArticleIdList`` — a direct child of ``PubmedData``, and
#: the path Babel reads (``createcompendia/publications.py``).
_ARTICLE_ID_PATH = "PubmedData/ArticleIdList/ArticleId"


def _article_ids(element: etree._Element) -> list[tuple[str, str]]:
    """``(id_type, id_value)`` pairs for the article itself.

    We can't use ``Article.xrefs``: upstream collects
    ``PubmedData.findall(".//ArticleIdList/ArticleId")``, and that ``.//``
    descends into ``<ReferenceList>``, so every *cited* reference's DOI/PMID is
    attributed to the citing article — one observed record (PMID:41136637)
    picked up 426 DOIs that were not its own. The direct path above takes only
    the article's own ``<ArticleIdList>``. See FUTURE.md.

    The ``pubmed`` entry is dropped: it just restates the ``pmid`` column.
    """
    return [
        (article_id.get("IdType"), article_id.text.strip())
        for article_id in element.findall(_ARTICLE_ID_PATH)
        if article_id.get("IdType") not in (None, "pubmed")
        and article_id.text
        and article_id.text.strip()
    ]


def _cited_pmids(element: etree._Element) -> list[int]:
    """Cited PMIDs from ``<ReferenceList>``, deduplicated, in document order.

    We can't use ``Article.cites_pubmed_ids``: upstream looks for
    ``.//ReferenceList/Reference`` under ``MedlineCitation``, but PubMed puts
    ``<ReferenceList>`` under ``<PubmedData>``, so upstream always yields an
    empty list on real data. Searching the whole ``PubmedArticle`` element
    finds it in either position. See FUTURE.md.
    """
    seen: dict[int, None] = {}
    for article_id in element.findall(".//ReferenceList/Reference//ArticleIdList/ArticleId"):
        if article_id.get("IdType") == "pubmed" and article_id.text:
            text = article_id.text.strip()
            if text.isdigit():
                seen.setdefault(int(text), None)
    return list(seen)



def _pmid_version(element: etree._Element) -> int | None:
    pmid_tag = element.find("MedlineCitation/PMID")
    if pmid_tag is None:
        return None
    version = pmid_tag.get("Version")
    return int(version) if version else None


def parse_file(path: str | Path) -> ParsedFile:
    """Parse a (gzipped) PubMed XML file into a :class:`ParsedFile`.

    lxml transparently decompresses ``.xml.gz`` files given a path.
    """
    tree = etree.parse(os.fspath(path))
    root = tree.getroot()

    result = ParsedFile()

    for element in root.findall("PubmedArticle"):
        try:
            article = _extract_article(
                element,
                ror_grounder=None,
                mesh_grounder=None,
                author_grounder=None,
            )
        except (ValueError, KeyError) as exc:
            logger.warning("skipping article in %s: %s", path, exc)
            result.n_failed += 1
            continue
        if article is None:
            continue
        year, month, day, medline_date = _raw_pubdate(element)
        result.articles.append(
            ParsedArticle(
                article=article,
                pmid_version=_pmid_version(element),
                pub_year=year,
                pub_month=month,
                pub_day=day,
                medline_date=medline_date,
                cited_pmids=_cited_pmids(element),
                article_ids=_article_ids(element),
            )
        )

    for pmid_tag in root.findall("DeleteCitation/PMID"):
        if pmid_tag.text and pmid_tag.text.strip().isdigit():
            result.deleted_pmids.append(int(pmid_tag.text.strip()))

    if result.n_failed:
        logger.warning(
            "%s: %d article(s) failed to parse and were skipped", path, result.n_failed
        )

    return result
