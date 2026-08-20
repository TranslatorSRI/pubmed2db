"""Tests for JSON and Parquet export."""

from __future__ import annotations

import gzip
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


def _open_shard(path):
    """Open a shard whichever way it was written -- gzip is the default now."""
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


def _read_ndjson(paths):
    docs = []
    for path in paths:
        with _open_shard(path) as handle:
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
        # so uppercase "PMCID:" precedes lowercase "doi:", with the PMID first.
        "identifiers": ["PMID:1001", "PMCID:PMC7654321", "doi:10.1038/example1001"],
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


def test_curie_sql_is_derived_from_id_prefixes():
    """The exporter's CURIEs come from ID_PREFIXES, not a second hand-written list.

    `validate` rebuilds the CURIEs it expects from the same mapping, so a
    re-hardcoded `CASE`/`IN` in the SQL would let a new id type or a casing fix
    reach the validator alone — reporting every sampled record as a mismatch
    against a correct export.
    """
    from pubmed2db.export import ID_PREFIXES, _LATEST_METADATA_SQL

    for id_type, prefix in ID_PREFIXES.items():
        assert f"'{id_type}'" in _LATEST_METADATA_SQL
        assert f"'{prefix}:'" in _LATEST_METADATA_SQL


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
    # Written uncompressed on purpose: a run that flips --gzip must clear the
    # other form too, or both are left for `validate` to read as one export.
    stale = out / "pubmed_metadata_99.ndjson"
    stale.write_text(json.dumps({"id": "PMID:404"}) + "\n")

    paths = export_json(loaded_con, out)
    assert not stale.exists()
    assert _read_ndjson(paths).keys() == {"PMID:1001", "PMID:1003"}


def test_reexport_removes_stale_shards(loaded_con, tmp_path):
    """Shards from an earlier, wider (or gzipped) export must not survive: a
    consumer globbing the directory would read two exports at once."""
    from pubmed2db.export import export_json

    out = tmp_path / "json"
    export_json(loaded_con, out, shards=2)
    export_json(loaded_con, out, shards=1, gzip_output=True)

    # The COPY writer names shards from FILENAME_PATTERN '{i}', so the exact
    # stem is DuckDB's business -- what matters is that exactly one shard is
    # left and it is the gzipped one this run wrote.
    survivors = sorted(path.name for path in out.iterdir())
    assert len(survivors) == 1, survivors
    assert survivors[0].endswith(".ndjson.gz")


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


def test_placeholder_looking_fields_survive_to_the_export(con, gz_fixture, tmp_path):
    """A validation run flagged two records as exporting blank where Entrez has a
    value: a non-numeric `<Issue>Suppl</Issue>` and an `<ArticleTitle>` of the
    literal "[Not Available].". Neither is dropped anywhere in the pipeline."""
    import json

    from pubmed2db.export import export_json
    from pubmed2db.load import load_file

    load_file(con, gz_fixture("pubmed25n0004"), kind="baseline")
    out = tmp_path / "json"
    export_json(con, out, shards=1)

    docs = _read_ndjson(sorted(out.glob("pubmed_metadata_*")))
    assert docs["PMID:10137601"]["issue"] == "Suppl"
    assert docs["PMID:10137601"]["volume"] == "3 Suppl"
    assert docs["PMID:28972331"]["article_title"] == "[Not Available]."


def test_parquet_export_removes_a_dropped_table_s_file(loaded_con, tmp_path):
    """A re-export overwrites its own file names, so a table removed from the
    schema would otherwise leave its Parquet behind for consumers to glob."""
    from pubmed2db.export import export_parquet

    out = tmp_path / "parquet"
    out.mkdir()
    orphan = out / "reference_citation.parquet"  # dropped from schema.sql
    orphan.write_bytes(b"stale")

    written = export_parquet(loaded_con, out)

    assert not orphan.exists()
    assert set(out.glob("*.parquet")) == set(written)


def test_parquet_exports_every_table(loaded_con, tmp_path):
    """One file per table, `pipeline_run` included -- it carries the journal
    refresh provenance, which nothing else in the export records."""
    from pubmed2db.export import export_parquet

    out = tmp_path / "parquet"
    written = export_parquet(loaded_con, out)

    tables = {
        row[0]
        for row in loaded_con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE schema_name = 'main' AND NOT temporary"
        ).fetchall()
    }
    assert {path.stem for path in written} == tables


def test_export_progress_line_renders(loaded_con, tmp_path, monkeypatch, caplog):
    """The JSON export's progress branch only fires after 10 s of real work.

    That means it never executes in the suite, so a mismatched %-format arg there
    would first surface partway through a 20-minute production run. Force the
    interval to zero and assert the line actually renders.
    """
    import logging
    import time

    from pubmed2db import export as ex

    out = tmp_path / "json"
    out.mkdir()
    (out / "pubmed_metadata_0.ndjson").write_bytes(b"x" * 4096)
    with caplog.at_level(logging.INFO, logger="pubmed2db.export"):
        ex._log_writing_progress(out, time.monotonic() - 5)
        ex.export_json(loaded_con, tmp_path / "json")

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("writing:"))
    for fragment in ("GiB across 1 shard(s)", "MiB/s", "elapsed", "RSS"):
        assert fragment in line, line


#: Month and MedlineDate spellings the two implementations must agree on --
#: the parametrized cases above plus the edges an index-based SQL lookup can
#: get wrong (0, out of range, whitespace, non-numeric) and a non-ASCII spelling
#: (Python's `str.isalpha()` is Unicode-aware; RE2's `[A-Za-z]` would not be).
#:
#: The tab/newline cases are not decoration either: SQL `trim()` strips spaces
#: only while Python's `.strip()` strips all whitespace, so " 3 " alone passed
#: while "\tMar" gave "Mar" from the function and "" from the export. `parse`
#: stores `findtext("Month")` verbatim, so a `<Month>` spanning a line does
#: reach here.
_MONTH_INPUTS = ["3", "03", " 3 ", "Mar", "March", "Sept", "SEPTEMBER", "sep",
                 "Spring", "Sep-Dec", "Winter", "Frühling",
                 "13", "0", "99999999999999999999", "", "  ", None,
                 "\tMar", "3\n", "\n3", "\r\nMarch\t", "\t\n", "\tSep-Dec",
                 "²", "١٢", "3²"]
#: The non-ASCII and vertical-tab separators are the cases the pinning test
#: could not previously reach: every entry used a plain space, so it could not
#: see that RE2's `\s` is `[\t\n\f\r ]` while Python's includes `\v` and
#: Unicode spaces. The `"²"`/`"١٢"` month entries above are the matching pair —
#: `isdigit()` admits both, `int()` parses only one, and SQL's `[0-9]+` neither.
_MEDLINE_INPUTS = ["1998 Spring", "1994 Sep-Dec", "1978 Jul-Aug", "1998 September",
                   "1998 Dec-1999 Jan", "1999-2000",
                   "1998\xa0Spring", "1998\x0bSpring", "1998\u2003Sep-Dec",
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
    raw = b"".join(gzip.decompress(p.read_bytes()) for p in paths)
    assert "Étude sur les protéines — a test".encode() in raw
    assert rb"\u00c9" not in raw.lower()   # not json.dumps(ensure_ascii=True)'s form
    assert _read_ndjson(paths)["PMID:1003"]["article_title"].startswith("Étude")


def test_export_duration_covers_the_snapshot_not_just_the_copy(loaded_con, tmp_path, caplog):
    """slurm/README.md tells operators to size --time from this line.

    Materializing `_latest_snapshot` is a window function over the whole
    `article` table -- the reason export memory scales with the database -- and
    with the sort gone it is plausibly the dominant phase. A clock started after
    it understates the job it is used to size.

    Pinned by ordering rather than by timing: the clock must be read before the
    snapshot statement runs, which a wall-clock assertion could only show
    flakily on a fixture this small.
    """
    import logging

    from pubmed2db import export as ex

    events: list[str] = []
    real_clock, real_execute = ex.time.monotonic, loaded_con.execute

    def watched_clock():
        events.append("clock")
        return real_clock()

    # DuckDB's `execute` is read-only on the connection, so watch it from a
    # proxy rather than by patching the object.
    class Watched:
        def __getattr__(self, name):
            return getattr(loaded_con, name)

        def execute(self, sql, *args, **kwargs):
            if "_latest_snapshot AS SELECT" in sql:
                events.append("snapshot")
            return real_execute(sql, *args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(ex.time, "monotonic", watched_clock)
    try:
        with caplog.at_level(logging.INFO, logger="pubmed2db.export"):
            ex.export_json(Watched(), tmp_path / "json")
    finally:
        monkey.undo()

    assert "snapshot" in events, events
    assert events.index("clock") < events.index("snapshot"), events
    assert any(r.getMessage().startswith("exported ") for r in caplog.records)


def test_the_heartbeat_survives_a_shard_vanishing(tmp_path):
    """DuckDB writes this directory while the heartbeat reads it, so a shard can
    disappear between the glob and the stat. Taking the thread down there would
    leave a 20-minute export silent -- the failure it exists to prevent."""
    from pubmed2db import export as ex

    out = tmp_path / "json"
    out.mkdir()
    present = out / "pubmed_metadata_0.ndjson"
    present.write_bytes(b"x" * 2048)
    ghost = out / "pubmed_metadata_1.ndjson"
    ghost.write_bytes(b"y" * 2048)

    real_glob = ex._shard_paths

    def racing(directory):
        paths = real_glob(directory)
        ghost.unlink()          # vanishes between the glob and the stat
        return paths

    ex._shard_paths = racing
    try:
        assert ex._shard_bytes(ex._shard_paths(out)) == 2048
    finally:
        ex._shard_paths = real_glob


def test_the_heartbeat_thread_never_dies_on_an_error(tmp_path, caplog, monkeypatch):
    """Any escape kills the thread and silences the run, so the loop swallows."""
    import logging
    import threading

    from pubmed2db import export as ex

    def boom(*_args):
        raise RuntimeError("stat storm")

    monkeypatch.setattr(ex, "_log_writing_progress", boom)
    monkeypatch.setattr(ex, "_PROGRESS_INTERVAL_S", 0.01)

    stop = threading.Event()
    with caplog.at_level(logging.WARNING, logger="pubmed2db.export"):
        thread = threading.Thread(target=ex._log_while_writing, args=(tmp_path, stop, 0.0))
        thread.start()
        threading.Event().wait(0.05)
        still_running = thread.is_alive()
        stop.set()
        thread.join(timeout=2)

    assert still_running, "the heartbeat died on the first error"
    assert not thread.is_alive()
    assert any("could not log export progress" in r.getMessage() for r in caplog.records)
