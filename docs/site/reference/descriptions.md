# Descriptions

The description and the label of a table and of a column: where they come from, the languages,
the commands, the SDK, the REST routes, the rights and the audit events. For the steps, see
[Describe a table](../how-to/describe-a-table.md) and
[Translate labels and descriptions](../how-to/translate-labels-and-descriptions.md).

## Two sources

| Source | Set by | Shown when |
|---|---|---|
| `project` | A deploy or a rollback, from the `label:`, `description:` and `columns:` of the asset's header and from the project's translation files; or the creation of the table by a run, when the deployed project describes it | there is no edit in that language |
| `edited` | `lakana table describe`, the SDK or the REST routes | always in its language, once set |

Each text has both sources per language: the main language's edit and project text, and for each
other language its own edit and translated text. An edit in a language wins over the deployed
text in that language, which wins over the main language's.

A deploy never writes or removes an edit. A reset removes the edit. A column whose text the header no
longer has loses its project text at the next deploy. The text of a column that is no longer in the
table's schema is kept and is not shown; it shows again if the column comes back.

A description is at most 4,000 characters and is not blank. A label is one line of at most 100
characters and is not blank. A table the project describes, but that
does not exist yet, gets the text when a run creates it.

## What the table shows, and in which language

`GET .../tables/{name}` and the table list return, next to the existing fields:

| Field | Type | Meaning |
|---|---|---|
| `label` | string or `null` | The table's short name for people, chosen like `description`. `null` for none. |
| `label_source` | `"edited"`, `"project"` or `null` | Where `label` came from. |
| `label_language` | string or `null` | The language of `label`; `null` exactly when `label` is. |
| `description` | string or `null` | The text shown: the edit if there is one, else the project's. `null` for none. |
| `description_source` | `"edited"`, `"project"` or `null` | Where `description` came from. |
| `description_language` | string or `null` | The language of `description`; `null` exactly when `description` is. |
| `language` | string | The language the texts were chosen for (see below). |
| `main_language` | string | The table's main language: its project's `main_language:`, else the server's `LAKANA_MAIN_LANGUAGE` (default `en`). A text without a translation shows in it. |
| `translations` | list | Per language with texts for the table: `{"language": "fr", "translated": 9, "total": 10}`, how many of its main-language texts that language has. Empty without translations. |

Each entry of `schema` has `label`, `label_source`, `label_language`, `description`,
`description_source` and `description_language` for its column. In `model`, `grain_language` is the
language of `grain`, and each measure has `label_language` and `description_language`.

Your languages come from the first of steps 1 to 4 that gives any; `language` in the answer is
the first of them. Each text shows in the first of your languages that has it, and step 5 catches
the rest:

1. `?lang=` (REST), `--lang` (CLI) or `lang=` (SDK), such as `de` or `fr-CH`;
2. your profile's language, if one is set (`lakana me set --language fr`, see
   [Your own language](#your-own-language));
3. the request's `Accept-Language` header, by its weights (REST only; the CLI and the SDK do not
   send one);
4. the server's main language, `LAKANA_MAIN_LANGUAGE` (default `en`);
5. for a text with no translation in your languages, the **table's** main-language text
   (`main_language` in the answer: its project's `main_language:`, else the server's), marked
   with that language.

A regional tag falls back to its language: `fr-CH` shows a `fr` text.

The server's and the table's main language can differ. A project with `main_language: de` and a
`translations/en.yaml` file, on a server with the default `LAKANA_MAIN_LANGUAGE=en`, shows a reader
who chose nothing the English texts: `language` is `en`, and a text the English file lacks shows in
German with `de` as its language (`[de]` in `lakana table get`).

One answer can therefore mix languages; that is what the `*_language` fields are for. A `lang`
that is not a language tag is `422`, and the message gives examples such as `fr-CH`; a malformed
`Accept-Language` is ignored. The tags, the files and the coverage are described in
[Translations](translations.md).

`lakana table get` prints the label and the description, a column table with a `label` column
when a column has one, and the coverage, such as `Translations: fr 9 of 10`. A text in another
language than the one asked for ends in its language, such as
`Description (from the project) [en]: One row per sale.` with `--lang fr`.

## Your own language

Set the language you read catalog texts in once, instead of passing `--lang` to every command;
the table then shows in it:

<!-- source: docs/examples/translate-a-table/run.sh#me -->
```bash
lakana me set --language fr
lakana table get gold.sales --env dev
```

From then on, `lakana table get`, the table list and the REST answers show your tables in French
where the project or an edit has a French text, and in the table's main language elsewhere.
`--lang` on a command, or `?lang=` on a request, still asks for another language.
`lakana me set --reset` removes your language. The tag is stored in its canonical spelling
(`fr-ch` becomes `fr-CH`); a value that is not a tag is refused with examples. `lakana whoami`
shows your language.

Each person sets only their own. A task or CI token cannot set one: the answer is `403` with
`a task or CI token cannot change its principal's profile; use a personal access token`. The
change is not audited: it is a display preference that changes nothing anyone else sees.

| Interface | How |
|---|---|
| CLI | `lakana me set --language TAG`, `lakana me set --reset` |
| SDK | `client.set_language("fr")`, `client.set_language(None)`; both return `Whoami`, whose `language` is the stored tag |
| REST | `PATCH /api/v1/whoami` with `{"language": "fr"}` or `{"language": null}`; the field must be sent. `GET /api/v1/whoami` has `language`. |

## Commands

| Command | What it does |
|---|---|
| `lakana table describe NAME --text TEXT` | Sets the table's description edit. |
| `lakana table describe NAME --label LABEL` | Sets the table's label edit. |
| `lakana table describe NAME --column COLUMN --text TEXT --label LABEL` | Sets a column's description and label edits. |
| `lakana table describe NAME [--column COLUMN] --reset` | Removes the edits of the description and the label. |
| `lakana table describe NAME … --lang LANGUAGE` | Does the same in that language; without `--lang`, in the table's main language. |
| `lakana table get NAME [--lang LANGUAGE]` | Shows the label, the description, the columns' labels and descriptions, and the coverage. |
| `lakana table list NAMESPACE [--lang LANGUAGE]` | Lists the tables; with `--output json`, their texts in that language. |
| `lakana me set --language TAG` | Sets your own language; `--reset` removes it. |

`--text`, `--label` or both set texts; `--reset` alone removes both edits. Anything else is
refused with `Give --text TEXT and/or --label LABEL to set, or --reset to go back to the
project's texts.` The command ends with the language it edited, such as
`Described gold.sales in dev (fr)`.

An edit in a language other than the table's main language is a translation edit: it shows to a
reader of that language, wins over that language's translation file, and a deploy never
replaces it. An edit with `--lang` naming the main language itself is the main-language edit.
A translation edit does not need a translation file; the language then appears in the coverage.

## SDK

`client.tables.describe(environment, "namespace.table", description, label=…, column=None,
lang=None)` returns the `Table`, with its texts in `lang` (or your usual language). Pass
`description`, `label` or both; a field left out stays as it is, and `None` resets its edit.
Passing neither raises `ValueError("give description, label or both; None removes an edit")`.
`Table.description`, `Table.description_source`, `Column.description` and
`Column.description_source` hold what the server returned; they are `None` for an older server.

`client.tables.get(environment, "namespace.table", lang="fr")` and
`client.tables.list(environment, namespace, lang="fr")` choose the language. `Table` and `Column`
have `label`, `label_source`, `label_language` and `description_language`; `Table` also has
`language`, `main_language` and `translations` (a tuple of `TranslationCoverage(language,
translated, total)`). `TableModel.grain_language` and `Measure.label_language` and
`Measure.description_language` say which language a model text is in. From a server older than
translations, the languages are `None`, `language` and `main_language` are `en`, and
`translations` is empty.

## REST routes

Under `/api/v1/environments/{env}/namespaces/{path}/tables/{name}`:

| Route | Body | Answer |
|---|---|---|
| `PATCH …/{name}[?lang=fr]` | `{"description": "text", "label": "Sales"}`: one field or both, each a text or `null` | The table (`200`) |
| `PUT …/{name}/columns/{column}/description[?lang=fr]` | the same | The table (`200`) |

`lang` names the language edited; without it, the edit is in the table's main language. The
answer shows the table's texts in the reader's language, as `GET` does: `lang` if given, else your
profile's language, then `Accept-Language`, then the server's main language. A `lang` that is not
a language tag is `422` before anything is written.

A field left out stays as it is; `null` resets its edit, and a blank text is refused. A body with
neither field is `422` with `send `description`, `label` or both; null removes an edit`. A
description over 4,000 characters, a label over 100 characters or with more than one line is
`422`, and a column the table does not have is `404` and names the columns it has.

## Rights

The table's owner (the principal that created it), or a principal holding `DROP_TABLE` on the table.
Both routes also need `READ_TABLE`, checked before anything is written. The same rights cover
every language: there is no separate action for labels or translations. A refusal is `403` and is
recorded as `AUTHORIZATION_DENIED`.

## Audit events

| Event | When | `detail` |
|---|---|---|
| `TABLE_DESCRIBED` | A table's edits were set or reset | `table`, `language` (the language edited), `old_length` and `new_length` (length of the description's edit, `0` for none) when the description was sent, `label_old_length` and `label_new_length` when the label was sent, `reset` (every field sent was `null`) |
| `COLUMN_DESCRIBED` | A column's edits were set or reset | the same, and `column` |

The text is not recorded: it may be long, and the table shows it to anyone who may read the table.
