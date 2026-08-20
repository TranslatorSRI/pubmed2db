"""Small helpers shared by long-running pipeline steps (load, export)."""

from __future__ import annotations

import resource
import sys


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
