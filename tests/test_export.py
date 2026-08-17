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
        ("Spring", ""),
        ("13", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_month_to_abbrev(raw, expected):
    from pubmed2db.export import month_to_abbrev

    assert month_to_abbrev(raw) == expected


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
    # MedlineDate-only article ("1998 Spring"): the year is recovered from the
    # free-text date, but month/day stay empty -- a season has no single month.
    assert three["pub_year"] == "1998"
    assert three["pub_month"] == ""
    assert three["pub_day"] == ""
    assert three["issue"] == ""
    assert three["abstract"] == ""
    assert all(value is not None for value in three.values())


def test_json_sharding(loaded_con, tmp_path):
    from pubmed2db.export import export_json

    paths = export_json(loaded_con, tmp_path / "json", shards=2)
    assert len(paths) == 2
    docs = _read_ndjson(paths)
    assert set(docs) == {"PMID:1001", "PMID:1003"}


def test_reexport_removes_stale_shards(loaded_con, tmp_path):
    """Shards from an earlier, wider (or gzipped) export must not survive: a
    consumer globbing the directory would read two exports at once."""
    from pubmed2db.export import export_json

    out = tmp_path / "json"
    export_json(loaded_con, out, shards=2)
    export_json(loaded_con, out, shards=1, gzip_output=True)

    assert sorted(p.name for p in out.iterdir()) == ["pubmed_metadata_00000.ndjson.gz"]


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
