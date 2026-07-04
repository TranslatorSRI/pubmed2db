#!/usr/bin/env python
"""Benchmark the PubMed load path on one or more files.

Times parsing and insertion *separately* (they have very different costs:
parsing is CPU-bound Python at ~2s/file, insertion is the DuckDB write) and
reports per-file throughput and the process's peak RSS. Use it to spot
performance regressions and to estimate Slurm ``--mem``/wall-time sizing.

    uv run python scripts/benchmark_load.py data/pubmed/baseline/pubmed26n0001.xml.gz

By default each file is loaded into a fresh in-memory DuckDB so runs are
independent; pass ``--db PATH`` to benchmark against an on-disk database instead.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args()

    os.environ.setdefault("PYSTOW_HOME", str(Path(args.data_dir).resolve()))

    # Imported after PYSTOW_HOME is set, mirroring the CLI.
    import duckdb

    from pubmed2db.db import init_schema, parse_file_name
    from pubmed2db.load import _article_rows, load_parsed
    from pubmed2db.parse import parse_file
    from pubmed2db.util import peak_rss_gib

    header = f"{'file':28} {'articles':>8} {'parse_s':>8} {'load_s':>8} {'rows':>9} {'rows/s':>9} {'peakRSS':>8}"
    print(header)
    print("-" * len(header))

    for path in args.files:
        source_file = path.name
        order_key = parse_file_name(source_file)[2]

        t0 = time.perf_counter()
        parsed = parse_file(path)
        t_parse = time.perf_counter() - t0

        n_rows = sum(
            len(rows)
            for article in parsed.articles
            for rows in _article_rows(article, source_file, order_key).values()
        )

        con = duckdb.connect(args.db)
        init_schema(con)
        t0 = time.perf_counter()
        load_parsed(con, parsed, source_file, kind="baseline")
        t_load = time.perf_counter() - t0
        con.close()

        rows_per_s = n_rows / t_load if t_load else 0
        print(
            f"{source_file:28} {len(parsed.articles):8d} {t_parse:8.2f} {t_load:8.2f} "
            f"{n_rows:9d} {rows_per_s:9.0f} {peak_rss_gib():7.1f}G"
        )


if __name__ == "__main__":
    main()
