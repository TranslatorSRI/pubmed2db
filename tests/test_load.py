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

    # Citations land as BIGINT, so they join straight back to article.pmid.
    assert loaded_con.execute(
        "SELECT cited_pmid FROM reference_citation WHERE pmid = 1001 AND source_file = ?",
        [baseline],
    ).fetchall() == [(9001,)]

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


def test_source_file_registry_counts(loaded_con):
    rows = loaded_con.execute(
        "SELECT file_name, kind, n_articles, n_deletions FROM source_file ORDER BY file_order_key"
    ).fetchall()
    assert rows == [
        ("pubmed25n0001.xml.gz", "baseline", 3, 0),
        ("pubmed25n0002.xml.gz", "update", 1, 1),
    ]
