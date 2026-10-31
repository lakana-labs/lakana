# Translations

A project writes the labels and descriptions of its tables, columns and measures once, in its
main language, in the asset headers. To show them in more languages, it adds one file per
language under `translations/`. This page lists the file format, the rules a deploy checks, the
limits, and every message a deploy gives with its next step.

A deploy checks the files and records their texts with the project version. The catalog then shows
each reader the texts in their language, and says which language each text is in; see
[Descriptions](descriptions.md#what-the-table-shows-and-in-which-language).

## The main language

The asset headers are written in the project's main language. A project with translation files
must say which one it is, in [`lakana.yaml`](lakana-yaml.md#main_language-and-translations):

```yaml
main_language: de
```

A project without translation files needs no `main_language:`. Its texts are in the instance's
main language, the server setting `LAKANA_MAIN_LANGUAGE` (default `en`).

## The files

One file per language, directly under `translations/` at the root of the repository, named after
the language's tag: `translations/fr.yaml`, `translations/de-CH.yaml`. A tag is a language, then
optionally a script and a region (BCP 47), spelled in its canonical case: `en`, `fr-CH`,
`zh-Hant`. There is no file for the main language: its texts are the asset headers.

Each file is keyed by the table's dotted name, as the project builds it:

```yaml
# translations/fr.yaml
gold.fact_sales:
  label: Ventes par jour
  description: Une ligne par magasin et par jour, après retours.
  grain: Une ligne par magasin et par jour.
  columns:
    store_id:
      label: Magasin
      description: Le magasin qui a vendu.
  measures:
    net_amount:
      label: Chiffre d'affaires net
```

| Key | What it translates | Rules |
|---|---|---|
| `label` | The table's `label:` | One line, at most 100 characters |
| `description` | The table's `description:` | At most 4,000 characters |
| `grain` | The table's `grain:` | One line, at most 300 characters |
| `columns.<column>.label`, `.description` | The column's texts under `columns:` | As the table's label and description |
| `measures.<measure>.label`, `.description` | The measure's texts under `measures:` | As the table's label and description |

- **Every translated text needs its main-language text.** A translation translates a text the
  asset header gives: a table's `label`, `description` or `grain`, a column's label or
  description under `columns:`, a measure's label or description under `measures:`. Describe the
  column in the header first, then translate it.
- No text may be empty or contain a control character other than a newline or tab, a
  bidirectional control or a line separator.
- An artifact has no catalog texts, so it has no translations.
- Top-level keys without a dot are reserved for later sections of the same files.
- The file is read with the YAML rules of every project file: no anchors, aliases, merge keys or
  duplicate keys.

## Coverage and strict mode

A **text** is one header text: a table's label, description or grain, a column's label or
description, a measure's label or description. Every deploy, dry runs included, prints one hint
per file saying how many of them the file translates:

- ``translations/fr.yaml: 45 of 48 texts translated; the other 3 show in `de` ``
- `translations/fr.yaml: 48 of 48 texts translated`

A text without a translation shows in the main language. `lakana table get` prints the same count
per language for one table (`Translations: fr 9 of 10`), and REST returns it as `translations`. To
refuse a deploy with gaps in a language instead, list it under `translations.require`:

```yaml
main_language: de
translations:
  require: [fr]
```

A deploy then names the missing texts, as the file nests them, at most 20 of them:

``translations/fr.yaml: translations.require in lakana.yaml needs a `fr` text for every header text, and 3 are missing: gold.dim_store.description, gold.fact_sales.columns.store_id.description, gold.fact_sales.label. Add them to translations/fr.yaml, or remove `fr` from translations.require.``

Strict mode reads the project's files only: a text typed into the catalog does not count for it.

## Limits

| Limit | Value |
|---|---|
| Translation files | 50 (languages besides the main one) |
| Size of one file | 1 MiB |
| Size of all files together | 10 MiB |
| Keys and list items of one file, counted at every depth while it is read | 50,000 |
| Problems reported per check and file | 50, then a count of the rest |
| Missing texts named by strict mode | 20, then a count of the rest |

## Messages

A deploy refuses a project with any of these problems. Each message starts with the file's path
and, where there is one, the key's line.

### The folder and the file names

| Message | Next step |
|---|---|
| `a translation file ends in .yaml; rename it to translations/de.yaml` | Rename the `.yml` file. |
| `translations/ holds only one <language>.yaml file per language, such as translations/de.yaml; move this file elsewhere` | Move a README or any other file out of the folder. |
| `translations/ holds files, not folders; put each language's texts in one file such as translations/de.yaml` | Put each language's texts in one file. |
| `translations must be a folder holding one <language>.yaml file per language, such as translations/de.yaml; move this file there under its language's name` | Make `translations` a folder. |
| `symlinks are not allowed under translations/` | Replace the link with the file. |
| `translations/ holds 51 files; a project has at most 50 languages besides its main language` | Remove languages. |
| ``` `German` is not a language tag Lakana takes: a language, then optionally a script and a region (BCP 47), such as en, de, fr-CH or zh-Hant; name the file after the language of its texts, such as translations/de.yaml``` | Name the file after a tag. |
| ``` `DE` is written `de`; use that spelling: rename the file to translations/de.yaml``` | Rename the file to the canonical spelling. |
| ``` `en` is the project's main language (`main_language:` in lakana.yaml); its texts belong in the asset headers. Move them there, or remove the file``` | Write those texts in the asset headers. |
| `larger than 1024 KiB` | Keep only the texts of tables the project builds. |
| `translations/fr.yaml is not valid UTF-8` | Save the file as UTF-8. |
| `translations/ holds 10241 KiB of translation files, and together they may hold at most 10240 KiB; keep only the texts of tables, columns and measures the project builds` | Remove texts of tables the project does not build. Files past the limit are not read. |
| `the path has a non-UTF-8 or control character` | Rename the file after its language, such as `translations/de.yaml`. |

### `lakana.yaml`

| Message | Next step |
|---|---|
| ``a project with translations must say which language its asset headers are written in; add `main_language:` with that language, such as `main_language: en`, to lakana.yaml`` | Add `main_language:`. |
| ``translations.require lists `fr`, but there is no translations/fr.yaml; add the file with a text for every header text, or remove `fr` from the list`` | Add the file, or remove the language. |
| ``translations.require in lakana.yaml needs a `fr` text for every header text, and 3 are missing: … Add them to translations/fr.yaml, or remove `fr` from translations.require.`` | Translate the texts named. |

The messages about the two keys themselves are in [`lakana.yaml`](lakana-yaml.md#main_language-and-translations).

### The content of a file

| Message | Next step |
|---|---|
| ``translations/fr.yaml must be a mapping of table names to their texts, such as `gold.fact_sales: {label: Ventes}` `` | Key the file by table. |
| `has more than 50000 keys, and a translation file has at most 50000; keep only the texts of tables, columns and measures the project builds` | Remove entries. The file is refused while it is read, at the line of the first key past the limit. |
| ``` `1` is not a table name; a key is a table's dotted name, such as `gold.fact_sales` ``` | Key the entry by the table's dotted name; quote a name YAML reads as a number, `yes` or `null`. |
| ``` `gold.fact_sales` must be a mapping of its texts, such as `gold.fact_sales: {label: …, description: …, columns: …}` ``` | Write the texts under their keys, such as `label: Ventes`. |
| ``gold.fact_sales.columns: must be a mapping of names to their texts, such as `{store_id: {label: …, description: …}}` `` (also `measures`) | Key the columns or measures by name. |
| ``gold.fact_sales.columns: `store_id` must be a mapping such as `{label: …, description: …}` `` | Give the column `label:` or `description:`. |
| ``gold.fact_sales.label: must be a text; in YAML, quote a value that reads as a number, a date, `yes` or `null` `` | Write the text as a string, quoted if need be. |
| ``` `gold.fact_sale` is not a table this project builds; did you mean `gold.fact_sales`? Fix the name, or remove the entry``` | Fix the table's name. The suggestion appears only when a name is close. |
| ``` `ml.model` is an artifact, which has no catalog texts to translate; remove the entry``` | Remove the entry. |
| `gold.fact_sales.labl: Extra inputs are not permitted` | Fix the misspelt key. |
| `gold.fact_sales.label: must be one line`, `… must be at most 100 characters`, `… must not be empty` | Shorten or fill in the label. |
| `gold.fact_sales.grain: must be one line`, `… must be at most 300 characters` | Shorten the grain. |
| `gold.fact_sales.description: must be at most 4000 characters` | Shorten the description. |
| `gold.fact_sales.label: must not contain a surrogate, a control character other than a newline or tab, a bidirectional control or a line separator` | Remove the character. |
| ``gold.fact_sales.columns.store_id: needs `label:` or `description:` `` | Give the column a text, or remove it. |
| ``` `store_idd` is not a column the header of `gold.fact_sales` describes (`columns:` in assets/gold/fact_sales.sql); fix the name, or describe the column there first``` | Fix the name, or describe the column in the header. |
| ``` `net_amout` is not a measure of `gold.fact_sales` (`measures:` in assets/gold/fact_sales.sql); fix the name, or remove the entry``` | Fix the measure's name. |
| ``the header of `gold.fact_sales` has no label to translate; add `label:` in assets/gold/fact_sales.sql, or remove this text`` (also `description`, `grain`) | Add the text to the header first. |
| ``the header of `gold.fact_sales` gives column `store_id` no label to translate; add `label:` for it under `columns:` in assets/gold/fact_sales.sql, or remove this text`` (also `description`) | Add the column's text to the header first. |
| ``measure `net_amount` of `gold.fact_sales` has no label to translate; add `label:` to it under `measures:` in assets/gold/fact_sales.sql, or remove this text`` (also `description`) | Add the measure's text to the header first. |
| `… and 12 more problems in translations/fr.yaml; fix the ones above and deploy again to see the rest` | Fix the problems shown and deploy again. |

A key or name that a message repeats is cut to 80 characters, ending in `…`.

### YAML

The file is read with the rules of every project file. Each message starts with the file's name.

| Message | Next step |
|---|---|
| `translations/fr.yaml is not valid YAML: …` | Fix the syntax at the line named. |
| `translations/fr.yaml is not valid YAML: it holds the character U+0000, which YAML does not allow; remove it` | Remove the character at the line named. |
| `translations/fr.yaml: anchors and aliases are not allowed.` | Write each text out. |
| `translations/fr.yaml: merge keys (<<) are not allowed.` | Write each key out. |
| `translations/fr.yaml: duplicate key 'gold.fact_sales'.` | Keep one entry per key. |
| `translations/fr.yaml: nesting is deeper than 64 levels.` | Keep to the format above. |

## Workers and manifest version

A project version that declares `main_language`, a label or a translation has manifest version
11. It needs workers of the release that introduced it or later: a worker of an earlier release
refuses its manifest. Every other project keeps its manifest version.
