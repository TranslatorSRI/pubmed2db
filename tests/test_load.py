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
    assert loaded_con.execute(
        "SELECT count(*) FROM latest_article WHERE pmid = 1002"
    ).fetchone()[0] == 0
    assert loaded_con.execute(
        "SELECT count(*) FROM article WHERE pmid = 1002"
    ).fetchone()[0] == 1


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
        "SELECT grant_id, acronym, agency, country FROM grant_ WHERE pmid = 1001"
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
        "SELECT position, affiliation FROM author_affiliation WHERE pmid = 1001 "
        "AND source_file = ?",
        [baseline],
    ).fetchall() == [(1, "Department of Testing, Example University.")]


def test_no_reference_citation_table(con):
    """The citation graph is deliberately not stored -- see parse._cited_pmids."""
    assert con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name = 'reference_citation'"
    ).fetchone()[0] == 0


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
    assert con.execute(
        "SELECT count(*) FROM article WHERE source_file = 'pubmed25n0003.xml.gz'"
    ).fetchone()[0] == 0
    assert needs_load(con, "pubmed25n0003.xml.gz") is True


def test_unparseable_filename_is_ignored_not_fatal(con, gz_fixture, tmp_path):
    """A stray file in the download directory must not abort the whole run."""
    from pubmed2db.load import load_files

    stray = tmp_path / "notes.xml.gz"
    stray.write_bytes(b"")

    assert load_files(con, [(stray, "update"), (gz_fixture("pubmed25n0001"), "baseline")]) == (1, [])
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


def test_schema_migrates_a_database_without_the_skip_columns(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` leaves an existing database at its old shape,
    so the columns added after the first release need their own migration."""
    import duckdb

    from pubmed2db.db import connect, init_schema

    db_path = tmp_path / "old.duckdb"
    old = duckdb.connect(db_path)
    old.execute(
        """
        CREATE TABLE source_file (
            file_name TEXT PRIMARY KEY, kind TEXT NOT NULL, year_yy INTEGER NOT NULL,
            file_number INTEGER NOT NULL, file_order_key BIGINT NOT NULL,
            published_md5 TEXT, downloaded_at TIMESTAMP, processed_at TIMESTAMP,
            n_articles INTEGER, n_deletions INTEGER
        )
        """
    )
    old.execute(
        "INSERT INTO source_file VALUES "
        "('pubmed25n0001.xml.gz', 'baseline', 25, 1, 25000001, NULL, NULL, NULL, NULL, NULL)"
    )
    old.close()

    con = connect(db_path)
    try:
        init_schema(con)  # idempotent: the migration must survive a second run
        assert con.execute(
            "SELECT n_failed, n_book_records FROM source_file"
        ).fetchall() == [(None, None)]
    finally:
        con.close()
