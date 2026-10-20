#!/usr/bin/env python
"""Move a finished export out of the way, as a build directory ready to publish.

`export` writes into one fixed directory (``data/json``) and deletes the previous
shards before it writes, so a finished export has to leave that directory before
the next run. This script moves it to ``<data-dir>/builds/<build>/``, laid out
the way builds are published on stars (shards, ``metadata.json`` and
``SHA256SUMS`` at the top level), and gathers beside it what belongs to the same
build but is written elsewhere:

- ``manifests/``: the dated ``validation_report-*.json`` for this export and
  the PMID manifest it wrote (copied: the originals in ``data/manifests/`` are
  the chain the next run diffs against, and never move);
- ``logs/``: the Slurm step logs newer than the last archived build's;
- ``source_file.csv``: the ``source_file`` registry from the database, which is
  the only record of the MD5 NCBI published for each file (no sidecars are kept
  on disk). It is what makes the archived XML files reproducible once the
  database has been wiped for a rebuild. It is cut at the build's cutoff file
  (``corpus.cutoff_file`` in ``metadata.json``), since the database may have
  synced or loaded later files since the export.

    uv run python scripts/archive_build.py                # data/json -> data/builds/<build>/
    uv run python scripts/archive_build.py --data-dir /scratch/me/pubmed

The build name is read from ``metadata.json`` (``corpus.build_name``), so the
export must have finished; the script refuses an export with no
``metadata.json``, one whose ``validate`` run failed, and one that was never
validated (``--without-validation`` to override the last). It never copies the
database: at 76 GB that is published straight from ``data/pubmed.duckdb``, and
the script prints the ``rsync`` lines for both.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def latest_report(manifest_dir: Path, export_dir: Path, not_before: float) -> Path | None:
    """The newest dated report that describes *this* export, if any.

    ``validate`` records ``inputs.export_dir`` absolute; a report from before it
    did holds the path as given, relative to where validate ran, and is matched
    only when this script runs from the same directory.
    """
    candidates = sorted(manifest_dir.glob("validation_report-*.json"), reverse=True)
    for path in candidates:
        if path.stat().st_mtime < not_before:
            continue
        try:
            inputs = json.loads(path.read_text()).get("inputs", {})
        except (OSError, ValueError):
            continue
        reported = inputs.get("export_dir")
        if reported and Path(reported).resolve() == export_dir.resolve():
            return path
    return None


def newest_archived_log_mtime(builds_dir: Path) -> float:
    """When the newest already-archived step log was written (0 if none)."""
    return max(
        (p.stat().st_mtime for p in builds_dir.glob("*/logs/*.out")), default=0.0
    )


def dump_source_file(db_path: Path, out: Path, cutoff_file: str | None) -> bool:
    """Copy the ``source_file`` registry, up to ``cutoff_file``, to CSV.

    Read-only; False if it could not. Rows past the cutoff are files the
    database learned of after the export (a later ``download`` or ``load``),
    which this build did not contain.
    """
    import duckdb

    from pubmed2db.db import parse_file_name

    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error as exc:
        print(f"warning: could not open {db_path} read-only ({exc}); "
              f"no source_file.csv written", file=sys.stderr)
        return False

    in_build = "true"
    if cutoff_file is None:
        print("warning: metadata.json names no cutoff file; source_file.csv lists "
              "every file the database knows of", file=sys.stderr)
    else:
        in_build = f"file_order_key <= {parse_file_name(cutoff_file)[2]}"
    try:
        later = con.execute(f"SELECT count(*) FROM source_file WHERE NOT ({in_build})").fetchone()[0]
        if later:
            print(f"note: left {later} file(s) after the cutoff {cutoff_file} out of "
                  "source_file.csv; the database has moved on since this export")
        con.execute(
            f"COPY (SELECT * FROM source_file WHERE {in_build} ORDER BY file_order_key) "
            "TO '" + out.as_posix().replace("'", "''") + "' (HEADER)"
        )
    finally:
        con.close()
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="The pipeline's --data-dir (default: data).")
    parser.add_argument("--export-dir", type=Path, default=None,
                        help="The finished export (default: $EXPORT_DIR, else <data-dir>/json).")
    parser.add_argument("--name", default=None,
                        help="Build name, when metadata.json has no corpus.build_name "
                             "(a cutoff file download never synced).")
    parser.add_argument("--without-validation", action="store_true",
                        help="Archive an export that validate has not been run on.")
    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir
    export_dir: Path = args.export_dir or Path(os.environ.get("EXPORT_DIR") or data_dir / "json")
    builds_dir = data_dir / "builds"
    manifest_dir = data_dir / "manifests"
    logs_dir = data_dir / "logs"
    db_path = data_dir / "pubmed.duckdb"

    metadata_path = export_dir / "metadata.json"
    if not metadata_path.is_file():
        fail(f"{metadata_path} does not exist: no finished export in {export_dir} "
             "(it is written last, so a run that died leaves none).")
    metadata = json.loads(metadata_path.read_text())
    build = args.name or metadata.get("corpus", {}).get("build_name")
    if not build:
        fail("metadata.json has no corpus.build_name (the cutoff file was never synced "
             "by `download`); pass --name.")
    dest = builds_dir / build
    if dest.exists():
        fail(f"{dest} already exists. A re-export of the same build replaces it only "
             "once you have moved or removed that directory yourself.")

    report = latest_report(manifest_dir, export_dir, metadata_path.stat().st_mtime)
    manifest: Path | None = None
    if report is None:
        if not args.without_validation:
            fail(f"no validation_report-*.json in {manifest_dir} describes {export_dir} "
                 "(written after its metadata.json). Run validate first, or pass "
                 "--without-validation.")
    else:
        content = json.loads(report.read_text())
        if content.get("status") == "fail":
            fail(f"{report} says this export failed validation; fix the export "
                 "rather than archiving it.")
        written = content.get("inputs", {}).get("manifest_written")
        if written and Path(written).is_file():
            manifest = Path(written)
        elif written:
            print(f"warning: {report.name} names the manifest {written}, which is not "
                  "there from this directory; not copying it", file=sys.stderr)

    # The previous build's newest log bounds what belongs to this one; read it
    # before this build's directory exists under builds/.
    logs_since = newest_archived_log_mtime(builds_dir)
    logs = sorted(p for p in logs_dir.glob("pubmed2db-*.out")
                  if p.stat().st_mtime > logs_since) if logs_dir.is_dir() else []

    builds_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(export_dir), str(dest))
    print(f"moved {export_dir} -> {dest}")

    if report is not None:
        (dest / "manifests").mkdir()
        shutil.copy2(report, dest / "manifests" / report.name)
        print(f"copied {report.name} -> {dest / 'manifests'}")
        if manifest is not None:
            shutil.copy2(manifest, dest / "manifests" / manifest.name)
            print(f"copied {manifest.name} -> {dest / 'manifests'}")
        else:
            print("note: the report names no manifest (a fail run writes none); "
                  "nothing copied")
    if logs:
        (dest / "logs").mkdir()
        for log in logs:
            shutil.copy2(log, dest / "logs" / log.name)
        print(f"copied {len(logs)} step log(s) -> {dest / 'logs'}")

    if db_path.is_file():
        cutoff = metadata.get("corpus", {}).get("cutoff_file")
        if dump_source_file(db_path, dest / "source_file.csv", cutoff):
            print(f"wrote {dest / 'source_file.csv'} (the files this build loaded, "
                  "with the MD5 NCBI published for each)")
    else:
        print(f"note: {db_path} not found; no source_file.csv written")

    print()
    print(f"Build {build} is in {dest}. To publish it:")
    print(f"  rsync -a {dest}/ HOST:/var/babel_outputs/pubmed2db/{build}/")
    if db_path.is_file():
        print(f"  rsync {db_path} HOST:/var/babel_outputs/pubmed2db/{build}/pubmed.duckdb")
        print("The second line must run before the next download/load changes the database.")


if __name__ == "__main__":
    main()
