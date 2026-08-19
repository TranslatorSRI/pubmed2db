"""Small helpers shared by long-running pipeline steps (load, export)."""

from __future__ import annotations

import resource
import sys


def peak_rss_gib() -> float:
    """Peak resident set size of this process so far, in GiB.

    ``ru_maxrss`` is a high-water mark in bytes on macOS and KiB on Linux; we log
    it after long-running steps so a Slurm run reveals how much ``--mem`` it
    really needs.
    """
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1024**3 if sys.platform == "darwin" else rss / 1024**2


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
