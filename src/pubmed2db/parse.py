"""Parse PubMed XML files into structured records.

We drive the XML iteration ourselves (rather than using
``pubmed_downloader.iterate_process_*``) for two reasons:

1. We reuse the `pubmed_downloader library
   <https://github.com/cthoyt/pubmed-downloader>`_'s
   :func:`pubmed_downloader.api._extract_article` for the rich record (authors,
   MeSH, grants, citations, history, ...), called with all grounders ``None``
   so no heavy ``pyobo``/``orcid`` lookups happen.
2. In the same pass we capture what that library's pipeline drops or gets
   wrong: the *raw* ``PubDate`` components (so ``MedlineDate``-only and
   partial dates survive with full fidelity), ``<DeleteCitation>`` PMIDs
   (needed for latest-version selection), and the article's own IDs (see
   :func:`_article_ids`). Publication types and MeSH headings are read here
   too, because upstream's model of them loses names or order, or rewrites
   their IDs (:func:`_publication_types`, :func:`_mesh_headings`).

:func:`_cited_pmids` is deliberately unused — see its docstring.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from lxml import etree
from pubmed_downloader.api import Article, _extract_article

logger = logging.getLogger(__name__)


class MeshQualifier(NamedTuple):
    """A ``<QualifierName>``: its UI, its text and ``MajorTopicYN``, as PubMed wrote them."""

    ui: str | None
    name: str | None
    major: bool


class MeshHeading(NamedTuple):
    """A ``<MeshHeading>``: its descriptor's UI, text and ``MajorTopicYN``, and its qualifiers."""

    ui: str
    name: str | None
    major: bool
    qualifiers: list[MeshQualifier]


@dataclass
class ParsedArticle:
    """A ``pubmed_downloader`` :class:`Article` plus the fields we extract ourselves."""

    article: Article
    pmid_version: int | None = None
    #: Raw ``PubDate`` children, preserved verbatim. ``pub_month`` holds
    #: ``<Month>`` *or* ``<Season>`` (``"Mar"``, ``"03"``, ``"Sep-Dec"``), which
    #: the DTD makes mutually exclusive; ``medline_date`` holds free-text dates
    #: like ``"1998 Spring"``.
    pub_year: str | None = None
    pub_month: str | None = None
    pub_day: str | None = None
    medline_date: str | None = None
    #: ``(id_type, id_value)`` for the article itself; see :func:`_article_ids`
    #: for why we don't use the upstream ``Article.xrefs``.
    article_ids: list[tuple[str, str]] = field(default_factory=list)
    #: ``(mesh_ui, name)`` in PubMed's order; see :func:`_publication_types` for
    #: why we don't use the upstream ``Article.type_mesh_ids``.
    publication_types: list[tuple[str, str]] = field(default_factory=list)
    #: In PubMed's order; see :func:`_mesh_headings` for why we don't use the
    #: upstream ``Article.headings``.
    mesh_headings: list[MeshHeading] = field(default_factory=list)

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
    n_book_records: int = 0


_PUBDATE_PATH = "MedlineCitation/Article/Journal/JournalIssue/PubDate"


def _raw_pubdate(element: etree._Element) -> tuple[str | None, ...]:
    pub_date = element.find(_PUBDATE_PATH)
    if pub_date is None:
        return (None, None, None, None)
    return (
        pub_date.findtext("Year"),
        # The DTD makes these mutually exclusive -- PubDate is
        # ``((Year, ((Month, Day?) | Season)?) | MedlineDate)`` -- so a season
        # ("Sep-Dec", "Winter") can share the month's column instead of needing
        # one of its own, and a schema migration with it. The export passes it
        # through verbatim; see ``export.normalize_month``.
        pub_date.findtext("Month") or pub_date.findtext("Season"),
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


#: The article's publication types. Exact rather than ``.//``, for the reason
#: `_ARTICLE_ID_PATH` is: a descendant search is how two upstream fields ended up
#: reading another record's data.
_PUBLICATION_TYPE_PATH = "MedlineCitation/Article/PublicationTypeList/PublicationType"


def _publication_types(element: etree._Element) -> list[tuple[str, str]]:
    """``(mesh_ui, name)`` pairs, in document order.

    We can't use ``Article.type_mesh_ids``: upstream keeps only the UI and
    *sorts* it, losing both the name ("Review" is what a consumer filters on)
    and PubMed's order — which is not alphabetical (``Letter, Research
    Support…, Comment``) and is exactly the order esummary's ``pubtype``
    serves. The DTD requires ``UI``; an entry without one is skipped rather
    than stored as a type nothing can join on.
    """
    return [
        (ui, (pt.text or "").strip())
        for pt in element.findall(_PUBLICATION_TYPE_PATH)
        if (ui := (pt.get("UI") or "").strip())
    ]


_MESH_HEADING_PATH = "MedlineCitation/MeshHeadingList/MeshHeading"


def _mesh_headings(element: etree._Element) -> list[MeshHeading]:
    """The article's MeSH headings, in document order, as PubMed wrote them.

    Upstream's ``Heading`` is a model we would otherwise read the UI and name
    through, and it moves: 0.1.0 renamed its fields and runs every descriptor
    ID through ``_ground_mesh`` (prefix stripping, cutting at ``Q``), and drops
    a qualifier with no text where 0.0.14 rejected the article. None of that
    changed a value on real data (four 2026 files loaded identically under
    both versions), but the next release could, silently. Reading the elements
    here pins ``mesh_heading``/``mesh_qualifier`` to the XML, as the column
    comments say they are, and keeps the names PubMed's own (AGENTS.md: MeSH
    labels are per record, never looked up).

    Text is kept verbatim, ``None`` when the element is empty. A heading
    without ``DescriptorName/@UI``, which the DTD requires, is skipped rather
    than stored as a descriptor nothing can join on. ``MajorTopicYN``
    defaults to ``N``, as the DTD declares. Upstream still parses the headings
    inside ``_extract_article``, so an article it rejects is rejected before
    this runs.
    """
    headings = []
    for heading in element.iterfind(_MESH_HEADING_PATH):
        descriptor = heading.find("DescriptorName")
        if descriptor is None or not (ui := descriptor.get("UI")):
            continue
        headings.append(
            MeshHeading(
                ui=ui,
                name=descriptor.text,
                major=descriptor.get("MajorTopicYN", "N") == "Y",
                qualifiers=[
                    MeshQualifier(
                        ui=q.get("UI"), name=q.text, major=q.get("MajorTopicYN", "N") == "Y"
                    )
                    for q in heading.iterfind("QualifierName")
                ],
            )
        )
    return headings


def _cited_pmids(element: etree._Element) -> list[int]:
    """Cited PMIDs from ``<ReferenceList>``, deduplicated, in document order.

    **Parked: nothing calls this.** The citation graph was dropped — one real
    article carries ~444 references, so storing it corpus-wide would have made
    ``reference_citation`` the largest table in the database, for data no
    consumer wanted. Calling this per article would also cost an XPath search
    across all ~40M records for a result we discard.

    Kept because it is the working half of a known upstream bug we still intend
    to investigate (see FUTURE.md): ``Article.cites_pubmed_ids`` looks for
    ``.//ReferenceList/Reference`` under ``MedlineCitation``, but PubMed puts
    ``<ReferenceList>`` under ``<PubmedData>``, so upstream always yields an
    empty list on real data. Searching the whole ``PubmedArticle`` element finds
    it in either position.

    To re-enable: re-add the ``reference_citation`` table to ``schema.sql`` and
    to ``load._VERSIONED_TABLES`` (which `export` derives from), call this
    from :func:`parse_file`, and insert the rows in ``load._article_rows``.
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


#: The top-level children of ``PubmedArticleSet`` that `parse_file` reads. The
#: DTD is ``((PubmedArticle | PubmedBookArticle)+, DeleteCitation?)``.
_RECORD_TAGS = ("PubmedArticle", "PubmedBookArticle", "DeleteCitation")


def _parse_article(element: etree._Element, path: str | Path, result: ParsedFile) -> None:
    """Read one ``<PubmedArticle>`` into ``result``, counting one that is rejected."""
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
        return
    if article is None:
        # Upstream returns None rather than raising for an empty
        # <ArticleTitle> or a missing <MedlineJournalInfo> — both real in
        # PubMed. Count them, or they vanish from n_articles unremarked.
        logger.warning("skipping article in %s: extractor returned None", path)
        result.n_failed += 1
        return
    year, month, day, medline_date = _raw_pubdate(element)
    result.articles.append(
        ParsedArticle(
            article=article,
            pmid_version=_pmid_version(element),
            pub_year=year,
            pub_month=month,
            pub_day=day,
            medline_date=medline_date,
            article_ids=_article_ids(element),
            publication_types=_publication_types(element),
            mesh_headings=_mesh_headings(element),
        )
    )


def parse_file(path: str | Path) -> ParsedFile:
    """Parse a (gzipped) PubMed XML file into a :class:`ParsedFile`.

    The file is streamed with ``iterparse`` and each record's element is
    discarded once read, so the process holds the parsed records but never
    the document tree. ``etree.parse`` kept the whole tree alive while every
    record was built: ~1.2 GiB of a ~2.2 GiB per-file peak for a 30,000-article
    baseline file (#25), paid once per process today and once per *worker*
    when files are parsed in parallel (#68). Nothing needed the tree: every
    extractor works on one ``<PubmedArticle>`` at a time.

    ``iterparse`` does not decompress a ``.gz`` path the way ``etree.parse``
    does, hence the explicit ``gzip.open``. The three record kinds are read in
    one pass, in document order, so a file with a ``<DeleteCitation>`` and no
    articles, or the reverse, parses the same as before.
    """
    result = ParsedFile()
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as handle:
        for _event, element in etree.iterparse(handle, events=("end",), tag=_RECORD_TAGS):
            if element.tag == "PubmedArticle":
                _parse_article(element, path, result)
            elif element.tag == "PubmedBookArticle":
                # Bookshelf citations share these files per the DTD. We parse
                # journal citations only; say how many records that skipped
                # rather than dropping them silently — the count is what
                # issue #27 needs to decide whether to support them.
                result.n_book_records += 1
            else:
                for pmid_tag in element.findall("PMID"):
                    if pmid_tag.text and pmid_tag.text.strip().isdigit():
                        result.deleted_pmids.append(int(pmid_tag.text.strip()))
            # Drop the record, and any earlier siblings still hanging off the
            # root, so the tree never holds more than the current record.
            element.clear(keep_tail=True)
            while element.getprevious() is not None:
                del element.getparent()[0]

    if result.n_book_records:
        logger.warning(
            "%s: skipped %d PubmedBookArticle record(s); book citations are not parsed (see #27)",
            path,
            result.n_book_records,
        )
    if result.n_failed:
        logger.warning("%s: %d article(s) failed to parse and were skipped", path, result.n_failed)

    return result
