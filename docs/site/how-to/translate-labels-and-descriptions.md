# Translate labels and descriptions

After this guide a table shows its label and descriptions in French to the people who read French,
and in English to everyone else. You write the English texts once in the asset header, the French
ones in a translation file next to the assets, and you can correct a French text by hand. You need
a project that deploys (see [Projects](projects.md)) and, to edit a text by hand, to own the table
or hold the right to drop it.

Every text in an answer says which language it is in, so a reader always knows when a text is not
yet translated.

## Say which language your headers are in

Add `main_language:` to `lakana.yaml`. A project with translation files must have it:

<!-- source: docs/examples/translate-a-table/project/lakana.yaml -->
```yaml
# Lakana project. Reference: https://lakana-labs.github.io/lakana/how-to/projects/
repo: shop
main_language: en
defaults:
  materialize: table
```

## Write the labels and descriptions in the header

A label is a short name for people, one line of at most 100 characters. Give a column a label in
the long form of `columns:`; the short form is a description alone:

<!-- source: docs/examples/translate-a-table/project/assets/gold/sales.sql -->
```sql
/* lakana
label: Sales per store
description: One row per store, with its net sales.
columns:
  store_id:
    label: Store
    description: The store that sold.
  net_amount: Sales after returns, in euros.
*/
SELECT store_id, SUM(net_amount) AS net_amount
FROM bronze.sales
GROUP BY store_id
```

These five texts (the table's label and description, `store_id`'s label and description, and
`net_amount`'s description) are the English texts the French file translates.

## Add a translation file

Add one file per language under `translations/`, named after the language's tag, and key it by
the table's dotted name. Translate as much or as little as you like:

<!-- source: docs/examples/translate-a-table/project/translations/fr.yaml -->
```yaml
gold.sales:
  label: Ventes par magasin
  columns:
    store_id:
      label: Magasin
```

Every key must translate a text the header has. The format and the limits are in
[Translations](../reference/translations.md).

## Deploy

<!-- source: docs/examples/translate-a-table/run.sh#deploy -->
```bash
lakana deploy --env dev
```

The hint says how much of the file is translated, and which language the rest shows in:

<!-- output: translate-a-table#deploy -->
```text
Queued deploy 95fd84d0-07cf-491e-963a-dce7a61006b0
Deployed shop in dev at 04bd115f44ee14ead8dbe4e49f62a8307ff24717
┏━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ change ┃ table        ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━┩
│ added  │ bronze.sales │
│ added  │ gold.sales   │
└────────┴──────────────┘
hint: translations/fr.yaml: 2 of 5 texts translated; the other 3 show in `en`
Hint: run 'lakana run --env dev' to run what you just deployed.
```

## Read the table in French

`--lang` asks for a language. A text with a French translation shows in French; every other text
shows in the main language and ends in `[en]`. The `Translations` line counts the French texts:

<!-- source: docs/examples/translate-a-table/run.sh#get-fr -->
```bash
lakana table get gold.sales --env dev --lang fr
```

<!-- output: translate-a-table#get-fr -->
```text
┌───────────────────┬──────────────────────────────────────────────────────────────────────────────┐
│ name              │ sales                                                                        │
│ namespace         │ gold                                                                         │
│ kind              │ TABLE                                                                        │
│ state             │ published                                                                    │
│ uuid              │ 8689b47b-17e6-4e8d-8024-45d2e32471bf                                         │
│ metadata_location │ file:///tmp/tmp.hOsHF7pyxI/home/data/lakana/local/lake/dev/managed/gold/sal… │
└───────────────────┴──────────────────────────────────────────────────────────────────────────────┘
Label: Ventes par magasin
Description (from the project) [en]: One row per store, with its net sales.
Translations: fr 2 of 5
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ name       ┃ type          ┃ nullable ┃ label   ┃ description                         ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ store_id   │ int           │ True     │ Magasin │ The store that sold. [en]           │
│ net_amount │ decimal(38,1) │ True     │         │ Sales after returns, in euros. [en] │
└────────────┴───────────────┴──────────┴─────────┴─────────────────────────────────────┘
```

A regional tag falls back to its language: `--lang fr-CH` shows the `fr` texts.

## Set your own language

Instead of passing `--lang` to every command, set your language once. Every command, and every
REST answer without `?lang=`, then shows French where there is a French text:

<!-- source: docs/examples/translate-a-table/run.sh#me -->
```bash
lakana me set --language fr
lakana table get gold.sales --env dev
```

<!-- output: translate-a-table#me -->
```text
Your language is now fr
┌───────────────────┬──────────────────────────────────────────────────────────────────────────────┐
│ name              │ sales                                                                        │
│ namespace         │ gold                                                                         │
│ kind              │ TABLE                                                                        │
│ state             │ published                                                                    │
│ uuid              │ 8689b47b-17e6-4e8d-8024-45d2e32471bf                                         │
│ metadata_location │ file:///tmp/tmp.hOsHF7pyxI/home/data/lakana/local/lake/dev/managed/gold/sal… │
└───────────────────┴──────────────────────────────────────────────────────────────────────────────┘
Label: Ventes par magasin
Description (from the project) [en]: One row per store, with its net sales.
Translations: fr 2 of 5
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ name       ┃ type          ┃ nullable ┃ label   ┃ description                         ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ store_id   │ int           │ True     │ Magasin │ The store that sold. [en]           │
│ net_amount │ decimal(38,1) │ True     │         │ Sales after returns, in euros. [en] │
└────────────┴───────────────┴──────────┴─────────┴─────────────────────────────────────┘
```

`lakana me set --reset` removes it again. A task or CI token cannot set a language; use your
personal access token.

## Correct a translation by hand

`lakana table describe` with `--lang` sets a text in that language. It wins over the translation
file, and a deploy never replaces it. Without `--lang`, the edit is in the main language:

<!-- source: docs/examples/translate-a-table/run.sh#edit -->
```bash
lakana table describe gold.sales --env dev --lang fr --text "Une ligne par magasin."
```

<!-- output: translate-a-table#edit -->
```text
Described gold.sales in dev (fr)
```

`--label` sets a label the same way, and `--reset` with `--lang fr` goes back to the file's French
texts. The rights and the audit records are those of any description; see
[Descriptions](../reference/descriptions.md).

## Require a complete translation

By default a missing translation shows in the main language. To refuse a deploy while French texts
are missing, list the language under `translations.require` in `lakana.yaml`:

```yaml
main_language: en
translations:
  require: [fr]
```

The deploy then names the missing texts, at most 20 of them, and the file to add them to.

## Fix a refused key

A key that names a table the project does not build refuses the deploy. The message names the
file, the line and, when a name is close, the table you probably meant:

<!-- source: docs/examples/translate-a-table/run.sh#refused -->
```bash
lakana deploy --env dev
```

<!-- output: translate-a-table#refused -->
```text
Queued deploy 366149fa-b4fa-427b-9224-3f4d862c314f
Error: translations/fr.yaml:1: `gold.sale` is not a table this project builds; did you mean
`gold.sales`? Fix the name, or remove the entry
```

Fix the name in `translations/fr.yaml`, commit, and deploy again. Every message a deploy gives
about translation files, with its next step, is in [Translations](../reference/translations.md#messages).
