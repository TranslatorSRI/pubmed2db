"""Tests for the `validate` command.

All network access goes through `validate._eutils`, which these tests
monkeypatch with canned einfo/efetch responses so nothing touches the wire.
"""

from __future__ import annotations

import json

import pytest

import pubmed2db.validate as validate

# Canned efetch articles keyed by PMID, mirroring the loaded fixtures' latest
# versions (1001 v2 and 1003). 1002 is deliberately absent — PubMed no longer
# serves it — so deletion confirmation sees it as gone.
_EFETCH = {
    1001: """
      <PubmedArticle><MedlineCitation><PMID>1001</PMID>
        <Article><Journal>
          <JournalIssue><Volume>581</Volume><Issue>7807</Issue>
            <PubDate><Year>2020</Year><Month>Mar</Month><Day>16</Day></PubDate>
          </JournalIssue>
          <Title>Nature</Title><ISOAbbreviation>Nature</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Revised title for article one.</ArticleTitle>
        <Abstract><AbstractText>The revised abstract for article one.</AbstractText></Abstract>
      </Article></MedlineCitation></PubmedArticle>""",
    1003: """
      <PubmedArticle><MedlineCitation><PMID>1003</PMID>
        <Article><Journal>
          <JournalIssue><Volume>3</Volume>
            <PubDate><MedlineDate>1998 Spring</MedlineDate></PubDate>
          </JournalIssue>
          <Title>Journal of Examples</Title>
          <ISOAbbreviation>J. Examples</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Article three with a MedlineDate range.</ArticleTitle>
      </Article></MedlineCitation></PubmedArticle>""",
}


def _fake_eutils_factory(articles, *, entrez_count=2):
    """Build a fake `_eutils` returning canned einfo/efetch bytes."""

    def fake(endpoint, params, *, api_key=None, email=None, **_):
        if endpoint == "einfo.fcgi":
            return json.dumps(
                {"einforesult": {"dbinfo": [{"count": str(entrez_count)}]}}
            ).encode()
        if endpoint == "efetch.fcgi":
            wanted = [int(x) for x in params["id"].split(",")]
            body = "".join(articles[p] for p in wanted if p in articles)
            return f"<PubmedArticleSet>{body}</PubmedArticleSet>".encode()
        raise AssertionError(f"unexpected endpoint {endpoint}")

    return fake


@pytest.fixture
def export_dir(loaded_con, tmp_path):
    """A real NDJSON export of the loaded fixtures (PMIDs 1001, 1003)."""
    from pubmed2db.export import export_json

    out = tmp_path / "export"
    export_json(loaded_con, out)
    return out


def test_offline_structure_passes(export_dir):
    report = validate.run_validation(export_dir, online=False)
    assert report["status"] == "pass", report["errors"]
    assert report["errors"] == []
    assert report["checks"]["structure"]["records_total"] == 2


def test_malformed_and_structural_errors(export_dir):
    shard = next(export_dir.glob("*.ndjson"))
    with shard.open("a") as handle:
        handle.write("{not json}\n")
        handle.write(json.dumps({"id": "PMID:1001"}) + "\n")  # duplicate + missing fields
        handle.write(json.dumps({**_valid_doc(), "id": "1099"}) + "\n")  # bad id
        handle.write(json.dumps({**_valid_doc(), "id": "PMID:1200", "volume": None}) + "\n")

    report = validate.run_validation(export_dir, online=False)
    codes = {e["code"] for e in report["errors"]}
    assert report["status"] == "fail"
    assert {"malformed_json", "missing_fields", "invalid_ids", "null_values", "duplicate_pmids"} <= codes


def _valid_doc():
    return {
        "id": "PMID:9",
        "journal_name": "", "journal_abbrev": "", "article_title": "",
        "volume": "", "issue": "", "pub_year": "", "pub_month": "",
        "pub_day": "", "abstract": "",
    }


def test_coverage_uses_entrez_and_db(export_dir, loaded_con, monkeypatch):
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    cov = report["checks"]["coverage"]
    assert cov["entrez_total"] == 2
    assert cov["pct_of_entrez"] == pytest.approx(1.0)
    assert cov["db_latest_count"] == 2
    assert cov["pct_of_db"] == pytest.approx(1.0)


def test_field_validation_matches(export_dir, loaded_con, monkeypatch):
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    fv = report["checks"]["field_validation"]
    assert fv["checked"] == 2
    assert fv["mismatches"] == []
    assert fv["soft_mismatches"] == []
    assert report["status"] == "pass", report["errors"] + report["warnings"]


def test_field_validation_flags_mismatch(export_dir, loaded_con, monkeypatch):
    tampered = dict(_EFETCH)
    tampered[1001] = _EFETCH[1001].replace(
        "Revised title for article one.", "A completely different title."
    )
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(tampered))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    mismatched = report["checks"]["field_validation"]["mismatches"]
    assert any(m["field"] == "article_title" and m["pmid"] == 1001 for m in mismatched)


def test_field_validation_flags_missing_from_api(export_dir, loaded_con, monkeypatch):
    only_1001 = {1001: _EFETCH[1001]}  # 1003 no longer served by PubMed
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(only_1001))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    assert 1003 in report["checks"]["field_validation"]["missing_from_api"]
    assert any(e["code"] == "sampled_pmid_absent" for e in report["errors"])


def test_deletion_check_confirms_dropped(export_dir, loaded_con, monkeypatch):
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    deletions = report["checks"]["deletions"]
    # PMID 1002 was deleted in the update file and PubMed no longer serves it.
    assert deletions["source"] == "database"
    assert 1002 in deletions["confirmed_deleted"]
    assert deletions["present_in_export"] == []


def test_deletions_skipped_without_db(export_dir):
    report = validate.run_validation(export_dir, online=False)
    assert report["checks"]["deletions"]["source"] is None
    assert any("deletions" in s for s in report["skipped_checks"])


def test_cli_validate_offline(export_dir):
    from click.testing import CliRunner

    from pubmed2db.cli import main

    no_db = str(export_dir.parent / "absent.duckdb")
    result = CliRunner().invoke(main, ["--db", no_db, "validate", str(export_dir), "--offline"])
    assert result.exit_code == 0, result.output
    assert "Validation PASS" in result.output
    assert (export_dir / "validation_report.json").exists()


def test_cli_validate_fails_on_error(export_dir):
    from click.testing import CliRunner

    from pubmed2db.cli import main

    shard = next(export_dir.glob("*.ndjson"))
    with shard.open("a") as handle:
        handle.write("{broken\n")

    no_db = str(export_dir.parent / "absent.duckdb")
    result = CliRunner().invoke(main, ["--db", no_db, "validate", str(export_dir), "--offline"])
    assert result.exit_code == 1
    assert "Validation FAIL" in result.output


def test_expected_fields_matches_spec():
    """EXPECTED_FIELDS derives from the exporter; lock it to the shipped spec.

    The 10 DocumentMetadataAPI field names are an external contract (Node
    Annotator / ElasticSearch consume them), so changing the export shape should
    trip a test rather than silently re-define what validate accepts.
    """
    assert set(validate.EXPECTED_FIELDS) == {
        "id",
        "journal_name",
        "journal_abbrev",
        "article_title",
        "volume",
        "issue",
        "pub_year",
        "pub_month",
        "pub_day",
        "abstract",
    }
