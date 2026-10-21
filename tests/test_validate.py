"""Tests for the `validate` command.

All network access goes through `validate._eutils`, which these tests
monkeypatch with canned einfo/efetch responses so nothing touches the wire.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

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
        <PublicationTypeList>
          <PublicationType UI="D016422">Letter</PublicationType>
          <PublicationType UI="D013485">Research Support, Non-U.S. Gov't</PublicationType>
          <PublicationType UI="D016420">Comment</PublicationType>
        </PublicationTypeList>
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
          <ISOAbbreviation>J Examples</ISOAbbreviation>
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
            return json.dumps({"einforesult": {"dbinfo": [{"count": str(entrez_count)}]}}).encode()
        if endpoint == "efetch.fcgi":
            wanted = [int(x) for x in params["id"].split(",")]
            body = "".join(articles[p] for p in wanted if p in articles)
            return f"<PubmedArticleSet>{body}</PubmedArticleSet>".encode()
        if endpoint == "esummary.fcgi":
            # Shaped like the live response: every ID asked about is named, a
            # missing one with "error".
            wanted = params["id"].split(",")
            result = {"uids": wanted}
            for uid in wanted:
                result[uid] = (
                    {"uid": uid}
                    if int(uid) in articles
                    else {"uid": uid, "error": "cannot get document summary"}
                )
            return json.dumps({"result": result}).encode()
        raise AssertionError(f"unexpected endpoint {endpoint}")

    return fake


@pytest.fixture
def export_dir(loaded_con, tmp_path):
    """A real export of the loaded fixtures (PMIDs 1001, 1003).

    Gzipped, because that is what `export` writes by default -- and `validate`
    is supposed to read the default form with no flag saying so.
    """
    from pubmed2db.export import export_json

    out = tmp_path / "export"
    export_json(loaded_con, out)
    return out


def _append_to_shard(export_dir, *lines: str) -> None:
    """Append raw lines to one shard, whichever way it was written.

    Appending to a gzip shard writes a second member; readers concatenate them
    transparently, which is itself worth exercising -- a real export appended to
    by a fixing script would look exactly like this.
    """
    shard = next(iter(sorted(export_dir.glob("pubmed_metadata_*"))))
    opener = gzip.open if shard.name.endswith(".gz") else open
    with opener(shard, "at", encoding="utf-8") as handle:
        handle.writelines(line + "\n" for line in lines)


def test_offline_structure_passes(export_dir):
    report = validate.run_validation(export_dir, online=False)
    assert report["status"] == "pass", report["errors"]
    assert report["errors"] == []
    assert report["checks"]["structure"]["records_total"] == 2


def test_structure_reports_identifier_coverage(export_dir):
    """The share of records carrying each identifier type is reported.

    No check gates on it — one export in isolation cannot say whether 96% or 6%
    is right — but a collapse between two runs is invisible without the number.
    """
    report = validate.run_validation(export_dir, online=False)
    coverage = report["checks"]["structure"]["identifier_coverage"]
    assert coverage["doi"] == {"records": 1, "pct": 50.0}
    assert coverage["PMC"] == {"records": 1, "pct": 50.0}
    assert "50.0% doi" in validate.format_summary(report)


def test_a_shard_that_vanishes_is_reported_not_raised(export_dir, monkeypatch):
    """`export` publishes in place, so a shard can go between find_shards and the
    read. That is the half-written export this check exists to catch -- it must
    land in `unreadable_shards`, not take the whole run down."""
    shards = [*validate.find_shards(export_dir), export_dir / "pubmed_metadata_09999.ndjson"]
    monkeypatch.setattr(validate, "find_shards", lambda _: shards)

    report = validate.run_validation(export_dir, online=False)

    unreadable = report["checks"]["structure"]["unreadable_shards"]
    assert [entry["shard"] for entry in unreadable] == ["pubmed_metadata_09999.ndjson"]
    # The surviving shard was still read.
    assert report["checks"]["structure"]["records_total"] == 2


def test_offline_run_does_not_announce_entrez_work(export_dir, caplog):
    """An --offline run announcing "comparing N records against Entrez" would
    contradict its own start line: check_fields returns immediately offline."""
    import logging

    with caplog.at_level(logging.INFO, logger="pubmed2db.validate"):
        validate.run_validation(export_dir, online=False)

    messages = [r.getMessage() for r in caplog.records]
    assert any("offline (no Entrez checks)" in m for m in messages), messages
    assert not any("against Entrez" in m for m in messages), messages


def test_duplicate_examples_are_distinct_pmids(export_dir):
    """One PMID exported many times must not consume every example slot.

    The example list is capped, so listing the same offender 25 times would
    hide 19 other duplicated PMIDs behind a single bug.
    """
    _append_to_shard(
        export_dir,
        *(json.dumps({**_valid_doc(), "id": "PMID:1001"}) for _ in range(25)),
    )

    structure = validate.run_validation(export_dir, online=False)["checks"]["structure"]
    assert structure["duplicate_pmids"] == [1001]


def test_malformed_and_structural_errors(export_dir):
    _append_to_shard(
        export_dir,
        "{not json}",
        json.dumps({"id": "PMID:1001"}),  # duplicate + missing fields
        json.dumps({**_valid_doc(), "id": "1099"}),  # bad id
        json.dumps({**_valid_doc(), "id": "PMID:1200", "volume": None}),
    )

    report = validate.run_validation(export_dir, online=False)
    codes = {e["code"] for e in report["errors"]}
    assert report["status"] == "fail"
    assert {
        "malformed_json",
        "missing_fields",
        "invalid_ids",
        "null_values",
        "duplicate_pmids",
    } <= codes


@pytest.mark.parametrize(
    "pub_month,warns",
    [
        ("Mar", False),  # an ordinary abbreviation
        ("", False),  # absent, the empty-string convention
        ("Spring", False),  # a season -- the export now passes these through
        ("Sep-Dec", False),  # the DocumentMetadataAPI example's range
        ("Jul-Aug", False),
        ("Winter-Spring", False),  # a range of seasons
        ("Dec-1999 Jan", True),  # cross-year: legitimate input, still worth seeing
        ("3", True),  # unnormalized -- the export should never emit this
        ("Marzipan", True),
    ],
)
def test_month_format_accepts_what_the_export_can_emit(export_dir, pub_month, warns):
    """The `month-format` check must accept every shape `export.pub_month` emits.

    The check used to allow only the 12 abbreviations, which was right when the
    export blanked everything else. Now that seasons and ranges pass through
    (issue #14), a too-narrow set here would warn on every one of the ~7% of
    records carrying them -- while a wide-open set would stop flagging anything.
    So the odd-but-real "Dec-1999 Jan" is deliberately still a warning.
    """
    _append_to_shard(export_dir, json.dumps({**_valid_doc(), "pub_month": pub_month}))

    report = validate.run_validation(export_dir, online=False)
    warned = any(w["code"] == "invalid_months" for w in report["warnings"])
    assert warned is warns, report["warnings"]


def _valid_doc():
    return {
        "id": "PMID:9",
        "identifiers": ["PMID:9"],
        "journal_name": "",
        "journal_abbrev": "",
        "article_title": "",
        "volume": "",
        "issue": "",
        "pub_year": "",
        "pub_month": "",
        "pub_day": "",
        "pub_date": "",
        "publication_types": [],
        "abstract": "",
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
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH, entrez_count=4))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    assert report["checks"]["coverage"]["pct_of_entrez"] == pytest.approx(0.5)
    assert any(w["code"] == "coverage_out_of_band" for w in report["warnings"]), report["warnings"]


def test_field_validation_matches(export_dir, loaded_con, monkeypatch):
    """Also guards that identifiers compare as a *set*: the export sorts them
    (`PMC:` before `doi:`) while `_EFETCH` lists them in document order
    (doi first), so an order-sensitive comparison fails here."""
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    fv = report["checks"]["field_validation"]
    assert fv["checked"] == 2
    assert fv["mismatches"] == []
    for group in validate._SOFT_GROUPS:
        assert fv[group.code] == [], group.code
    assert report["status"] == "pass", report["errors"] + report["warnings"]


@pytest.mark.parametrize(
    "label,edit",
    [
        # Indexing added a type after our last update file -- the usual cause.
        (
            "added",
            lambda x: x.replace(
                '<PublicationType UI="D016420">Comment</PublicationType>',
                '<PublicationType UI="D016420">Comment</PublicationType>'
                '<PublicationType UI="D016454">Review</PublicationType>',
            ),
        ),
        # Same types, different order: order is part of what we ship.
        (
            "reordered",
            lambda x: x.replace(
                '<PublicationType UI="D016422">Letter</PublicationType>', ""
            ).replace(
                '<PublicationType UI="D016420">Comment</PublicationType>',
                '<PublicationType UI="D016420">Comment</PublicationType>'
                '<PublicationType UI="D016422">Letter</PublicationType>',
            ),
        ),
    ],
)
def test_publication_types_mismatch_is_soft(export_dir, loaded_con, monkeypatch, label, edit):
    """Compared in order, reported, never fatal: NLM revises types when MEDLINE
    indexing completes, so live efetch can be ahead of the load."""
    tampered = dict(_EFETCH)
    tampered[1001] = edit(_EFETCH[1001])
    assert tampered[1001] != _EFETCH[1001]
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(tampered))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")

    fv = report["checks"]["field_validation"]
    assert [m["field"] for m in fv["publication_type_mismatches"]] == ["publication_types"]
    assert fv["journal_mismatches"] == fv["pub_date_mismatches"] == []
    assert fv["mismatches"] == []
    # A warning under its own code -- not a failure, and not blamed on the journal.
    assert report["status"] == "warn", report["errors"]
    assert [w["code"] for w in report["warnings"]] == ["publication_type_mismatches"]


def test_unnamed_publication_types_blame_the_load_not_indexing(loaded_con, tmp_path, monkeypatch):
    """A database loaded before names were parsed exported `"name": ""` on
    every type. That must read as "rebuild", once, under its own code -- not as
    every sampled record's types having been revised by indexing."""
    from pubmed2db.export import export_json

    loaded_con.execute("UPDATE publication_type SET type_name = NULL")
    out = tmp_path / "export"
    export_json(loaded_con, out)
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    report = validate.run_validation(out, con=loaded_con, email="me@example.com")

    assert report["status"] == "warn", report["errors"]
    [warning] = report["warnings"]
    assert warning["code"] == "publication_type_names_missing"
    assert "Rebuild the database" in warning["message"]
    assert report["checks"]["field_validation"]["publication_type_mismatches"] == []


def test_unnamed_publication_types_still_compare_their_ids(monkeypatch):
    """Comparing on ids alone must not hide a real revision."""
    report = validate.Report()
    doc = {**_valid_doc(), "publication_types": [{"id": "MESH:D016428", "name": ""}]}
    revised = {
        **doc,
        "publication_types": [
            {"id": "MESH:D016428", "name": "Journal Article"},
            {"id": "MESH:D016454", "name": "Review"},
        ],
    }
    monkeypatch.setattr(validate, "efetch_documents", lambda *a, **k: {9: revised})
    validate.check_fields(
        report,
        {9: doc},
        online=True,
        api_key=None,
        email="me@example.com",
        abstract_threshold=0.9,
    )
    assert [w["code"] for w in report.warnings] == ["publication_type_mismatches"]


@pytest.mark.parametrize(
    "types",
    [
        "Review",  # not a list
        [{"id": "MeSH:D016454", "name": "Review"}],  # Biolink's casing is MESH
        [{"id": "D016454", "name": "Review"}],  # no prefix
        [{"id": "MESH:D016454"}],  # no name
        [{"id": "MESH:D016454", "label": "Review"}],  # renamed key
        [{"id": "MESH:D016454", "name": "Review", "x": 1}],  # extra key
        ["MESH:D016454"],  # bare CURIE
    ],
)
def test_malformed_publication_types_fail_structure(export_dir, types):
    """The efetch side builds its ids from the same `MESH_PREFIX` as the
    export, so a prefix slip passes the field comparison; only a structural
    check written against the contract itself catches it."""
    _append_to_shard(
        export_dir, json.dumps({**_valid_doc(), "id": "PMID:1200", "publication_types": types})
    )
    # The append changed the shard's checksum; that error is not under test.
    (export_dir / "metadata.json").unlink()
    report = validate.run_validation(export_dir, online=False)
    assert [e["code"] for e in report["errors"]] == ["invalid_publication_types"]


def test_one_soft_group_cannot_crowd_out_anothers_examples(monkeypatch):
    """Each soft check points at its own example list. A single shared list
    capped at 20 filled up with whichever group came first."""
    report = validate.Report()
    sample, fetched = {}, {}
    for pmid in range(1, validate._MAX_EXAMPLES + 6):
        doc = {**_valid_doc(), "id": f"PMID:{pmid}", "pub_date": "2020"}
        sample[pmid] = doc
        fetched[pmid] = {**doc, "publication_types": [{"id": "MESH:D016454", "name": "Review"}]}
    fetched[pmid] = {**fetched[pmid], "pub_date": "2021"}  # only the last record
    monkeypatch.setattr(validate, "efetch_documents", lambda *a, **k: fetched)
    validate.check_fields(
        report,
        sample,
        online=True,
        api_key=None,
        email="me@example.com",
        abstract_threshold=0.9,
    )

    fv = report.checks["field_validation"]
    assert len(fv["publication_type_mismatches"]) == validate._MAX_EXAMPLES
    assert [m["pmid"] for m in fv["pub_date_mismatches"]] == [pmid]
    sees = {c.name: c.see for c in report.checks_run}
    assert sees["pub-date-soft"] == "checks.field_validation.pub_date_mismatches"


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
    """A DOI that disagrees with Entrez is reported, but only as advisory.

    Identifiers are deliberately outside the gated core-field rate: a PMCID
    assigned upstream since our last update file is not an export defect, and
    it lands on the same ahead-of-print records that already mismatch on
    volume/issue. See the comment at the comparison.
    """
    tampered = dict(_EFETCH)
    tampered[1001] = _EFETCH[1001].replace("10.1038/example1001", "10.1038/somethingelse")
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(tampered))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    checks = report["checks"]["field_validation"]
    entry = next(m for m in checks["identifier_mismatches"] if m["pmid"] == 1001)
    assert "doi:10.1038/somethingelse" in entry["entrez"]
    assert "doi:10.1038/example1001" in entry["exported"]
    assert not any(m["field"] == "identifiers" for m in checks["mismatches"])
    statuses = {c["name"]: c["status"] for c in report["checks_run"]}
    assert statuses["identifiers-soft"] == "warn"
    assert statuses["core-fields"] != "fail"


def test_identifier_comparison_ignores_doi_case(export_dir, loaded_con, monkeypatch):
    """A case-only DOI change is not a difference in the data.

    DOIs are case-insensitive by specification and PubMed is not consistent
    about its own, so folding case here keeps a re-cased DOI out of the report.
    """
    recased = dict(_EFETCH)
    recased[1001] = _EFETCH[1001].replace("10.1038/example1001", "10.1038/EXAMPLE1001")
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(recased))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")
    assert report["checks"]["field_validation"]["identifier_mismatches"] == []


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
        "PMID:1001",
        "doi:10.1038/example1001",
        "PMC:PMC7654321",
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

    _append_to_shard(export_dir, "{broken")

    no_db = str(export_dir.parent / "absent.duckdb")
    result = CliRunner().invoke(main, ["--db", no_db, "validate", str(export_dir), "--offline"])
    assert result.exit_code == 1
    assert "Validation FAIL" in result.output


def test_expected_fields_matches_spec():
    """EXPECTED_FIELDS derives from the exporter; lock it to the shipped spec.

    The 13 exported field names are an external contract (Node Annotator /
    ElasticSearch consume them), so changing the export shape should trip a test
    rather than silently re-define what validate accepts. Nine of them are the
    DocumentMetadataAPI spec's; `id`, `identifiers`, `pub_date` and
    `publication_types` are ours.
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
        "pub_date",
        "publication_types",
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
    """A PMID that vanished without a recorded deletion fails an offline run.

    Offline there is no second opinion to ask, so the database's word is final.
    """
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1003, 4242}, manifest)

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, online=False
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["unexplained"] == [4242]
    assert report["status"] == "fail"
    assert any(e["code"] == "unexplained_drops" for e in report["errors"])
    assert drops["pubmed_confirmation"] == "offline"


def test_a_drop_pubmed_no_longer_serves_is_a_warning(export_dir, loaded_con, monkeypatch, tmp_path):
    """The fresh-database rebuild: deletions the new database never loaded.

    A database built from a new baseline has no deleted_pmid row for anything
    PubMed retired between the previous build and that baseline, so by the
    database alone every such drop failed the run, no manifest was written, and
    the next run diffed against the same old manifest and failed the same way.
    Asked online, PubMed does not serve 4242 either, which explains the drop:
    a warning (so the manifest is still written) rather than an error.
    """
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    manifest = _manifest_from_before_the_database(tmp_path, {1001, 1003, 4242})
    manifest_out = tmp_path / "next.txt.gz"

    report = validate.run_validation(
        export_dir,
        con=loaded_con,
        previous_manifest=manifest,
        manifest_out=manifest_out,
        email="me@example.com",
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["pubmed_confirmation"] == "entrez"
    assert drops["database_rebuilt_since"] is True
    assert drops["retired_upstream"] == [4242]
    assert drops["unexplained"] == []
    assert report["status"] == "warn"
    assert any(w["code"] == "drops_retired_upstream" for w in report["warnings"])
    assert not any(e["code"] == "unexplained_drops" for e in report["errors"])
    assert manifest_out.exists()


def _manifest_from_before_the_database(tmp_path, pmids, *, copied=False):
    """A previous manifest written before the database existed: a rebuild.

    ``copied`` gives the file a fresh mtime, as ``cp -r`` would, leaving only
    the gzip header to say when it was written.
    """
    manifest = tmp_path / "prev.txt.gz"
    an_hour_ago = time.time() - 3600
    with gzip.GzipFile(manifest, "wb", mtime=an_hour_ago) as handle:
        handle.write("".join(f"{p}\n" for p in sorted(pmids)).encode())
    if not copied:
        os.utime(manifest, (an_hour_ago, an_hour_ago))
    return manifest


def test_a_copied_manifest_still_marks_a_rebuild(export_dir, loaded_con, monkeypatch, tmp_path):
    """The gzip header, not the file's mtime, says when a manifest was written."""
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    manifest = _manifest_from_before_the_database(tmp_path, {1001, 1003, 4242}, copied=True)
    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, email="me@example.com"
    )
    assert report["checks"]["drops_since_previous"]["database_rebuilt_since"] is True
    assert any(w["code"] == "drops_retired_upstream" for w in report["warnings"])


def test_a_drop_pubmed_no_longer_serves_fails_a_continuing_database(
    export_dir, loaded_con, monkeypatch, tmp_path
):
    """The same drop on a database that exported it last time: a missed update file.

    A continuing database loads every update file, so a record PubMed retired
    has its DeleteCitation here unless a file was skipped. PubMed agreeing the
    record is gone does not explain how *this* database lost it.
    """
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1003, 4242}, manifest)
    manifest_out = tmp_path / "next.txt.gz"

    report = validate.run_validation(
        export_dir,
        con=loaded_con,
        previous_manifest=manifest,
        manifest_out=manifest_out,
        email="me@example.com",
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["database_rebuilt_since"] is False
    assert drops["retired_upstream"] == [4242]
    assert report["status"] == "fail"
    assert any(e["code"] == "missed_deletions" for e in report["errors"])
    assert not manifest_out.exists()


def test_a_rebuilt_database_with_a_load_gap_does_not_excuse_retired_drops(
    export_dir, loaded_con, monkeypatch, tmp_path
):
    """A fresh build that skipped an update file missed that file's deletions.

    They look exactly like the deletions published before the new baseline, so
    being rebuilt is not enough: every file inside the build's range must have
    loaded, or a warning run writes a manifest that buries the gap for good.
    """
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    # 25n0002 is loaded; register 25n0004 as loaded too, leaving 25n0003 out.
    loaded_con.execute(
        "INSERT INTO source_file (file_name, kind, year_yy, file_number, file_order_key, "
        "processed_at) VALUES ('pubmed25n0004.xml.gz', 'update', 25, 4, 25000004, now())"
    )
    manifest = _manifest_from_before_the_database(tmp_path, {1001, 1003, 4242})
    manifest_out = tmp_path / "next.txt.gz"

    report = validate.run_validation(
        export_dir,
        con=loaded_con,
        previous_manifest=manifest,
        manifest_out=manifest_out,
        email="me@example.com",
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["database_rebuilt_since"] is True
    assert drops["load_gaps"] == ["pubmed25n0003.xml.gz"]
    assert drops["retired_upstream"] == [4242]
    assert report["status"] == "fail"
    error = next(e for e in report["errors"] if e["code"] == "missed_deletions")
    assert "load_gaps" in error["message"]
    assert not any(w["code"] == "drops_retired_upstream" for w in report["warnings"])
    assert not manifest_out.exists()


def test_lost_records_and_missed_deletions_are_both_reported(
    export_dir, loaded_con, monkeypatch, tmp_path
):
    """One error must not hide the other: they send the operator different ways."""
    served = dict(_EFETCH)
    served[4242] = _EFETCH[1003].replace("1003", "4242")
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(served))
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1003, 4242, 4243}, manifest)

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, email="me@example.com"
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["unexplained"] == [4242]
    assert drops["retired_upstream"] == [4243]
    codes = {e["code"]: e["count"] for e in report["errors"]}
    assert codes["unexplained_drops"] == 1
    assert codes["missed_deletions"] == 1


def test_a_continuing_database_asks_pubmed_about_a_sample_only(
    export_dir, loaded_con, monkeypatch, tmp_path
):
    """Where PubMed's answer cannot excuse a drop, a sample names the failure.

    On a continuing database every unexplained drop fails, whatever PubMed says,
    so confirming all of them would spend rate-limited requests on a verdict
    already reached.
    """
    asked: list[str] = []
    fake = _fake_eutils_factory(_EFETCH)

    def counting(endpoint, params, **kwargs):
        if endpoint == "esummary.fcgi":
            asked.extend(params["id"].split(","))
        return fake(endpoint, params, **kwargs)

    monkeypatch.setattr(validate, "_eutils", counting)
    monkeypatch.setattr(validate, "_DROP_DIAGNOSE_SAMPLE", 2)
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1003, 4242, 4243, 4244}, manifest)

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, email="me@example.com"
    )
    drops = report["checks"]["drops_since_previous"]
    assert len(asked) == 2 and set(asked) < {"4242", "4243", "4244"}
    assert drops["pubmed_confirmation"] == "sampled"
    assert drops["retired_upstream"] == sorted(int(p) for p in asked)
    assert drops["unexplained"] == sorted({4242, 4243, 4244} - {int(p) for p in asked})
    codes = {e["code"] for e in report["errors"]}
    assert codes == {"unexplained_drops", "missed_deletions"}


def test_a_degraded_esummary_response_is_not_an_explanation(
    export_dir, loaded_con, monkeypatch, tmp_path
):
    """A response that does not mention a PMID says nothing about it.

    NCBI can answer 200 with an error body or a truncated one. Reading every
    PMID missing from that as "PubMed dropped it" would turn lost records into a
    warning, and the warning run's manifest would bury them for good.
    """
    fake = _fake_eutils_factory(_EFETCH)

    def degraded(endpoint, params, **kwargs):
        if endpoint == "esummary.fcgi":
            return b'{"esummaryresult": ["Unable to obtain query #1"]}'
        return fake(endpoint, params, **kwargs)

    monkeypatch.setattr(validate, "_eutils", degraded)
    manifest = _manifest_from_before_the_database(tmp_path, {1001, 1003, 4242})

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, email="me@example.com"
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["pubmed_confirmation"] == "unreachable"
    assert drops["retired_upstream"] == []
    assert drops["unexplained"] == [4242]
    assert report["status"] == "fail"


def test_pmids_served_needs_an_answer_for_every_pmid(monkeypatch):
    """A well-formed response that leaves a PMID out raises rather than guessing."""

    def partial(endpoint, params, **_):
        return json.dumps({"result": {"uids": ["1001"], "1001": {"uid": "1001"}}}).encode()

    monkeypatch.setattr(validate, "_eutils", partial)
    with pytest.raises(RuntimeError, match="did not answer"):
        validate.pmids_served([1001, 4242], api_key=None, email=None)


def test_a_drop_pubmed_still_serves_is_an_error(export_dir, loaded_con, monkeypatch, tmp_path):
    """A PMID PubMed still serves but the export lost is the real failure."""
    served = dict(_EFETCH)
    served[4242] = _EFETCH[1003].replace("1003", "4242")
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(served))
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1003, 4242}, manifest)

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, email="me@example.com"
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["pubmed_confirmation"] == "entrez"
    assert drops["unexplained"] == [4242]
    assert drops["retired_upstream"] == []
    assert report["status"] == "fail"
    error = next(e for e in report["errors"] if e["code"] == "unexplained_drops")
    assert "still serves" in error["message"]


def test_too_many_unexplained_drops_are_not_sent_to_pubmed(
    export_dir, loaded_con, monkeypatch, tmp_path
):
    """Past the cap the previous manifest is suspect, not the export."""

    def no_esummary(endpoint, params, **_):
        if endpoint == "esummary.fcgi":
            raise AssertionError("esummary must not be called over the limit")
        return _fake_eutils_factory(_EFETCH)(endpoint, params)

    monkeypatch.setattr(validate, "_eutils", no_esummary)
    monkeypatch.setattr(validate, "_DROP_CONFIRM_LIMIT", 1)
    # Rebuilt: a continuing database only ever sends a sample, so has no cap.
    manifest = _manifest_from_before_the_database(tmp_path, {1001, 1003, 4242, 4243})

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, email="me@example.com"
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["pubmed_confirmation"] == "over_limit"
    assert drops["unexplained"] == [4242, 4243]
    assert report["status"] == "fail"
    error = next(e for e in report["errors"] if e["code"] == "unexplained_drops")
    assert "without --previous-manifest" in error["message"]
    assert "run validate online" not in error["message"]


def test_unreachable_pubmed_leaves_drops_unexplained(export_dir, loaded_con, monkeypatch, tmp_path):
    """No answer is not an explanation: the run fails and says why."""

    def dead(endpoint, params, **_):
        raise RuntimeError("eutils down")

    monkeypatch.setattr(validate, "_eutils", dead)
    manifest = tmp_path / "prev.txt.gz"
    validate.write_manifest({1001, 1003, 4242}, manifest)

    report = validate.run_validation(
        export_dir, con=loaded_con, previous_manifest=manifest, email="me@example.com"
    )
    drops = report["checks"]["drops_since_previous"]
    assert drops["pubmed_confirmation"] == "unreachable"
    assert drops["unexplained"] == [4242]
    error = next(e for e in report["errors"] if e["code"] == "unexplained_drops")
    assert "reachable" in error["message"]
    assert "run validate online" not in error["message"]


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


@pytest.mark.parametrize(
    "exported_abs,entrez_abs,kind",
    [
        ("", "An abstract efetch serves but the baseline XML lacks.", "exported_blank"),
        ("An abstract we exported that efetch no longer serves.", "", "entrez_blank"),
        (
            "The exported abstract, quite unlike the other.",
            "Totally different text.",
            "low_similarity",
        ),
    ],
)
def test_blank_abstract_is_missing_data_not_truncation(monkeypatch, exported_abs, entrez_abs, kind):
    """PMID 4825553 has no <Abstract> in its baseline file; efetch serves one.

    The 2026-09-26 corpus run reported it as "abstract text diverged (possible
    truncation)" because the abstract mismatch carried only a similarity and
    `_mismatch_kind` never saw a blank side. It is missing data, and the report
    should say so -- while the 0.0 still counts as a low-similarity record for
    the advisory `abstract` check.
    """
    report = validate.Report()
    doc = {**_valid_doc(), "id": "PMID:1", "abstract": exported_abs}
    fetched = {1: {**doc, "abstract": entrez_abs}}
    monkeypatch.setattr(validate, "efetch_documents", lambda *a, **k: fetched)
    validate.check_fields(
        report,
        {1: doc},
        online=True,
        api_key=None,
        email="me@example.com",
        abstract_threshold=0.9,
    )

    fv = report.checks["field_validation"]
    assert fv["core_mismatches"] == 1
    assert fv["mismatches_by_kind"][kind] == 1
    assert fv["abstract_similarity"]["min"] < 0.9


def test_group_mismatches_tallies_by_field_and_kind():
    grouped = validate.group_mismatches(
        [
            {"field": "pub_year", "exported": "", "entrez": "1978"},
            {"field": "pub_year", "exported": "", "entrez": "1975"},
            {"field": "volume", "exported": "12", "entrez": "13"},
        ]
    )
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
    grouped = validate.group_mismatches(
        [
            {"field": "pub_year", "exported": "", "entrez": "1978"},
        ]
    )
    check = {
        "name": "core-fields",
        "section": "field accuracy",
        "status": "warn",
        "expectation": "x",
        "observed": "y",
        "code": "field_mismatches",
        "count": 1,
        "see": "checks.field_validation.mismatches",
        "detail": grouped,
    }
    lines = "\n".join(validate._mismatch_detail(check))
    assert "1 exported blank where Entrez has a value (missing data)" in lines
    assert "0 exported a different value (incorrect data)" in lines


def test_summary_shows_a_blank_abstract_as_missing_data():
    """The example line for a blank-on-one-side abstract shows both values,
    clipped, under "missing data" -- not `similarity 0.0` under "possible
    truncation", which is what PMID:4825553 read as before the mismatch
    carried its values."""
    grouped = validate.group_mismatches(
        [
            {
                "field": "abstract",
                "exported": "",
                "entrez": "An abstract efetch serves but the baseline XML lacks.",
            },
        ]
    )
    check = {
        "name": "core-fields",
        "section": "field accuracy",
        "status": "warn",
        "expectation": "x",
        "observed": "y",
        "code": "field_mismatches",
        "count": 1,
        "see": "checks.field_validation.mismatches",
        "detail": grouped,
    }
    lines = "\n".join(validate._mismatch_detail(check))
    assert "1 exported blank where Entrez has a value (missing data)" in lines
    assert 'abstract exported "" vs. Entrez "An abstract efetch serves but the basel…"' in lines
    assert "similarity" not in lines
    assert "truncation" not in lines


def test_summary_flags_truncated_example_lists():
    """A capped example list must say so rather than implying it is complete."""
    grouped = validate.group_mismatches(
        [{"field": "pub_year", "exported": "", "entrez": "1978"}] * 20
    )
    check = {
        "name": "core-fields",
        "section": "field accuracy",
        "status": "warn",
        "expectation": "x",
        "observed": "y",
        "code": "field_mismatches",
        "count": 43,
        "see": None,
        "detail": grouped,
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


def test_summary_columns_line_up(export_dir):
    """Every row's expectation and observation start in the same column, in
    every section -- fixed widths went ragged once check names outgrew them."""
    report = validate.run_validation(export_dir, online=False)
    rows = [line for line in validate.format_summary(report).splitlines() if line.startswith("  [")]
    starts = set()
    for check in report["checks_run"]:
        [row] = [r for r in rows if r[9:].startswith(check["name"] + " ")]
        starts.add(row.index(check["expectation"], 9 + len(check["name"])))
    assert len(starts) == 1, "\n".join(rows)


def test_report_records_the_thresholds_it_was_judged_against(export_dir):
    """A report that cannot reproduce its own thresholds cannot be re-read later."""
    report = validate.run_validation(
        export_dir,
        online=False,
        abstract_threshold=0.8,
        entrez_low=0.5,
        entrez_high=1.5,
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
    report["checks_run"].append(
        {
            "name": "future-check",
            "section": "some-new-section",
            "expectation": "something nobody has rendered yet",
            "status": "fail",
            "observed": "it went wrong",
            "code": "future_code",
            "count": 3,
            "see": "checks.future",
            "detail": {},
        }
    )
    report["errors"].append(
        {
            "code": "future_code",
            "message": "A check the renderer knows nothing about.",
            "count": 3,
            "see": "checks.future",
        }
    )

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
    # Same recovery for the month half, which the export now also ships.
    assert fetched[1003]["pub_month"] == "Spring"
    assert [
        m
        for m in report["checks"]["field_validation"]["mismatches"]
        if m["field"] in ("pub_year", "pub_month")
    ] == []


@pytest.mark.parametrize(
    "exported,entrez,same",
    [
        # PMID:5684047 -- efetch drops the [ ] marking a translated title.
        (
            "[Surgical results in the reconstruction of the lacrimal ducts with out cannula probes].",
            "Surgical results in the reconstruction of the lacrimal ducts with out cannula probes.",
            True,
        ),
        # PMID:10205128 -- the archival title has a trailing space and no period.
        (
            "Rationing-talk and action in health care ",
            "Rationing-talk and action in health care.",
            True,
        ),
        # PMID:33511611 -- an empty <ArticleTitle/> is served as "[Not Available].".
        ("", "[Not Available].", True),
        # PMID:36326224 -- no terminal period inside the brackets.
        (
            "[An infectious diseases ward dedicated to elderly patients: why and how?]",
            "[An infectious diseases ward dedicated to elderly patients: why and how?].",
            True,
        ),
        # A change of wording must still be reported.
        ("Revised title for article one.", "A completely different title.", False),
    ],
)
def test_title_renderings_converge(exported, entrez, same):
    """efetch re-renders <ArticleTitle>; the export ships it verbatim.

    The four real pairs are every title mismatch of the 2026-09-26 corpus
    sample, each checked against its raw baseline file. Only the rendering is
    folded, only at comparison time, and through `_compare_value` so the hook
    is pinned as well as the helper.
    """
    a = validate._compare_value("article_title", {"article_title": exported})
    b = validate._compare_value("article_title", {"article_title": entrez})
    assert (a == b) is same


def test_date_renderings_are_compared_normalized(export_dir, loaded_con, monkeypatch):
    """The two sides can be *written* differently while describing one date.

    `pub_date` takes a MedlineDate whole, but efetch serves a re-serialization,
    so `efetch_documents` reconstructs one -- normalizing the month, as it must
    for the <Year>+<Season> rendering to converge with <MedlineDate>. A baseline
    "1998 September" therefore meets a reconstructed "1998 Sep". Both correct;
    not a difference in the data, and not something a report should show.
    """
    from pubmed2db.export import pub_date

    # The strings really do differ -- this is not a vacuous comparison.
    assert pub_date(None, None, None, "1998 September") == "1998 September"
    assert pub_date("1998", "September", None, None) == "1998 Sep"
    assert validate._normalize_date("1998 September") == validate._normalize_date("1998 Sep")
    # Same for a zero-padded day, either side of export.normalize_day.
    assert validate._normalize_date("2022 Aug 01") == validate._normalize_date("2022 Aug 1")
    # ...but a real difference still survives normalization.
    assert validate._normalize_date("1998 Spring") != validate._normalize_date("1998 Sep")

    # And the wiring, not just the helper: an export holding the MedlineDate
    # form against efetch serving the <Year>+<Month> one, end to end.
    report = validate.Report()
    sample = {1003: {**_valid_doc(), "id": "PMID:1003", "pub_date": "1998 September"}}
    fetched = {1003: {**{f: "" for f in validate.EXPECTED_FIELDS}, "pub_date": "1998 Sep"}}
    monkeypatch.setattr(validate, "efetch_documents", lambda *a, **k: fetched)
    validate.check_fields(
        report,
        sample,
        online=True,
        api_key=None,
        email="me@example.com",
        abstract_threshold=0.9,
    )

    fields = report.checks["field_validation"]
    assert fields["checked"] == 1
    assert all(m.get("field") != "pub_date" for m in fields["mismatches"]), fields
    assert fields["pub_date_mismatches"] == [], fields


def test_each_soft_reason_is_its_own_check(monkeypatch):
    """A pub_date disagreement used to be reported as `journal_mismatches`,
    with a message blaming the journal's different source -- one check covered
    every soft field long after they stopped sharing a reason."""
    report = validate.Report()
    doc = {**_valid_doc(), "id": "PMID:9", "pub_date": "2020 Mar 16"}
    sample = {9: doc}
    fetched = {9: {**doc, "pub_date": "2020 Mar 17"}}
    monkeypatch.setattr(validate, "efetch_documents", lambda *a, **k: fetched)
    validate.check_fields(
        report,
        sample,
        online=True,
        api_key=None,
        email="me@example.com",
        abstract_threshold=0.9,
    )

    statuses = {c.name: c.status for c in report.checks_run}
    assert statuses["journal-soft"] == "pass"
    assert statuses["pub-date-soft"] == "warn"
    assert [w["code"] for w in report.warnings] == ["pub_date_mismatches"]


def test_pub_date_cannot_fail_a_run(export_dir, loaded_con):
    """SOFT, not CORE -- and for a reason normalization does not address.

    `pub_date` is derived from the same three columns as pub_year/pub_month/
    pub_day, so gating on it would let one date disagreement contribute four
    mismatches instead of three, tightening the FAIL threshold on exactly the
    records most likely to trip it. validate.py makes the same argument for
    keeping the `identifiers` comparison advisory.
    """
    assert "pub_date" in validate.SOFT_FIELDS
    assert "pub_date" not in validate.CORE_FIELDS

    report = validate.run_validation(export_dir, con=loaded_con, online=False)
    expectations = report["checks"]["field_validation"]
    assert expectations is None or "pub_date" not in str(report.get("errors", []))


def test_season_rendering_is_not_a_false_mismatch(export_dir, loaded_con, monkeypatch):
    """The <Year>+<Season> rendering must compare equal to the MedlineDate form.

    efetch serves PMID:8000234 as <Year>1994</Year><Season>Sep-Dec</Season>
    where the baseline holds <MedlineDate>1994 Sep-Dec</MedlineDate> (issue #14).
    Both sides go through export.pub_month, so the two renderings of the same
    record must agree -- otherwise fixing the export just moves the disagreement.
    """
    seasonal = dict(_EFETCH)
    seasonal[1003] = _EFETCH[1003].replace(
        "<MedlineDate>1998 Spring</MedlineDate>",
        "<Year>1998</Year><Season>Spring</Season>",
    )
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(seasonal))
    report = validate.run_validation(export_dir, con=loaded_con, email="me@example.com")

    fetched = validate.efetch_documents([1003], api_key=None, email=None)
    assert (fetched[1003]["pub_year"], fetched[1003]["pub_month"]) == ("1998", "Spring")
    # The convergence that makes pub_date checkable: assembled from <Year>+<Season>
    # here, taken whole from <MedlineDate> in the export, one string either way.
    assert fetched[1003]["pub_date"] == "1998 Spring"
    assert [
        m
        for m in report["checks"]["field_validation"]["mismatches"]
        if m["field"] in ("pub_year", "pub_month", "pub_date")
    ] == []


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


def test_a_failed_run_writes_no_manifest(export_dir, monkeypatch, tmp_path, caplog):
    """A manifest is the next run's baseline, so a failed run must not leave one.

    06-validate.sbatch passes --manifest on every cluster run, which makes this
    the default path rather than a deliberate choice. A truncated or half-written
    export yields a short PMID set; adopting it as the baseline makes the *next*
    run report the recovered corpus as "N added" while the real drops go
    unnoticed -- the one comparison the manifest exists for.
    """
    import logging

    # An unreadable shard is a FAIL, and is exactly the half-written export the
    # structure check exists to catch.
    shards = [*validate.find_shards(export_dir), export_dir / "pubmed_metadata_09999.ndjson"]
    monkeypatch.setattr(validate, "find_shards", lambda _: shards)

    manifest = tmp_path / "pmids.txt.gz"
    with caplog.at_level(logging.WARNING, logger="pubmed2db.validate"):
        report = validate.run_validation(export_dir, online=False, manifest_out=manifest)

    assert report["status"] == "fail"
    assert not manifest.exists()
    # The report must not claim a manifest it did not write, or the next run's
    # operator goes looking for a file that is not there.
    assert report["inputs"]["manifest_written"] is None
    assert any("not writing the PMID manifest" in r.getMessage() for r in caplog.records)


def test_a_warning_still_writes_a_manifest(export_dir, loaded_con, monkeypatch, tmp_path):
    """WARN is not FAIL: the usual warning says nothing about the PMID set.

    "Entrez was unreachable" is a statement about the network, not about the
    shard read that built the set -- and refusing to write on it would mean a
    cluster with flaky egress never accumulates a baseline at all.
    """

    def dead(endpoint, params, **_):
        if endpoint == "efetch.fcgi":
            raise RuntimeError("eutils efetch.fcgi failed after 3 attempts")
        return _fake_eutils_factory(_EFETCH)(endpoint, params)

    monkeypatch.setattr(validate, "_eutils", dead)
    manifest = tmp_path / "pmids.txt.gz"
    report = validate.run_validation(
        export_dir, con=loaded_con, email="me@example.com", manifest_out=manifest
    )

    assert report["status"] == "warn"
    assert validate.read_manifest(manifest) == {1001, 1003}
    assert report["inputs"]["manifest_written"] == str(manifest)


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

    with pytest.raises(RuntimeError, match="after 1 attempt"):
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


def test_start_line_reports_api_key_state_without_the_key(export_dir, monkeypatch, caplog):
    """The one thing a run is silently misconfigured on is the API key.

    Reported once, up front, so a rate-limited run is diagnosable from the log
    rather than from the 3-vs-10 req/s pace. The key itself never appears.
    """
    monkeypatch.setattr(validate, "_eutils", _fake_eutils_factory(_EFETCH))
    with caplog.at_level("INFO", logger="pubmed2db.validate"):
        validate.run_validation(export_dir, api_key="sekrit", email="me@example.com")

    start = next(m for m in caplog.messages if m.startswith("starting validation"))
    assert "online with an NCBI API key" in start
    assert "sekrit" not in "\n".join(caplog.messages)

    caplog.clear()
    with caplog.at_level("INFO", logger="pubmed2db.validate"):
        validate.run_validation(export_dir, email="me@example.com")
    assert "online without an NCBI API key" in "\n".join(caplog.messages)


def test_progress_lines_and_peak_rss_are_logged(export_dir, monkeypatch, caplog):
    """Progress must report an ETA over the gzipped shards `export` writes by
    default, where the readable byte count (uncompressed) is not the one
    `st_size` measures -- the fixture is already in that form."""
    assert all(p.name.endswith(".gz") for p in export_dir.glob("pubmed_metadata_*"))

    monkeypatch.setattr(validate, "_PROGRESS_INTERVAL_S", 0.0)
    with caplog.at_level("INFO", logger="pubmed2db.validate"):
        report = validate.run_validation(export_dir, online=False)

    assert report["checks"]["structure"]["records_total"] == 2
    progress = [m for m in caplog.messages if m.startswith("progress:")]
    assert progress and "remaining" in progress[0]
    assert any(m.startswith("validation finished in") and "peak RSS" in m for m in caplog.messages)


def test_export_metadata_catches_a_changed_or_missing_shard(loaded_con, tmp_path):
    """metadata.json lists every shard with its SHA-256. Damage is gzip's to
    catch (shard-readable); the hash catches a shard that is intact but not the
    one this export wrote, even one with the right record count."""
    import gzip

    from pubmed2db.export import export_json

    out = tmp_path / "json"
    shards = export_json(loaded_con, out, shards=1)
    # The hashes come from the structure read; a second pass over every shard
    # would double validate's disk reads on the full corpus.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(validate, "file_sha256", lambda path: 1 / 0)
        report = validate.run_validation(out, online=False)
    check = next(c for c in report["checks_run"] if c["name"] == "export-metadata")
    assert check["status"] == "pass", check
    assert report["data_model_version"] == "1.0.0"

    original = shards[0].read_bytes()
    lines = gzip.decompress(original).splitlines(keepends=True)

    # Same records, one value changed: valid gzip, valid NDJSON, the right
    # count. Only the hash can tell this is not the shard the export wrote.
    shards[0].write_bytes(gzip.compress(b"".join(lines).replace(b"Revised title", b"Other title")))
    report = validate.run_validation(out, online=False)
    check = next(c for c in report["checks_run"] if c["name"] == "export-metadata")
    assert check["status"] == "fail"
    assert check["observed"] == "pubmed_metadata_0.ndjson.gz: checksum differs from metadata.json"

    # Rewritten with one record fewer: the count disagrees too.
    shards[0].write_bytes(gzip.compress(b"".join(lines[:-1])))
    report = validate.run_validation(out, online=False)
    assert report["status"] == "fail"
    check = next(c for c in report["checks_run"] if c["name"] == "export-metadata")
    assert "checksum differs" in check["observed"]
    assert "1 record(s) read, 2 exported" in check["observed"]

    # A hand-edited count is reported, not a crash.
    metadata = json.loads((out / "metadata.json").read_text())
    (out / "metadata.json").write_text(json.dumps({**metadata, "records": "2"}))
    report = validate.run_validation(out, online=False)
    check = next(c for c in report["checks_run"] if c["name"] == "export-metadata")
    assert check["status"] == "fail"
    assert "not an integer" in check["observed"]

    # No metadata.json: the export's version is unknown, even beside a 1.0
    # database, which the shards may predate.
    (out / "metadata.json").unlink()
    report = validate.run_validation(out, online=False, con=loaded_con)
    check = next(c for c in report["checks_run"] if c["name"] == "export-metadata")
    assert check["status"] == "skip"
    assert report["data_model_version"] is None


def test_report_records_absolute_paths(export_dir, tmp_path, monkeypatch):
    """06-validate.sbatch passes paths relative to SLURM_SUBMIT_DIR; the report
    must not, or archive_build.py run from elsewhere cannot match it to its export."""
    monkeypatch.chdir(export_dir.parent)
    report = validate.run_validation(
        Path(export_dir.name), online=False, manifest_out=Path("pmids.txt.gz")
    )
    assert report["inputs"]["export_dir"] == str(export_dir.resolve())
    assert report["inputs"]["manifest_written"] == str(
        (export_dir.parent / "pmids.txt.gz").resolve()
    )
