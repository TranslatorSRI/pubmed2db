"""Load parsed PubMed files into the normalized DuckDB tables.

Loading keeps full version history: every file's rows are tagged with their
``source_file`` provenance, so a PMID revised across many files coexists as
several rows. Re-loading a file is idempotent — its existing rows are deleted
first — which is also how an MD5 change triggers a refresh.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import duckdb
import pyarrow as pa
import pystow
import requests
from lxml import etree
from pubmed_downloader.utils import Collective

from .datamodel import record_load
from .db import NEEDS_LOAD_SQL, parse_file_name, record_run
from .parse import ParsedArticle, ParsedFile, parse_file
from .util import current_rss_gib, download_file, eta_str, fmt_duration, peak_rss_gib

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
    "article_grant",
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

    for position, text in enumerate(a.abstract):
        rows["abstract_text"].append(
            (pmid, source_file, position, text.label, text.category, text.text)
        )

    for author in a.authors:
        is_collective = isinstance(author, Collective)
        # Upstream numbers every <Author> from 1 and then drops the ones it
        # cannot name, so this is the XML index made 0-based like every other
        # ordering column: a gap is an author upstream dropped, not a bug here.
        position = author.position - 1
        rows["author"].append(
            (
                pmid,
                source_file,
                position,
                "collective" if is_collective else "author",
                author.name,
                None if is_collective else author.orcid,
                None if is_collective else author.valid,
            )
        )
        if not is_collective:
            for aff_position, aff in enumerate(author.affiliations):
                rows["author_affiliation"].append(
                    (pmid, source_file, position, aff_position, aff.name)
                )

    # A descriptor can repeat within one record, so a qualifier is tied to its
    # heading by position; the descriptor UI alone was ambiguous.
    for heading_position, heading in enumerate(parsed.mesh_headings):
        rows["mesh_heading"].append(
            (pmid, source_file, heading_position, heading.ui, heading.name, heading.major)
        )
        for qualifier in heading.qualifiers:
            rows["mesh_qualifier"].append(
                (pmid, source_file, heading_position, heading.ui,
                 qualifier.ui, qualifier.name, qualifier.major)
            )

    for position, (type_ui, type_name) in enumerate(parsed.publication_types):
        rows["publication_type"].append((pmid, source_file, position, type_ui, type_name))

    for g in a.grants:
        rows["article_grant"].append((pmid, source_file, g.id, g.acronym, g.agency, g.country))

    for id_type, id_value in parsed.article_ids:
        rows["article_id"].append((pmid, source_file, id_type, id_value))

    for h in a.history:
        rows["history"].append((pmid, source_file, h.status, h.date))

    return rows


#: Name under which a per-table batch is registered for the bulk insert below.
_BATCH = "_load_batch"

def _insert_batch(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """Columnar bulk-insert of ``rows`` (positional tuples) into ``table``.

    Registering the batch as an Arrow table and inserting it via
    ``INSERT ... SELECT`` is ~25x faster than row-by-row ``executemany`` on the
    large per-version tables (parse stays ~2s while insert drops from ~75s to
    ~4s per file — see scripts/benchmark_load.py).
    """
    columns = zip(*rows)  # transpose row tuples -> per-column sequences
    batch = pa.table({str(i): pa.array(col) for i, col in enumerate(columns)})
    con.register(_BATCH, batch)
    try:
        # `article` appends now() for its server-side `loaded_at` column.
        loaded_at = ", now()" if table == "article" else ""
        con.execute(f"INSERT INTO {table} BY POSITION SELECT *{loaded_at} FROM {_BATCH}")
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
) -> int:
    """Insert a :class:`ParsedFile` (replacing any prior rows for the file).

    Returns the number of articles stored, which is the post-dedup count
    recorded in ``source_file.n_articles``.
    """
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

        batches: dict[str, list[tuple]] = {t: [] for t in _VERSIONED_TABLES}
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
                 downloaded_at, processed_at, n_articles, n_deletions,
                 n_failed, n_book_records)
            VALUES (?, ?, ?, ?, ?, now(), now(), ?, ?, ?, ?)
            ON CONFLICT (file_name) DO UPDATE SET
                kind = excluded.kind,
                -- Files obtained outside `download` (rsync/FTP) have no
                -- downloaded_at; stamp one so the status arithmetic and
                -- needs_load's `downloaded_at IS NOT NULL` still work. A real
                -- download's timestamp is never overwritten.
                downloaded_at = COALESCE(source_file.downloaded_at, excluded.downloaded_at),
                year_yy = excluded.year_yy,
                file_number = excluded.file_number,
                file_order_key = excluded.file_order_key,
                processed_at = excluded.processed_at,
                n_articles = excluded.n_articles,
                n_deletions = excluded.n_deletions,
                n_failed = excluded.n_failed,
                n_book_records = excluded.n_book_records
            """,
            [
                source_file,
                kind,
                year_yy,
                file_number,
                order_key,
                len(deduped),
                len(parsed.deleted_pmids),
                parsed.n_failed,
                parsed.n_book_records,
            ],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(deduped)


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
    n_articles = load_parsed(con, parsed, source_file, kind=kind)
    # Report RSS *now* as well as the high-water mark: ru_maxrss only ever rises,
    # so on its own a growing number says nothing about whether this process is
    # actually holding more memory than it was ten files ago.
    current = current_rss_gib()
    logger.info(
        "loaded %s: %d articles, %d deletions, %d failed to parse, "
        "%d book record(s) skipped (RSS %s, peak %.1f GiB)",
        source_file,
        n_articles,
        len(parsed.deleted_pmids),
        parsed.n_failed,
        parsed.n_book_records,
        "n/a" if current is None else f"{current:.1f} GiB",
        peak_rss_gib(),
    )
    return parsed


def needs_load(con: duckdb.DuckDBPyConnection, source_file: str, *, force: bool = False) -> bool:
    """Whether a file should be (re)loaded.

    Single-file form of :data:`pubmed2db.db.NEEDS_LOAD_SQL` (the same rule
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
    # Filter before sorting: parse_file_name raises on anything that isn't a
    # PubMed XML filename, and one stray file in the download directory must not
    # abort the run before a single file is loaded.
    keyed: list[tuple[int, Path, str]] = []
    for path, kind in files:
        try:
            keyed.append((parse_file_name(path.name)[2], path, kind))
        except ValueError:
            logger.warning("ignoring %s: not a PubMed XML filename", path.name)
    keyed.sort(key=lambda t: t[0])
    to_load = [(p, k) for _, p, k in keyed if needs_load(con, p.name, force=force)]
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
        # Elapsed and the running rate are both here so a Slurm run tells you
        # what --time to request next: the rate is what scales to the remaining
        # files, and elapsed is what you compare against the limit you asked for.
        logger.info(
            "progress: %d/%d files this run, %d remaining · %.1f s/file · "
            "elapsed %s · %s",
            done, total, remaining, elapsed / done, fmt_duration(elapsed),
            "done" if remaining <= 0 else f"~{eta_str(elapsed, done, remaining)} to go",
        )

    if failed:
        logger.error(
            "%d of %d file(s) failed to load and were skipped: %s",
            len(failed), total, ", ".join(failed),
        )
    if loaded:
        record_load(con)

    return loaded, failed


#: Maps keys in NLM's J_Entrez/J_Medline overview file to our journal columns.
#: ``IsoAbbr`` is not read: it equalled ``MedAbbr`` in 42,056 of 42,056 journals,
#: so data model 1.0 dropped the column that held it (#48).
_JOURNAL_KEYS = {
    "JournalTitle": "title",
    "MedAbbr": "abbreviation_medline",
    "NlmId": "nlm_catalog_id",
}


def _parse_journal_overview(path: Path):
    """Yield ``(record, issns)`` from an NLM journal overview file.

    The file is a series of ``key: value`` blocks separated by ``---`` lines.
    Upstream's ``catalog.process_journal_overview()`` works since 0.1.0
    (pubmed-downloader#16), and reads today's file identically (42,130 journals,
    checked 2026-10-19), but we keep this parser because it is tolerant where
    upstream's is strict: upstream raises on a key outside its ``REMAPPING``, on
    a line with no ``:`` and on a block with no ``NlmId``. If NLM added a field,
    upstream's parser would fail the whole ``journals`` step, while this one
    ignores the field. Upstream also opens the file without an ``encoding``.
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


#: NLM's serial catalog. It carries the publication years `J_Entrez.txt` omits,
#: but *not* the journal titles — those stay with J_Entrez, which is PubMed's own
#: rendering. `docs/JournalCatalog.md` has the measurements behind that split.
_SERFILE_LISTING = "https://ftp.nlm.nih.gov/projects/serfilelease/"
_SERFILE_BASELINE_RE = re.compile(r"serfilebase\.(\d{4})\.xml\b")
_SERFILE_UPDATE_RE = re.compile(r"serfile\.(\d{8})\.xml\b")
_YEAR_RE = re.compile(r"\d{4}")


def _serfile_urls() -> list[str]:
    """Return the newest serfile baseline plus its monthly updates, oldest first.

    We enumerate the listing ourselves rather than calling
    ``pubmed_downloader.catalog.ensure_serfile_catalog()``, which skips
    ``serfilebase*`` and takes every monthly delta instead. That is **not** a
    coverage problem — measured, its 83 files carry 151,974 distinct records
    against the baseline's 150,942, because NLM re-releases the whole catalog
    through the deltas over time. It is a cost problem: 2.52 GiB against 872 MiB
    here, and 80% of the records it yields are superseded duplicates that a
    caller has to dedupe. Anchoring on the baseline avoids both.
    """
    response = requests.get(_SERFILE_LISTING, timeout=300)
    # Without this a 503 maintenance page reads as "no baseline in the
    # listing", which sends whoever reads the log looking for a renamed file.
    response.raise_for_status()
    html = response.text
    # Both patterns require `.xml` immediately after the digits, which is what
    # rejects the `.marcxml.xml` spelling of the same data.
    baselines = set(_SERFILE_BASELINE_RE.findall(html))
    if not baselines:
        raise ValueError(f"no serfilebase.YYYY.xml in the listing at {_SERFILE_LISTING}")
    year = max(baselines)
    # Updates from the baseline's year onward. NLM posts the baseline in mid
    # January, so the January delta is usually already inside it; re-applying it
    # is harmless because these two year fields do not churn.
    updates = sorted(d for d in set(_SERFILE_UPDATE_RE.findall(html)) if d >= f"{year}0101")
    names = [f"serfilebase.{year}.xml"] + [f"serfile.{d}.xml" for d in updates]
    return [_SERFILE_LISTING + name for name in names]


def _validator(headers) -> str | None:
    """The ETag from a response's headers, falling back to ``Last-Modified``."""
    return headers.get("ETag") or headers.get("Last-Modified")


def _serfile_head(url: str) -> tuple[str | None, int | None]:
    """The server's ``(validator, size in bytes)`` for a catalog file.

    NLM publishes no ``.md5`` sidecars for these files, unlike the PubMed
    baseline where :mod:`pubmed2db.download` uses them to notice a republished
    file. The ETag is the validator this server does offer, and a HEAD does the
    same job for a few hundred bytes instead of ~450 MB. It asks for
    ``identity``, as `util.download_file` does, so the length is the size of
    the file that download writes. A server that compresses anyway gets
    ``None`` for the size: a length compressed on the fly need not match the
    next GET's (NLM serves these uncompressed today). Both are ``None`` on any
    network trouble — the caller then keeps whatever is already cached rather
    than failing.

    Redirects are followed explicitly: ``requests.head`` does not by default,
    and ``raise_for_status`` passes a 3xx, so a moved file would otherwise be
    described by the redirect's headers — usually no ETag at all, which would
    freeze the cached copy forever.
    """
    try:
        response = requests.head(
            url, timeout=60, allow_redirects=True, headers={"Accept-Encoding": "identity"}
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.debug("could not read a validator for %s (%s); keeping the cached copy", url, exc)
        return None, None
    headers = response.headers
    length = headers.get("Content-Length", "")
    size = int(length) if length.isdigit() and "Content-Encoding" not in headers else None
    return _validator(headers), size


def _ensure_serfile() -> list[Path]:
    """Download the serfile baseline and updates, returning their local paths.

    Skipping a cached file by *name* alone would let a republished one keep its
    stale bytes indefinitely — the hazard `download.py` meets with the PubMed
    files' published ``.md5`` sidecars, which this server does not offer. These
    files are immutable by name in the normal case, and the baseline is ~450 MB,
    so re-fetching unconditionally is out; instead each one gets an ``.etag``
    sidecar and is re-fetched only when the server's validator moves. A new month simply arrives under a new name, which
    re-scraping the listing picks up.

    A monthly update that fails to download is skipped with a warning, the same
    posture `_parse_serfile` takes towards one that fails to parse: one bad month
    must not cost every journal its years. The baseline is not skippable — the
    updates only amend it — so its failure propagates.
    """
    module = pystow.module("pubmed2db", "serfile")
    urls = _serfile_urls()
    paths = []
    for i, url in enumerate(urls):
        try:
            paths.append(_ensure_serfile_file(module, url))
        except Exception as exc:  # noqa: BLE001 — network or disk; see docstring
            if i == 0:  # _serfile_urls puts the baseline first
                raise
            logger.warning("%s failed to download (%s); skipping it", url, exc)
    if paths:
        _prune_serfile_cache(paths[0].parent, {url.rsplit("/", 1)[1] for url in urls})
    return paths


def _ensure_serfile_file(module: pystow.Module, url: str) -> Path:
    """Fetch one catalog file, re-fetching it if the server's copy has moved.

    The sidecar is written only after a successful download, and
    `util.download_file` never leaves a partial file under the real name, so a
    failed fetch is simply retried on the next run. A failed *re*-fetch keeps
    the cached copy: a republished file is usually a corrected one, not proof
    the copy we hold is unusable, and losing a ~450 MB baseline to one timeout
    would cost a fresh database every journal's years.
    """
    path = module.join(name=url.rsplit("/", 1)[1])
    stamp = path.with_name(path.name + ".etag")
    validator, size = _serfile_head(url)
    cached = path.is_file()
    # A cached copy whose size disagrees with the server's is not the file the
    # server serves -- truncated by something other than `util.download_file`
    # (which renames into place only when complete), such as an older build
    # that wrote straight to this name. The ETag alone would not notice: it
    # describes the server's copy, not ours. This copy is not worth keeping.
    wrong_size = cached and size is not None and path.stat().st_size != size
    # A missing sidecar counts as republished: it marks a file of unknown
    # provenance. An unreadable validator leaves whatever is cached alone.
    republished = (
        cached
        and validator is not None
        and (not stamp.is_file() or stamp.read_text().strip() != validator)
    )
    if cached and not (wrong_size or republished):
        return path
    if wrong_size:
        logger.warning(
            "%s is %d bytes but the server's copy is %d; re-fetching it",
            path.name,
            path.stat().st_size,
            size,
        )
    elif republished:
        logger.info("%s was republished; re-fetching it", path.name)
    try:
        headers = download_file(url, path)
    except Exception as exc:
        if republished and not wrong_size:
            # The sidecar is left alone, so the next run tries again.
            logger.warning("%s could not be re-fetched (%s); keeping the cached copy", path.name, exc)
            return path
        raise
    # The GET's own validator describes exactly the bytes written, and it is
    # there even when the HEAD failed -- without it, a file first fetched
    # during a HEAD outage would have no sidecar and be fetched again next run.
    validator = _validator(headers) or validator
    if validator is not None:
        stamp.write_text(validator)
    return path


def _prune_serfile_cache(directory: Path, keep: set[str]) -> None:
    """Delete cached catalog files the listing no longer names.

    Only the newest baseline and that year's updates are ever read, so a new
    baseline year leaves the previous year's ~890 MB unreachable. Unlike the
    PubMed files `download` keeps (which are the corpus, and the operator's to
    delete), these are a cache only this module reads, so they are pruned here.
    Only names the serfile patterns match are touched, sidecars included. A
    ``.part`` goes even for a kept name: it is a transfer a SIGKILL interrupted
    (`util.download_file` cleans up in a ``finally``, which that skips), and the
    next fetch starts a fresh one, so it is ~450 MB nothing will read.
    """
    for entry in directory.iterdir():
        partial = entry.name.endswith(".part")
        name = entry.name.removesuffix(".part").removesuffix(".etag")
        if name in keep and not partial:
            continue
        if _SERFILE_BASELINE_RE.fullmatch(name) or _SERFILE_UPDATE_RE.fullmatch(name):
            logger.info("removing %s, which the catalog listing no longer names", entry.name)
            entry.unlink()


def _catalog_year(raw: str | None) -> int | None:
    """Parse a catalog publication year, rejecting MARC's wildcard forms.

    Roughly 1,600 values in the baseline are ``19uu``, ``199u`` or ``uuuu`` —
    MARC's "unknown digit" placeholder — and ``int()`` raises on those. Only four
    digits count as a year.
    """
    return int(raw) if raw and _YEAR_RE.fullmatch(raw) else None


def _catalog_active(end_raw: str | None) -> bool | None:
    """Whether a journal is still publishing, from its catalog end year.

    ``9999`` is the "still publishing" sentinel. A missing, blank or all-wildcard
    (``uuuu``) end year means the status is *unknown* — the ``uuuu`` records are
    open-ended imprints like ``c1999-`` — so it is NULL, not false. A partly
    known year (``19uu``, ``199u``) is a journal that ceased at an uncertain
    date, so it is false even though `_catalog_year` gives it no ``end_year``.
    Measured on a bulk catalog re-release (serfile.20260501.xml, 45,934
    records): 214 ``uuuu``, ~2,100 partial wildcards.
    """
    end = (end_raw or "").strip()
    if end == "9999":
        return True
    if not end.strip("u"):
        return None
    return False


def _parse_serfile(paths: list[Path]) -> dict[str, tuple[int | None, int | None, bool | None]]:
    """Map ``nlm_catalog_id -> (start_year, end_year, active)`` from serfile XML.

    Files are read in the order given, so a later monthly update wins over the
    baseline. This uses ``iterparse`` rather than the whole-tree ``etree.parse``
    that :mod:`pubmed2db.parse` uses for the much smaller PubMed files: the
    baseline is ~450 MB, and streaming it costs ~5 s and ~50 MiB.

    A monthly update that will not parse is skipped, but a baseline that will
    not parse, or holds no records, raises. The updates only amend it, so
    skipping it would return a few thousand records that `load_journals`
    cannot tell from a real catalog -- and would load NULL years for every
    other journal, where the exception lets it keep the years already loaded.
    """
    years: dict[str, tuple[int | None, int | None, bool | None]] = {}
    for path in paths:
        is_baseline = bool(_SERFILE_BASELINE_RE.fullmatch(path.name))
        n_before = len(years)
        try:
            for _, element in etree.iterparse(
                os.fspath(path), tag="NLMCatalogRecord", recover=True
            ):
                nlm_id = element.findtext("NlmUniqueID")
                if nlm_id:
                    end_raw = element.findtext("PublicationInfo/PublicationEndYear")
                    # 9999 is the catalog's "still publishing" sentinel, not a year.
                    years[nlm_id] = (
                        _catalog_year(element.findtext("PublicationInfo/PublicationFirstYear")),
                        None if end_raw == "9999" else _catalog_year(end_raw),
                        _catalog_active(end_raw),
                    )
                element.clear()
                while element.getprevious() is not None:
                    del element.getparent()[0]
        except etree.XMLSyntaxError as exc:
            # NLM really does publish empty files -- serfile.20240903.xml is
            # Content-Length: 0 -- and `recover=True` does not save an empty
            # document. Per file, because the alternative is that one bad
            # update costs every journal its years: `_journal_years` catches
            # broadly, so an exception escaping here would empty the mapping.
            if is_baseline:
                raise
            logger.warning("%s failed to parse (%s); skipping it", path, exc)
        if is_baseline and len(years) == n_before:
            raise ValueError(f"{path} parsed to no catalog records")
    return years


def _journal_years() -> dict[str, tuple[int | None, int | None, bool | None]]:
    """Publication years for the journal dimension, empty if the catalog is unreachable.

    These three columns are an enrichment: the title and abbreviation the export
    actually reads come from J_Entrez. Losing a ~450 MB download must not cost us
    the dimension, so a failure here returns nothing rather than raising — the
    same posture ``cli.update`` takes towards the journal step as a whole — and
    `load_journals` keeps whatever years a previous run loaded.
    """
    try:
        return _parse_serfile(_ensure_serfile())
    except Exception as exc:  # noqa: BLE001 — network, XML or disk; all survivable
        logger.warning(
            "serial catalog unavailable (%s); loading journals without publication years", exc
        )
        return {}


def _ensure_journal_overview() -> Path:
    """Fetch NLM's journal overview, ``J_Entrez.txt``, afresh; return its path.

    Fetched on every run: a cached copy would freeze the journal dimension at
    whatever the first run downloaded while `status` kept reporting a fresh
    refresh. Not through pubmed_downloader's ``ensure_journal_overview``, which
    is pystow's ``ensure()``: no timeout on a ~10 MB transfer from the server
    whose stall hung this very step, and with ``force=True`` it writes straight
    to the real name, so an interrupted transfer left a truncated file. The
    path is the one pystow used, so an existing cache is simply replaced.
    """
    from pubmed_downloader.catalog import J_ENTREZ_PATH, MODULE

    path = Path(MODULE.join(name=J_ENTREZ_PATH.rsplit("/", 1)[1]))
    download_file(J_ENTREZ_PATH, path)
    return path


def load_journals(con: duckdb.DuckDBPyConnection) -> int:
    """Load the journal dimension from J_Entrez.txt and NLM's serial catalog.

    Downloads NLM's journal overview (J_Entrez) and replaces the ``journal`` /
    ``journal_issn`` tables. Returns the number of
    journals loaded.

    J_Entrez defines the row set and the titles; the serial catalog only fills
    ``start_year`` / ``end_year`` / ``active``. `docs/JournalCatalog.md` records
    why the titles do not come from the catalog.
    """
    path = _ensure_journal_overview()
    years = _journal_years()

    journals: dict[str, dict] = {}
    issn_rows: list[tuple[str, str, str]] = []
    for record, issns in _parse_journal_overview(path):
        nlm_id = record["nlm_catalog_id"]
        if nlm_id in journals:  # first occurrence wins; nlm_catalog_id is the PK
            continue
        journals[nlm_id] = record
        for value, issn_type in issns:
            issn_rows.append((nlm_id, value, issn_type))

    if not journals:
        # A truncated download or an error page parses to nothing. Bail out
        # before the DELETEs rather than replacing a good journal dimension with
        # an empty one (and before executemany, which rejects an empty list).
        logger.warning(
            "journal overview %s yielded no journals; leaving the journal tables as they are",
            path,
        )
        return 0

    if not years:
        # The catalog could not be read (a real one has ~150k records). The
        # DELETE below would then replace every year a previous run loaded with
        # NULL -- the same "don't swap a good dimension for an empty one" rule
        # as the guard above -- so carry the existing years over instead.
        years = {
            nlm_id: (start, end, active)
            for nlm_id, start, end, active in con.execute(
                "SELECT nlm_catalog_id, start_year, end_year, active FROM journal"
            ).fetchall()
        }
        if years:
            logger.warning(
                "keeping the publication years already loaded for %d journals", len(years)
            )

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM journal")
        con.execute("DELETE FROM journal_issn")
        con.executemany(
            "INSERT INTO journal VALUES (?,?,?,?,?,?)",
            [
                (
                    nlm_id,
                    rec.get("title"),
                    rec.get("abbreviation_medline"),
                    # The overview file has no publication years; the serial
                    # catalog does. A journal the catalog does not cover (~1% of
                    # them, mostly proceedings volumes) keeps NULLs rather than
                    # being dropped — see docs/JournalCatalog.md.
                    *years.get(nlm_id, (None, None, None)),
                )
                for nlm_id, rec in journals.items()
            ],
        )
        if issn_rows:
            con.executemany("INSERT INTO journal_issn VALUES (?,?,?)", issn_rows)
        record_run(con, "journals")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(journals)
