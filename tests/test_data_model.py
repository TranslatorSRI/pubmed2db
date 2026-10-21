"""The data model contract: its version, its shape, and what it promises.

`docs/data-model/` is the consumer-facing description; these tests are what
stops the database drifting away from it. A failure here usually means the
shape changed — which is fine, but only together with a version bump.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from pubmed2db import datamodel
from pubmed2db.datamodel import DATA_MODEL_VERSION, IncompatibleDatabase
from pubmed2db.db import connect

# --- version -----------------------------------------------------------------


def test_new_database_records_the_version_and_its_creator(con):
    meta = datamodel.metadata(con)
    assert meta["data_model_version"] == DATA_MODEL_VERSION
    assert meta["created_at"]
    assert meta["created_by"] == datamodel.code_version()
    assert "last_loaded_by" not in meta


def test_reopening_keeps_the_creation_stamp(tmp_path):
    path = tmp_path / "db.duckdb"
    first = connect(path)
    created = datamodel.metadata(first)["created_at"]
    first.execute(
        "UPDATE pubmed2db_metadata SET value = '2000-01-01T00:00:00+00:00' WHERE key = 'created_at'"
    )
    first.close()
    second = connect(path)
    try:
        assert datamodel.metadata(second)["created_at"] == "2000-01-01T00:00:00+00:00" != created
    finally:
        second.close()


def test_load_records_which_code_loaded(con, gz_fixture):
    from pubmed2db.load import load_files

    load_files(con, [(gz_fixture("pubmed25n0001"), "baseline")])
    meta = datamodel.metadata(con)
    assert meta["last_loaded_by"] == datamodel.code_version()
    assert meta["last_loaded_at"]


def _old_database(path, *statements):
    raw = duckdb.connect(str(path))
    for statement in statements:
        raw.execute(statement)
    raw.close()


def test_a_pre_1_0_database_is_refused_and_left_untouched(tmp_path):
    """Every database built before 1.0 lacks the metadata table. 1.0 renamed and
    dropped columns, which `CREATE TABLE IF NOT EXISTS` cannot do, so opening one
    must fail loudly rather than half-migrate it."""
    path = tmp_path / "old.duckdb"
    _old_database(path, "CREATE TABLE grant_ (pmid BIGINT, source_file TEXT)")

    with pytest.raises(IncompatibleDatabase, match="predates pubmed2db data model"):
        connect(path)

    raw = duckdb.connect(str(path))
    try:
        assert [r[0] for r in raw.execute("SHOW TABLES").fetchall()] == ["grant_"]
    finally:
        raw.close()


@pytest.mark.parametrize(
    ("stored", "message"),
    [("0.9.0", "rebuild the database"), ("2.0.0", "rebuild the database"),
     ("1.99.0", "newer than"), ("one", "unrecognized")],
)
def test_an_incompatible_version_is_refused(tmp_path, stored, message):
    path = tmp_path / "db.duckdb"
    connect(path).close()
    raw = duckdb.connect(str(path))
    raw.execute(
        "UPDATE pubmed2db_metadata SET value = ? WHERE key = 'data_model_version'", [stored]
    )
    raw.close()

    with pytest.raises(IncompatibleDatabase, match=message):
        connect(path)


def test_an_older_minor_version_is_raised_to_ours(tmp_path, monkeypatch):
    """The additive case: schema.sql adds the minor version's columns, and the
    stored version follows. Simulated by running code one minor ahead."""
    path = tmp_path / "db.duckdb"
    connect(path).close()
    major, minor, _ = datamodel.parse_version(DATA_MODEL_VERSION)
    newer = f"{major}.{minor + 1}.0"
    monkeypatch.setattr(datamodel, "DATA_MODEL_VERSION", newer)

    con = connect(path)
    try:
        assert datamodel.stored_version(con) == newer
    finally:
        con.close()


def test_a_migration_missing_a_column_is_refused_and_not_stamped(tmp_path, monkeypatch):
    """A minor version whose CREATE TABLE gained a column without the matching
    ALTER TABLE leaves a migrated database short of it; `load` inserts by
    position, so opening it must fail before the version is raised. Simulated
    by code one minor ahead and a table whose columns differ from schema.sql's
    (dropping one instead trips schema.sql's own COMMENT ON first)."""
    path = tmp_path / "db.duckdb"
    connect(path).close()
    raw = duckdb.connect(str(path))
    raw.execute("ALTER TABLE source_file ADD COLUMN stray INTEGER")
    raw.close()
    major, minor, _ = datamodel.parse_version(DATA_MODEL_VERSION)
    monkeypatch.setattr(datamodel, "DATA_MODEL_VERSION", f"{major}.{minor + 1}.0")

    with pytest.raises(IncompatibleDatabase, match="source_file"):
        connect(path)
    raw = duckdb.connect(str(path))
    try:
        assert datamodel.stored_version(raw) == DATA_MODEL_VERSION
    finally:
        raw.close()


def test_a_read_only_open_writes_nothing(tmp_path):
    """`validate` opens read-only, so it works on a read-only mount and never
    rewrites a database's views or version."""
    path = tmp_path / "db.duckdb"
    connect(path).close()
    path.chmod(0o444)
    try:
        con = connect(path, read_only=True)
        try:
            assert datamodel.stored_version(con) == DATA_MODEL_VERSION
        finally:
            con.close()
    finally:
        path.chmod(0o644)


def test_cli_explains_an_incompatible_database(tmp_path):
    from click.testing import CliRunner

    from pubmed2db.cli import main

    path = tmp_path / "old.duckdb"
    _old_database(path, "CREATE TABLE article (pmid BIGINT)")
    result = CliRunner().invoke(main, ["--db", str(path), "status"])
    assert result.exit_code == 1
    assert "predates pubmed2db data model" in result.output
    assert "Traceback" not in result.output


def test_code_version_ignores_a_repository_it_is_not_in(tmp_path, monkeypatch):
    """An installed copy inside someone else's checkout must not report that
    checkout's commit as ours."""
    from pubmed2db import __version__

    fake_module = tmp_path / "site-packages" / "pubmed2db" / "datamodel.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.touch()
    monkeypatch.setattr(datamodel, "__file__", str(fake_module))
    assert datamodel.code_version() == __version__


# --- documented semantics, on the edge-case fixture ----------------------------


@pytest.fixture
def edge_con(loaded_con, gz_fixture):
    """`loaded_con` plus pubmed25n0005, whose header lists the cases it covers."""
    from pubmed2db.load import load_file

    load_file(loaded_con, gz_fixture("pubmed25n0005"), kind="update")
    return loaded_con


def _rows(con, sql):
    return con.execute(sql).fetchall()


def test_author_position_is_the_0_based_xml_index_with_gaps(edge_con):
    """The second <Author> has no fore name, so upstream drops it. Its slot
    stays empty rather than the third author moving up, so a position always
    means the same <Author> element."""
    assert _rows(edge_con, "SELECT position, name FROM author WHERE pmid = 2001 ORDER BY 1") == [
        (0, "Ada First"),
        (2, "Cy Third"),
    ]


def test_affiliations_keep_their_order_and_join_their_author(edge_con):
    assert _rows(
        edge_con,
        "SELECT a.name, af.position, af.affiliation FROM author_affiliation af "
        "JOIN author a ON a.pmid = af.pmid AND a.source_file = af.source_file "
        "AND a.position = af.author_position WHERE af.pmid = 2001 ORDER BY af.position",
    ) == [("Cy Third", 0, "Primary Institute."), ("Cy Third", 1, "Secondary Institute.")]


def test_a_repeated_descriptor_keeps_each_heading_its_own_qualifiers(edge_con):
    """`Humans` heads two headings with different qualifiers. Joined on the
    descriptor UI, each qualifier would attach to both."""
    assert _rows(
        edge_con,
        "SELECT h.position, h.descriptor_name, q.qualifier_name FROM mesh_heading h "
        "LEFT JOIN mesh_qualifier q ON q.pmid = h.pmid AND q.source_file = h.source_file "
        "AND q.heading_position = h.position WHERE h.pmid = 2001 ORDER BY h.position",
    ) == [(0, "Humans", "genetics"), (1, "Animals", None), (2, "Humans", "metabolism")]


def test_an_article_deleted_by_its_own_file_is_not_latest(edge_con):
    assert _rows(edge_con, "SELECT count(*) FROM article WHERE pmid = 2002") == [(1,)]
    assert _rows(edge_con, "SELECT count(*) FROM latest_article WHERE pmid = 2002") == [(0,)]


def test_a_pmid_reinstated_after_its_deletion_is_latest_again(edge_con):
    assert _rows(edge_con, "SELECT article_title FROM latest_article WHERE pmid = 1002") == [
        ("Reinstated after its deletion.",)
    ]


def test_timestamps_are_instants_not_wall_clock_times(con, gz_fixture):
    """Before 1.0 these were naive TIMESTAMPs holding the loading machine's
    local time, mixed with Python's UTC: unreadable without knowing the node's
    time zone. Now each is an aware instant, comparable from anywhere."""
    from datetime import UTC, datetime, timedelta

    from pubmed2db.load import load_file

    load_file(con, gz_fixture("pubmed25n0001"), kind="baseline")
    downloaded, processed, loaded = con.execute(
        "SELECT s.downloaded_at, s.processed_at, max(a.loaded_at) FROM source_file s "
        "JOIN article a ON a.source_file = s.file_name GROUP BY ALL"
    ).fetchone()
    now = datetime.now(UTC)
    for value in (downloaded, processed, loaded):
        assert value.tzinfo is not None
        assert abs(now - value) < timedelta(minutes=5)


def test_each_child_table_has_a_latest_view_matching_the_export(edge_con):
    """The views are the consumer's version of the exports' snapshot filter, so
    they must select exactly the rows the Parquet export ships."""
    from pubmed2db.export import _VERSIONED_CHILDREN

    for table in _VERSIONED_CHILDREN:
        assert _rows(
            edge_con,
            f"(SELECT * FROM latest_{table} EXCEPT ALL SELECT c.* FROM {table} c "
            "WHERE EXISTS (SELECT 1 FROM latest_article la "
            "WHERE la.pmid = c.pmid AND la.source_file = c.source_file)) "
            f"UNION ALL (SELECT c.* FROM {table} c WHERE EXISTS (SELECT 1 FROM latest_article la "
            "WHERE la.pmid = c.pmid AND la.source_file = c.source_file) "
            f"EXCEPT ALL SELECT * FROM latest_{table})",
        ) == [], table
    # PMID 1001's superseded baseline authors are excluded; its update's kept.
    assert _rows(edge_con, "SELECT name FROM latest_author WHERE pmid = 1001") == [("Jane Smith",)]
    assert _rows(edge_con, "SELECT count(*) FROM latest_author WHERE pmid = 2002") == [(0,)]


# --- the pinned shape ----------------------------------------------------------

GOLDEN_DIR = Path(__file__).parent / "data_model"

_BUMP_HELP = (
    "The data model's shape changed. If that was deliberate, bump "
    "datamodel.DATA_MODEL_VERSION (minor if this only adds, major otherwise; "
    "docs/data-model/README.md says which), then write tests/data_model/<MAJOR.MINOR>.json "
    "with `uv run python -m pubmed2db.datamodel --golden tests/data_model/<MAJOR.MINOR>.json` "
    "and regenerate the doc. Never overwrite an existing version's file."
)


def _golden_files() -> list[tuple[tuple[int, int], Path]]:
    return sorted(
        (datamodel.parse_version(path.stem), path) for path in GOLDEN_DIR.glob("*.json")
    )


def test_a_fresh_database_has_exactly_this_versions_shape(con):
    golden = json.loads((GOLDEN_DIR / f"{datamodel.shape_version(DATA_MODEL_VERSION)}.json").read_text())
    assert datamodel.shape(con) == golden, _BUMP_HELP


def test_the_newest_golden_file_is_this_version():
    """A golden file for an unreleased version, or a minor bump without one,
    both mean the version and the shape have come apart. One file per
    MAJOR.MINOR: those are the levels that describe the shape."""
    assert _golden_files()[-1][1].stem == datamodel.shape_version(DATA_MODEL_VERSION)


def test_minor_versions_only_add():
    """Mechanically, what a minor bump promises consumers: nothing they read
    goes away or moves. Between majors anything may change."""
    files = _golden_files()
    for (old_version, old_path), (new_version, new_path) in zip(files, files[1:]):
        if old_version[0] == new_version[0]:
            old = json.loads(old_path.read_text())
            new = json.loads(new_path.read_text())
            assert datamodel.only_adds(old, new) == [], f"{old_path.stem} -> {new_path.stem}"


def test_only_adds_catches_what_a_minor_version_must_not_do():
    base = {
        "relations": {
            "t": {"kind": "table", "tier": "stable", "columns": ["a INTEGER", "b VARCHAR"]},
            "v": {"kind": "view", "tier": "stable", "columns": ["a INTEGER"]},
        },
        "ndjson_fields": ["id", "title"],
    }

    def changed(**edits):
        new = json.loads(json.dumps(base))
        for path, value in edits.items():
            relation, key = path.split("__")
            new["relations"][relation][key] = value
        return new

    grown = changed(t__columns=["a INTEGER", "b VARCHAR", "c DATE"], v__columns=["z INTEGER", "a INTEGER"])
    grown["relations"]["new_table"] = {"kind": "table", "tier": "stable", "columns": []}
    grown["ndjson_fields"].append("extra")
    assert datamodel.only_adds(base, grown) == []

    assert datamodel.only_adds(base, changed(t__columns=["a INTEGER", "c DATE", "b VARCHAR"]))
    assert datamodel.only_adds(base, changed(t__columns=["a BIGINT", "b VARCHAR"]))
    assert datamodel.only_adds(base, changed(v__columns=[]))
    assert datamodel.only_adds(base, changed(t__tier="internal"))
    assert datamodel.only_adds(base, {**base, "ndjson_fields": ["id"]})
    dropped = json.loads(json.dumps(base))
    del dropped["relations"]["v"]
    assert datamodel.only_adds(base, dropped) == ["v was removed"]


def test_every_relation_declares_its_tier_and_every_contract_column_its_meaning(con):
    """The comments are the data dictionary shipped inside the file, so a
    column without one is undocumented to anyone holding only the database."""
    for relation in datamodel.describe(con):
        assert relation["tier"] in datamodel.TIERS, relation["name"]
        if relation["tier"] == "internal" or relation["kind"] == "view":
            continue  # views say "Columns as <table>"
        for column in relation["columns"]:
            assert column["comment"].strip(), f"{relation['name']}.{column['name']}"


def test_every_latest_view_names_the_table_it_filters(con):
    from pubmed2db.export import _VERSIONED_CHILDREN

    views = {r["name"]: r for r in datamodel.describe(con) if r["kind"] == "view"}
    assert set(views) == {"latest_article"} | {f"latest_{t}" for t in _VERSIONED_CHILDREN}
    for table in _VERSIONED_CHILDREN:
        assert f"Columns as {table}." in views[f"latest_{table}"]["comment"]


# --- invariants the documentation promises ------------------------------------------

_CHILDREN_SQL = "SELECT table_name FROM duckdb_columns() WHERE column_name = 'source_file' " \
    "AND table_name NOT IN ('article', 'deleted_pmid') AND table_name NOT LIKE 'latest_%' " \
    "GROUP BY 1"


def test_pmid_and_source_file_identify_an_article_version(edge_con):
    assert _rows(
        edge_con, "SELECT pmid, source_file FROM article GROUP BY ALL HAVING count(*) > 1"
    ) == []


def test_every_child_row_belongs_to_a_stored_article_version(edge_con):
    children = [r[0] for r in _rows(edge_con, _CHILDREN_SQL)]
    assert len(children) == 9
    for table in children:
        assert _rows(
            edge_con,
            f"SELECT count(*) FROM {table} c ANTI JOIN article a "
            "ON a.pmid = c.pmid AND a.source_file = c.source_file",
        ) == [(0,)], table


def test_positions_are_0_based_and_unique_within_their_parent(edge_con):
    for table, parent in (
        ("abstract_text", ""),
        ("author", ""),
        ("author_affiliation", ", author_position"),
        ("mesh_heading", ""),
        ("publication_type", ""),
    ):
        assert _rows(
            edge_con,
            f"SELECT min(position) >= 0, count(*) = count(DISTINCT (pmid, source_file{parent}, position)) "
            f"FROM {table}",
        ) == [(True, True)], table
    # Gap-free where the docs say so; author.position may have gaps by design.
    for table in ("abstract_text", "mesh_heading", "publication_type"):
        assert _rows(
            edge_con,
            f"SELECT count(*) FROM (SELECT max(position) + 1 = count(*) AS ok FROM {table} "
            "GROUP BY pmid, source_file) WHERE NOT ok",
        ) == [(0,)], table


@pytest.mark.parametrize(
    ("table", "column", "domain"),
    [
        ("source_file", "kind", {"baseline", "update"}),
        ("author", "kind", {"author", "collective"}),
        (
            "history",
            "pub_status",
            {"received", "accepted", "revised", "aheadofprint", "ecollection", "pubmed",
             "medline", "entrez", "pmc-release", "retracted"},
        ),
    ],
)
def test_closed_vocabularies_hold(edge_con, table, column, domain):
    """The columns the docs give a closed list of values for (`journal_issn`'s is
    in test_journals). `article_id.id_type` is deliberately absent: it is
    documented as open."""
    values = {r[0] for r in _rows(edge_con, f"SELECT DISTINCT {column} FROM {table}")}
    assert values and values <= domain
    assert _rows(edge_con, f"SELECT count(*) FROM {table} WHERE {column} IS NULL") == [(0,)]


# --- docs/data-model/ -------------------------------------------------------------

DOCS = Path(__file__).parent.parent / "docs" / "data-model"
DOC = DOCS / "README.md"
CHANGELOG = DOCS / "Changelog.md"
REFERENCE = DOCS / "Reference.md"


def _doc_section(heading: str) -> str:
    text = DOC.read_text()
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else None]


def test_the_docs_column_reference_is_generated_from_the_database(con):
    """The reference half of the doc is the database's own comments, rendered.
    Hand-editing it, or changing a comment without regenerating, fails here."""
    expected = datamodel.render_reference(datamodel.describe(con))
    assert expected in REFERENCE.read_text(), (
        "docs/data-model/Reference.md is stale: run "
        "`uv run python -m pubmed2db.datamodel --docs docs/data-model/Reference.md`"
    )


def test_the_doc_names_only_columns_that_exist(con):
    """Prose like `author.position` must name a real column. The changelog,
    a separate file, is exempt: naming what no longer exists is its job."""
    import re

    columns = {
        (r["name"], c["name"]) for r in datamodel.describe(con) for c in r["columns"]
    }
    relations = {r["name"] for r in datamodel.describe(con)}
    named = set(re.findall(r"`([a-z_]+)\.([a-z_]+)`", DOC.read_text()))
    unknown = {pair for pair in named if pair[0] in relations and pair not in columns}
    assert not unknown


def test_the_doc_describes_every_ndjson_field_in_order():
    import re

    from pubmed2db.export import JSON_FIELDS

    rows = [
        line for line in _doc_section("The NDJSON record").splitlines()
        if line.startswith("| `")
    ]
    documented = [
        name for row in rows for name in re.findall(r"`([a-z_]+)`", row.split("|")[1])
    ]
    assert documented == list(JSON_FIELDS)


def test_every_recipe_in_the_doc_runs(edge_con, tmp_path, monkeypatch):
    """The recipes are written for someone holding only a build's files, so run
    them that way: attach the database read-only, with the Parquet export beside
    it, from an empty session."""
    import re

    from pubmed2db.export import export_parquet

    export_parquet(edge_con, tmp_path / "parquet")
    db_path = Path(edge_con.execute("SELECT path FROM duckdb_databases() WHERE NOT internal").fetchone()[0])
    edge_con.close()
    (tmp_path / "pubmed.duckdb").write_bytes(db_path.read_bytes())
    monkeypatch.chdir(tmp_path)

    blocks = re.findall(r"```sql\n(.*?)```", _doc_section("Recipes"), re.S)
    assert len(blocks) >= 8
    session = duckdb.connect()
    try:
        for block in blocks:
            for statement in filter(str.strip, block.split(";")):
                session.execute(statement)
    finally:
        session.close()


def test_the_patch_level_is_unused():
    """The version is the contract, so a value fix does not bump it: it goes in
    the code's release notes. Without this, a 1.0.1 for a value fix, with its
    `## 1.0.1` changelog entry, would pass every other test here."""
    assert datamodel.parse_version(DATA_MODEL_VERSION)[2] == 0, (
        "the data model's patch level is unused (docs/data-model/README.md, "
        "\"Versioning and stability\"): list a value fix in the release notes, "
        "and bump the minor or major version only for a change to the contract"
    )
    import re

    headings = re.findall(r"^## (\d+\.\d+\.\d+)$", CHANGELOG.read_text(), re.M)
    assert headings and all(heading.split(".")[2] == "0" for heading in headings)


def test_patch_levels_neither_refuse_nor_downgrade(tmp_path, monkeypatch):
    """The patch level is unused (test_the_patch_level_is_unused), so it must
    never decide whether a database opens: code a patch behind or ahead opens
    it, and the stored version only ever rises."""
    path = tmp_path / "db.duckdb"
    connect(path).close()
    major, minor, patch = datamodel.parse_version(DATA_MODEL_VERSION)

    monkeypatch.setattr(datamodel, "DATA_MODEL_VERSION", f"{major}.{minor}.{patch + 1}")
    connect(path).close()
    monkeypatch.setattr(datamodel, "DATA_MODEL_VERSION", f"{major}.{minor}.{patch}")
    con = connect(path)
    try:
        assert datamodel.stored_version(con) == f"{major}.{minor}.{patch + 1}"
    finally:
        con.close()


def test_the_changelog_has_an_entry_for_this_version():
    """Every bump tells consumers what changed and what each output's
    consumers must do; a version with no entry has told them nothing."""
    assert f"\n## {DATA_MODEL_VERSION}\n" in CHANGELOG.read_text()


def test_links_between_the_data_model_docs_resolve():
    """The three files link each other by absolute URL on `main`, so the doc
    reads the same without the repository. GitHub cannot check those links
    before the branch merges; this does, against the files and their headings."""
    import re

    base = "https://github.com/TranslatorSRI/pubmed2db/blob/main/docs/data-model/"

    def anchors(path: Path) -> set[str]:
        # GitHub's slug: lower-case, drop punctuation but `-` and `_`, spaces to `-`.
        return {
            re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
            for heading in re.findall(r"^#+ (.+)$", path.read_text(), re.M)
        }

    broken = []
    for source in (DOC, CHANGELOG, REFERENCE):
        for name, anchor in re.findall(re.escape(base) + r"([\w.]+)(?:#([\w-]+))?", source.read_text()):
            target = DOCS / name
            if not target.is_file() or (anchor and anchor not in anchors(target)):
                broken.append(f"{source.name}: {name}#{anchor}")
    assert not broken
