# Descriptions

The description of a table and of a column: where it comes from, the commands, the SDK, the REST
routes, the rights and the audit events. For the steps, see [Describe a table](../how-to/describe-a-table.md).

## Two sources

| Source | Set by | Shown when |
|---|---|---|
| `project` | A deploy or a rollback, from the `description:` and `columns:` of the asset's header; or the creation of the table by a run, when the deployed project describes it | there is no edit |
| `edited` | `lakana table describe`, the SDK or the REST routes | always, once set |

A deploy never writes or removes an edit. A reset removes the edit. A column whose text the header no
longer has loses its project text at the next deploy. The text of a column that is no longer in the
table's schema is kept and is not shown; it shows again if the column comes back.

A description is at most 4,000 characters and is not blank. A table the project describes, but that
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

The texts are chosen in the first language that has one, in this order:

1. `?lang=` (REST), `--lang` (CLI) or `lang=` (SDK), such as `de` or `fr-CH`;
2. your profile's language, if one is set;
3. the request's `Accept-Language` header, by its weights (REST only; the CLI and the SDK do not
   send one);
4. the table's main language.

A regional tag falls back to its language: `fr-CH` shows a `fr` text. A text with no translation in
any of these languages shows in the main language, so one answer can mix languages; that is what
the `*_language` fields are for. A `lang` that is not a language tag is `422`, and the message gives
examples such as `fr-CH`; a malformed `Accept-Language` is ignored. The tags, the files and the
coverage are described in [Translations](translations.md).

`lakana table get` prints the label and the description, a column table with a `label` column
when a column has one, and the coverage, such as `Translations: fr 9 of 10`. A text in another
language than the one asked for ends in its language, such as
`Description (from the project) [en]: One row per sale.` with `--lang fr`.

## Commands

| Command | What it does |
|---|---|
| `lakana table describe NAME --text TEXT` | Sets the table's edit. |
| `lakana table describe NAME --column COLUMN --text TEXT` | Sets a column's edit. |
| `lakana table describe NAME [--column COLUMN] --reset` | Removes the edit. |
| `lakana table get NAME [--lang LANGUAGE]` | Shows the label, the description, the columns' labels and descriptions, and the coverage. |
| `lakana table list NAMESPACE [--lang LANGUAGE]` | Lists the tables; with `--output json`, their texts in that language. |

Exactly one of `--text` and `--reset` is required.

## SDK

`client.tables.describe(environment, "namespace.table", text, column=None)` returns the `Table`.
`text=None` resets. `Table.description`, `Table.description_source`, `Column.description` and
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
| `PATCH …/{name}` | `{"description": "text"}` or `{"description": null}` | The table (`200`) |
| `PUT …/{name}/columns/{column}/description` | the same | The table (`200`) |

`null` resets the edit, and a blank text is refused. The field must be sent: a body without
`description` is `422`. A text over 4,000 characters is `422`, a column the table does not have is
`404` and names the columns it has.

## Rights

The table's owner (the principal that created it), or a principal holding `DROP_TABLE` on the table.
Both routes also need `READ_TABLE`, checked before anything is written. There is no separate action. A refusal is `403` and is
recorded as `AUTHORIZATION_DENIED`.

## Audit events

| Event | When | `detail` |
|---|---|---|
| `TABLE_DESCRIBED` | A table's edit was set or reset | `table`, `old_length`, `new_length` (length of the edit, `0` for none), `reset` |
| `COLUMN_DESCRIBED` | A column's edit was set or reset | the same, and `column` |

The text is not recorded: it may be long, and the table shows it to anyone who may read the table.
