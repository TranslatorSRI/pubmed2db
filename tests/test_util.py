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


class _StreamedResponse:
    """Enough of `requests.Response` for `download_file`."""

    def __init__(self, status, chunks):
        self.status, self.chunks = status, chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        import requests

        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Server Error")

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def test_download_file_sets_a_timeout(monkeypatch, tmp_path):
    """pystow's urllib backend has none, and a stalled NLM connection hung the
    journals step indefinitely on a live run. The read timeout bounds a stall."""
    import pubmed2db.util as util_mod

    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return _StreamedResponse(200, [b"<NLMCatalog", b"RecordSet/>"])

    monkeypatch.setattr(util_mod.requests, "get", fake_get)
    util_mod.download_file("https://x/serfile.20260901.xml", tmp_path / "f.xml")

    assert (tmp_path / "f.xml").read_bytes() == b"<NLMCatalogRecordSet/>"
    assert calls[0]["timeout"] and calls[0]["stream"]


def test_download_file_never_leaves_a_partial_file(monkeypatch, tmp_path):
    """A 5xx must not be saved as the file (pystow's requests backend would),
    and a transfer that dies partway must not leave a truncated file under the
    real name -- nor replace a good cached one."""
    import requests

    import pubmed2db.util as util_mod

    target = tmp_path / "serfile.20260901.xml"
    target.write_text("previous good copy")

    monkeypatch.setattr(
        util_mod.requests, "get", lambda url, **k: _StreamedResponse(503, [b"<html>"])
    )
    with pytest.raises(requests.HTTPError):
        util_mod.download_file("https://x", target)

    stalled = requests.ConnectionError("Read timed out")
    monkeypatch.setattr(
        util_mod.requests, "get", lambda url, **k: _StreamedResponse(200, [b"<NLM", stalled])
    )
    with pytest.raises(requests.ConnectionError):
        util_mod.download_file("https://x", target)

    assert target.read_text() == "previous good copy"
    assert list(tmp_path.iterdir()) == [target]  # no .part left behind
