"""Tests for JSON and Parquet export."""

from __future__ import annotations

import json

import pytest


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3", "Mar"),
        ("03", "Mar"),
        ("Mar", "Mar"),
        ("March", "Mar"),
        ("Sept", "Sep"),
        ("sep", "Sep"),
        # Approximate months pass through verbatim (issue #14). "Sep-Dec" is the
        # case the old 3-char prefix match silently truncated to "Sep".
        ("Sep-Dec", "Sep-Dec"),
        ("Jul-Aug", "Jul-Aug"),
        ("Spring", "Spring"),
        ("13", ""),         # a number that isn't a month is garbage, not a season
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_month(raw, expected):
    from pubmed2db.export import normalize_month

    assert normalize_month(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1994 Sep-Dec", "Sep-Dec"),      # the DocumentMetadataAPI example, PMID:8000234
        ("1998 Spring", "Spring"),
        ("1978 Jul-Aug", "Jul-Aug"),
        ("1998 Dec-1999 Jan", "Dec-1999 Jan"),  # verbatim; we don't invent "Dec-Jan"
        ("  2001 Winter", "Winter"),
        ("1999-2000", ""),                # a year range has no month; not "-2000"
        ("n.d.", ""),
        ("Spring 1998", ""),              # year not leading; don't guess
        (None, ""),
        ("", ""),
    ],
)
def test_month_from_medline_date(raw, expected):
    from pubmed2db.export import _month_from_medline_date

    assert _month_from_medline_date(raw) == expected


def test_pub_month_prefers_the_column_over_the_medline_date():
    """The MedlineDate is a fallback, not an override."""
    from pubmed2db.export import pub_month

    assert pub_month("Mar", "1998 Spring") == "Mar"
    assert pub_month(None, "1994 Sep-Dec") == "Sep-Dec"
    assert pub_month("Sep-Dec", None) == "Sep-Dec"   # efetch's <Season> form
    assert pub_month(None, None) == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1998 Spring", "1998"),
        ("1978 Jul-Aug", "1978"),
        ("1998 Dec-1999 Jan", "1998"),   # leading year wins on a cross-year range
        ("1999-2000", "1999"),
        ("  2001 Winter", "2001"),
        ("n.d.", ""),                     # no year to recover
        ("Spring 1998", ""),              # year not leading; don't guess
        (None, ""),
        ("", ""),
    ],
)
def test_year_from_medline_date(raw, expected):
    from pubmed2db.export import _year_from_medline_date

    assert _year_from_medline_date(raw) == expected


def _read_ndjson(paths):
    docs = []
    for path in paths:
        with path.open() as handle:
            docs.extend(json.loads(line) for line in handle)
    return {doc["id"]: doc for doc in docs}


def test_json_export_uses_spec_fields(loaded_con, tmp_path):
    from pubmed2db.export import export_json

    paths = export_json(loaded_con, tmp_path / "json")
    docs = _read_ndjson(paths)

    # Deleted PMID 1002 absent; latest version of 1001 present.
    assert set(docs) == {"PMID:1001", "PMID:1003"}

    one = docs["PMID:1001"]
    assert one == {
        "id": "PMID:1001",
        # v2's PMCID (PMC7654321), not v1's PMC1234567, and no `pii`. Sorted,
        # so uppercase "PMC:" precedes lowercase "doi:", with the PMID first.
        "identifiers": ["PMID:1001", "PMC:PMC7654321", "doi:10.1038/example1001"],
        "journal_name": "Nature",
        "journal_abbrev": "Nature",
        "article_title": "Revised title for article one.",
        "volume": "581",
        "issue": "7807",
        "pub_year": "2020",
        "pub_month": "Mar",
        "pub_day": "16",
        "abstract": "The revised abstract for article one.",
    }


def test_json_export_empty_string_not_null(loaded_con, tmp_path):
    from pubmed2db.export import export_json

    docs = _read_ndjson(export_json(loaded_con, tmp_path / "json"))
    three = docs["PMID:1003"]
    # No DOI or PMCID: the record still carries its own PMID, never null.
    assert three["identifiers"] == ["PMID:1003"]
    # MedlineDate-only article ("1998 Spring"): both the year and the season are
    # recovered from the free-text date. pub_day stays empty -- a season has no
    # single day, and the spec's own PMID:8000234 example leaves it blank.
    assert three["pub_year"] == "1998"
    assert three["pub_month"] == "Spring"
    assert three["pub_day"] == ""
    assert three["issue"] == ""
    assert three["abstract"] == ""
    assert all(value is not None for value in three.values())


def test_json_sharding(loaded_con, tmp_path):
    """`shards` is a cap on writer threads, so it is a maximum, not a promise.

    DuckDB writes one file per thread that produced rows, and two fixture
    records will not occupy two threads -- what must hold is that no more than
    the requested number of shards appears and that no record is lost.
    """
    from pubmed2db.export import export_json

    before = loaded_con.execute("SELECT current_setting('threads')").fetchone()[0]
    paths = export_json(loaded_con, tmp_path / "json", shards=2)
    assert 1 <= len(paths) <= 2
    assert _read_ndjson(paths).keys() == {"PMID:1001", "PMID:1003"}
    # The thread cap applies to that COPY only; a later query gets the
    # connection's own parallelism back.
    assert loaded_con.execute("SELECT current_setting('threads')").fetchone()[0] == before


def test_json_export_replaces_a_previous_run(loaded_con, tmp_path):
    """A shard from an earlier run must not survive into this one's output.

    DuckDB appends to a per-thread output directory rather than clearing it, so
    a run that produces fewer files than its predecessor would otherwise leave
    stale records behind for `validate` (and the ingest) to read as current.
    """
    from pubmed2db.export import export_json

    out = tmp_path / "json"
    out.mkdir()
    stale = out / "pubmed_metadata_99.ndjson"
    stale.write_text(json.dumps({"id": "PMID:404"}) + "\n")

    paths = export_json(loaded_con, out)
    assert not stale.exists()
    assert _read_ndjson(paths).keys() == {"PMID:1001", "PMID:1003"}


def test_parquet_latest_filters_versions(loaded_con, tmp_path):
    from pubmed2db.export import export_parquet

    out = tmp_path / "pq"
    export_parquet(loaded_con, out, latest=True)

    n_article = loaded_con.execute(
        f"SELECT count(*) FROM read_parquet('{(out / 'article.parquet').as_posix()}')"
    ).fetchone()[0]
    n_abstract = loaded_con.execute(
        f"SELECT count(*) FROM read_parquet('{(out / 'abstract_text.parquet').as_posix()}')"
    ).fetchone()[0]
    # Latest set: articles 1001 (v2) and 1003; only 1001 v2's single abstract section.
    assert n_article == 2
    assert n_abstract == 1


def test_parquet_all_keeps_history(loaded_con, tmp_path):
    from pubmed2db.export import export_parquet

    out = tmp_path / "pq_all"
    export_parquet(loaded_con, out, latest=False)
    n_article = loaded_con.execute(
        f"SELECT count(*) FROM read_parquet('{(out / 'article.parquet').as_posix()}')"
    ).fetchone()[0]
    # All versions: 1001 (x2), 1002, 1003.
    assert n_article == 4


def test_writing_progress_line_renders(tmp_path, caplog):
    """The heartbeat only fires a minute into a real export, so it never runs in
    the suite -- and a mismatched %-format arg there would first surface partway
    through a production run. Render one line directly."""
    import logging
    import time

    from pubmed2db import export as ex

    out = tmp_path / "json"
    out.mkdir()
    (out / "pubmed_metadata_0.ndjson").write_bytes(b"x" * 4096)
    with caplog.at_level(logging.INFO, logger="pubmed2db.export"):
        ex._log_writing_progress(out, time.monotonic() - 5)

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("writing:"))
    for fragment in ("GiB across 1 shard(s)", "MiB/s", "elapsed", "RSS"):
        assert fragment in line, line


#: Month and MedlineDate spellings the two implementations must agree on --
#: the parametrized cases above plus the edges an index-based SQL lookup can
#: get wrong (0, out of range, whitespace, non-numeric) and a non-ASCII spelling
#: (Python's `str.isalpha()` is Unicode-aware; RE2's `[A-Za-z]` would not be).
_MONTH_INPUTS = ["3", "03", " 3 ", "Mar", "March", "Sept", "SEPTEMBER", "sep",
                 "Spring", "Sep-Dec", "Winter", "Frühling",
                 "13", "0", "99999999999999999999", "", "  ", None]
_MEDLINE_INPUTS = ["1998 Spring", "1994 Sep-Dec", "1978 Jul-Aug", "1998 September",
                   "1998 Dec-1999 Jan", "1999-2000",
                   "  2001 Winter", "n.d.", "Spring 1998", "12345", "", None]


def test_pub_month_sql_matches_python(con):
    """The export builds pub_month in SQL; `validate` builds efetch's side with
    the Python function. If they disagree, every record the export normalizes
    reads as a mismatch against PubMed.

    The full cross product, not each input alone: the SQL falls through from the
    column to the MedlineDate, so the two sources interact.
    """
    from pubmed2db.export import _PUB_MONTH_SQL, pub_month

    for month in _MONTH_INPUTS:
        for medline in _MEDLINE_INPUTS:
            got = con.execute(
                f"SELECT {_PUB_MONTH_SQL} "
                "FROM (SELECT ? AS pub_month, ? AS medline_date) la",
                [month, medline],
            ).fetchone()[0]
            assert got == pub_month(month, medline), f"{month=!r} {medline=!r}"


def test_pub_year_sql_matches_year_from_medline_date(con):
    """Same contract for the MedlineDate year fallback."""
    from pubmed2db.export import _PUB_YEAR_SQL, _year_from_medline_date

    for raw in _MEDLINE_INPUTS:
        got = con.execute(
            f"SELECT {_PUB_YEAR_SQL} FROM (SELECT NULL AS pub_year, ? AS medline_date) la",
            [raw],
        ).fetchone()[0]
        assert got == _year_from_medline_date(raw), f"medline_date={raw!r}"


def test_json_export_writes_utf8_not_escapes(loaded_con, tmp_path):
    """Non-ASCII must reach the file as UTF-8, not as \\uXXXX escapes.

    The Python writer said so explicitly (`ensure_ascii=False`); DuckDB's JSON
    writer does it by default, which is a behavior nothing else here would
    notice changing.
    """
    from pubmed2db.export import export_json

    loaded_con.execute(
        "UPDATE article SET article_title = 'Étude sur les protéines — a test'"
        " WHERE pmid = 1003"
    )
    paths = export_json(loaded_con, tmp_path / "json")
    raw = b"".join(p.read_bytes() for p in paths)
    assert "Étude sur les protéines — a test".encode() in raw
    assert rb"\u00c9" not in raw.lower()   # not json.dumps(ensure_ascii=True)'s form
    assert _read_ndjson(paths)["PMID:1003"]["article_title"].startswith("Étude")
