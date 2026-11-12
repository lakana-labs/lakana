# Table list

Every table you may read, across environments and namespaces, in one list: the two routes, every
query parameter and field, the sorts, the cursor, who sees what, the SDK calls and the command.
For the steps, see [Find a table](../how-to/find-a-table.md#browse-every-table-you-may-read).

## The routes

| Route | Answers |
|---|---|
| `GET /api/v1/tables` | One page of the tables you may read, filtered, searched and sorted. |
| `GET /api/v1/tables/tree` | How many tables you may read there are in each environment and namespace. |

Both need a signed-in caller (`401` otherwise) and take no environment in the path.

## The query parameters of the list

| Parameter | Default | Meaning |
|---|---|---|
| `environment` | every environment you may read | Only this environment. If it does not exist: a `404` when you may read environments (`READ_ENVIRONMENT`), otherwise an empty page, as for one that exists. |
| `namespace` | every namespace | Only this namespace and the ones beneath it, dotted: `gold` lists `gold` and `gold.sales`, not `golden`. |
| `q` | no search | Only tables whose name, label, description or grain, or a column's name, label, description or measure, contains this text, in any letter case. 1 to 200 characters after trimming. `%`, `_` and `\` match themselves. The rules are those of [Search](search.md). |
| `model_kind` | any | Only tables whose asset header declares this kind: `fact`, `dimension`, `wide` or `staging`. |
| `sort` | `name` | What to sort by: `name`, `owner`, `health`, `rows`, `size`, `last_write`, `last_read` or `created`. |
| `order` | by sort | `asc` or `desc`. |
| `limit` | `50` | Tables per page, 1 to 200. |
| `cursor` | first page | The `next_cursor` of the previous page. |
| `lang` | your profile's language | The language in which translated texts are matched, as in [Search](search.md#languages). |

A value that is not valid is a `422` that names the choices or the range. A NUL character in
`environment`, `namespace` or `q` is refused.

## The fields of a table

The answer is `{"items": [...], "next_cursor": ..., "capped": ...}`. An item has:

| Field | Type | Meaning |
|---|---|---|
| `environment` | text | Its environment. |
| `namespace` | text | Its namespace, dotted. |
| `name` | text | Its name. |
| `uuid` | text | Its id. |
| `kind` | text | `TABLE`, `EXTERNAL`, `VIEW` or `ARTIFACT`. |
| `model_kind` | text or `null` | The `kind` its asset header declares. |
| `state` | text | `not_published` or `kept` for a managed table so marked (see [Check your data](../how-to/check-your-data.md)); `published` otherwise. |
| `owner` | object or `null` | `{"name", "kind"}` of the principal that owns it; `null` when that principal was deleted. |
| `created_at` | time | When it was created. |
| `row_count` | integer or `null` | Its rows, as stored with the last write. |
| `size_bytes` | integer or `null` | The size of its data files. `null` whenever `last_write_at` is `null`: a table without a snapshot, a view, an external table and an artifact have no size. |
| `last_write_at` | time or `null` | When its current snapshot was committed. |
| `last_read_at` | time or `null` | When a succeeded query last read it. |
| `readers_7d` | integer | How many principals read it in the last 7 UTC days, today included. Never who. |
| `health_summary` | object | Its health in one word: `state` (`failing`, `warning`, `healthy` or `not_judged`), `item`, `text` and `others`, how many other findings the word leaves out. See [Table health](table-health.md#the-health-in-one-word). |
| `match` | object or `null` | With `q`, what matched: `kind` (`table` or `column`), `matched` (the field), and for a column `column` and `column_type`, for a text the `snippet` and its `language` and whether it is a `translated` text. |

A table whose figures are not known yet shows them as `null`; it is listed all the same. On an
instance that has just been upgraded, the worker fills them in within its next sweeps.

## The sorts

| `sort` | Sorts by | Default order |
|---|---|---|
| `name` | name, then namespace, then environment | `asc` |
| `owner` | the owner's name | `asc` |
| `health` | the health word, the worst first | `asc` |
| `rows` | `row_count` | `desc` |
| `size` | `size_bytes` | `desc` |
| `last_write` | `last_write_at` | `desc` |
| `last_read` | `last_read_at` | `desc` |
| `created` | `created_at` | `desc` |

A table with no value for the key comes last, in either order. Ties are broken in a
fixed order, so a page never repeats or skips a table.

## Paging

A page has at most `limit` tables. When more follow, `next_cursor` holds an opaque text; send it
as `cursor` with the same filters and sort to get the next page. It is `null` on the last page. A
cursor that is not valid for the request is a `422`: start again without
it.

`capped` is `true` when the search text matched more of the tables you may read than one search
keeps (5,000). Narrow the text, or add `namespace` or `environment`.

## The tree

`GET /api/v1/tables/tree` answers `{"count", "environments": [{"name", "count", "namespaces":
[{"path", "count"}]}]}`. A count is what the list answers for the same filter without a search
text: a namespace counts the tables in it and beneath it, an environment all of its tables, and
`count` at the top every environment. Only tables you may read are counted. An environment is
shown if you may read it or any table in it, and a namespace if you may read the namespaces of its
environment or any table in or beneath it.

## Who sees what

A table is listed only if you may read it (`READ_TABLE` on the table, its namespace or its
environment, or a task token's asset scope). What you may not read is left out, never refused: the
list does not say that anything is missing, counts no hidden table, and records no denied
attempt. Reading the list writes no audit event.

Everyone who may read a table sees its owner, when it was last read and how many principals read
it in the last 7 days, never who read it. Who read it is for the administrator
([Table health](table-health.md#readers)).

The health word is the one you may see: a check of a model table that tells of a table you may not
read is left out of your word.

## In the SDK

`client.catalog.list(...)` returns one `TableListPage` (`items`, `next_cursor`, `capped`);
`client.catalog.iter(...)` yields every `TableListItem` across the pages; `client.catalog.tree()`
returns a `TableTree`. They take the parameters above as keyword arguments (`environment`,
`namespace`, `q`, `model_kind`, `sort`, `order`, `limit`, `cursor` and `lang`; `iter` has no
`cursor`).

```python
import lakana

client = lakana.Client()

# The ten tables written most recently, anywhere you may read.
page = client.catalog.list(sort="last_write", limit=10)
for item in page.items:
    print(item.environment, item.namespace, item.name, item.health_summary.text)

# Every table in silver or beneath it that mentions "order".
for item in client.catalog.iter(namespace="silver", q="order"):
    print(item.name, item.match.matched if item.match else "")

tree = client.catalog.tree()
print(tree.count, [(env.name, env.count) for env in tree.environments])
```

Over REST, the same request is a `GET` of `/api/v1/tables` with the parameters in the query and
your token as `Authorization: Bearer TOKEN`. The answer for `?q=order&limit=1`:

```json
{
  "items": [
    {
      "environment": "dev",
      "namespace": "silver",
      "name": "orders",
      "uuid": "6d2c3e1a-0c2b-4a2e-9a55-2f3f6a1b7c11",
      "kind": "TABLE",
      "model_kind": null,
      "state": "published",
      "owner": {"name": "ada", "kind": "USER"},
      "created_at": "2026-11-01T08:00:00Z",
      "row_count": 1200,
      "size_bytes": 48211,
      "last_write_at": "2026-11-10T08:00:00Z",
      "last_read_at": "2026-11-11T09:30:00Z",
      "readers_7d": 3,
      "health_summary": {"state": "healthy", "item": "healthy", "text": "Healthy", "others": 0},
      "match": {
        "kind": "table",
        "matched": "name",
        "column": null,
        "column_type": null,
        "snippet": null,
        "language": null,
        "translated": false
      }
    }
  ],
  "next_cursor": "eyJ2IjoxfQ",
  "capped": false
}
```

## On the command line

`lakana table list` without a namespace lists across namespaces, in the environment you pass with
`--env` or your profile's, or in every environment with `--all-environments`:

<!-- source: docs/examples/find-a-table/run.sh#table-list-search -->
```bash
lakana table list --search order --under silver
```

| Option | Meaning |
|---|---|
| `--search TEXT` | The `q` parameter. A `match` column says what matched. |
| `--under NAMESPACE` | The `namespace` parameter: this namespace and the ones beneath it. |
| `--all-environments` | Every environment you may read; cannot be combined with `--env`. |
| `--sort KEY`, `--order asc\|desc` | The `sort` and `order` parameters. |
| `--limit N` | Tables per page, 1 to 200; 50 by default. |
| `--cursor TEXT` | The cursor of the previous page. The command prints the full command for the next page. |

The columns are `name`, `namespace`, `environment`, `kind`, `owner`, `health` (the word, and
`(+N)` for N other findings), `rows`, `size`, `last_write` and `last_read` (UTC), and `match` with
`--search`. A value that is not known is shown as nothing. On a narrow terminal the least useful
columns are left out, and a line says how to see them; `--output json` has every field of the
table above.

With a namespace, `lakana table list NAMESPACE` keeps its own columns, `--model-kind`, `--lang`
and `--dropped`; the options of the two modes do not mix, and the command says which to leave out.
