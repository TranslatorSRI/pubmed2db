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
      </Article></MedlineCitation>
      <PubmedData><ArticleIdList>
        <ArticleId IdType="pubmed">1001</ArticleId>
        <ArticleId IdType="doi">10.1038/example1001</ArticleId>
        <ArticleId IdType="pmc">PMC7654321</ArticleId>
        <ArticleId IdType="pii">S0140-6736(20)30183-5</ArticleId>
      </ArticleIdList></PubmedData></PubmedArticle>""",
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
      </Article></MedlineCitation>
      <PubmedData><ArticleIdList>
        <ArticleId IdType="pubmed">1003</ArticleId>
      </ArticleIdList></PubmedData></PubmedArticle>""",
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
        "identifiers": ["PMID:9"],
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


def test_coverage_warns_outside_default_band(export_dir, loaded_con, monkeypatch):
    """A short export must trip the ±5% band, not slip through it."""
    # 2 exported against an Entrez total of 4 is 50% — inside the old [0.1, 1.5]
    # band, outside the calibrated default one.
    monkeypatch.setattr(
        validate, "_eutils", _fake_eutils_factory(_EFETCH, entrez_count=4)
    )
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    assert report["checks"]["coverage"]["pct_of_entrez"] == pytest.approx(0.5)
    assert any(w["code"] == "coverage_out_of_band" for w in report["warnings"]), (
        report["warnings"]
    )


def test_field_validation_matches(export_dir, loaded_con, monkeypatch):
    """Also guards that identifiers compare as a *set*: the export sorts them
    (`PMC:` before `doi:`) while `_EFETCH` lists them in document order
    (doi first), so an order-sensitive comparison fails here."""
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


def test_field_validation_flags_identifier_mismatch(export_dir, loaded_con, monkeypatch):
    """A DOI that disagrees with Entrez is caught like any other core field."""
    tampered = dict(_EFETCH)
    tampered[1001] = _EFETCH[1001].replace("10.1038/example1001", "10.1038/somethingelse")
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(tampered))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    mismatched = report["checks"]["field_validation"]["mismatches"]
    entry = next(m for m in mismatched if m["field"] == "identifiers" and m["pmid"] == 1001)
    assert "doi:10.1038/somethingelse" in entry["entrez"]
    assert "doi:10.1038/example1001" in entry["exported"]


def test_efetch_identifiers_exclude_cited_references(monkeypatch):
    """validate reads the article's own ArticleIdList, not its references'.

    The mirror of `test_article_ids_exclude_cited_references` on the parse side — if
    validate over-matched the way upstream's parser did, every sampled record
    with references would be reported as a mismatch against a correct export.
    """
    with_refs = _EFETCH[1001].replace(
        "</ArticleIdList></PubmedData>",
        "</ArticleIdList>"
        "<ReferenceList><Reference><ArticleIdList>"
        '<ArticleId IdType="doi">10.9999/not-our-doi</ArticleId>'
        "</ArticleIdList></Reference></ReferenceList></PubmedData>",
    )
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory({1001: with_refs}))
    docs = validate.efetch_documents([1001], api_key=None, email="me@example.com")
    assert docs[1001]["identifiers"] == [
        "PMID:1001", "doi:10.1038/example1001", "PMC:PMC7654321",
    ]


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

    The 11 exported field names are an external contract (Node Annotator /
    ElasticSearch consume them), so changing the export shape should trip a test
    rather than silently re-define what validate accepts. Nine of them are the
    DocumentMetadataAPI spec's; `id` and `identifiers` are our extensions.
    """
    assert set(validate.EXPECTED_FIELDS) == {
        "id",
        "identifiers",
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


def test_manifest_round_trips(export_dir, tmp_path):
    """write_manifest/read_manifest agree, and the file is sorted and gzipped."""
    import gzip

    path = tmp_path / "pmids.txt.gz"
    validate.write_manifest({1003, 1001, 1002}, path)

    lines = gzip.open(path, "rt").read().split()
    assert lines == ["1001", "1002", "1003"]  # sorted, one per line
    assert validate.read_manifest(path) == {1001, 1002, 1003}


def test_drops_explained_by_recorded_deletion(export_dir, loaded_con, tmp_path):
    """A dropped PMID the DB marks deleted is expected, not an error."""
    # The export holds 1001 and 1003; 1002 was deleted by the update file.
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1002, 1003}, manifest)

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, online=False
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["dropped"] == 1
    assert drops["explained_by_deletion"] == [1002]
    assert drops["unexplained"] == []
    assert not [e for e in report["errors"] if e["code"] == "unexplained_drops"]


def test_unexplained_drop_is_an_error(export_dir, loaded_con, tmp_path):
    """A PMID that vanished without a recorded deletion fails the run."""
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1003, 4242}, manifest)

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, online=False
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["unexplained"] == [4242]
    assert report["status"] == "fail"
    assert any(e["code"] == "unexplained_drops" for e in report["errors"])


# --------------------------------------------------------------------------- #
# Named checks, statuses, and the rendered summary
# --------------------------------------------------------------------------- #


def test_every_check_is_enumerated_and_arrays_are_projections(export_dir):
    """checks_run lists passing checks too, and the legacy arrays derive from it."""
    report = validate.run_validation(export_dir, online=False)

    names = {c["name"] for c in report["checks_run"]}
    assert {"json-parse", "pmid-unique", "vs-entrez", "core-fields", "deleted-gone"} <= names
    # Passing checks are recorded, which is the whole point.
    assert any(c["status"] == "pass" for c in report["checks_run"])

    # errors/warnings/skipped_checks are projections, so they cannot disagree.
    def codes(status):
        return [c["code"] for c in report["checks_run"] if c["status"] == status and c["code"]]

    assert [e["code"] for e in report["errors"]] == codes("fail")
    assert [w["code"] for w in report["warnings"]] == codes("warn")
    assert len(report["skipped_checks"]) == sum(
        1 for c in report["checks_run"] if c["status"] == "skip"
    )


def test_na_is_distinct_from_skip(loaded_con, export_dir):
    """A check with nothing to verify is n/a, and stays out of skipped_checks.

    `skipped_checks` is a to-do list for the reviewer — things they could get by
    passing a flag. "the database has no deletions to sample" is not actionable,
    so it must not land there.
    """
    loaded_con.execute("DELETE FROM deleted_pmid")
    report = validate.run_validation(export_dir, con=loaded_con, online=False)

    by_name = {c["name"]: c for c in report["checks_run"]}
    assert by_name["deleted-absent"]["status"] == "n/a"
    assert not any("deleted-absent" in s for s in report["skipped_checks"])
    # ...whereas a genuinely skipped check does appear there.
    assert by_name["vs-entrez"]["status"] == "skip"
    assert any("vs-entrez" in s for s in report["skipped_checks"])


@pytest.mark.parametrize(
    "mismatch,expected",
    [
        ({"field": "pub_year", "exported": "", "entrez": "1978"}, "exported_blank"),
        ({"field": "issue", "exported": "Suppl", "entrez": ""}, "entrez_blank"),
        ({"field": "volume", "exported": "12", "entrez": "13"}, "values_differ"),
        ({"field": "abstract", "similarity": 0.4}, "low_similarity"),
    ],
)
def test_mismatch_kind_classification(mismatch, expected):
    assert validate._mismatch_kind(mismatch) == expected


def test_group_mismatches_tallies_by_field_and_kind():
    grouped = validate.group_mismatches([
        {"field": "pub_year", "exported": "", "entrez": "1978"},
        {"field": "pub_year", "exported": "", "entrez": "1975"},
        {"field": "volume", "exported": "12", "entrez": "13"},
    ])
    assert grouped["by_field"] == {"pub_year": 2, "volume": 1}
    assert grouped["by_kind"]["exported_blank"] == 2
    assert grouped["by_kind"]["values_differ"] == 1
    # One worked example per kind, so a reader sees the shape of each.
    assert set(grouped["examples"]) == {"exported_blank", "values_differ"}


def test_summary_always_reports_the_incorrect_data_count():
    """The `values_differ` line prints even at zero — that zero is the verdict.

    "20 fields disagreed" is unactionable; "20 blank, 0 wrong" says the export is
    incomplete but not corrupt, which is the call a reviewer actually has to make.
    """
    grouped = validate.group_mismatches([
        {"field": "pub_year", "exported": "", "entrez": "1978"},
    ])
    check = {
        "name": "core-fields", "section": "field accuracy", "status": "warn",
        "expectation": "x", "observed": "y", "code": "field_mismatches",
        "count": 1, "see": "checks.field_validation.mismatches", "detail": grouped,
    }
    lines = "\n".join(validate._mismatch_detail(check))
    assert "1 exported blank where Entrez has a value (missing data)" in lines
    assert "0 exported a different value (incorrect data)" in lines


def test_summary_flags_truncated_example_lists():
    """A capped example list must say so rather than implying it is complete."""
    grouped = validate.group_mismatches(
        [{"field": "pub_year", "exported": "", "entrez": "1978"}] * 20
    )
    check = {
        "name": "core-fields", "section": "field accuracy", "status": "warn",
        "expectation": "x", "observed": "y", "code": "field_mismatches",
        "count": 43, "see": None, "detail": grouped,
    }
    assert "(20 of 43 shown)" in "\n".join(validate._mismatch_detail(check))


def test_summary_lists_sections_skips_and_gaps(export_dir):
    report = validate.run_validation(export_dir, online=False)
    out = validate.format_summary(report)

    for heading in ("STRUCTURE", "COVERAGE", "FIELD ACCURACY", "DELETIONS", "NOT CHECKED"):
        assert heading in out
    # Skipped checks were previously invisible on stdout entirely.
    assert "[skip]" in out
    assert "no --previous-report" in out
    # Passing checks are shown with their expectation, not just counted.
    assert "every line parses as JSON" in out
    # The derived half of NOT CHECKED tracks CORE_FIELDS/SOFT_FIELDS.
    assert "journal_name" in out and "article_title" in out


def test_report_records_the_thresholds_it_was_judged_against(export_dir):
    """A report that cannot reproduce its own thresholds cannot be re-read later."""
    report = validate.run_validation(
        export_dir, online=False, abstract_threshold=0.8,
        entrez_low=0.5, entrez_high=1.5,
    )
    assert report["inputs"]["abstract_threshold"] == 0.8
    assert report["inputs"]["entrez_low"] == 0.5
    assert report["inputs"]["entrez_high"] == 1.5


def test_field_validation_records_its_denominator(export_dir, monkeypatch):
    """core_mismatch_rate is unauditable without the comparison count."""
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    report = validate.run_validation(export_dir, online=True)

    fv = report["checks"]["field_validation"]
    assert fv["core_comparisons"] > 0
    expected = round(fv["core_mismatches"] / fv["core_comparisons"], 4)
    assert fv["core_mismatch_rate"] == expected


def test_summary_surfaces_findings_from_unrendered_checks(export_dir):
    """A check the renderer does not place in a section must still reach stdout.

    Insurance against a future check being added without touching format_summary
    and silently vanishing from the human-readable output.
    """
    report = validate.run_validation(export_dir, online=False)
    report["checks_run"].append({
        "name": "future-check", "section": "some-new-section",
        "expectation": "something nobody has rendered yet", "status": "fail",
        "observed": "it went wrong", "code": "future_code", "count": 3,
        "see": "checks.future", "detail": {},
    })
    report["errors"].append({
        "code": "future_code", "message": "A check the renderer knows nothing about.",
        "count": 3, "see": "checks.future",
    })

    out = validate.format_summary(report)
    assert "OTHER FINDINGS" in out
    assert "A check the renderer knows nothing about." in out


def test_medline_date_year_is_not_a_false_mismatch(export_dir, loaded_con, monkeypatch):
    """efetch returning a bare MedlineDate must not read as a pub_year mismatch.

    The export recovers a year from a free-text MedlineDate ("1998 Spring" ->
    "1998"). efetch usually renders those records as <Year>+<Season> instead,
    but not always — and when it returns the archival form, comparing it raw
    against a recovered year makes every such record a mismatch. validate
    applies the exporter's own recovery so the two sides are comparable.
    PMID 1003's canned response is deliberately the MedlineDate form.
    """
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")

    fetched = validate.efetch_documents([1003], api_key=None, email=None)
    assert fetched[1003]["pub_year"] == "1998"
    assert [m for m in report["checks"]["field_validation"]["mismatches"]
            if m["field"] == "pub_year"] == []


# --------------------------------------------------------------------------- #
# Failure modes the checks must survive rather than crash on
# --------------------------------------------------------------------------- #


def test_truncated_shard_is_reported_not_raised(export_dir):
    """A shard cut off mid-write (a killed export) is a finding, not a traceback."""
    import gzip as _gzip

    shard = export_dir / "truncated.ndjson.gz"
    with _gzip.open(shard, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(_valid_doc()) + "\n")
    shard.write_bytes(shard.read_bytes()[:-8])  # lose the end-of-stream marker

    report = validate.run_validation(export_dir, online=False)
    assert report["status"] == "fail"
    assert any(e["code"] == "unreadable_shard" for e in report["errors"])
    assert report["checks"]["structure"]["unreadable_shards"][0]["shard"] == shard.name


def test_examples_are_capped_but_counted():
    """Findings keep their full count while retaining only a few examples."""
    found = validate._Examples()
    for i in range(validate._MAX_EXAMPLES + 500):
        found.append({"line": i})
    assert len(found) == validate._MAX_EXAMPLES + 500
    assert len(found.examples) == validate._MAX_EXAMPLES


def test_offline_results_survive_an_entrez_outage(export_dir, loaded_con, monkeypatch):
    """A mid-run network failure must not discard the offline checks or the manifest."""
    def dead(endpoint, params, **_):
        if endpoint == "efetch.fcgi":
            raise RuntimeError("eutils efetch.fcgi failed after 3 attempts")
        return _fake_eutils_factory(_EFETCH)(endpoint, params)

    monkeypatch.setattr(validate, "_eutils", dead)
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")

    assert report["checks"]["structure"]["records_total"] == 2
    assert any(w["code"] == "field_check_unreachable" for w in report["warnings"])
    assert report["checks"]["field_validation"] == {"sampled": 2, "checked": 0}


def test_permanent_http_errors_are_not_retried(monkeypatch):
    """A 4xx (a bad api_key, say) fails on the first call: retrying cannot fix it."""
    import requests

    calls = []

    class _Resp:
        status_code = 400

        def raise_for_status(self):
            raise requests.HTTPError("400 Client Error", response=self)

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(validate.requests, "get", fake_get)
    monkeypatch.setattr(validate.time, "sleep", lambda _: pytest.fail("slept on a 4xx"))

    with pytest.raises(RuntimeError):
        validate._eutils("einfo.fcgi", {"db": "pubmed"}, api_key="bad", email=None)
    assert len(calls) == 1


def test_reinstated_pmid_is_not_an_explained_drop(export_dir, loaded_con, tmp_path):
    """A PMID deleted then re-added is live, so losing it from the export is an error."""
    # 1002 is deleted in the fixtures; re-add it as a later version, which is
    # what latest_article resolves to. The export predates that, so it is absent.
    loaded_con.execute(
        "INSERT INTO article (pmid, source_file, file_order_key, article_title, loaded_at)"
        " VALUES (1002, 'pubmed25n0009.xml.gz', 25000009, 'Reinstated.', now())"
    )
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1002, 1003}, manifest)

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, online=False
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["explained_by_deletion"] == []
    assert drops["unexplained"] == [1002]
