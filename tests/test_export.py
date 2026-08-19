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

    docs = {
        json.loads(line)["id"]: json.loads(line)
        for line in (out / "pubmed_metadata_00000.ndjson").read_text().splitlines()
    }
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
