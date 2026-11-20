"""Parse PubMed files in parallel into per-file Parquet shards for ``load``.

DuckDB has a single writer, and a serial load of the corpus is a day (#68),
nearly all of it in the database rather than the parse. So the parse, which
is embarrassingly parallel, runs here in a process pool with no database open:
each worker turns one XML file into the same typed Arrow tables the direct
insert registers (`load.build_batches`) and writes them to Parquet, one file
per table, under a directory named for the PubMed file, with a manifest
carrying the counts the ``source_file`` registry records. `load` then inserts
whole groups of files per transaction from those shards.

Layout, under ``<data-dir>/stage/`` by default::

    pubmed26n0001/            complete: exists only after an atomic rename
        manifest.json
        article.parquet ... deleted_pmid.parquet   (every table, 0 rows allowed)
    pubmed26n0001.part/       in progress; swept at the next `stage` start
    pubmed26n0001.bad/        a shard `load` could not insert: inspect, then delete

A shard is identified by the XML it came from (size and mtime) and the code
that wrote it (`datamodel.code_version`). Anything else is not a shard and
is re-staged, so a changed download or a parsing change is never served from
a stale shard -- which is what keeps ``load --force``'s promise, that it
applies the current parse to the whole corpus, true.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from .datamodel import DATA_MODEL_VERSION, code_version
from .db import parse_file_name
from .load import FileCounts, build_batches
from .parse import parse_file
from .util import current_rss_gib, eta_str, fmt_duration, peak_rss_gib

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
MANIFEST = "manifest.json"
PART = ".part"
BAD = ".bad"
_SUFFIX = ".xml.gz"


@dataclass
class Manifest:
    """What one shard holds and where it came from; ``manifest.json``."""

    file_name: str
    kind: str
    year_yy: int
    file_number: int
    file_order_key: int
    n_articles: int
    n_deletions: int
    n_failed: int
    n_book_records: int
    rows: dict[str, int]
    xml_size: int
    xml_mtime_ns: int
    code_version: str
    data_model_version: str
    staged_at: str
    peak_rss_gib: float
    manifest_version: int = MANIFEST_VERSION

    @property
    def counts(self) -> FileCounts:
        return FileCounts(self.n_articles, self.n_deletions, self.n_failed, self.n_book_records)

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> Manifest:
        data = json.loads(path.read_text())
        if data.get("manifest_version") != MANIFEST_VERSION:
            raise ValueError(f"manifest version {data.get('manifest_version')!r}")
        return cls(**data)


@dataclass
class Shard:
    """A complete shard directory and its manifest."""

    path: Path
    manifest: Manifest

    @property
    def file_name(self) -> str:
        return self.manifest.file_name

    def parquet(self, table: str) -> Path:
        return self.path / f"{table}.parquet"


def shard_dir(stage_dir: Path, file_name: str) -> Path:
    """Where ``file_name``'s complete shard lives."""
    return Path(stage_dir) / file_name.removesuffix(_SUFFIX)


def stage_file(path: str | Path, kind: str, stage_dir: str | Path) -> Manifest:
    """Parse one file and write its shard; returns the manifest written.

    Everything goes into a ``.part`` directory first and is renamed into
    place last, so a complete directory is the only thing a reader can find
    under the final name. A previous shard under that name is replaced.
    """
    path = Path(path)
    stat = path.stat()
    year_yy, file_number, order_key = parse_file_name(path.name)
    parsed = parse_file(path)
    batches, n_articles = build_batches(parsed, path.name, order_key)

    final = shard_dir(stage_dir, path.name)
    part = final.with_name(final.name + PART)
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    for table, batch in batches.items():
        pq.write_table(batch, part / f"{table}.parquet", compression="zstd")
    manifest = Manifest(
        file_name=path.name,
        kind=kind,
        year_yy=year_yy,
        file_number=file_number,
        file_order_key=order_key,
        n_articles=n_articles,
        n_deletions=len(parsed.deleted_pmids),
        n_failed=parsed.n_failed,
        n_book_records=parsed.n_book_records,
        rows={table: batch.num_rows for table, batch in batches.items()},
        xml_size=stat.st_size,
        xml_mtime_ns=stat.st_mtime_ns,
        code_version=code_version(),
        data_model_version=DATA_MODEL_VERSION,
        staged_at=datetime.now(UTC).isoformat(timespec="seconds"),
        peak_rss_gib=peak_rss_gib(),
    )
    manifest.write(part / MANIFEST)
    shutil.rmtree(final, ignore_errors=True)
    os.replace(part, final)
    return manifest


def find_shard(stage_dir: str | Path, xml_path: str | Path) -> Shard | str:
    """The fresh shard for ``xml_path``, or the reason there is none.

    Fresh means: the directory is complete, its manifest reads, the XML on
    disk is the one it was parsed from (size and mtime), and the code that
    wrote it is the code running now. The reason is a short phrase for a
    log line that counts them.
    """
    xml_path = Path(xml_path)
    path = shard_dir(stage_dir, xml_path.name)
    if not path.is_dir():
        return "not staged"
    try:
        manifest = Manifest.read(path / MANIFEST)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("%s: unreadable manifest (%s); will re-stage", path, exc)
        return "unreadable manifest"
    if manifest.file_name != xml_path.name:
        return "manifest names another file"
    if not xml_path.is_file():
        return "XML missing"
    stat = xml_path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (manifest.xml_size, manifest.xml_mtime_ns):
        return "XML changed since it was staged"
    if manifest.code_version != code_version():
        return f"staged by {manifest.code_version}, this is {code_version()}"
    if not all((path / f"{table}.parquet").is_file() for table in manifest.rows):
        return "a table's Parquet file is missing"
    return Shard(path, manifest)


def find_shards(stage_dir: str | Path, files: list[Path]) -> dict[str, Shard]:
    """Fresh shards for ``files``, keyed by PubMed file name, logging why the rest are not."""
    stage_dir = Path(stage_dir)
    if not stage_dir.is_dir():
        return {}
    shards: dict[str, Shard] = {}
    reasons: dict[str, int] = {}
    for path in files:
        found = find_shard(stage_dir, path)
        if isinstance(found, Shard):
            shards[path.name] = found
        elif found != "not staged":
            reasons[found] = reasons.get(found, 0) + 1
    for reason, count in sorted(reasons.items()):
        logger.info("%d staged file(s) not usable: %s", count, reason)
    if shards:
        logger.info("%d fresh shard(s) found under %s", len(shards), stage_dir)
    return shards


def remove_shard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def sweep(stage_dir: str | Path, keep: set[str] | None = None) -> list[str]:
    """Delete ``.part`` directories (a transfer a kill interrupted) and, given
    ``keep``, shards for files no longer on disk. Returns what was removed."""
    stage_dir = Path(stage_dir)
    removed = []
    if not stage_dir.is_dir():
        return removed
    for entry in stage_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.endswith(PART):
            remove_shard(entry)
            removed.append(entry.name)
        elif keep is not None and not entry.name.endswith(BAD) and entry.name not in keep:
            remove_shard(entry)
            removed.append(entry.name)
    for name in removed:
        logger.info("removed %s from %s", name, stage_dir)
    return removed


def _init_worker(level: int) -> None:
    """Worker processes are spawned, not forked, so they configure their own
    logging; without this, the parse's warnings (an article the extractor
    rejected, a book record skipped) would be lost in the pool."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _stage_worker(job: tuple[str, str, str]) -> tuple[str, Manifest | None, str | None]:
    """Pool entry point: ``(path, kind, stage_dir)`` -> ``(file_name, manifest, error)``."""
    path, kind, stage_dir = job
    try:
        return Path(path).name, stage_file(path, kind, stage_dir), None
    except Exception as exc:  # noqa: BLE001 - reported by name; the rest carry on
        return Path(path).name, None, f"{type(exc).__name__}: {exc}"


def default_workers() -> int:
    """CPUs this process may use. ``SLURM_CPUS_PER_TASK`` first: on the cluster
    the limit is a cgroup quota, not an affinity mask, so the OS count reads the
    whole node (AGENTS.md)."""
    slurm = os.environ.get("SLURM_CPUS_PER_TASK", "")
    if slurm.isdigit() and int(slurm) > 0:
        return int(slurm)
    count = getattr(os, "process_cpu_count", os.cpu_count)()
    return max(1, count or 1)


def stage_files(
    files: list[tuple[Path, str]],
    *,
    stage_dir: str | Path,
    workers: int,
    force: bool = False,
    con=None,
) -> tuple[int, list[str]]:
    """Stage every ``(path, kind)`` that needs it, ``workers`` files at a time.

    Skips a file with a fresh shard, and one the database (``con``, opened
    read-only) says is loaded and up to date, unless ``force``. Returns
    ``(n_staged, failed_file_names)``: a file that fails to parse is logged
    and skipped rather than aborting the run, and a later run retries it,
    the posture `load_files` takes.
    """
    from .load import needs_load

    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    sweep(stage_dir)

    keyed: list[tuple[int, Path, str]] = []
    for path, kind in files:
        try:
            keyed.append((parse_file_name(path.name)[2], path, kind))
        except ValueError:
            logger.warning("ignoring %s: not a PubMed XML filename", path.name)
    keyed.sort(key=lambda t: t[0])

    jobs: list[tuple[str, str, str]] = []
    for _, path, kind in keyed:
        if not force:
            if con is not None and not needs_load(con, path.name):
                continue
            if isinstance(find_shard(stage_dir, path), Shard):
                continue
        jobs.append((str(path), kind, str(stage_dir)))
    total = len(jobs)
    if total == 0:
        return 0, []

    logger.info("staging %d file(s) with %d worker(s) into %s", total, workers, stage_dir)
    run_start = time.monotonic()
    staged = 0
    failed: list[str] = []
    context = multiprocessing.get_context("spawn")
    with context.Pool(
        workers, initializer=_init_worker, initargs=(logging.getLogger().getEffectiveLevel(),)
    ) as pool:
        for done, (name, manifest, error) in enumerate(
            pool.imap_unordered(_stage_worker, jobs), start=1
        ):
            if manifest is None:
                logger.error("failed to stage %s; skipping: %s", name, error)
                failed.append(name)
            else:
                staged += 1
                logger.info(
                    "staged %s: %d articles, %d deletions, %d failed to parse, "
                    "%d book record(s) skipped (worker peak %.1f GiB)",
                    name,
                    manifest.n_articles,
                    manifest.n_deletions,
                    manifest.n_failed,
                    manifest.n_book_records,
                    manifest.peak_rss_gib,
                )
            remaining = total - done
            elapsed = time.monotonic() - run_start
            current = current_rss_gib()
            logger.info(
                "progress: %d/%d files this run, %d remaining · %.1f s/file · elapsed %s · %s "
                "(RSS %s, peak %.1f GiB)",
                done,
                total,
                remaining,
                elapsed / done,
                fmt_duration(elapsed),
                "done" if remaining <= 0 else f"~{eta_str(elapsed, done, remaining)} to go",
                "n/a" if current is None else f"{current:.1f} GiB",
                peak_rss_gib(),
            )

    if failed:
        logger.error(
            "%d of %d file(s) failed to stage and were skipped: %s",
            len(failed),
            total,
            ", ".join(sorted(failed)),
        )
    return staged, failed
