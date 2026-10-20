# pubmed2db data model changelog

What changed in each version of the [pubmed2db data model](https://github.com/TranslatorSRI/pubmed2db/blob/main/docs/data-model/README.md),
newest first. Each entry says, per output, what a consumer has to do. The
version scheme is in
[Versioning and stability](https://github.com/TranslatorSRI/pubmed2db/blob/main/docs/data-model/README.md#versioning-and-stability).

## 1.0.0

The first versioned data model, first built after `2026sep26`. The
`2026aug21` and `2026sep26` builds predate it.

| Output | What a consumer must do |
| --- | --- |
| NDJSON | Match PMC IDs in `identifiers` as `PMC:PMC…`, the prefix Babel uses, where earlier builds used `PMCID:`. Every other field is unchanged. `metadata.json` is new beside the shards, and worth checking downloads against. |
| Parquet | Update table and column names as below. `pubmed2db_metadata.parquet` and `metadata.json` are new. |
| DuckDB | Rebuild: pubmed2db refuses a pre-1.0 database. Update queries as below. |

| Pre-1.0 | 1.0 |
| --- | --- |
| no version anywhere | `pubmed2db_metadata`, `metadata.json`, and `data_model_version` in the validation report |
| `grant_` | `article_grant` |
| `abstract_text.seq` | `abstract_text.position` |
| `author.position` 1-based | 0-based, still with gaps |
| `author_affiliation.position` (the author's) | `author_position`, plus the affiliation's own `position` |
| `mesh_heading` unordered; qualifiers join on `descriptor_ui` | `mesh_heading.position`, `mesh_qualifier.heading_position` |
| `publication_type (…, type_ui, type_name, position)` | `position` moved next to `source_file` |
| `journal.abbreviation_iso` | dropped (equal to `abbreviation_medline` for every journal) |
| `author_affiliation.ror` | dropped (always NULL) |
| naive `TIMESTAMP`s in the loading machine's local time | `TIMESTAMPTZ` |
| — | `source_file.published_at`, and the `latest_<table>` views |
