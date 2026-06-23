"""Load parsed PubMed files into the normalized DuckDB tables.

Loading keeps full version history: every file's rows are tagged with their
``source_file`` provenance, so a PMID revised across many files coexists as
several rows. Re-loading a file is idempotent — its existing rows are deleted
first — which is also how an MD5 change triggers a refresh.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
from pubmed_downloader.utils import Collective

from .db import get_registry, parse_file_name
from .parse import ParsedArticle, ParsedFile, parse_file

logger = logging.getLogger(__name__)

#: Per-version tables that carry a ``source_file`` column (cleared on reload).
_VERSIONED_TABLES = (
    "article",
    "abstract_text",
    "author",
    "author_affiliation",
    "mesh_heading",
    "mesh_qualifier",
    "publication_type",
    "grant_",
    "reference_citation",
    "article_id",
    "history",
    "deleted_pmid",
)


def _article_rows(pa: ParsedArticle, source_file: str, order_key: int) -> dict[str, list[tuple]]:
    """Build the per-table insert tuples for a single parsed article."""
    a = pa.article
    pmid = a.pubmed
    ji = a.journal_issue
    rows: dict[str, list[tuple]] = {t: [] for t in _VERSIONED_TABLES if t != "deleted_pmid"}

    rows["article"].append(
        (
            pmid,
            pa.pmid_version,
            source_file,
            order_key,
            a.title,
            a.journal.nlm_catalog_id,
            a.journal.issn,
            ji.volume,
            ji.issue,
            pa.pub_year,
            pa.pub_month,
            pa.pub_day,
            pa.medline_date,
            a.date_completed,
            a.date_revised,
        )
    )

    for seq, text in enumerate(a.abstract):
        rows["abstract_text"].append(
            (pmid, source_file, seq, text.label, text.category, text.text)
        )

    for author in a.authors:
        is_collective = isinstance(author, Collective)
        rows["author"].append(
            (
                pmid,
                source_file,
                author.position,
                "collective" if is_collective else "author",
                author.name,
                None if is_collective else author.orcid,
                None if is_collective else author.valid,
            )
        )
        if not is_collective:
            for aff in author.affiliations:
                ror = aff.reference.curie if aff.reference else None
                rows["author_affiliation"].append(
                    (pmid, source_file, author.position, aff.name, ror)
                )

    for heading in a.headings:
        rows["mesh_heading"].append(
            (pmid, source_file, heading.mesh_id, heading.name, heading.major)
        )
        for qualifier in heading.qualifiers or []:
            rows["mesh_qualifier"].append(
                (pmid, source_file, heading.mesh_id, qualifier.mesh_id, qualifier.name, qualifier.major)
            )

    for type_ui in a.type_mesh_ids:
        rows["publication_type"].append((pmid, source_file, type_ui))

    for g in a.grants:
        rows["grant_"].append((pmid, source_file, g.id, g.acronym, g.agency, g.country))

    for cited in a.cites_pubmed_ids:
        rows["reference_citation"].append((pmid, source_file, cited))

    for xref in a.xrefs:
        rows["article_id"].append((pmid, source_file, xref.prefix, xref.identifier))

    for h in a.history:
        rows["history"].append((pmid, source_file, h.status, h.date))

    return rows


_INSERTS: dict[str, str] = {
    "article": "INSERT INTO article VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, now())",
    "abstract_text": "INSERT INTO abstract_text VALUES (?,?,?,?,?,?)",
    "author": "INSERT INTO author VALUES (?,?,?,?,?,?,?)",
    "author_affiliation": "INSERT INTO author_affiliation VALUES (?,?,?,?,?)",
    "mesh_heading": "INSERT INTO mesh_heading VALUES (?,?,?,?,?)",
    "mesh_qualifier": "INSERT INTO mesh_qualifier VALUES (?,?,?,?,?,?)",
    "publication_type": "INSERT INTO publication_type VALUES (?,?,?)",
    "grant_": "INSERT INTO grant_ VALUES (?,?,?,?,?,?)",
    "reference_citation": "INSERT INTO reference_citation VALUES (?,?,?)",
    "article_id": "INSERT INTO article_id VALUES (?,?,?,?)",
    "history": "INSERT INTO history VALUES (?,?,?,?)",
    "deleted_pmid": "INSERT INTO deleted_pmid VALUES (?,?,?)",
}


def delete_file_rows(con: duckdb.DuckDBPyConnection, source_file: str) -> None:
    """Remove every row previously loaded from ``source_file``."""
    for table in _VERSIONED_TABLES:
        con.execute(f"DELETE FROM {table} WHERE source_file = ?", [source_file])


def load_parsed(
    con: duckdb.DuckDBPyConnection,
    parsed: ParsedFile,
    source_file: str,
    *,
    kind: str,
) -> None:
    """Insert a :class:`ParsedFile` (replacing any prior rows for the file)."""
    year_yy, file_number, order_key = parse_file_name(source_file)

    con.execute("BEGIN TRANSACTION")
    try:
        delete_file_rows(con, source_file)

        batches: dict[str, list[tuple]] = {t: [] for t in _INSERTS}
        for pa in parsed.articles:
            for table, table_rows in _article_rows(pa, source_file, order_key).items():
                batches[table].extend(table_rows)
        for pmid in parsed.deleted_pmids:
            batches["deleted_pmid"].append((pmid, source_file, order_key))

        for table, rows in batches.items():
            if rows:
                con.executemany(_INSERTS[table], rows)

        con.execute(
            """
            INSERT INTO source_file
                (file_name, kind, year_yy, file_number, file_order_key,
                 processed_at, n_articles, n_deletions)
            VALUES (?, ?, ?, ?, ?, now(), ?, ?)
            ON CONFLICT (file_name) DO UPDATE SET
                kind = excluded.kind,
                year_yy = excluded.year_yy,
                file_number = excluded.file_number,
                file_order_key = excluded.file_order_key,
                processed_at = excluded.processed_at,
                n_articles = excluded.n_articles,
                n_deletions = excluded.n_deletions
            """,
            [
                source_file,
                kind,
                year_yy,
                file_number,
                order_key,
                len(parsed.articles),
                len(parsed.deleted_pmids),
            ],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def load_file(
    con: duckdb.DuckDBPyConnection,
    path: str | Path,
    *,
    kind: str,
    source_file: str | None = None,
) -> ParsedFile:
    """Parse and load a single PubMed file, replacing any prior rows for it."""
    path = Path(path)
    source_file = source_file or path.name
    parsed = parse_file(path)
    load_parsed(con, parsed, source_file, kind=kind)
    logger.info(
        "loaded %s: %d articles, %d deletions",
        source_file,
        len(parsed.articles),
        len(parsed.deleted_pmids),
    )
    return parsed


def needs_load(con: duckdb.DuckDBPyConnection, source_file: str, *, force: bool = False) -> bool:
    """Whether a file should be (re)loaded.

    Loads when never processed, when re-downloaded since the last load
    (``downloaded_at > processed_at``, i.e. the published MD5 changed), or when
    ``force`` is set.
    """
    if force:
        return True
    row = con.execute(
        "SELECT processed_at, downloaded_at FROM source_file WHERE file_name = ?",
        [source_file],
    ).fetchone()
    if row is None or row[0] is None:
        return True
    processed_at, downloaded_at = row
    return downloaded_at is not None and downloaded_at > processed_at


def load_files(
    con: duckdb.DuckDBPyConnection,
    files: list[tuple[Path, str]],
    *,
    force: bool = False,
) -> int:
    """Load a list of ``(path, kind)`` files in chronological order, skipping
    ones already up to date. Returns the number of files loaded."""
    ordered = sorted(files, key=lambda pk: parse_file_name(pk[0].name)[2])
    loaded = 0
    for path, kind in ordered:
        if not needs_load(con, path.name, force=force):
            logger.debug("skipping up-to-date %s", path.name)
            continue
        load_file(con, path, kind=kind)
        loaded += 1
    return loaded


#: Maps keys in NLM's J_Entrez/J_Medline overview file to our journal columns.
_JOURNAL_KEYS = {
    "JournalTitle": "title",
    "MedAbbr": "abbreviation_medline",
    "IsoAbbr": "abbreviation_iso",
    "NlmId": "nlm_catalog_id",
}


def _parse_journal_overview(path: Path):
    """Yield ``(record, issns)`` from an NLM journal overview file.

    The file is a series of ``key: value`` blocks separated by ``---`` lines.
    We parse it directly rather than via ``pubmed_downloader.catalog`` because
    that package's ``Journal`` model (<=0.0.14) requires start/end years that the
    overview file does not provide, so it raises on the real data.
    """
    record: dict[str, str] = {}
    issns: list[tuple[str, str]] = []

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("---"):
                if record.get("nlm_catalog_id"):
                    yield record, issns
                record, issns = {}, []
                continue
            key, sep, value = (part.strip() for part in line.partition(":"))
            if not sep or not value:
                continue
            if key == "ISSN (Print)":
                issns.append((value, "Print"))
            elif key == "ISSN (Online)":
                issns.append((value, "Electronic"))
            elif key in _JOURNAL_KEYS:
                record[_JOURNAL_KEYS[key]] = value
    if record.get("nlm_catalog_id"):
        yield record, issns


def load_journals(con: duckdb.DuckDBPyConnection, *, force: bool = False) -> int:
    """Load the NLM Catalog journal dimension.

    Downloads NLM's journal overview (J_Entrez) via ``pubmed_downloader`` and
    replaces the ``journal`` / ``journal_issn`` tables. Returns the number of
    journals loaded.
    """
    from pubmed_downloader.catalog import ensure_journal_overview

    path = Path(ensure_journal_overview(force=force))

    journals: dict[str, dict] = {}
    issn_rows: list[tuple[str, str, str]] = []
    for record, issns in _parse_journal_overview(path):
        nlm_id = record["nlm_catalog_id"]
        if nlm_id in journals:  # first occurrence wins; nlm_catalog_id is the PK
            continue
        journals[nlm_id] = record
        for value, issn_type in issns:
            issn_rows.append((nlm_id, value, issn_type))

    con.execute("DELETE FROM journal")
    con.execute("DELETE FROM journal_issn")
    con.executemany(
        "INSERT INTO journal VALUES (?,?,?,?,?,?,?)",
        [
            (
                nlm_id,
                rec.get("title"),
                rec.get("abbreviation_medline"),
                rec.get("abbreviation_iso"),
                None,  # start_year: not present in the overview file
                None,  # end_year: not present in the overview file
                None,  # active: unknown from the overview file
            )
            for nlm_id, rec in journals.items()
        ],
    )
    con.executemany("INSERT INTO journal_issn VALUES (?,?,?)", issn_rows)
    return len(journals)
