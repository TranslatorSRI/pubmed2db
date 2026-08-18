"""Download PubMed baseline and update files, with MD5 sidecar tracking.

We reuse ``pubmed_downloader`` for the actual file transfers (pystow-backed,
HTTP, skip-by-name). On top of that we fetch each ``<file>.md5`` sidecar, store
the published checksum in the ``source_file`` registry, and bump
``downloaded_at`` whenever a file is new or its checksum changed — which is what
later triggers a reload in :func:`pubmed2db.load.needs_load`.

PubMed files are normally immutable, so a checksum change is rare; verifying it
is cheap insurance and the mechanism that lets a corrected file be picked up.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import duckdb
import requests
from pubmed_downloader.api import (
    BASELINE_MODULE,
    BASELINE_PATH,
    BASELINE_URL,
    UPDATES_MODULE,
    UPDATES_PATH,
    UPDATES_URL,
    _ensure_urls,
)
from tqdm import tqdm

from .db import register_source_file

logger = logging.getLogger(__name__)

_MD5_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")


def parse_md5_text(text: str) -> str | None:
    """Extract the checksum from a PubMed ``.md5`` sidecar.

    The sidecar looks like ``MD5(pubmed25n0001.xml.gz)= 1a2b...``; we just pull
    out the 32-hex-digit token.
    """
    match = _MD5_RE.search(text)
    return match.group(1).lower() if match else None


def file_md5(path: Path) -> str:
    """Compute the MD5 hex digest of a local file."""
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "md5").hexdigest()


def _sync_kind(
    con: duckdb.DuckDBPyConnection,
    *,
    kind: str,
    base_url: str,
    list_cache: Path,
    ensure_module,
    registry: dict[str, str | None],
    limit: int | None,
    verify: bool,
) -> list[tuple[Path, str]]:
    # Always refresh the remote listing so newly published updatefiles appear.
    # _ensure_urls sorts it newest-first, so the newest N — what's useful for
    # testing — is the head. Slicing with None is the no-limit case.
    urls = _ensure_urls(base_url, list_cache, force=True)[:limit]

    results: list[tuple[Path, str]] = []
    for url in tqdm(urls, desc=f"Syncing PubMed {kind}", unit="file"):
        file_name = url.rsplit("/", 1)[-1]
        prior = registry.get(file_name)
        try:
            response = requests.get(url + ".md5", timeout=60)
            response.raise_for_status()
            published_md5 = parse_md5_text(response.text)
            if published_md5 is None:
                logger.warning("no checksum in the md5 sidecar for %s", file_name)
        except requests.RequestException as exc:
            logger.warning("could not fetch md5 for %s: %s", file_name, exc)
            published_md5 = None

        if published_md5 is None:
            # Keep the last-known checksum whenever the sidecar is unusable — a
            # transient failure, or an error page served with HTTP 200. Wiping it
            # would flag every already-known file as changed, re-parsing the whole
            # corpus on the next load, and keep doing so on every later sync.
            published_md5 = prior

        # Newness is registry membership, not `prior is None`: a known file can
        # have a NULL checksum (registered by `load` from an rsynced copy, or a
        # sidecar that has never been fetchable). Treating that as new would bump
        # downloaded_at on every sync and re-parse the file every run.
        changed = file_name not in registry or prior != published_md5

        # ensure() skips by file name, so a file republished under its old name
        # would keep its stale bytes on disk: we would record the new checksum
        # against the old content and never look again. Drop the local copy
        # before fetching, whether or not we go on to hash it. Only when we had
        # a prior checksum to compare — a first sync over an existing cache must
        # not re-download the whole corpus.
        if prior is not None and published_md5 is not None and prior != published_md5:
            logger.info("published md5 changed for %s; re-downloading", file_name)
            Path(ensure_module.join(name=file_name)).unlink(missing_ok=True)

        path = Path(ensure_module.ensure(url=url))

        # Only hash files we just fetched or whose published checksum moved:
        # re-hashing an unchanged, already-verified corpus costs tens of GiB of
        # I/O per sync and, since PubMed files are immutable, never catches
        # anything. Corruption happens at download time, which is still covered.
        if verify and published_md5 is not None and changed:
            actual = file_md5(path)
            if actual != published_md5:
                logger.warning(
                    "md5 mismatch for %s (got %s, expected %s); re-downloading",
                    file_name,
                    actual,
                    published_md5,
                )
                path.unlink(missing_ok=True)
                path = Path(ensure_module.ensure(url=url))
                # A retry that is also corrupt must not be registered as good.
                # Delete it too: `load` globs the download directories rather
                # than reading sync()'s return value, so a corrupt file left on
                # disk would be loaded anyway — and, being unregistered, would
                # be re-parsed on every later run.
                actual = file_md5(path)
                if actual != published_md5:
                    logger.error(
                        "md5 still mismatched for %s after re-downloading "
                        "(got %s, expected %s); discarding it",
                        file_name,
                        actual,
                        published_md5,
                    )
                    path.unlink(missing_ok=True)
                    continue
                changed = True

        # Bump downloaded_at only when new/changed, so unchanged files are not
        # needlessly reloaded.
        register_source_file(
            con,
            file_name,
            kind=kind,
            published_md5=published_md5,
            downloaded_at=changed,
        )
        results.append((path, kind))

    return results


def sync(
    con: duckdb.DuckDBPyConnection,
    *,
    baseline: bool = True,
    updates: bool = True,
    limit: int | None = None,
    verify: bool = True,
) -> list[tuple[Path, str]]:
    """Download baseline and/or update files, tracking MD5 checksums.

    Returns the list of ``(local_path, kind)`` for all files now present, which
    can be passed straight to :func:`pubmed2db.load.load_files`.
    """
    registry = {
        r[0]: r[1]
        for r in con.execute("SELECT file_name, published_md5 FROM source_file").fetchall()
    }

    results: list[tuple[Path, str]] = []
    if baseline:
        results += _sync_kind(
            con,
            kind="baseline",
            base_url=BASELINE_URL,
            list_cache=BASELINE_PATH,
            ensure_module=BASELINE_MODULE,
            registry=registry,
            limit=limit,
            verify=verify,
        )
    if updates:
        results += _sync_kind(
            con,
            kind="update",
            base_url=UPDATES_URL,
            list_cache=UPDATES_PATH,
            ensure_module=UPDATES_MODULE,
            registry=registry,
            limit=limit,
            verify=verify,
        )
    return results
