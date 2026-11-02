# Glossary

A business glossary says what the words your team uses mean, and where the data for them lives:
"revenue" is the term `net_revenue`, defined as revenue after returns and discounts, and it points
to the column `gold.fact_sales.net_amount`. The glossary is kept as code in the project's
repository, next to the assets, and every deploy checks it and deploys it with the project.

How the glossary is read (REST, the SDK, the CLI, `table get` and search) is described in a later
section of this page.

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
| `links` | no | Table → the list of its columns the term is about. The table is a dotted name as the project builds it; `[]` links the table itself. At most 50 tables and 200 columns per term, no column twice for one table. A column with a declared [measure](asset-front-matter.md) is a measure link: there is no separate syntax for measures. |
| `related` | no | Ids of related terms, of this repository or of another repository deployed to the same environment: at most 20, not the term itself, none twice. |

## Rules and limits

- A term has no other fields; an unknown field is refused, naming the allowed ones.
- No text may contain a control character other than a newline or tab (in a definition only), a
  bidirectional control or a line separator.
- At most 50 YAML files under `glossary/`, each at most 1 MiB, and at most 2,000 terms per
  repository.
- A glossary file ends in `.yaml`: a `.yml` file is refused (rename it). Folders and symlinks
  under `glossary/` are refused, and so is a file named `glossary` at the repository root.
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
asset's `SELECT` returns, or the table's current columns for any other table. Each refusal names
the file and line, the term id and what to do:

| Problem | Message | Fix |
|---|---|---|
| A linked column the table does not have | `glossary/sales.yaml:7: net_revenue links gold.fact_sales column net_amout, which gold.fact_sales does not have (columns: date, store_id, net_amount); fix the column name or remove the link.` | Correct the column, or remove it from the list. At most 20 columns are listed. |
| A linked table that is missing, or that you may not read | `glossary/sales.yaml:6: net_revenue links gold.fact_sale, which does not exist in environment dev, or you may not read it; fix the table name (did you mean gold.fact_sales?), build the table first, or ask for READ_TABLE on it.` | Correct the name, deploy and run the table's project first, or ask an admin for `READ_TABLE`. |
| Columns of an artifact | ``glossary/models.yaml:6: net_revenue links columns of gold.churn_model, an artifact, which has no columns; link the artifact itself with `gold.churn_model: []`, or remove the link.`` | Link the artifact with an empty list. |
| A related id no repository defines | `glossary/sales.yaml:10: net_revenue names gros_revenue under related:, which is not a term of this repository or of another repository deployed to dev; fix the id (did you mean gross_revenue?), or remove it from related:.` | Correct the id, or remove it. |
| A term id another repository owns | `glossary/sales.yaml:1: net_revenue is a term of repository finance in dev, and a term id belongs to the repository that deployed it first; rename the term here, or remove it from finance first.` | Rename the term, or remove it from the other repository and deploy that first. |

A message about another repository's table names its columns or kind only to a reader who may read
that table; anyone else sees the missing-table message in its place.

Links are checked again at every deploy. When this project stops building a linked table or column,
its deploy is refused until the link is fixed. When another repository drops a table or column one
of your terms links to, your terms stay as they are, and your next deploy is refused until you fix
or remove the link. Another repository is never blocked by a term it does not own.

## Stable ids

The term id is what links, related terms and translations refer to, so it is stable. Changing
`name` keeps the id, its links and its translations. Changing the key makes a new term, and the old
one disappears at the next deploy.

## Workers and manifest 13

A project version with a `glossary/` folder, even an empty one, has manifest version 13. It needs
workers of the release that introduced it or later: a worker of an earlier release refuses its
manifest, so upgrade the server and every worker before you deploy a glossary. Every other project
keeps its manifest version.
