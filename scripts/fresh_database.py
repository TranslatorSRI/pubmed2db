#!/usr/bin/env python
"""Prepare a data directory for a rebuild from a fresh database, losing nothing.

The README recommends a fresh database at every new baseline year, and requires
one at a major data-model bump. Deleting ``pubmed.duckdb`` is the easy part;
two other things have to happen with it, and this script does all three after
showing you the plan and asking:

1. **Refuse if a finished export is still in ``data/json``** (or ``$EXPORT_DIR``,
   or ``--export-dir``). Its
   ``metadata.json`` means ``scripts/archive_build.py`` has not moved it to
   ``data/builds/<build>/`` yet, and the database it came from is about to go.
2. **Move every baseline year but NCBI's current one out of the download cache.**
   ``load`` loads every ``*.xml.gz`` under ``data/pubmed/baseline/`` and
   ``data/pubmed/updates/``, registered or not, so last year's ~1,300 files
   left in place are re-parsed into the fresh database (a day of ``load``) and
   recreate the additive database the rebuild exists to replace. The files are
   kept, not deleted: they go to ``data/pubmed-archive/<year>/{baseline,updates}/``,
   which nothing reads. (If you do not want them, ``rm -r`` that directory.)
   The year to keep is the one NCBI's baseline listing serves *now*, not the
   newest on disk: this runs before ``submit.sh all`` downloads the new
   baseline, so at the year boundary the newest year on disk is the one being
   superseded. Mid-year, NCBI's year is the one on disk, nothing moves, and the
   fresh ``load`` re-parses it all, which is the point of a mid-year rebuild.
   ``--keep-year`` names the year instead of asking NCBI.
3. **Delete the database**, its write-ahead log and its spill files: the
   default ``<db>.tmp`` directory, and DuckDB's ``duckdb_temp_*`` files in a
   configured one (``--temp-dir``, default ``$DUCKDB_TEMP_DIR`` as
   ``slurm/config.sh`` sets it, else ``$PUBMED2DB_DUCKDB_TEMP_DIR``). A
   configured directory may be shared, so only those files go, never the
   directory. DuckDB removes them on a clean exit, so any left are from a job
   that died.

Left alone, deliberately: ``data/manifests/`` (the next ``validate`` diffs the
rebuilt corpus against the last build's manifest, which is the one comparison
that shows what a rebuild dropped), ``data/logs/``, ``data/builds/``, and the
serial-catalog cache under ``data/pubmed2db/`` (``journals`` re-checks it).

    uv run python scripts/fresh_database.py              # shows the plan, asks
    uv run python scripts/fresh_database.py --yes        # for a batch job
    uv run python scripts/fresh_database.py --keep-year 2027   # without asking NCBI

Then ``./slurm/submit.sh all``: a fresh database needs ``journals`` as well as
``load`` (grep its log for ``serial catalog unavailable``), and the first
``validate`` is expected to warn ``drops_retired_upstream`` for the PMIDs PubMed
retired between the previous build and the new baseline.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

from pubmed2db.db import parse_file_name

#: pubmed_downloader's BASELINE_URL, written out rather than imported: importing
#: it sets up pystow, which writes under ~/.data unless PYSTOW_HOME is set.
BASELINE_URL = "https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/"


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def files_by_year(cache_dirs: list[Path]) -> dict[int, list[Path]]:
    """``{year_yy: [files]}`` over the download cache, ignoring other files."""
    years: dict[int, list[Path]] = {}
    for directory in cache_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            try:
                year_yy = parse_file_name(path.name)[0]
            except ValueError:
                continue
            years.setdefault(year_yy, []).append(path)
    return years


def ncbi_baseline_year() -> int:
    """The two-digit year of the baseline NCBI serves now, from its listing."""
    import requests

    response = requests.get(BASELINE_URL, timeout=60)
    response.raise_for_status()
    years = re.findall(r"pubmed(\d{2})n\d+\.xml\.gz", response.text)
    if not years:
        raise ValueError(f"no baseline files listed at {BASELINE_URL}")
    return max(int(y) for y in years)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="The pipeline's --data-dir (default: data).")
    parser.add_argument("--export-dir", type=Path, default=None,
                        help="Where export writes (default: $EXPORT_DIR, else <data-dir>/json).")
    parser.add_argument("--temp-dir", type=Path, default=None,
                        help="DuckDB's configured spill directory, if any (default: "
                             "$DUCKDB_TEMP_DIR, else $PUBMED2DB_DUCKDB_TEMP_DIR).")
    parser.add_argument("--keep-year", type=int, default=None,
                        help="The baseline year to keep, e.g. 2027 (default: the one NCBI "
                             "serves now). Every other year's XML is moved.")
    parser.add_argument("--yes", action="store_true", help="Do not ask; proceed.")
    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir
    cache = data_dir / "pubmed"
    cache_dirs = [cache / "baseline", cache / "updates"]
    archive = data_dir / "pubmed-archive"
    db_path = data_dir / "pubmed.duckdb"
    to_delete = [p for p in (db_path, db_path.with_suffix(".duckdb.wal"),
                             db_path.with_suffix(".duckdb.tmp")) if p.exists()]
    temp_dir = args.temp_dir or next(
        (Path(v) for v in (os.environ.get("DUCKDB_TEMP_DIR"),
                           os.environ.get("PUBMED2DB_DUCKDB_TEMP_DIR")) if v), None)
    spill = sorted(temp_dir.glob("duckdb_temp_*")) if temp_dir and temp_dir.is_dir() else []

    export_dir = args.export_dir or Path(os.environ.get("EXPORT_DIR") or data_dir / "json")
    metadata = export_dir / "metadata.json"
    if metadata.is_file():
        fail(f"{metadata} exists: a finished export has not been archived, and the "
             "database it came from is about to be deleted. Run\n"
             f"  uv run python scripts/archive_build.py --data-dir {data_dir} "
             f"--export-dir {export_dir}\n"
             "first (or delete the export yourself if it is not wanted).")

    if args.keep_year is not None:
        keep = args.keep_year % 100
    else:
        try:
            keep = ncbi_baseline_year()
        except Exception as exc:  # noqa: BLE001 - any failure means the same thing
            fail(f"could not read NCBI's baseline listing ({exc}); pass --keep-year "
                 "with the year of the baseline the fresh database should load.")
    years = files_by_year(cache_dirs)
    moves: list[tuple[Path, Path]] = []
    for year, paths in sorted(years.items()):
        if year == keep:
            continue
        for path in paths:
            moves.append((path, archive / f"20{year:02d}" / path.parent.name / path.name))

    print(f"Fresh database under {data_dir}:")
    if moves:
        print(f"  move {len(moves)} file(s) from superseded baseline year(s) "
              f"{', '.join(f'20{y:02d}' for y in sorted(years) if y != keep)} "
              f"to {archive}/ (kept, not deleted)")
    elif years:
        print(f"  only NCBI's current baseline year (20{keep:02d}) on disk; nothing to "
              "move, and the fresh load re-parses all of it")
    else:
        print(f"  no PubMed files under {cache}")
    if keep not in years:
        print(f"  (the 20{keep:02d} baseline is not downloaded yet; `submit.sh all` "
              "fetches it)")
    for path in to_delete:
        print(f"  delete {path}")
    if not to_delete:
        print(f"  {db_path} does not exist; nothing to delete")
    if spill:
        print(f"  delete {len(spill)} DuckDB spill file(s) in {temp_dir} (left by a job "
              "that died; make sure none is running)")
    print(f"  leave {data_dir / 'manifests'}, {data_dir / 'logs'}, {data_dir / 'builds'} "
          f"and {data_dir / 'pubmed2db'} as they are")

    if not moves and not to_delete and not spill:
        print("Nothing to do.")
        return
    if not args.yes:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:  # sbatch, cron: stdin is /dev/null and no one can answer
            fail("no answer on stdin (a batch job?); pass --yes to proceed without asking.")
        if answer not in ("y", "yes"):
            print("Nothing changed.")
            return

    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    if moves:
        print(f"moved {len(moves)} file(s) to {archive}/")
    for path in to_delete:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        print(f"deleted {path}")
    for path in spill:
        path.unlink()
    if spill:
        print(f"deleted {len(spill)} spill file(s) in {temp_dir}")

    print()
    print("Next: ./slurm/submit.sh all   (a fresh database needs `journals` too;")
    print("      grep its log for 'serial catalog unavailable' before trusting the export).")
    print("The first validate is expected to warn drops_retired_upstream: PMIDs PubMed")
    print("retired between the previous build and the new baseline, which this database")
    print("never saw a DeleteCitation for.")


if __name__ == "__main__":
    main()
