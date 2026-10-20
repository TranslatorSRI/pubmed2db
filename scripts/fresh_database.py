#!/usr/bin/env python
"""Prepare a data directory for a rebuild from a fresh database, losing nothing.

The README recommends a fresh database at every new baseline year, and requires
one at a major data-model bump. Deleting ``pubmed.duckdb`` is the easy part;
two other things have to happen with it, and this script does all three after
showing you the plan and asking:

1. **Refuse if a finished export is still in ``data/json``.** Its
   ``metadata.json`` means ``scripts/archive_build.py`` has not moved it to
   ``data/builds/<build>/`` yet, and the database it came from is about to go.
2. **Move the superseded baseline year's XML out of the download cache.**
   ``load`` loads every ``*.xml.gz`` under ``data/pubmed/baseline/`` and
   ``data/pubmed/updates/``, registered or not, so last year's ~1,300 files
   left in place are re-parsed into the fresh database (a day of ``load``) and
   recreate the additive database the rebuild exists to replace. The files are
   kept, not deleted: they go to ``data/pubmed-archive/<year>/{baseline,updates}/``,
   which nothing reads. (If you do not want them, ``rm -r`` that directory.)
   With one baseline year on disk nothing moves and the fresh ``load`` re-parses
   it all, which is the point of a mid-year rebuild.
3. **Delete the database**, its write-ahead log and its spill directory.

Left alone, deliberately: ``data/manifests/`` (the next ``validate`` diffs the
rebuilt corpus against the last build's manifest, which is the one comparison
that shows what a rebuild dropped), ``data/logs/``, ``data/builds/``, and the
serial-catalog cache under ``data/pubmed2db/`` (``journals`` re-checks it).

    uv run python scripts/fresh_database.py              # shows the plan, asks
    uv run python scripts/fresh_database.py --yes        # for a batch job

Then ``./slurm/submit.sh all``: a fresh database needs ``journals`` as well as
``load`` (grep its log for ``serial catalog unavailable``), and the first
``validate`` is expected to warn ``drops_retired_upstream`` for the PMIDs PubMed
retired between the previous build and the new baseline.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

_YEAR = re.compile(r"^pubmed(\d{2})n\d+\.xml\.gz$")


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
            match = _YEAR.match(path.name)
            if match:
                years.setdefault(int(match.group(1)), []).append(path)
    return years


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="The pipeline's --data-dir (default: data).")
    parser.add_argument("--yes", action="store_true", help="Do not ask; proceed.")
    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir
    cache = data_dir / "pubmed"
    cache_dirs = [cache / "baseline", cache / "updates"]
    archive = data_dir / "pubmed-archive"
    db_path = data_dir / "pubmed.duckdb"
    to_delete = [p for p in (db_path, db_path.with_suffix(".duckdb.wal"),
                             db_path.with_suffix(".duckdb.tmp")) if p.exists()]

    metadata = data_dir / "json" / "metadata.json"
    if metadata.is_file():
        fail(f"{metadata} exists: a finished export has not been archived, and the "
             "database it came from is about to be deleted. Run\n"
             f"  uv run python scripts/archive_build.py --data-dir {data_dir}\n"
             "first (or delete the export yourself if it is not wanted).")

    years = files_by_year(cache_dirs)
    keep = max(years) if years else None
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
    elif keep is not None:
        print(f"  one baseline year on disk (20{keep:02d}); nothing to move, and the "
              "fresh load re-parses all of it")
    else:
        print(f"  no PubMed files under {cache}")
    for path in to_delete:
        print(f"  delete {path}")
    if not to_delete:
        print(f"  {db_path} does not exist; nothing to delete")
    print(f"  leave {data_dir / 'manifests'}, {data_dir / 'logs'}, {data_dir / 'builds'} "
          f"and {data_dir / 'pubmed2db'} as they are")

    if not moves and not to_delete:
        print("Nothing to do.")
        return
    if not args.yes:
        answer = input("Proceed? [y/N] ").strip().lower()
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

    print()
    print("Next: ./slurm/submit.sh all   (a fresh database needs `journals` too;")
    print("      grep its log for 'serial catalog unavailable' before trusting the export).")
    print("The first validate is expected to warn drops_retired_upstream: PMIDs PubMed")
    print("retired between the previous build and the new baseline, which this database")
    print("never saw a DeleteCitation for.")


if __name__ == "__main__":
    main()
