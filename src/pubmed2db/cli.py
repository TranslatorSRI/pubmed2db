"""Command-line interface for pubmed2db."""

from __future__ import annotations

import logging
import os
from contextlib import closing
from pathlib import Path

import click
from click.core import ParameterSource

from . import __version__
from .db import connect

def _load_local(con, *, force: bool, require_files: bool) -> None:
    """Load every downloaded file, reporting the counts the CLI prints.

    Scans the download directories rather than one sync's results, so files
    downloaded by an earlier run aren't skipped. Shared by `load` and `update`,
    which differ only in whether an empty directory is an error.
    """
    from .download import local_files
    from .load import load_files

    files = local_files()
    if require_files and not files:
        raise click.ClickException("No downloaded files found; run `pubmed2db download` first.")
    loaded, failed = load_files(con, files, force=force)
    click.echo(f"Loaded {loaded} file(s); {len(files)} local file(s) checked.")
    if failed:
        raise click.ClickException(
            f"{len(failed)} file(s) failed to load: {', '.join(failed)}"
        )


def _sync_options(f):
    """Shared ``--baseline/--updates/--limit/--verify`` options for ``download``
    and ``update``, which both call :func:`pubmed2db.download.sync`."""
    f = click.option("--verify/--no-verify", default=True, help="Verify downloaded files against MD5.")(f)
    f = click.option(
        "--limit",
        type=click.IntRange(min=1),
        default=None,
        help="Only sync the newest N files (testing).",
    )(f)
    f = click.option("--updates/--no-updates", default=True, help="Sync update files.")(f)
    f = click.option("--baseline/--no-baseline", default=True, help="Sync baseline files.")(f)
    return f


@click.group()
@click.version_option(__version__)
@click.option(
    "--data-dir",
    default="data",
    show_default=True,
    envvar="PUBMED2DB_DATA_DIR",
    show_envvar=True,
    help="Root directory for downloaded PubMed files and the database.",
)
@click.option(
    "--db",
    default=None,
    show_default=False,
    envvar="PUBMED2DB_DB",
    show_envvar=True,
    help="Path to the DuckDB database (default: <data-dir>/pubmed.duckdb).",
)
@click.option(
    "--threads",
    type=click.IntRange(min=1),
    default=None,
    envvar="PUBMED2DB_THREADS",
    show_envvar=True,
    help=(
        "Cap DuckDB's thread pool (default: the Slurm allocation's "
        "--cpus-per-task, or the machine's core count off a cluster). Rarely "
        "needed; use it to leave a busy node headroom."
    ),
)
@click.option(
    "--temp-dir",
    default=None,
    envvar="PUBMED2DB_DUCKDB_TEMP_DIR",
    show_envvar=True,
    help=(
        "Where DuckDB spills when a query exceeds memory (point at local "
        "scratch for large exports)."
    ),
)
@click.option(
    "--memory-limit",
    default=None,
    envvar="PUBMED2DB_DUCKDB_MEMORY_LIMIT",
    show_envvar=True,
    help=(
        "Cap DuckDB's buffer pool, e.g. '48GB' (default: ~76% of a Slurm "
        "allocation, which leaves only the remaining quarter for the lxml tree, "
        "the parsed records and the Arrow batch)."
    ),
)
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging.")
@click.pass_context
def main(
    ctx: click.Context,
    data_dir: str,
    db: str | None,
    threads: int | None,
    temp_dir: str | None,
    memory_limit: str | None,
    verbose: bool,
) -> None:
    """Download, store, and export PubMed abstracts."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Set PYSTOW_HOME before any pubmed_downloader import so its module paths
    # resolve under data_dir rather than ~/.data.
    os.environ["PYSTOW_HOME"] = str(Path(data_dir).resolve())
    ctx.ensure_object(dict)
    ctx.obj["db"] = db or str(Path(data_dir) / "pubmed.duckdb")
    ctx.obj["threads"] = threads
    ctx.obj["temp_dir"] = temp_dir
    ctx.obj["memory_limit"] = memory_limit
    ctx.obj["verbose"] = verbose


def _connect(ctx: click.Context):
    """Open the database with the group-level DuckDB tuning options applied."""
    return connect(
        ctx.obj["db"],
        threads=ctx.obj["threads"],
        temp_directory=ctx.obj["temp_dir"],
        memory_limit=ctx.obj["memory_limit"],
    )


@main.command()
@_sync_options
@click.pass_context
def download(ctx: click.Context, baseline: bool, updates: bool, limit: int | None, verify: bool) -> None:
    """Download baseline/update files and record MD5 checksums."""
    from .download import sync

    with closing(_connect(ctx)) as con:
        results = sync(con, baseline=baseline, updates=updates, limit=limit, verify=verify)
        click.echo(f"Synced {len(results)} file(s).")


@main.command()
@click.pass_context
def journals(ctx: click.Context) -> None:
    """Refresh the journal dimension from the NLM Catalog."""
    from .load import load_journals

    with closing(_connect(ctx)) as con:
        n = load_journals(con)
        click.echo(f"Loaded {n} journals.")


@main.command()
@click.option("--force", is_flag=True, help="Reload files even if already up to date.")
@click.pass_context
def load(ctx: click.Context, force: bool) -> None:
    """Parse downloaded files into the database (full history).

    Loads article data only. Run `pubmed2db journals` to (re)load the journal
    dimension used at export time, or `pubmed2db update` to do everything.
    """
    with closing(_connect(ctx)) as con:
        _load_local(con, force=force, require_files=True)


@main.command()
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "parquet"]),
    required=True,
    help="Export format.",
)
@click.option("--out", required=True, type=click.Path(file_okay=False), help="Output directory.")
@click.option(
    "--shards",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="JSON: number of NDJSON shards.",
)
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
    from .status import export_readiness
    from .util import peak_rss_gib

    # Flags that only apply to the other format are silently inert otherwise,
    # which is an expensive thing to discover after a 20-minute export.
    for flag, option, applies_to in (
        ("--shards", "shards", "json"),
        ("--gzip", "gzip_output", "json"),
        ("--latest", "latest", "parquet"),
    ):
        if fmt != applies_to and ctx.get_parameter_source(option) == ParameterSource.COMMANDLINE:
            click.echo(
                f"Warning: {flag} only applies to --format {applies_to}; ignoring it.",
                err=True,
            )

    with closing(_connect(ctx)) as con:
        error, warnings = export_readiness(con)
        if error:
            raise click.ClickException(error)
        for warning in warnings:
            click.echo(f"Warning: {warning}", err=True)

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


@main.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option("--previous-report", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Prior validation_report.json to compare coverage against.")
@click.option("--previous-manifest", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Prior pmids.txt.gz manifest; reports which PMIDs disappeared "
                   "since that export.")
@click.option("--manifest", "manifest", type=click.Path(dir_okay=False), default=None,
              help="Write this export's sorted PMID manifest here (gzipped), for a "
                   "later run to diff against with --previous-manifest. Skipped "
                   "when the run fails, so a bad export cannot become the next "
                   "run's baseline.")
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
@click.option("--entrez-low", type=float, default=0.95, show_default=True,
              help="Lower bound of the acceptable exported/Entrez fraction. "
                   "Widen for a partial export (e.g. a --limit test run).")
@click.option("--entrez-high", type=float, default=1.05, show_default=True,
              help="Upper bound of the acceptable exported/Entrez fraction.")
@click.option("--out", type=click.Path(dir_okay=False), default=None,
              help="Report path (default: <directory>/validation_report.json).")
@click.option("--fail-on-warn", is_flag=True, help="Exit non-zero on warnings too.")
@click.pass_context
def validate(
    ctx: click.Context,
    directory: str,
    previous_report: str | None,
    previous_manifest: str | None,
    manifest: str | None,
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
        candidate = _connect(ctx)
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
            previous_manifest=Path(previous_manifest) if previous_manifest else None,
            manifest_out=Path(manifest) if manifest else None,
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
    from .status import export_readiness, summarize

    with closing(_connect(ctx)) as con:
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
        if len(s["baseline_years"]) > 1:
            years = ", ".join(f"20{y:02d}" for y in s["baseline_years"])
            click.echo(
                f"           {len(s['baseline_years'])} baseline years present ({years}); "
                f"only 20{s['baseline_years'][-1]:02d} is exported"
            )
            click.echo(
                '           the older year(s) are dead weight — see the README\'s '
                '"Re-running after a gap"'
            )
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
        if s["skipped_records"]:
            # Records the loader could not store: extractor rejections plus book
            # citations. Kept visible because they are otherwise invisible —
            # they never become rows, so no count downstream is short.
            click.echo(f"           {s['skipped_records']} record(s) skipped while parsing")
        click.echo(
            f"           {s['article_versions']} article version(s); "
            f"{s['latest_documents']} latest document(s)"
        )

        error, warnings = export_readiness(con)
        if error:
            click.echo(f"Export:    blocked — {error}")
        elif warnings:
            click.echo(f"Export:    ready, but: {'; '.join(warnings)}")
        else:
            click.echo("Export:    ready")


@main.command()
@_sync_options
@click.option("--force", is_flag=True, help="Reload files even if already up to date.")
@click.pass_context
def update(
    ctx: click.Context,
    baseline: bool,
    updates: bool,
    limit: int | None,
    verify: bool,
    force: bool,
) -> None:
    """Download, refresh journals, and load — for scheduled runs."""
    from .download import sync
    from .load import load_journals

    with closing(_connect(ctx)) as con:
        results = sync(con, baseline=baseline, updates=updates, limit=limit, verify=verify)
        click.echo(f"Synced {len(results)} file(s).")
        # The journal refresh is the least critical of the three steps: don't
        # let an NLM Catalog outage throw away a completed download and skip the
        # load. The previous journal dimension stays in place.
        try:
            n = load_journals(con)
            click.echo(f"Loaded {n} journals.")
        except Exception as exc:  # noqa: BLE001 - any failure here is non-fatal
            click.echo(
                f"Journal refresh failed ({exc}); keeping the existing journals.", err=True
            )
        _load_local(con, force=force, require_files=False)


if __name__ == "__main__":
    main()
