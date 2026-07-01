"""Command-line interface for pubmed2db."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from . import __version__
from .db import connect

DEFAULT_DATA_DIR = os.environ.get("PUBMED2DB_DATA_DIR", "data")


def _local_files() -> list[tuple[Path, str]]:
    """Enumerate already-downloaded PubMed files (baseline + updates)."""
    from pubmed_downloader.api import BASELINE_MODULE, UPDATES_MODULE

    files: list[tuple[Path, str]] = []
    for module, kind in ((BASELINE_MODULE, "baseline"), (UPDATES_MODULE, "update")):
        files.extend((path, kind) for path in Path(module.base).glob("*.xml.gz"))
    return files


@click.group()
@click.version_option(__version__)
@click.option(
    "--data-dir",
    default=DEFAULT_DATA_DIR,
    show_default=True,
    help="Root directory for downloaded PubMed files and the database.",
)
@click.option(
    "--db",
    default=None,
    show_default=False,
    help="Path to the DuckDB database (default: <data-dir>/pubmed.duckdb).",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging.")
@click.pass_context
def main(ctx: click.Context, data_dir: str, db: str | None, verbose: bool) -> None:
    """Download, store, and export PubMed abstracts."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Set PYSTOW_HOME before any pubmed_downloader import so its module paths
    # resolve under data_dir rather than ~/.data.
    os.environ["PYSTOW_HOME"] = str(Path(data_dir).resolve())
    ctx.ensure_object(dict)
    ctx.obj["db"] = db or os.environ.get(
        "PUBMED2DB_DB", str(Path(data_dir) / "pubmed.duckdb")
    )
    ctx.obj["verbose"] = verbose


@main.command()
@click.option("--baseline/--no-baseline", default=True, help="Sync baseline files.")
@click.option("--updates/--no-updates", default=True, help="Sync update files.")
@click.option("--limit", type=int, default=None, help="Only sync the first N files (testing).")
@click.option("--verify/--no-verify", default=True, help="Verify downloaded files against MD5.")
@click.pass_context
def download(ctx: click.Context, baseline: bool, updates: bool, limit: int | None, verify: bool) -> None:
    """Download baseline/update files and record MD5 checksums."""
    from .download import sync

    con = connect(ctx.obj["db"])
    try:
        results = sync(con, baseline=baseline, updates=updates, limit=limit, verify=verify)
        click.echo(f"Synced {len(results)} file(s).")
    finally:
        con.close()


@main.command()
@click.pass_context
def journals(ctx: click.Context) -> None:
    """Refresh the journal dimension from the NLM Catalog."""
    from .load import load_journals

    con = connect(ctx.obj["db"])
    try:
        n = load_journals(con)
        click.echo(f"Loaded {n} journals.")
    finally:
        con.close()


@main.command()
@click.option("--force", is_flag=True, help="Reload files even if already up to date.")
@click.pass_context
def load(ctx: click.Context, force: bool) -> None:
    """Parse downloaded files into the database (full history).

    Loads article data only. Run `pubmed2db journals` to (re)load the journal
    dimension used at export time, or `pubmed2db update` to do everything.
    """
    from .load import load_files

    con = connect(ctx.obj["db"])
    try:
        files = _local_files()
        if not files:
            raise click.ClickException(
                "No downloaded files found; run `pubmed2db download` first."
            )
        loaded = load_files(con, files, force=force)
        click.echo(f"Loaded {loaded} of {len(files)} file(s).")
    finally:
        con.close()


@main.command()
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "parquet"]),
    required=True,
    help="Export format.",
)
@click.option("--out", required=True, type=click.Path(file_okay=False), help="Output directory.")
@click.option("--shards", type=int, default=1, show_default=True, help="JSON: number of NDJSON shards.")
@click.option("--gzip/--no-gzip", "gzip_output", default=False, help="JSON: gzip each shard as it's written.")
@click.option(
    "--latest/--all",
    default=True,
    help="Parquet: export only the latest version (default) or full history.",
)
@click.pass_context
def export(ctx: click.Context, fmt: str, out: str, shards: int, gzip_output: bool, latest: bool) -> None:
    """Export the latest abstracts to JSON, or the database to Parquet."""
    from .export import export_json, export_parquet
    from .status import articles_loaded, journals_loaded, pending_file_count
    from .util import peak_rss_gib

    con = connect(ctx.obj["db"])
    try:
        if not articles_loaded(con):
            raise click.ClickException(
                "No articles loaded; run `pubmed2db load` first."
            )
        pending = pending_file_count(con)
        if pending:
            click.echo(
                f"Warning: {pending} downloaded file(s) not yet loaded; "
                "run `pubmed2db load` to include them in the export.",
                err=True,
            )
        if not journals_loaded(con):
            click.echo(
                "Warning: journal table is empty, so journal names will be blank; "
                "run `pubmed2db journals` to populate them.",
                err=True,
            )
        if ctx.obj.get("verbose"):
            # Surfaces DuckDB's own progress bar for the long COPY queries below,
            # giving feedback even within a single large table's export.
            con.execute("PRAGMA enable_progress_bar")

        click.echo(f"Starting {fmt} export to {out}...")
        if fmt == "json":
            paths = export_json(con, out, shards=shards, gzip_output=gzip_output)
        else:
            paths = export_parquet(con, out, latest=latest)
        click.echo(
            f"Wrote {len(paths)} file(s) to {out} (peak RSS {peak_rss_gib():.1f} GiB)."
        )
    finally:
        con.close()


@main.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option("--previous-report", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Prior validation_report.json to compare coverage against.")
@click.option("--sample-size", type=int, default=15, show_default=True,
              help="Records sampled per shard for API field validation.")
@click.option("--drop-sample", type=int, default=10, show_default=True,
              help="Dropped PMIDs sampled for deletion confirmation.")
@click.option("--seed", type=int, default=0, show_default=True,
              help="Seed for reproducible sampling.")
@click.option("--abstract-threshold", type=float, default=0.90, show_default=True,
              help="Minimum abstract similarity ratio before it counts as a mismatch.")
@click.option("--online/--offline", default=True, show_default=True,
              help="Run the Entrez API checks, or offline structure/DB checks only.")
@click.option("--api-key", default=None, envvar="NCBI_API_KEY",
              help="NCBI API key (raises the rate limit to 10 req/s).")
@click.option("--email", default=None, envvar="NCBI_EMAIL",
              help="Contact email sent to NCBI, per their usage etiquette.")
@click.option("--entrez-low", type=float, default=0.1, show_default=True,
              help="Lower bound of the acceptable exported/Entrez fraction.")
@click.option("--entrez-high", type=float, default=1.5, show_default=True,
              help="Upper bound of the acceptable exported/Entrez fraction.")
@click.option("--out", type=click.Path(dir_okay=False), default=None,
              help="Report path (default: <directory>/validation_report.json).")
@click.option("--fail-on-warn", is_flag=True, help="Exit non-zero on warnings too.")
@click.pass_context
def validate(
    ctx: click.Context,
    directory: str,
    previous_report: str | None,
    sample_size: int,
    drop_sample: int,
    seed: int,
    abstract_threshold: float,
    online: bool,
    api_key: str | None,
    email: str | None,
    entrez_low: float,
    entrez_high: float,
    out: str | None,
    fail_on_warn: bool,
) -> None:
    """Validate a directory of exported NDJSON shards and write a report.

    Writes a pretty-printed validation_report.json alongside the export whose
    leading errors/warnings arrays are empty when everything checks out. Exits
    non-zero on errors (add --fail-on-warn to also fail on warnings). The DuckDB
    database is used when present; pass --offline to skip all network checks.
    """
    from .status import articles_loaded
    from .validate import format_summary, run_validation, write_report

    export_dir = Path(directory)
    out_path = Path(out) if out else export_dir / "validation_report.json"

    # Use the database only if it already exists and has articles loaded; never
    # create it here, and leave DB-derived sections blank when it is unavailable.
    con = None
    db_path = Path(ctx.obj["db"])
    if db_path.exists():
        candidate = connect(str(db_path))
        if articles_loaded(candidate):
            con = candidate
        else:
            candidate.close()

    try:
        if online and not email:
            click.echo(
                "Warning: no --email/NCBI_EMAIL set; NCBI asks that API callers "
                "identify themselves.",
                err=True,
            )
        report = run_validation(
            export_dir,
            con=con,
            previous_report=Path(previous_report) if previous_report else None,
            sample_size=sample_size,
            drop_sample=drop_sample,
            seed=seed,
            abstract_threshold=abstract_threshold,
            online=online,
            api_key=api_key,
            email=email,
            entrez_low=entrez_low,
            entrez_high=entrez_high,
        )
    finally:
        if con is not None:
            con.close()

    write_report(report, out_path)
    click.echo(format_summary(report))
    click.echo(f"Report written to {out_path}")

    if report["status"] == "fail":
        ctx.exit(1)
    if report["status"] == "warn" and fail_on_warn:
        ctx.exit(2)


def _fmt_ts(ts: object) -> str:
    """Render a timestamp for the status report, or 'never' if missing."""
    return ts.strftime("%Y-%m-%d %H:%M") if ts is not None else "never"


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Report what's been downloaded, loaded, and is ready to export."""
    from .status import summarize

    con = connect(ctx.obj["db"])
    try:
        s = summarize(con)
        pct = (
            f"{100 * s['loaded_files'] / s['downloaded_files']:.1f}%"
            if s["downloaded_files"]
            else "n/a"
        )

        click.echo(f"Database:  {ctx.obj['db']}")
        click.echo(
            f"Download:  {s['downloaded_files']} of {s['known_files']} known file(s) "
            f"downloaded ({s['baseline_files']} baseline, {s['update_files']} update)"
        )
        click.echo(f"           last download: {_fmt_ts(s['last_download'])}")
        click.echo(
            f"Journals:  {s['journals']} loaded; "
            f"last refresh: {_fmt_ts(s['journals_refreshed'])}"
        )
        click.echo(
            f"Load:      {s['loaded_files']} of {s['downloaded_files']} downloaded "
            f"file(s) loaded ({pct})"
        )
        if s["pending_files"]:
            click.echo(
                f"           {s['pending_files']} file(s) downloaded but not loaded "
                "→ run `pubmed2db load`"
            )
        click.echo(f"           last load: {_fmt_ts(s['last_load'])}")
        click.echo(
            f"           {s['article_versions']} article version(s); "
            f"{s['latest_documents']} latest document(s)"
        )

        # Mirror the export command's prerequisites as an at-a-glance verdict.
        if not s["articles_loaded"]:
            click.echo("Export:    blocked — no articles loaded; run `pubmed2db load`")
        elif not s["journals_loaded"]:
            click.echo("Export:    ready, but journal names will be blank "
                       "(run `pubmed2db journals`)")
        elif s["pending_files"]:
            click.echo("Export:    ready, but would miss the unloaded file(s) above")
        else:
            click.echo("Export:    ready")
    finally:
        con.close()


@main.command()
@click.option("--limit", type=int, default=None, help="Only sync the first N files (testing).")
@click.option("--force", is_flag=True, help="Reload files even if already up to date.")
@click.pass_context
def update(ctx: click.Context, limit: int | None, force: bool) -> None:
    """Download, refresh journals, and load — for scheduled runs."""
    from .download import sync
    from .load import load_files, load_journals

    con = connect(ctx.obj["db"])
    try:
        results = sync(con, limit=limit)
        click.echo(f"Synced {len(results)} file(s).")
        n = load_journals(con)
        click.echo(f"Loaded {n} journals.")
        loaded = load_files(con, results, force=force)
        click.echo(f"Loaded {loaded} file(s).")
    finally:
        con.close()


if __name__ == "__main__":
    main()
