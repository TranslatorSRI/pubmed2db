"""Load parsed PubMed files into the normalized DuckDB tables.

Loading keeps full version history: every file's rows are tagged with their
``source_file`` provenance, so a PMID revised across many files coexists as
several rows. Re-loading a file is idempotent — its existing rows are deleted
first — which is also how an MD5 change triggers a refresh.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import duckdb
import pyarrow as pa
from pubmed_downloader.utils import Collective

from .db import parse_file_name, record_run
from .parse import ParsedArticle, ParsedFile, parse_file
from .status import NEEDS_LOAD_SQL
from .util import eta_str, peak_rss_gib

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
    "article_id",
    "history",
    "deleted_pmid",
)


def _article_rows(parsed: ParsedArticle, source_file: str, order_key: int) -> dict[str, list[tuple]]:
    """Build the per-table insert tuples for a single parsed article."""
    a = parsed.article
    pmid = a.pubmed
    ji = a.journal_issue
    rows: dict[str, list[tuple]] = {t: [] for t in _VERSIONED_TABLES if t != "deleted_pmid"}

    rows["article"].append(
        (
            pmid,
            parsed.pmid_version,
            source_file,
            order_key,
            a.title,
            a.journal.nlm_catalog_id,
            a.journal.issn,
            ji.volume,
            ji.issue,
            parsed.pub_year,
            parsed.pub_month,
            parsed.pub_day,
            parsed.medline_date,
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

    for id_type, id_value in parsed.article_ids:
        rows["article_id"].append((pmid, source_file, id_type, id_value))

    for h in a.history:
        rows["history"].append((pmid, source_file, h.status, h.date))

    return rows


#: Name under which a per-table batch is registered for the bulk insert below.
_BATCH = "_load_batch"

#: Bulk-insert SQL per table. Each row batch is registered as an Arrow table and
#: inserted columnar via ``INSERT ... SELECT``, which is ~25x faster than
#: row-by-row ``executemany`` on the large per-version tables (parse stays ~2s
#: while insert drops from ~75s to ~4s per file — see scripts/benchmark_load.py).
#: ``article`` appends ``now()`` for its server-side ``loaded_at`` column.
_INSERT_SELECT: dict[str, str] = {
    table: (
        f"INSERT INTO {table} BY POSITION SELECT *, now() FROM {_BATCH}"
        if table == "article"
        else f"INSERT INTO {table} BY POSITION SELECT * FROM {_BATCH}"
    )
    for table in _VERSIONED_TABLES
}


def _insert_batch(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """Columnar bulk-insert of ``rows`` (positional tuples) into ``table``."""
    columns = zip(*rows)  # transpose row tuples -> per-column sequences
    batch = pa.table({str(i): pa.array(col) for i, col in enumerate(columns)})
    con.register(_BATCH, batch)
    try:
        con.execute(_INSERT_SELECT[table])
    finally:
        con.unregister(_BATCH)


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

        # A PMID can appear more than once within a single file (e.g. citation
        # correction artifacts); keep only the last occurrence so `article`'s
        # documented (pmid, source_file) identity actually holds and
        # `latest_article`'s per-file ranking never needs a tiebreaker.
        deduped: dict[int, ParsedArticle] = {}
        for article in parsed.articles:
            deduped[article.pubmed] = article

        batches: dict[str, list[tuple]] = {t: [] for t in _INSERT_SELECT}
        for article in deduped.values():
            for table, table_rows in _article_rows(article, source_file, order_key).items():
                batches[table].extend(table_rows)
        for pmid in parsed.deleted_pmids:
            batches["deleted_pmid"].append((pmid, source_file, order_key))

        for table, rows in batches.items():
            if rows:
                _insert_batch(con, table, rows)

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
                len(deduped),
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
        "loaded %s: %d articles, %d deletions, %d failed to parse (peak RSS %.1f GiB)",
        source_file,
        len(parsed.articles),
        len(parsed.deleted_pmids),
        parsed.n_failed,
        peak_rss_gib(),
    )
    return parsed


def needs_load(con: duckdb.DuckDBPyConnection, source_file: str, *, force: bool = False) -> bool:
    """Whether a file should be (re)loaded.

    Single-file form of :data:`pubmed2db.status.NEEDS_LOAD_SQL` (the same rule
    :func:`pubmed2db.status.pending_file_count` applies registry-wide), plus a
    never-registered file and ``force`` both counting as needing a load.
    """
    if force:
        return True
    row = con.execute(
        f"SELECT ({NEEDS_LOAD_SQL}) FROM source_file WHERE file_name = ?",
        [source_file],
    ).fetchone()
    return row is None or bool(row[0])


def load_files(
    con: duckdb.DuckDBPyConnection,
    files: list[tuple[Path, str]],
    *,
    force: bool = False,
) -> tuple[int, list[str]]:
    """Load a list of ``(path, kind)`` files in chronological order, skipping
    ones already up to date.

    Returns ``(n_loaded, failed_file_names)``. A file that fails to load is
    logged and skipped rather than aborting the run: a full baseline is ~1,300
    files, and one truncated download shouldn't cost a multi-hour job. Each
    file loads in its own transaction, so a failure leaves no partial rows and
    no ``processed_at`` watermark — a later run retries it.
    """
    ordered = sorted(files, key=lambda pk: parse_file_name(pk[0].name)[2])
    to_load = [(p, k) for p, k in ordered if needs_load(con, p.name, force=force)]
    total = len(to_load)
    if total == 0:
        return 0, []

    run_start = time.monotonic()
    loaded = 0
    failed: list[str] = []
    for i, (path, kind) in enumerate(to_load):
        try:
            load_file(con, path, kind=kind)
            loaded += 1
        except Exception:  # noqa: BLE001 - keep going; the file is retried next run
            logger.exception("failed to load %s; skipping", path.name)
            failed.append(path.name)
        done = i + 1
        remaining = total - done
        elapsed = time.monotonic() - run_start
        eta = eta_str(elapsed, done, remaining)
        logger.info(
            "progress: %d/%d files this run, %d remaining, ~%s to go",
            done, total, remaining, eta,
        )

    if failed:
        logger.error(
            "%d of %d file(s) failed to load and were skipped: %s",
            len(failed), total, ", ".join(failed),
        )

    return loaded, failed


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

    con.execute("BEGIN TRANSACTION")
    try:
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
        record_run(con, "journals")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(journals)
