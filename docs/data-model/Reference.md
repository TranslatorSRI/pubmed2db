# pubmed2db data model: column reference

Every table, column and view of the [pubmed2db data model](https://github.com/TranslatorSRI/pubmed2db/blob/main/docs/data-model/README.md),
generated from the comments shipped inside the database. Regenerate after
changing `schema.sql` with
`python -m pubmed2db.datamodel --docs docs/data-model/Reference.md`.

<!-- BEGIN GENERATED REFERENCE: python -m pubmed2db.datamodel --docs docs/data-model/Reference.md -->

## `abstract_text`

Stable. Per version: one row per non-empty Abstract/AbstractText section, in order. A structured abstract has several; most have one. OtherAbstract and CopyrightInformation are not stored.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `position` | INTEGER | 0-based order of the section among the stored (non-empty) ones, so without gaps. |
| `label` | VARCHAR | The section's @Label as the publisher wrote it, e.g. 'BACKGROUND'. NULL for an unstructured abstract. |
| `nlm_category` | VARCHAR | NLM's normalization of the label (@NlmCategory): BACKGROUND, OBJECTIVE, METHODS, RESULTS, CONCLUSIONS or UNASSIGNED. NULL when absent. |
| `text` | VARCHAR | The section's text, inline markup flattened. |

## `article`

Stable. One row per version of a PubMed article: each file that carries a PMID adds a version. (pmid, source_file) is unique, by construction rather than by constraint. Use latest_article for the current version of each PMID. Text is verbatim from the XML: not trimmed, inline markup flattened to its text, NULL where the element is absent.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | PubMed ID (MedlineCitation/PMID). |
| `pmid_version` | INTEGER | MedlineCitation/PMID/@Version: 1 for almost every record, higher only for PubMed's rare versioned citations. Not what orders versions here; file_order_key is. |
| `source_file` | VARCHAR | The file this version came from; joins source_file.file_name. With pmid, identifies the version, and every per-version child table joins on the pair. |
| `file_order_key` | BIGINT | source_file.file_order_key of that file, copied here so latest_article can rank versions without a join. |
| `article_title` | VARCHAR | Article/ArticleTitle. An empty element is stored as an empty string. PubMed brackets translated titles: [Title in English]. |
| `nlm_catalog_id` | VARCHAR | MedlineJournalInfo/NlmUniqueID; joins journal.nlm_catalog_id. |
| `issn_linking` | VARCHAR | MedlineJournalInfo/ISSNLinking: the linking ISSN that groups a journal's print and electronic ISSNs. |
| `volume` | VARCHAR | Journal/JournalIssue/Volume. |
| `issue` | VARCHAR | Journal/JournalIssue/Issue. |
| `pub_year` | VARCHAR | JournalIssue/PubDate/Year. NULL when the date is a free-text medline_date instead. |
| `pub_month` | VARCHAR | JournalIssue/PubDate/Month or, when the date is approximate, PubDate/Season (the DTD makes them exclusive): 'Mar', '03', 'Spring' and 'Sep-Dec' all occur. |
| `pub_day` | VARCHAR | JournalIssue/PubDate/Day. |
| `medline_date` | VARCHAR | JournalIssue/PubDate/MedlineDate: a free-text date used instead of Year/Month/Day, e.g. '1998 Spring', '1978 Jul-Aug', '1998 Dec-1999 Jan'. |
| `date_completed` | DATE | MedlineCitation/DateCompleted: when NLM finished indexing the record. A missing month or day reads as the 1st. |
| `date_revised` | DATE | MedlineCitation/DateRevised: when NLM last revised the record. A missing month or day reads as the 1st. |
| `loaded_at` | TIMESTAMP WITH TIME ZONE | When this version was loaded into the database. |

## `article_grant`

Stable. Per version: one row per GrantList/Grant, the funding the article acknowledges. Unordered.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `grant_id` | VARCHAR | GrantID, the award number as written, e.g. R01 GM123456. NULL when absent. |
| `acronym` | VARCHAR | Acronym: the funding institute's two-letter code, e.g. GM. NULL when absent. |
| `agency` | VARCHAR | Agency, e.g. NIGMS NIH HHS. |
| `country` | VARCHAR | Country of the agency, e.g. United States. |

## `article_id`

Stable. Per version: the article's own identifiers, from PubmedData/ArticleIdList (never those of the references it cites). The pubmed entry is omitted, since it repeats pmid. Unordered.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `id_type` | VARCHAR | ArticleId/@IdType as PubMed writes it: doi, pmc, pii, mid and others. An open set: a new type appearing is not a data model change. |
| `id_value` | VARCHAR | The identifier, whitespace-trimmed, with its published case (PubMed is not consistent; DOIs are case-insensitive). A pmc value already starts with PMC. |

## `author`

Stable. Per version: one row per AuthorList/Author, a person or a collective (group) author. Authors the parser cannot name are dropped and leave a gap in position.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `position` | INTEGER | 0-based index of the Author element in its AuthorList. A gap is an author the parser could not name (no last name, or no fore name or initials) and who has no ORCID: such authors are not stored. |
| `kind` | VARCHAR | 'author' for a person, 'collective' for a CollectiveName. |
| `name` | VARCHAR | A person's 'ForeName LastName' (Initials when there is no fore name; Suffix dropped), or a collective's name without its trailing period. NULL for a person identified only by ORCID. |
| `orcid` | VARCHAR | Bare ORCID iD (0000-0002-1825-0097) from Identifier[@Source=ORCID], URL prefix removed; the checksum is not validated. NULL for collectives and authors without one. |
| `valid` | BOOLEAN | @ValidYN: false for a name PubMed marks as erroneous, true otherwise (including when the attribute is absent). NULL for collectives. |

## `author_affiliation`

Stable. Per version: one row per non-empty AffiliationInfo/Affiliation of a person author. Collective authors' affiliations are not stored, nor are affiliation identifiers.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `author_position` | INTEGER | The author: joins author.position (with pmid and source_file). |
| `position` | INTEGER | 0-based order among this author's affiliations. |
| `affiliation` | VARCHAR | The affiliation as free text. |

## `history`

Stable. Per version: one row per PubmedData/History/PubMedPubDate, the record's dated life events (received, accepted, entered PubMed, ...). Unordered.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `pub_status` | VARCHAR | @PubStatus: received, accepted, revised, aheadofprint, ecollection, pubmed, medline, entrez, pmc-release or retracted. Entries with any other status are not stored. |
| `date` | DATE | The event's date. A missing month or day reads as the 1st; Hour and Minute are dropped. |

## `journal`

Stable. One row per journal in NLM's J_Entrez.txt, with publication years from NLM's serial catalog. Not versioned: pubmed2db journals replaces the table wholesale. Join to article on nlm_catalog_id.

| Column | Type | Meaning |
| --- | --- | --- |
| `nlm_catalog_id` | VARCHAR | NLM Catalog ID (NlmId in J_Entrez.txt). |
| `title` | VARCHAR | JournalTitle from J_Entrez.txt: the same string as NCBI esummary's fulljournalname. |
| `abbreviation_medline` | VARCHAR | MedAbbr from J_Entrez.txt, the MEDLINE title abbreviation. J_Entrez's ISO abbreviation equalled it for every journal measured, so it is not stored separately. |
| `start_year` | INTEGER | PublicationFirstYear from NLM's serial catalog. NULL when it is not a four-digit year (MARC wildcards like 19uu) or the catalog lacks the journal. |
| `end_year` | INTEGER | PublicationEndYear from NLM's serial catalog. NULL for a journal still publishing (9999), an unknown or wildcard year, or one the catalog lacks. |
| `active` | BOOLEAN | Whether the journal is still publishing: true for end year 9999; false for a ceased journal, including partly known years like 19uu; NULL when unknown (uuuu) or not in the catalog. |

## `journal_issn`

Stable. The print and electronic ISSNs J_Entrez.txt lists for each journal. Replaced with journal.

| Column | Type | Meaning |
| --- | --- | --- |
| `nlm_catalog_id` | VARCHAR | The journal; joins journal.nlm_catalog_id. |
| `issn` | VARCHAR | The ISSN, e.g. 0028-0836. |
| `issn_type` | VARCHAR | 'Print' (J_Entrez's ISSN (Print)) or 'Electronic' (ISSN (Online)). |

## `mesh_heading`

Stable. Per version: one row per MeshHeadingList/MeshHeading, the MeSH descriptors NLM indexed the article with. Records not yet indexed have none.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `position` | INTEGER | 0-based order of the heading. A descriptor can repeat with different qualifiers, so this, not descriptor_ui, is what mesh_qualifier.heading_position joins. |
| `descriptor_ui` | VARCHAR | DescriptorName/@UI, the MeSH descriptor ID, e.g. D006801. |
| `descriptor_name` | VARCHAR | DescriptorName text, e.g. Humans. |
| `major_topic` | BOOLEAN | The descriptor's own @MajorTopicYN. A heading that is major only through a qualifier is false here; see mesh_qualifier.major_topic. |

## `mesh_qualifier`

Stable. Per version: one row per QualifierName (subheading) under a MeSH heading.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `heading_position` | INTEGER | The heading: joins mesh_heading.position (with pmid and source_file). |
| `descriptor_ui` | VARCHAR | The heading's descriptor UI, repeated for convenience. Not a join key on its own: a descriptor can head more than one heading. |
| `qualifier_ui` | VARCHAR | QualifierName/@UI, the MeSH qualifier ID, e.g. Q000235. |
| `qualifier_name` | VARCHAR | QualifierName text, e.g. genetics. |
| `major_topic` | BOOLEAN | The qualifier's @MajorTopicYN. |

## `publication_type`

Stable. Per version: one row per Article/PublicationTypeList/PublicationType (Journal Article, Review, Retracted Publication, ...).

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | With source_file, the article version; joins article. |
| `source_file` | VARCHAR | With pmid, the article version; joins article. |
| `position` | INTEGER | 0-based, in PubMed's order, which is not alphabetical and matches NCBI esummary's pubtype list. |
| `type_ui` | VARCHAR | The publication type's MeSH UI (@UI), e.g. D016454. |
| `type_name` | VARCHAR | Its name, e.g. Review. An empty string if the element is empty. |

## `deleted_pmid`

Provenance. One row per PMID listed in a file's DeleteCitation, whether or not that PMID was ever loaded. latest_article hides a PMID whose last deletion is in the same file as its latest version or a later one.

| Column | Type | Meaning |
| --- | --- | --- |
| `pmid` | BIGINT | The deleted PubMed ID. |
| `source_file` | VARCHAR | The file that deleted it; joins source_file.file_name. |
| `file_order_key` | BIGINT | That file's file_order_key, compared against article versions' to decide which came last. |

## `pubmed2db_metadata`

Provenance. Facts about this database as a whole, one per row: data_model_version (check its major version before reading anything else), created_at and created_by (the pubmed2db commit, as git describe), last_loaded_at and last_loaded_by. Timestamps are ISO 8601 in UTC.

| Column | Type | Meaning |
| --- | --- | --- |
| `key` | VARCHAR | The fact's name, e.g. data_model_version. |
| `value` | VARCHAR | Its value, as text. |

## `source_file`

Provenance. One row per PubMed XML file known to this database, loaded or not. The loaded file with the highest file_order_key is the build's cutoff: the next file belongs to the next build.

| Column | Type | Meaning |
| --- | --- | --- |
| `file_name` | VARCHAR | PubMed's file name, e.g. pubmed26n1636.xml.gz. Every per-version row's source_file joins here. |
| `kind` | VARCHAR | 'baseline' (the annual full re-issue) or 'update' (a daily updatefile). |
| `year_yy` | INTEGER | Two-digit release year from the file name: 26 for the 2026 baseline and the updatefiles that follow it. |
| `file_number` | INTEGER | Sequence number from the file name: 1636 in pubmed26n1636.xml.gz. Updatefiles continue numbering after the baseline. |
| `file_order_key` | BIGINT | year_yy * 1000000 + file_number: PubMed's chronological order as one integer. A row from a higher key supersedes one from a lower key. |
| `published_md5` | VARCHAR | Lower-case MD5 from NCBI's .md5 sidecar. NULL for a file that was loaded without ever being synced by pubmed2db download. |
| `published_at` | TIMESTAMP WITH TIME ZONE | When NCBI posted the file: the Last-Modified time of its .md5 sidecar. NULL for a file never synced by pubmed2db download. |
| `downloaded_at` | TIMESTAMP WITH TIME ZONE | When pubmed2db download last fetched a new or changed copy. A file loaded without being downloaded is stamped with its first load time. |
| `processed_at` | TIMESTAMP WITH TIME ZONE | When the file's rows were last loaded. NULL means downloaded but never loaded: none of its rows are in this database. |
| `n_articles` | INTEGER | PubmedArticle records stored from the file. A PMID repeated within one file is stored once (the last occurrence) and counted once. |
| `n_deletions` | INTEGER | PMIDs listed in the file's DeleteCitation element. |
| `n_failed` | INTEGER | PubmedArticle records the parser rejected and did not store (a missing title, journal ID or other required element). |
| `n_book_records` | INTEGER | PubmedBookArticle records (Bookshelf citations) in the file. They are counted but not parsed: no table holds them. |

## `pipeline_run`

Internal. When pipeline steps that leave no other trace last ran (currently only 'journals'). Not part of the versioned data model; may change in any release.

## Views

| View | Meaning |
| --- | --- |
| `latest_abstract_text` | Stable. The abstract_text rows of each PMID's latest_article version. Columns as abstract_text. |
| `latest_article` | Stable. The current version of every PMID: its article row from the highest file_order_key, unless a deletion in that file or a later one removed it. A PMID re-added after a deletion is current again. Columns as article. Computed on every read (a window over all of article); materialize it for repeated use. |
| `latest_article_grant` | Stable. The article_grant rows of each PMID's latest_article version. Columns as article_grant. |
| `latest_article_id` | Stable. The article_id rows of each PMID's latest_article version. Columns as article_id. |
| `latest_author` | Stable. The author rows of each PMID's latest_article version. Columns as author. |
| `latest_author_affiliation` | Stable. The author_affiliation rows of each PMID's latest_article version. Columns as author_affiliation. |
| `latest_history` | Stable. The history rows of each PMID's latest_article version. Columns as history. |
| `latest_mesh_heading` | Stable. The mesh_heading rows of each PMID's latest_article version. Columns as mesh_heading. |
| `latest_mesh_qualifier` | Stable. The mesh_qualifier rows of each PMID's latest_article version. Columns as mesh_qualifier. |
| `latest_publication_type` | Stable. The publication_type rows of each PMID's latest_article version. Columns as publication_type. |

<!-- END GENERATED REFERENCE -->
