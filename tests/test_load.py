"""Tests for loading, version history, and latest-version selection."""

from __future__ import annotations


def test_full_history_retained(loaded_con):
    # PMID 1001 exists in both the baseline and the update file.
    rows = loaded_con.execute(
        "SELECT source_file FROM article WHERE pmid = 1001 ORDER BY file_order_key"
    ).fetchall()
    assert [r[0] for r in rows] == ["pubmed25n0001.xml.gz", "pubmed25n0002.xml.gz"]


def test_latest_article_picks_newest_version(loaded_con):
    row = loaded_con.execute(
        "SELECT source_file, article_title, pub_day FROM latest_article WHERE pmid = 1001"
    ).fetchone()
    assert row == ("pubmed25n0002.xml.gz", "Revised title for article one.", "16")


def test_deleted_pmid_excluded_from_latest(loaded_con):
    # PMID 1002 was deleted in the update file -> absent from latest_article,
    # but its history row remains in the base table.
    assert (
        loaded_con.execute("SELECT count(*) FROM latest_article WHERE pmid = 1002").fetchone()[0]
        == 0
    )
    assert loaded_con.execute("SELECT count(*) FROM article WHERE pmid = 1002").fetchone()[0] == 1


def test_reload_is_idempotent(con, gz_fixture):
    from pubmed2db.load import load_file

    load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")
    first = con.execute("SELECT count(*) FROM article").fetchone()[0]
    load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")
    second = con.execute("SELECT count(*) FROM article").fetchone()[0]
    assert first == second == 3


def test_needs_load_and_md5_change(con, gz_fixture):
    from pubmed2db.load import load_files, needs_load

    files = [(gz_fixture("pubmed25n0001"), "baseline")]
    assert load_files(con, files) == (1, [])
    # Already processed and not re-downloaded -> skipped.
    assert needs_load(con, "pubmed25n0001.xml.gz") is False
    assert load_files(con, files) == (0, [])

    # Simulate a re-download with a changed checksum (downloaded_at > processed_at).
    con.execute(
        "UPDATE source_file SET downloaded_at = processed_at + INTERVAL 1 HOUR, "
        "published_md5 = 'changed' WHERE file_name = 'pubmed25n0001.xml.gz'"
    )
    assert needs_load(con, "pubmed25n0001.xml.gz") is True
    assert load_files(con, files) == (1, [])
    # Reload replaced rows rather than duplicating them.
    assert con.execute("SELECT count(*) FROM article").fetchone()[0] == 3


def test_child_tables_populated(loaded_con):
    """Cover the per-version tables the original fixtures never exercised."""
    baseline = "pubmed25n0001.xml.gz"

    assert loaded_con.execute(
        "SELECT grant_id, acronym, agency, country FROM article_grant WHERE pmid = 1001"
    ).fetchall() == [("R01 GM123456", "GM", "NIGMS NIH HHS", "United States")]

    assert loaded_con.execute(
        "SELECT descriptor_ui, qualifier_ui, qualifier_name, major_topic "
        "FROM mesh_qualifier WHERE pmid = 1001"
    ).fetchall() == [("D006801", "Q000235", "genetics", False)]

    # A CollectiveName author is stored as kind='collective' with no orcid/valid.
    assert loaded_con.execute(
        "SELECT kind, name, orcid, valid FROM author "
        "WHERE pmid = 1001 AND source_file = ? ORDER BY position",
        [baseline],
    ).fetchall() == [
        ("author", "Jane Smith", "0000-0002-1825-0097", True),
        ("collective", "The Example Study Group", None, None),
    ]

    # Affiliations attach to the named author only, never the collective.
    assert loaded_con.execute(
        "SELECT author_position, position, affiliation FROM author_affiliation "
        "WHERE pmid = 1001 AND source_file = ?",
        [baseline],
    ).fetchall() == [(0, 0, "Department of Testing, Example University.")]


def test_no_reference_citation_table(con):
    """The citation graph is deliberately not stored -- see parse._cited_pmids."""
    assert (
        con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'reference_citation'"
        ).fetchone()[0]
        == 0
    )


def test_bad_file_is_skipped_not_fatal(con, gz_fixture, tmp_path):
    """One corrupt file must not abort a run over the other ~1,300."""
    from pubmed2db.load import load_files, needs_load

    good = gz_fixture("pubmed25n0001")
    bad = tmp_path / "pubmed25n0003.xml.gz"
    bad.write_bytes(b"not gzipped XML at all")

    loaded, failed = load_files(con, [(bad, "update"), (good, "baseline")])

    assert loaded == 1
    assert failed == ["pubmed25n0003.xml.gz"]
    # The good file landed...
    assert con.execute("SELECT count(*) FROM article").fetchone()[0] == 3
    # ...and the bad one left no rows, no watermark, so a later run retries it.
    assert (
        con.execute(
            "SELECT count(*) FROM article WHERE source_file = 'pubmed25n0003.xml.gz'"
        ).fetchone()[0]
        == 0
    )
    assert needs_load(con, "pubmed25n0003.xml.gz") is True


def test_unparseable_filename_is_ignored_not_fatal(con, gz_fixture, tmp_path):
    """A stray file in the download directory must not abort the whole run."""
    from pubmed2db.load import load_files

    stray = tmp_path / "notes.xml.gz"
    stray.write_bytes(b"")

    assert load_files(con, [(stray, "update"), (gz_fixture("pubmed25n0001"), "baseline")]) == (
        1,
        [],
    )
    assert con.execute("SELECT count(*) FROM article").fetchone()[0] == 3


def test_load_stamps_downloaded_at(con, gz_fixture):
    """Files obtained outside `download` (rsync/FTP) still get a downloaded_at,
    which the status arithmetic and needs_load both depend on."""
    from pubmed2db.load import load_file, needs_load
    from pubmed2db.status import summarize

    load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")

    assert summarize(con)["downloaded_files"] == 1
    assert needs_load(con, "pubmed25n0001.xml.gz") is False


def test_load_does_not_overwrite_a_real_downloaded_at(con, gz_fixture):
    """A re-download pending a load stays pending after an unrelated reload."""
    from pubmed2db.db import register_source_file
    from pubmed2db.load import load_file

    path = gz_fixture("pubmed25n0001")
    load_file(con, path, kind="baseline")
    register_source_file(con, "pubmed25n0001.xml.gz", kind="baseline", published_md5="changed")
    before = con.execute(
        "SELECT downloaded_at FROM source_file WHERE file_name = 'pubmed25n0001.xml.gz'"
    ).fetchone()[0]

    load_file(con, path, kind="baseline")
    after = con.execute(
        "SELECT downloaded_at FROM source_file WHERE file_name = 'pubmed25n0001.xml.gz'"
    ).fetchone()[0]
    assert after == before


def test_source_file_registry_counts(loaded_con):
    rows = loaded_con.execute(
        "SELECT file_name, kind, n_articles, n_deletions FROM source_file ORDER BY file_order_key"
    ).fetchall()
    assert rows == [
        ("pubmed25n0001.xml.gz", "baseline", 3, 0),
        ("pubmed25n0002.xml.gz", "update", 1, 1),
    ]


def test_status_latest_count_matches_the_view(loaded_con):
    """`status`'s latest-document count is the `latest_article` view; pinned so
    a future "cheaper" rewrite of it has to keep agreeing with the view --
    including on the fixtures' revised and deleted PMIDs."""
    from pubmed2db.status import summarize

    expected = loaded_con.execute("SELECT count(*) FROM latest_article").fetchone()[0]
    assert summarize(loaded_con)["latest_documents"] == expected


def test_registry_records_the_skip_counts(con, gz_fixture, tmp_path):
    """Skipped records never become rows, so no downstream count is short --
    the registry is the only place they can be queried from."""
    import gzip

    from pubmed2db.load import load_file

    src = gz_fixture("pubmed25n0001")
    with gzip.open(src, "rt", encoding="utf-8") as handle:
        xml = handle.read()
    book = (
        "<PubmedBookArticle><BookDocument><PMID Version='1'>9001</PMID>"
        "<ArticleTitle>A chapter</ArticleTitle></BookDocument></PubmedBookArticle>"
    )
    path = tmp_path / "pubmed25n0007.xml.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(xml.replace("</PubmedArticleSet>", book + "</PubmedArticleSet>"))

    load_file(con, path, kind="baseline")

    assert con.execute(
        "SELECT n_failed, n_book_records FROM source_file WHERE file_name = ?",
        [path.name],
    ).fetchone() == (0, 1)

    from pubmed2db.status import summarize

    assert summarize(con)["skipped_records"] == 1


def test_publication_types_are_stored_with_names_and_order(loaded_con):
    """Direct DuckDB queries are a first-class consumer: the name and PubMed's
    order are columns, so filtering for "Review" needs no MeSH lookup."""
    rows = loaded_con.execute(
        "SELECT pt.position, pt.type_ui, pt.type_name FROM latest_article la"
        " JOIN publication_type pt USING (pmid, source_file)"
        " WHERE la.pmid = 1001 ORDER BY pt.position"
    ).fetchall()
    assert rows == [
        (0, "D016422", "Letter"),
        (1, "D013485", "Research Support, Non-U.S. Gov't"),
        (2, "D016420", "Comment"),
    ]


def test_load_progress_line_reports_rate_and_elapsed(con, gz_fixture, caplog):
    """The progress line must carry what sizes the next srun, not just an ETA.

    Also pins the last line ending in a bare "done": eta_str returns the literal
    string "done" when nothing remains, which read as "~done to go" before the
    caller special-cased it.
    """
    import logging

    from pubmed2db.load import load_files

    files = [(gz_fixture("pubmed25n0001"), "baseline"), (gz_fixture("pubmed25n0002"), "update")]
    with caplog.at_level(logging.INFO, logger="pubmed2db.load"):
        load_files(con, files)

    progress = [r.getMessage() for r in caplog.records if "progress:" in r.getMessage()]
    assert len(progress) == 2
    for fragment in ("files this run", "remaining", "s/file", "elapsed"):
        assert fragment in progress[0], progress[0]
    assert progress[0].endswith("to go")
    assert progress[-1].endswith("done") and "~done" not in progress[-1]


def test_load_logs_current_rss_alongside_the_peak(con, gz_fixture, caplog):
    """Both figures are logged: the high-water peak, and RSS as it stands now."""
    import logging

    from pubmed2db.load import load_file

    with caplog.at_level(logging.INFO, logger="pubmed2db.load"):
        load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")

    line = next(m for m in (r.getMessage() for r in caplog.records) if "loaded " in m)
    assert "RSS " in line and "peak " in line, line


def test_corpus_provenance_names_the_cutoff(loaded_con):
    """The cutoff is the last *loaded* file, and when NCBI posted it (#57)."""
    from pubmed2db.status import corpus_provenance

    loaded_con.execute(
        "UPDATE source_file SET published_at = TIMESTAMPTZ '2025-06-01 14:05:00+00' "
        "WHERE file_name = 'pubmed25n0002.xml.gz'"
    )
    # Downloaded but not loaded: not part of this build, so not its cutoff.
    from pubmed2db.db import register_source_file

    register_source_file(loaded_con, "pubmed25n0003.xml.gz", kind="update")

    provenance = corpus_provenance(loaded_con)
    assert provenance == {
        "build_name": "2025jun01",
        "baseline_year": 2025,
        "first_file": "pubmed25n0001.xml.gz",
        "cutoff_file": "pubmed25n0002.xml.gz",
        "cutoff_published_at": "2025-06-01T14:05:00+00:00",
        "files_loaded": 2,
        "article_versions_loaded": 4,
        "deletions_loaded": 1,
        "records_failed": 0,
        "book_records_skipped": 0,
    }


def test_corpus_provenance_has_no_date_when_the_cutoff_has_none(loaded_con):
    """A cutoff file with no posting time (copied in by rsync) names no build,
    rather than borrowing an earlier file's date: `arg_max` skips NULLs."""
    from pubmed2db.status import corpus_provenance

    loaded_con.execute(
        "UPDATE source_file SET published_at = TIMESTAMPTZ '2025-06-01 14:05:00+00' "
        "WHERE file_name = 'pubmed25n0001.xml.gz'"
    )
    provenance = corpus_provenance(loaded_con)
    assert provenance["cutoff_file"] == "pubmed25n0002.xml.gz"
    assert provenance["cutoff_published_at"] is None
    assert provenance["build_name"] is None


def test_build_name_is_the_utc_posting_date_of_the_cutoff():
    from datetime import datetime, timedelta, timezone

    from pubmed2db.status import build_name

    # pubmed26n1636 was posted at 14:05 US Eastern: the 24th, in UTC too.
    eastern = timezone(timedelta(hours=-4))
    assert build_name(datetime(2026, 9, 24, 14, 5, tzinfo=eastern)) == "2026sep24"
    # Late enough in the US evening to be the next day in UTC: UTC decides.
    assert build_name(datetime(2026, 9, 24, 21, 0, tzinfo=eastern)) == "2026sep25"
    assert build_name(None) is None


def test_load_logs_phase_timings(con, gz_fixture, caplog):
    """Each phase is timed on its own, because the per-file total grows with
    the database (#68) and the total cannot say which phase is growing."""
    import logging
    import time

    from pubmed2db.load import _VERSIONED_TABLES, LoadTiming, load_file, load_parsed
    from pubmed2db.parse import parse_file

    with caplog.at_level(logging.DEBUG, logger="pubmed2db.load"):
        load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")

    messages = [r.getMessage() for r in caplog.records]
    loaded = next(m for m in messages if m.startswith("loaded "))
    for phase in ("parse ", "build ", "delete ", "insert ", "commit "):
        assert phase in loaded, loaded
    timing = next(m for m in messages if m.startswith("timing "))
    for phase in ("delete ", "build ", "insert ", "registry ", "commit "):
        assert phase in timing, timing

    # A caller-supplied record is filled in, phase by phase, and the phases are
    # the whole load: what falls in none of them would be invisible in the logs.
    record = LoadTiming()
    parsed = parse_file(gz_fixture("pubmed25n0001"))
    started = time.perf_counter()
    load_parsed(con, parsed, "pubmed25n0001.xml.gz", kind="baseline", timing=record)
    wall = time.perf_counter() - started
    for phase in ("delete", "build", "registry", "commit"):
        assert getattr(record, phase) > 0, phase
    stored = {
        table: con.execute(
            f"SELECT count(*) FROM {table} WHERE source_file = 'pubmed25n0001.xml.gz'"
        ).fetchone()[0]
        for table in _VERSIONED_TABLES
    }
    assert set(record.inserts) == {table for table, n in stored.items() if n}
    assert all(seconds > 0 for seconds in record.inserts.values())
    assert record.n_rows == sum(stored.values())
    assert 0.8 * wall <= record.total <= wall
