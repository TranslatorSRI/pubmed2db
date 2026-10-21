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
- ``logs/``: the Slurm step logs of this build's run (see :func:`build_logs`);
- ``source_file.csv``: the ``source_file`` registry from the database, which is
  the only record of the MD5 NCBI published for each file (no sidecars are kept
  on disk). It is what makes the archived XML files reproducible once the
  database has been wiped for a rebuild. It holds the files the build loaded:
  cut at its cutoff file (``corpus.cutoff_file`` in ``metadata.json``), since
  the database may have synced or loaded later files since the export, and
  without files registered but never loaded, which ``corpus`` does not count.

The directories follow the pipeline's: ``--data-dir`` defaults to
``$DATA_DIR``, and the export and manifest directories to ``$EXPORT_DIR`` and
``$MANIFEST_DIR`` (what ``slurm/config.sh`` reads), each falling back to its
place under the data directory. ``submit.sh`` writes step logs to
``$DATA_DIR/logs``; an ``.sbatch`` file submitted by hand writes them to
``data/logs`` under the submit directory, so pass ``--logs-dir`` for those.

Nothing moves until everything that can fail has run: the report, manifest,
logs and ``source_file.csv`` are gathered in ``builds/.<build>.incoming/``
first, then the export is moved and they are renamed in beside it.

    uv run python scripts/archive_build.py                # data/json -> data/builds/<build>/
    uv run python scripts/archive_build.py --data-dir /scratch/me/pubmed
    uv run python scripts/archive_build.py --logs-dir data/logs   # logs of hand-run sbatch jobs

The build name is read from ``metadata.json`` (``corpus.build_name``), so the
export must have finished; the script refuses an export with no
``metadata.json``, one whose ``validate`` run failed, one that was never
validated (``--without-validation`` to override), and one whose
``source_file.csv`` it cannot write, say because a running ``load`` holds the
database (``--without-source-file`` to override: once the database is wiped
that record is gone). It never copies the
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
    """The newest report that describes *this* export, if any.

    Newest by mtime, not by name: a hand run's ``--out`` need not be dated, and
    ``validation_report-rerun.json`` sorts after every dated one.
    ``validate`` records ``inputs.export_dir`` absolute; a report from before it
    did holds the path as given, relative to where validate ran, and is matched
    only when this script runs from the same directory.
    """
    candidates = sorted(manifest_dir.glob("validation_report-*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
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


def previous_build_ended(builds_dir: Path, manifest_dir: Path, export_finished: float) -> float:
    """When the previous build's run ended, as far as the files on disk say (0 if unknown).

    The newest of two things. What archived builds kept with their original
    times: ``metadata.json``, and the reports, manifests and logs copied with
    ``copy2`` (not ``source_file.csv``, written at archive time, nor
    ``.<build>.incoming/``, a run that died before its move). And the newest
    report in ``manifest_dir`` that passed or warned before this export
    finished: the previous build's last validate, archived or not, which also
    bounds the first archive. A failed report is skipped, since it may be an
    earlier attempt at this build.
    """
    kept = [p for d in builds_dir.glob("*") if d.is_dir() and not d.name.startswith(".")
            for p in (d / "metadata.json", *d.glob("manifests/*"), *d.glob("logs/*"))
            if p.is_file()]
    ended = max((p.stat().st_mtime for p in kept), default=0.0)
    for path in manifest_dir.glob("validation_report-*.json"):
        mtime = path.stat().st_mtime
        if ended < mtime < export_finished:
            try:
                status = json.loads(path.read_text()).get("status")
            except (OSError, ValueError):
                continue
            if status != "fail":
                ended = mtime
    return ended


def build_logs(logs_dir: Path, *, since: float, export_finished: float,
               validated: float | None) -> list[Path]:
    """The step logs of this build's run.

    Anything written after the previous build ended (:func:`previous_build_ended`),
    except a validate log from before this export finished, which validated an
    earlier one, and any other step's log from after this build's validate
    report, which belongs to the next run.
    """
    if not logs_dir.is_dir():
        return []
    logs = []
    for path in logs_dir.glob("pubmed2db-*.out"):
        mtime = path.stat().st_mtime
        if mtime <= since:
            continue
        if path.name.startswith("pubmed2db-validate-"):
            if mtime < export_finished:
                continue
        elif validated is not None and mtime > validated:
            continue
        logs.append(path)
    return sorted(logs)


def dump_source_file(db_path: Path, out: Path, cutoff_file: str | None) -> bool:
    """Copy the files this build loaded, from the ``source_file`` registry, to CSV.

    Read-only; False, having said why, if it could not. Rows past the cutoff are files the
    database learned of after the export (a later ``download`` or ``load``),
    and rows never loaded were not in the build either: the same rows
    ``status.corpus_provenance`` counts for ``metadata.json``.
    """
    import duckdb

    from pubmed2db.db import parse_file_name

    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error as exc:
        print(f"could not open {db_path} read-only ({exc}); is a load or "
              "download still running?", file=sys.stderr)
        return False

    in_build = "processed_at IS NOT NULL"
    if cutoff_file is None:
        print("warning: metadata.json names no cutoff file; source_file.csv lists "
              "every file the database has loaded", file=sys.stderr)
    else:
        in_build += f" AND file_order_key <= {parse_file_name(cutoff_file)[2]}"
    try:
        later, unloaded = con.execute(
            "SELECT count(*) FILTER (WHERE processed_at IS NOT NULL), "
            "count(*) FILTER (WHERE processed_at IS NULL) "
            f"FROM source_file WHERE NOT ({in_build})"
        ).fetchone()
        if later:
            print(f"note: left {later} file(s) after the cutoff {cutoff_file} out of "
                  "source_file.csv; the database has moved on since this export")
        if unloaded:
            print(f"note: left {unloaded} file(s) the database never loaded out of "
                  "source_file.csv")
        con.execute(
            f"COPY (SELECT * FROM source_file WHERE {in_build} ORDER BY file_order_key) "
            "TO '" + out.as_posix().replace("'", "''") + "' (HEADER)"
        )
    except duckdb.Error as exc:
        out.unlink(missing_ok=True)
        print(f"could not dump source_file from {db_path} ({exc})", file=sys.stderr)
        return False
    finally:
        con.close()
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR") or "data"),
                        help="The pipeline's --data-dir (default: $DATA_DIR, else data).")
    parser.add_argument("--export-dir", type=Path, default=None,
                        help="The finished export (default: $EXPORT_DIR, else <data-dir>/json).")
    parser.add_argument("--manifest-dir", type=Path, default=None,
                        help="Where validate wrote its reports and manifests "
                             "(default: $MANIFEST_DIR, else <data-dir>/manifests).")
    parser.add_argument("--logs-dir", type=Path, default=None,
                        help="Where the Slurm step logs are (default: <data-dir>/logs).")
    parser.add_argument("--name", default=None,
                        help="Build name, when metadata.json has no corpus.build_name "
                             "(a cutoff file download never synced).")
    parser.add_argument("--without-validation", action="store_true",
                        help="Archive an export that validate has not been run on.")
    parser.add_argument("--without-source-file", action="store_true",
                        help="Archive without source_file.csv when the database cannot "
                             "be read. Its published MD5s are then recorded nowhere "
                             "once the database is wiped.")
    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir
    export_dir: Path = args.export_dir or Path(os.environ.get("EXPORT_DIR") or data_dir / "json")
    builds_dir = data_dir / "builds"
    manifest_dir: Path = args.manifest_dir or Path(
        os.environ.get("MANIFEST_DIR") or data_dir / "manifests")
    logs_dir: Path = args.logs_dir or data_dir / "logs"
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
            fail(f"{report} says this export failed validation; fix the cause, export "
                 "again with --overwrite (EXPORT_OVERWRITE=1 ./slurm/submit.sh export "
                 "validate), and archive that instead.")
        written = content.get("inputs", {}).get("manifest_written")
        if written and Path(written).is_file():
            manifest = Path(written)
        elif written:
            print(f"warning: {report.name} names the manifest {written}, which is not "
                  "there from this directory; not copying it", file=sys.stderr)

    # Read before this build's directory exists under builds/.
    export_finished = metadata_path.stat().st_mtime
    logs = build_logs(
        logs_dir, since=previous_build_ended(builds_dir, manifest_dir, export_finished),
        export_finished=export_finished,
        validated=None if report is None else report.stat().st_mtime,
    )

    # Gather everything beside the export first, so a failure here leaves the
    # export where it was and a re-run starts clean.
    builds_dir.mkdir(parents=True, exist_ok=True)
    incoming = builds_dir / f".{build}.incoming"
    if incoming.exists():
        shutil.rmtree(incoming)  # left by a run that died before the move
    incoming.mkdir()

    # First, as the one step that fails for a reason outside this script (a
    # job holding the database): nothing is copied or moved if it does.
    cutoff = metadata.get("corpus", {}).get("cutoff_file")
    if not db_path.is_file():
        wrote_csv = False
        print(f"{db_path} not found", file=sys.stderr)
    else:
        wrote_csv = dump_source_file(db_path, incoming / "source_file.csv", cutoff)
    if not wrote_csv:
        if not args.without_source_file:
            shutil.rmtree(incoming)
            fail("no source_file.csv, the only record of the MD5 NCBI published for "
                 "each file once the database is wiped. Fix the cause and re-run, or "
                 "pass --without-source-file.")
        print("note: archiving without source_file.csv (--without-source-file)")
    else:
        print(f"wrote {dest / 'source_file.csv'} (the files this build loaded, "
              "with the MD5 NCBI published for each)")

    if report is not None:
        (incoming / "manifests").mkdir()
        shutil.copy2(report, incoming / "manifests" / report.name)
        print(f"copied {report.name} -> {dest / 'manifests'}")
        if manifest is not None:
            shutil.copy2(manifest, incoming / "manifests" / manifest.name)
            print(f"copied {manifest.name} -> {dest / 'manifests'}")
        else:
            print("note: the report names no manifest (a fail run writes none); "
                  "nothing copied")
    if logs:
        (incoming / "logs").mkdir()
        for log in logs:
            shutil.copy2(log, incoming / "logs" / log.name)
        print(f"copied {len(logs)} step log(s) -> {dest / 'logs'}")
    else:
        print(f"note: no step logs of this build's run in {logs_dir}; "
              "pass --logs-dir if the jobs wrote them elsewhere")

    shutil.move(str(export_dir), str(dest))
    print(f"moved {export_dir} -> {dest}")
    for item in sorted(incoming.iterdir()):
        item.rename(dest / item.name)
    incoming.rmdir()

    print()
    print(f"Build {build} is in {dest}. To publish it:")
    print(f"  rsync -a {dest}/ HOST:/var/babel_outputs/pubmed2db/{build}/")
    if db_path.is_file():
        print(f"  rsync {db_path} HOST:/var/babel_outputs/pubmed2db/{build}/pubmed.duckdb")
        print("The second line must run before the next download/load changes the database.")


if __name__ == "__main__":
    main()
