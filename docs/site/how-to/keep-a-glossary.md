# Keep a glossary

After this guide your team's words live next to the data they name: someone who searches for
"revenue" finds the term `net_revenue`, what it means, who answers for it, and the column
`gold.fact_sales.net_amount` it points to. It is for teams where the finance people and the people
who build the tables say "revenue" and mean two things. You need a project that deploys (see
[Projects](projects.md)). The glossary is kept as code, in YAML files in your repository, and every
deploy checks it.

## Write a term

Add a file under `glossary/` at the repository root, one per area of the business. Each file maps a
term id to the term:

<!-- source: docs/examples/glossary/project/glossary/sales.yaml -->
```yaml
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
gross_revenue:
  name: Gross revenue
  definition: Revenue before returns and discounts.
  owner: finance
  links:
    gold.fact_sales: [gross_amount]
```

The id (`net_revenue`) is what links and translations refer to. `name`, `definition` and `owner` are
required; `synonyms`, `links` and `related` are not. Every field and limit is in the
[Glossary reference](../reference/glossary.md#fields).

## Link it to tables and columns

`links:` maps a table to the list of its columns the term is about. `[]` links the table itself
(`gold.dim_store: []`). A column that has a declared measure, such as `net_amount` here, is a
measure link: there is no separate syntax. The measure comes from the table's asset header, see
[Declare a table's model](declare-a-tables-model.md).

## Deploy

A glossary deploys with the project. Nothing else to switch on:

<!-- source: docs/examples/glossary/run.sh#deploy -->
```bash
lakana deploy --env dev
```

<!-- output: glossary#deploy -->
```text
Queued deploy 5b882b18-8ab0-4f74-9b13-de58ac9fd309
Deployed shop in dev at 15a26dfbcd4b0c31d4d0e79a7224f9511553e498
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ change ┃ table           ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.sales    │
│ added  │ bronze.stores   │
│ added  │ gold.dim_store  │
│ added  │ gold.fact_sales │
└────────┴─────────────────┘
Checks:
  gold.dim_store: key_not_null (generated), key_unique (generated)
  gold.fact_sales: key_not_null (generated), key_unique (generated), measures (generated), ref_store_id (generated)
Hint: run 'lakana run --env dev' to run what you just deployed.
```

## Find a term by a synonym

Search finds the term by its name, its synonyms or its definition, and finds the tables and columns
it links by the term's name and synonyms. "revenue" is a synonym of `net_revenue` and part of the
name of `gross_revenue`:

<!-- source: docs/examples/glossary/run.sh#search -->
```bash
lakana search revenue --env dev
```

<!-- output: glossary#search -->
```text
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ kind   ┃ name                         ┃ matched ┃ where         ┃ snippet       ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ table  │ gold.dim_store [dimension]   │ term    │ table         │ revenue       │
│ column │ gold.fact_sales.net_amount   │ term    │ decimal(38,1) │ revenue       │
│ term   │ net_revenue                  │ synonym │ shop          │ revenue       │
│ column │ gold.fact_sales.gross_amount │ term    │ decimal(38,1) │ Gross revenue │
│ term   │ gross_revenue                │ name    │ shop          │ Gross revenue │
└────────┴──────────────────────────────┴─────────┴───────────────┴───────────────┘
```

A table or column found by a term says `term` and that term's key in `matched`; `lakana --output json
search` gives the key in `term`. Ranking and who sees what are in [Search](../reference/search.md).

## Read a term, and a table's terms

`lakana term get` shows the definition, the owner, the links with their kind, and the terms that
name this one:

<!-- source: docs/examples/glossary/run.sh#term-get -->
```bash
lakana term get net_revenue --env dev
```

<!-- output: glossary#term-get -->
```text
Term:        net_revenue — Net revenue
Definition:  Revenue after returns and discounts, before tax.
Synonyms:    revenue, sales, turnover
Owner:       finance
Repository:  shop
Language:    en
Links:
  gold.dim_store (table)
  gold.fact_sales.net_amount (measure)
Related:     gross_revenue — Gross revenue
Named by:    (none)
```

`lakana term list` lists all terms. A table shows the terms that point to it, and to each of its
columns, in a `Terms:` section of `table get`:

<!-- source: docs/examples/glossary/run.sh#table-get -->
```bash
lakana table get gold.fact_sales --env dev
```

<!-- output: glossary#table-get -->
```text
┌───────────────────┬──────────────────────────────────────────────────────────────────────────────┐
│ name              │ fact_sales                                                                   │
│ namespace         │ gold                                                                         │
│ kind              │ TABLE                                                                        │
│ state             │ published                                                                    │
│ uuid              │ bfd137b2-fee3-4c93-9464-859dbaa74390                                         │
│ metadata_location │ file:///tmp/tmp.4HnnoT34zt/home/data/lakana/local/lake/dev/managed/gold/fac… │
└───────────────────┴──────────────────────────────────────────────────────────────────────────────┘
Description (from the project): Sales per store and day.
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ name         ┃ type          ┃ nullable ┃ description ┃ description_source ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ date         │ date          │ True     │             │                    │
│ store_id     │ int           │ True     │             │                    │
│ gross_amount │ decimal(38,1) │ True     │             │                    │
│ net_amount   │ decimal(38,1) │ True     │             │                    │
└──────────────┴───────────────┴──────────┴─────────────┴────────────────────┘
Terms:
  gross_amount: gross_revenue — Gross revenue
  net_amount: net_revenue — Net revenue
Model: fact
Grain: One row per store and calendar day.
Key: date, store_id
  store_id → gold.dim_store.store_id (exists)
Measures:
  net_amount: sum, additive, EUR — Net revenue
┏━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ check                    ┃ state  ┃ failing_rows ┃ finished_at               ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ key_not_null (generated) │ passed │ 0            │ 2026-11-04T00:16:41+00:00 │
│ key_unique (generated)   │ passed │ 0            │ 2026-11-04T00:16:41+00:00 │
│ measures (generated)     │ passed │ 0            │ 2026-11-04T00:16:42+00:00 │
│ ref_store_id (generated) │ passed │ 0            │ 2026-11-04T00:16:42+00:00 │
└──────────────────────────┴────────┴──────────────┴───────────────────────────┘
```

## Fix a link that does not resolve

A link to a column the table does not have refuses the deploy, and nothing changes. The message names
the file, the line, the term and the columns the table has. Here `net_amount` was typed `net_amout`:

<!-- source: docs/examples/glossary/run.sh#refused -->
```bash
lakana deploy --env dev
```

<!-- output: glossary#refused -->
```text
Queued deploy 42473dad-fdf6-4819-bde6-b4860676f7ca
Error: glossary/sales.yaml:8: net_revenue links gold.fact_sales column net_amout, which
gold.fact_sales does not have (columns: date, store_id, gross_amount, net_amount); fix the column
name or remove the link.
```

Correct the column, commit, and deploy again. Links are checked at every deploy. When another
repository drops a table or column one of your terms links to, your terms stay as they are, the link
shows as `missing`, and your next deploy is refused until you fix or remove the link. Every message
with its fix is in the [Glossary reference](../reference/glossary.md#deploy-checks).

## Translate it

A term's name, definition and extra synonyms are translated in the `glossary:` section of a
translation file. The project needs `main_language:` in `lakana.yaml`, as for any translation:

```yaml
# translations/fr.yaml
glossary:
  net_revenue:
    name: Chiffre d'affaires net
    definition: Le chiffre d'affaires après retours et remises, hors taxes.
    synonyms: [ventes, CA]
```

People who read French then see these texts, and search finds the term by "ventes". The rules are in
[Translations](../reference/translations.md).
