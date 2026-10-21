#!/usr/bin/env python
"""Benchmark the PubMed load path on one or more files.

Times parsing and the load's phases *separately* (the parse and the row build
are CPU-bound Python, ~2 s/file between them, and do not grow; the delete, the
per-table inserts and the COMMIT are DuckDB's, and at corpus scale their sum
grows with the database, #68). Reports per-file throughput, the database's size
(its WAL included) and the process's peak RSS. Use it to spot performance
regressions, to size Slurm ``--mem``/wall time, and to measure a candidate
setting before it goes into ``db.connect``.

    uv run python scripts/benchmark_load.py data/pubmed/baseline/pubmed26n0001.xml.gz

By default each file is loaded into a fresh in-memory DuckDB so runs are
independent; pass ``--db PATH`` to benchmark against an on-disk database
instead (a *copy*: the files are really loaded into it).

The growth only shows against a large database, which this can build without
the corpus: ``--synthetic N`` parses the given files once, then loads them
round-robin N times under made-up names (``pubmed90nNNNN.xml.gz``: a 2090
"year", so the order keys sit above every real file). That exercises the exact
insert path with real data, and the per-file row of its output is the growth
curve. A synthetic row's ``parse_s`` is ``-``: its parse was paid once, on the
real file's row. Run it again on the same ``--db`` and the numbering carries on
from the last synthetic file there, so the database keeps growing rather than
reloading the same names. The knobs below are the candidate settings to test
against it:

``--checkpoint-threshold SIZE``
    DuckDB checkpoints at COMMIT once the WAL exceeds this (default 16 MiB,
    which every file exceeds, so every per-file COMMIT checkpoints).
``--no-preserve-insertion-order``
    Lets DuckDB parallelize the insert and drop the ordering it otherwise
    keeps; nothing downstream reads insertion order.
``--files-per-txn N``
    N files per transaction instead of one, which is N times fewer COMMITs.
    Needs ``--db``: in memory every file gets its own database.
``--skip-delete``
    Skip the per-file ``DELETE ... WHERE source_file = ?`` entirely: the upper
    bound on what skipping it for a never-loaded file could save. Refused for a
    file the ``--db`` has already loaded, whose rows it would duplicate (the
    versioned tables have no key to stop it).
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("files", nargs="+", type=Path, help="PubMed .xml.gz file(s).")
    parser.add_argument(
        "--db",
        default=":memory:",
        help="DuckDB path to load into (default: a fresh in-memory DB per file).",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Sets PYSTOW_HOME so pubmed_downloader paths resolve (default: data).",
    )
    parser.add_argument(
        "--synthetic",
        type=int,
        default=0,
        metavar="N",
        help="Load the parsed files N times under synthetic names to grow the database.",
    )
    parser.add_argument(
        "--files-per-txn", type=int, default=1, metavar="N", help="Files per transaction."
    )
    parser.add_argument("--checkpoint-threshold", metavar="SIZE", help="e.g. 4GB.")
    parser.add_argument("--no-preserve-insertion-order", action="store_true")
    parser.add_argument("--skip-delete", action="store_true")
    parser.add_argument("--memory-limit", metavar="SIZE", help="DuckDB buffer-pool cap.")
    parser.add_argument("--threads", type=int)
    args = parser.parse_args()
    if args.synthetic and args.db == ":memory:":
        parser.error("--synthetic needs --db PATH: the point is an on-disk database that grows")
    if args.files_per_txn > 1 and args.db == ":memory:":
        parser.error("--files-per-txn needs --db PATH: in memory each file is loaded alone")
    return args


def main() -> None:
    args = _parse_args()
    os.environ.setdefault("PYSTOW_HOME", str(Path(args.data_dir).resolve()))

    # Imported after PYSTOW_HOME is set, mirroring the CLI.
    import duckdb

    from pubmed2db import load
    from pubmed2db.db import connect
    from pubmed2db.load import LoadTiming, _load_parsed_rows
    from pubmed2db.parse import parse_file
    from pubmed2db.util import peak_rss_gib

    if args.skip_delete:
        load.delete_file_rows = lambda con, source_file: None

    def open_db() -> duckdb.DuckDBPyConnection:
        con = connect(args.db, memory_limit=args.memory_limit, threads=args.threads)
        if args.checkpoint_threshold:
            con.execute(f"SET checkpoint_threshold = '{args.checkpoint_threshold}'")
        if args.no_preserve_insertion_order:
            con.execute("SET preserve_insertion_order = false")
        return con

    def db_gib(con: duckdb.DuckDBPyConnection) -> float:
        if args.db == ":memory:":
            return 0.0
        # The WAL too: under a large --checkpoint-threshold a COMMIT stops
        # checkpointing and the growth sits there, not in the main file.
        files = (Path(args.db), Path(f"{args.db}.wal"))
        return sum(f.stat().st_size for f in files if f.exists()) / 2**30

    header = (
        f"{'file':24} {'articles':>8} {'parse_s':>7} {'build_s':>7} {'delete_s':>8} "
        f"{'insert_s':>8} {'regist_s':>8} {'commit_s':>8} {'total_s':>7} {'rows':>9} "
        f"{'rows/s':>8} {'db_GiB':>7} {'peakRSS':>8}"
    )

    def report(name, n_articles, parse_s, timing: LoadTiming, con) -> None:
        # Both counts are what the load stored (after the dedup, deletions
        # included), so the rate is over the rows actually inserted.
        total = (parse_s or 0.0) + timing.total
        rows_per_s = timing.n_rows / timing.total if timing.total else 0
        parse_col = f"{parse_s:7.2f}" if parse_s is not None else f"{'-':>7}"
        print(
            f"{name:24} {n_articles:8d} {parse_col} {timing.build:7.2f} {timing.delete:8.2f} "
            f"{timing.insert:8.2f} {timing.registry:8.2f} {timing.commit:8.2f} "
            f"{total:7.2f} {timing.n_rows:9d} {rows_per_s:8.0f} {db_gib(con):7.2f} "
            f"{peak_rss_gib():7.1f}G",
            flush=True,
        )

    # Parse every file once.
    parsed_files = []
    for path in args.files:
        t0 = time.perf_counter()
        parsed = parse_file(path)
        parsed_files.append((path.name, parsed, time.perf_counter() - t0))

    # The real files: one connection each when in memory (independent runs),
    # one shared connection on disk, so that parse_s is paid once per file.
    con = open_db() if args.db != ":memory:" else None
    jobs = [(name, parsed, parse_s, "baseline") for name, parsed, parse_s in parsed_files]
    if args.synthetic:
        # Carry on from the last synthetic file a previous run left, so a rerun
        # grows the database instead of reloading (or, with --skip-delete,
        # duplicating) the same names.
        last = con.execute("SELECT max(file_number) FROM source_file WHERE year_yy = 90")
        first = (last.fetchone()[0] or 0) + 1
        jobs += [
            (f"pubmed90n{i:04d}.xml.gz", parsed_files[i % len(parsed_files)][1], None, "baseline")
            for i in range(first, first + args.synthetic)
        ]
    if args.skip_delete and con is not None:
        names = [name for name, *_ in jobs]
        loaded = con.execute(
            "SELECT file_name FROM source_file WHERE list_contains(?, file_name)", [names]
        ).fetchall()
        if loaded:
            raise SystemExit(
                f"--skip-delete would duplicate the rows of {len(loaded)} file(s) "
                f"{args.db} has already loaded, e.g. {loaded[0][0]}"
            )

    print(header)
    print("-" * len(header))

    pending: list = []

    def flush(con: duckdb.DuckDBPyConnection) -> None:
        """Load the pending files in one transaction, charging COMMIT to the last."""
        if not pending:
            return
        timings = [LoadTiming() for _ in pending]
        n_articles = []
        con.execute("BEGIN TRANSACTION")
        try:
            for (name, parsed, _parse_s, kind), timing in zip(pending, timings):
                n_articles.append(_load_parsed_rows(con, parsed, name, kind=kind, timing=timing))
            t0 = time.perf_counter()
            con.execute("COMMIT")
            timings[-1].commit = time.perf_counter() - t0
        except Exception:
            con.execute("ROLLBACK")
            raise
        for (name, _parsed, parse_s, _kind), timing, n in zip(pending, timings, n_articles):
            report(name, n, parse_s, timing, con)
        pending.clear()

    for job in jobs:
        if con is None:
            fresh = open_db()
            pending.append(job)
            flush(fresh)
            fresh.close()
            continue
        pending.append(job)
        if len(pending) >= args.files_per_txn:
            flush(con)
    if con is not None:
        flush(con)
        con.close()


if __name__ == "__main__":
    main()
