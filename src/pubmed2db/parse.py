"""Parse PubMed XML files into structured records.

We drive the XML iteration ourselves (rather than using
``pubmed_downloader.iterate_process_*``) for two reasons:

1. We reuse cthoyt's :func:`pubmed_downloader.api._extract_article` for the rich
   record (authors, MeSH, grants, citations, history, ...), called with all
   grounders ``None`` so no heavy ``pyobo``/``orcid`` lookups happen.
2. In the same pass we capture two things cthoyt's pipeline drops: the *raw*
   ``PubDate`` components (so ``MedlineDate``-only and partial dates survive with
   full fidelity) and ``<DeleteCitation>`` PMIDs (needed for latest-version
   selection).
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

    @property
    def pubmed(self) -> int:
        """The PMID."""
        return self.article.pubmed


@dataclass
class ParsedFile:
    """The result of parsing one PubMed XML file."""

    articles: list[ParsedArticle] = field(default_factory=list)
    deleted_pmids: list[int] = field(default_factory=list)


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
            )
        )

    for pmid_tag in root.findall("DeleteCitation/PMID"):
        if pmid_tag.text and pmid_tag.text.strip().isdigit():
            result.deleted_pmids.append(int(pmid_tag.text.strip()))

    return result
