"""Tests for the small helpers shared by the long-running pipeline steps."""

from __future__ import annotations

import pytest

from pubmed2db.util import current_rss_gib, eta_str, fmt_duration, peak_rss_gib


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0s"), (45, "45s"), (60, "1m 00s"), (187, "3m 07s"), (8100, "2h 15m")],
)
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


@pytest.mark.parametrize(
    "elapsed,done,remaining,expected",
    [
        (100.0, 0, 5, "?"),        # nothing finished yet, so no rate to project
        (100.0, 5, 0, "done"),
        (100.0, 5, 5, "1m 40s"),   # 20 s/item x 5 remaining
    ],
)
def test_eta_str(elapsed, done, remaining, expected):
    assert eta_str(elapsed, done, remaining) == expected


def test_peak_rss_is_a_high_water_mark():
    """peak_rss_gib never falls -- which is why it alone can't show a leak.

    A load logs this once per file, so a climbing number is the mark doing its
    job, not evidence that the process is holding more than it was. See
    slurm/README.md.
    """
    before = peak_rss_gib()
    ballast = bytearray(200 * 1024 * 1024)
    during = peak_rss_gib()
    assert during >= before
    del ballast
    assert peak_rss_gib() >= during   # freeing memory does not lower it


def test_current_rss_is_optional_but_plausible():
    """current_rss_gib reads /proc, so it is None off Linux rather than wrong."""
    rss = current_rss_gib()
    if rss is None:
        pytest.skip("no /proc on this platform (expected on macOS)")
    assert 0 < rss < 1024
    # It tracks the real footprint, so it must not exceed the high-water mark.
    assert rss <= peak_rss_gib() + 0.5
