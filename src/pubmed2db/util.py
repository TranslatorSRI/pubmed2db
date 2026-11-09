"""Small helpers shared by long-running pipeline steps (download, load, export)."""

from __future__ import annotations

import logging
import os
import resource
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

import requests
import urllib3

logger = logging.getLogger(__name__)


def peak_rss_gib() -> float:
    """Peak resident set size of this process so far, in GiB.

    ``ru_maxrss`` is a high-water mark in bytes on macOS and KiB on Linux; we log
    it after long-running steps so a Slurm run reveals how much ``--mem`` it
    really needs.

    **It only ever rises.** Logged once per file it is the maximum over the whole
    run to date, not that file's footprint, so a climbing number is not by itself
    evidence of a leak — pair it with :func:`current_rss_gib`, which can fall.
    """
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1024**3 if sys.platform == "darwin" else rss / 1024**2


def current_rss_gib() -> float | None:
    """Resident set size *right now*, in GiB, or ``None`` where unavailable.

    Unlike :func:`peak_rss_gib` this can go down, which is what distinguishes
    "memory is genuinely growing" from "the high-water mark was set once early
    and never came back down". Read from ``/proc``; returns ``None`` off Linux
    (macOS has no equivalent without a third-party dependency), so callers must
    treat it as optional.
    """
    try:
        with open("/proc/self/statm") as handle:
            pages = int(handle.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return pages * resource.getpagesize() / 1024**3


def _proc_kib(path: str, *keys: str) -> tuple[int, ...] | None:
    """The kB figures on the named ``Key:   1234 kB`` lines of a ``/proc`` file.

    One read for all of them, so figures from the same file are from the same
    instant. ``None`` if the file is unreadable or any key is missing.
    """
    found: dict[str, int] = {}
    try:
        with open(path) as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name in keys:
                    found[name] = int(rest.split()[0])
    except (OSError, IndexError, ValueError):
        return None
    if len(found) < len(keys):
        return None
    return tuple(found[key] for key in keys)


def virtual_size_gib() -> float | None:
    """This process's virtual size (``VmSize``) in GiB, or ``None`` off Linux.

    Under strict overcommit (``vm.overcommit_memory=2``, as on ``ht1``) what the
    kernel charges is reserved memory, not resident memory, so RSS can sit far
    below the point where an allocation is refused. ``VmSize`` is an upper
    bound on this process's share of that charge (it also counts read-only and
    shared mappings, which are not charged).
    """
    kib = _proc_kib("/proc/self/status", "VmSize")
    return None if kib is None else kib[0] / 1024**2


def commit_charge_gib() -> tuple[float, float] | None:
    """The node's ``(Committed_AS, CommitLimit)`` in GiB, or ``None`` off Linux.

    Both are **node-wide**, not per job: ``Committed_AS`` includes every
    neighbour's reservations. Under ``vm.overcommit_memory=2`` an allocation
    that would take it past ``CommitLimit`` fails, which DuckDB reports as
    ``(bad allocation)``; in the other modes the limit is not enforced.
    """
    kib = _proc_kib("/proc/meminfo", "Committed_AS", "CommitLimit")
    if kib is None:
        return None
    return kib[0] / 1024**2, kib[1] / 1024**2


def memory_map_count() -> tuple[int, int] | None:
    """This process's memory mappings and ``vm.max_map_count``, or ``None`` off Linux.

    Past ``max_map_count`` mappings, ``mmap`` fails with ENOMEM however much
    memory is free, which DuckDB reports as ``(bad allocation)``. The suspect
    for the 2026-11-09 export failure (#42): under ``vm.overcommit_memory=2``
    jemalloc hands memory back by remapping it ``PROT_NONE``, and each remap in
    the middle of a mapping splits it. Counting ``/proc/self/maps`` takes the
    kernel's mapping lock briefly; once a minute it is negligible.
    """
    try:
        with open("/proc/sys/vm/max_map_count") as handle:
            limit = int(handle.read())
        with open("/proc/self/maps", "rb") as handle:
            count = sum(1 for _ in handle)
    except (OSError, ValueError):
        return None
    return count, limit


def memory_summary() -> str:
    """``RSS … · VSZ … · node commit …/… GiB · maps …/…``, ``n/a`` where unavailable.

    The numbers that, together, say which memory limit a step is near: RSS
    against ``--mem`` (the cgroup), VSZ and the node's commit charge against
    ``CommitLimit`` (strict overcommit), and the mapping count against
    ``vm.max_map_count``. slurm/README.md, "A refused allocation", says how
    to read them.
    """
    rss = current_rss_gib()
    vsz = virtual_size_gib()
    commit = commit_charge_gib()
    maps = memory_map_count()
    return " · ".join(
        (
            f"RSS {'n/a' if rss is None else f'{rss:.1f} GiB'}",
            f"VSZ {'n/a' if vsz is None else f'{vsz:.1f} GiB'}",
            "node commit " + ("n/a" if commit is None else f"{commit[0]:.1f}/{commit[1]:.1f} GiB"),
            "maps " + ("n/a" if maps is None else f"{maps[0]}/{maps[1]}"),
        )
    )


def size_bytes(paths: Iterable[str | Path]) -> int:
    """Total size of the files that still exist, skipping any that vanish.

    For directories DuckDB is writing while we read them (shards, spill
    files): a path can disappear between listing and ``stat``, which is a
    missing data point, not a reason to take the heartbeat down.
    """
    total = 0
    for path in paths:
        try:
            total += os.stat(path).st_size
        except OSError:
            continue
    return total


def dir_size_gib(path: str | Path | None) -> float:
    """Total size of the files under ``path`` in GiB; 0 if it is missing or unset."""
    if not path:
        return 0.0
    files = (
        os.path.join(dirpath, name) for dirpath, _dirs, names in os.walk(path) for name in names
    )
    return size_bytes(files) / 1024**3


def fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m"


def eta_str(elapsed: float, done: int, remaining: int) -> str:
    """Estimate time remaining from progress so far, for a progress log line."""
    if remaining <= 0:
        return "done"
    if done <= 0:
        return "?"
    return fmt_duration(elapsed / done * remaining)


def _raw_chunks(response: requests.Response):
    """The undecoded body, raising requests' exceptions as ``iter_content`` does.

    ``raw.stream`` raises urllib3's own (a read timeout is
    ``urllib3.exceptions.ReadTimeoutError``), which an ``except
    requests.RequestException`` would not catch.
    """
    try:
        yield from response.raw.stream(1 << 20, decode_content=False)
    except urllib3.exceptions.ProtocolError as exc:
        raise requests.exceptions.ChunkedEncodingError(exc) from exc
    except urllib3.exceptions.ReadTimeoutError as exc:
        raise requests.exceptions.ConnectionError(exc) from exc
    except urllib3.exceptions.SSLError as exc:
        raise requests.exceptions.SSLError(exc) from exc


def download_file(
    url: str, path: Path, *, session: requests.Session | None = None
) -> Mapping[str, str]:
    """Stream ``url`` to ``path``, replacing it only once the transfer completes.

    Use this rather than pystow's ``ensure()`` for anything large. Its default
    urllib backend sets no timeout, so a connection the server stops sending on
    hangs the step until Slurm kills it — observed live on NLM's serial
    catalog, with serfile.20260901.xml stalled at 200 KB of 730 KB and the
    socket still open. Its requests backend takes a timeout but never checks
    the status, so it would save a 5xx page as the file. The read timeout bounds
    the gap between chunks, not the whole transfer. Writing to a ``.part`` and
    renaming means a killed job never leaves a truncated file under the real
    name for the next run to trust (a SIGKILL skips the ``finally``, so callers
    sweep stale ``.part`` files themselves).

    The file on disk is the bytes exactly as served, never decoded. requests
    asks for gzip by default and ``iter_content`` undoes any
    ``Content-Encoding``, so a server that labels a ``.xml.gz`` with
    ``Content-Encoding: gzip`` — a common Apache misconfiguration — would have
    had plain XML saved under the ``.gz`` name, failing every MD5 check. Asking
    for ``identity`` keeps a well-behaved server from compressing in transit,
    which is what makes the raw bytes the file.

    Pass a ``session`` to reuse its connections across many files. Returns the
    response's headers, so a caller can record a validator (an ETag) that
    describes exactly the bytes written.
    """
    logger.info("downloading %s", url)
    part = path.with_name(path.name + ".part")
    get = (session or requests).get
    try:
        with get(
            url, stream=True, timeout=(60, 120), headers={"Accept-Encoding": "identity"}
        ) as response:
            response.raise_for_status()
            with part.open("wb") as out:
                for chunk in _raw_chunks(response):
                    out.write(chunk)
            headers = response.headers
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)
    return headers
