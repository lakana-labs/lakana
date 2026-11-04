# Glossary

A business glossary says what the words your team uses mean, and where the data for them lives:
"revenue" is the term `net_revenue`, defined as revenue after returns and discounts, and it points
to the column `gold.fact_sales.net_amount`. The glossary is kept as code in the project's
repository, next to the assets, and every deploy checks it and deploys it with the project.

How the glossary is read (REST, the SDK, the CLI, `table get` and search) is described under
[Reading](#reading), and who sees which part under [Who sees what](#who-sees-what). For the steps,
see [Keep a glossary](../how-to/keep-a-glossary.md).

## Files

The glossary is a set of YAML files directly under `glossary/` at the repository root, one per
area of the business: `glossary/sales.yaml`, `glossary/finance.yaml`. Each file maps **term ids**
to terms. Other files in the folder, such as a `README.md`, are ignored.

```yaml
# glossary/sales.yaml
net_revenue:
  name: Net revenue
  definition: >
    Revenue after returns and discounts, before tax.
  synonyms: [revenue, sales, turnover]
  owner: finance
  links:
    gold.fact_sales: [net_amount]
    gold.dim_store: []
  related: [gross_revenue]
```

## Fields

| Field | Required | Rules |
|---|---|---|
| key (the term id) | yes | Lowercase letters, digits and underscores, starting with a letter, at most 63 characters (`net_revenue`). Unique across the repository's glossary files and across the environment: an id belongs to the repository that deployed it first. |
| `name` | yes | The name people see, in the project's main language: one line, 1 to 100 characters, no tab. |
| `definition` | yes | What the term means: 1 to 4,000 characters; newlines and tabs are allowed. |
| `synonyms` | no | Other words for the term: a list of at most 20, each one line of 1 to 100 characters with no tab. No two may be equal once case is ignored, and none may equal the name. |
| `owner` | yes | The team or person who answers for the term, with the rules of an asset header's `owner:`. |
| `links` | no | Table → the list of its columns the term is about. The table is a dotted name as the project builds it; `[]` links the table itself. A table or column name has at most 1,024 characters, no spaces around it, and no control, format or bidirectional character or line separator. At most 50 tables and 200 columns per term, no column twice for one table. A column with a declared [measure](asset-front-matter.md) is a measure link: there is no separate syntax for measures. |
| `related` | no | Ids of related terms, of this repository or of another repository deployed to the same environment: at most 20, not the term itself, none twice. |

## Rules and limits

- A term has no other fields; an unknown field is refused, naming the allowed ones.
- No text may contain a control character other than a newline or tab (in a definition only), a
  bidirectional control or a line separator.
- At most 50 YAML files under `glossary/`, each at most 1 MiB, and at most 2,000 terms per
  repository.
- A glossary file ends in `.yaml`: a `.yml` file is refused (rename it). Folders and symlinks
  under `glossary/` are refused, and so is a file named `glossary` at the repository root. A
  root folder named `glossary` in another case, such as `Glossary`, is refused too: rename it to
  `glossary`.
- Synonyms are compared after Unicode normalisation (NFC) and ignoring case, so `café` typed in
  two ways counts as the same word.
- The files are read with the YAML rules of every project file: no anchors, aliases, merge keys or
  duplicate keys. A term id defined in two files is refused.

## Translations

A translation file, `translations/<language>.yaml` (see [Translations](translations.md)), has a
top-level `glossary:` section that translates the terms:

```yaml
# translations/fr.yaml
glossary:
  net_revenue:
    name: Chiffre d'affaires net
    definition: Le chiffre d'affaires après retours et remises, hors taxes.
    synonyms: [ventes, CA]
```

- `name` and `definition` translate the term's own; `synonyms` are **additional** synonyms in
  that language, with the same limits per language.
- A translation of a term id the repository's glossary does not define is refused.
- Coverage and strict mode count a term's name and definition as texts, like a table's label and
  description.

## Deploy checks

Every deploy and dry run checks the glossary against the project and the environment, and writes
nothing when a check fails. A linked table is one the project builds, or one that exists in the
environment and that you, the deployer, may read; a table this repository built before and the new
version no longer builds counts as missing. A link's columns are checked against the columns the
asset's `SELECT` returns, or the table's current columns for any other table. The columns of a
project table that the deploy cannot see, one built by `SELECT *` or by a Python asset, are not
checked at deploy: such a link is kept as written and resolves when the glossary is read. Each
refusal names the file and line, the term id and what to do. The examples are for
`glossary/sales.yaml` above, with the mistake on the line they name (for the artifact,
`gold.dim_store: []` on line 10 written as `gold.churn_model: [score]`):

| Problem | Message | Fix |
|---|---|---|
| A linked column the table does not have | `glossary/sales.yaml:9: net_revenue links gold.fact_sales column net_amout, which gold.fact_sales does not have (columns: date, store_id, net_amount); fix the column name or remove the link.` | Correct the column, or remove it from the list. At most 20 columns are listed. |
| A linked table that is missing, or that you may not read | `glossary/sales.yaml:9: net_revenue links gold.fact_sale, which does not exist in environment dev, or you may not read it; fix the table name (did you mean gold.fact_sales?), build the table first, or ask for READ_TABLE on it.` | Correct the name, deploy and run the table's project first, or ask an admin for `READ_TABLE`. |
| Columns of an artifact | ``glossary/sales.yaml:10: net_revenue links columns of gold.churn_model, an artifact, which has no columns; link the artifact itself with `gold.churn_model: []`, or remove the link.`` | Link the artifact with an empty list. |
| A related id no repository defines | `glossary/sales.yaml:11: net_revenue names gros_revenue under related:, which is not a term of this repository or of another repository deployed to dev; fix the id (did you mean gross_revenue?), or remove it from related:.` | Correct the id, or remove it. |
| A term id another repository owns | `glossary/sales.yaml:2: net_revenue is a term of repository finance in dev, and a term id belongs to the repository that deployed it first; rename the term here, or remove it from finance first.` | Rename the term, or remove it from the other repository and deploy that first. |

A message about another repository's table names its columns or kind only to a reader who may read
that table; anyone else sees the missing-table message in its place.

Links are checked again at every deploy. When this project stops building a linked table or column,
its deploy is refused until the link is fixed. When another repository drops a table or column one
of your terms links to, your terms stay as they are, and your next deploy is refused until you fix
or remove the link. Another repository is never blocked by a term it does not own.

A rollback checks term ownership too. When another repository has since deployed a term id of the
version you roll back to, the rollback is refused with the same message, except that its next step
reads `roll back to or deploy a version without net_revenue, or remove it from finance first.`

## Stable ids

The term id is what links, related terms and translations refer to, so it is stable. Changing
`name` keeps the id, its links and its translations. Changing the key makes a new term, and the old
one disappears at the next deploy.

## Workers and manifest 13

A project version with a `glossary/` folder, even an empty one, has manifest version 13. It needs
workers of the release that introduced it or later: a worker of an earlier release refuses its
manifest, so upgrade the server and every worker before you deploy a glossary. Every other project
keeps its manifest version.

## Reading

All routes are `GET` and need `READ_ENVIRONMENT` on the environment.

| Route | Answer |
|---|---|
| `/api/v1/environments/{env}/terms` | One page of terms, sorted by key: `{"items": [...], "next_cursor": ...}`. |
| `/api/v1/environments/{env}/terms/{key}` | One term; `404` for a key the environment does not have, `422` for one no term can have (over 63 characters, or with a control character such as NUL). |

| Parameter | Meaning |
|---|---|
| `owner` | List only. Only terms with exactly this owner. Over 1,000 characters, or with a control character such as NUL, is `422`. |
| `repository` | List only. Only terms this repository defines. Over 255 characters, or with a control character, is `422`. |
| `lang` | The language of the texts, such as `de` or `fr-CH`. Without it, your profile's language, then `Accept-Language`, then the instance's main language. A value that is not a language tag is `422`. |
| `limit` | List only. Terms per page, 1 to 200 (100). |
| `cursor` | List only. The `next_cursor` of the previous page; one the list did not issue is `422`. |

A term (`TermOut`) has `key`, `id` (stable across deploys), `name`, `definition`, `synonyms`,
`owner`, `repository`, `language` (the language `name` and `definition` are shown in), `links`,
`related` and `related_by`:

- A link is `{table, column, kind, missing}`. `kind` is `table`, `column` or `measure`; `column` is
  `null` for a link to the table itself; `missing` is `true` for a table that does not exist, and
  for a column the table no longer has.
- A related term is `{key, name, missing}`; `missing` is `true` when no repository defines the id.
  `related_by` holds the keys of the terms that name this one.
- The texts are shown in your language where a translation exists, else in the term's main
  language. The synonyms are the main language's, then those of your languages that come before
  the term's main language in your list ([Translations](translations.md)).

**SDK:** `client.terms.list(environment, owner=None, repository=None, lang=None, limit=None,
cursor=None)` returns one page; `client.terms.iter(environment, ...)` yields every term; and
`client.terms.get(environment, key, lang=None)` returns one `Term`.

**CLI:** `lakana term list [--owner] [--repository] [--lang] [--env]` and
`lakana term get KEY [--lang] [--env]`; `--output json` prints every field.

**`table get`:** the table and each column carry `terms` (`{key, name}` in your language): the terms
linked to the table itself and to that column. `lakana table get` shows them in a `Terms:` section.

**Search:** `kind=term` finds terms by key, name, synonym or definition (`matched` is `name`,
`synonym` or `definition`); a table or column a term links is also found by the term's name and
synonyms, with `matched: term` and the term's key in `term`. See [Search](search.md).

## Who sees what

| Result | Needs |
|---|---|
| a term (name, definition, synonyms, owner, repository, related) | `READ_ENVIRONMENT` |
| a link of a term to an existing table, and its column | `READ_TABLE` on that table; otherwise the link is left out, never refused |
| a link whose table does not exist | shown as missing to anyone who sees the term, with the name the file gave |
| a table or column found by a term's words in search | `READ_TABLE` on the table, as for every table and column result |
| the terms in `table get` | the table's own `READ_TABLE` (as `table get`) plus `READ_ENVIRONMENT` |

A link whose table exists but which the caller may not read stays hidden even when its column is
missing. A term whose links are all hidden still shows its definition. An environment the caller may
not read answers as search does: an empty page for the list, and `404` for one term.
