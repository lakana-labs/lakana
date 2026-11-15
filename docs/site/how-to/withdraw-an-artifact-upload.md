# List and withdraw an open upload

After this guide you can see the uploads you began and did not finish, and withdraw one that
holds a place it no longer needs. You need `READ_ENVIRONMENT` on the environment and
`WRITE_TABLE` on the artifact, and the upload must be your own.

An artifact is the output of an asset, and that is the normal way a version comes to exist: it
then has a run, a commit and lineage behind it. A version that a person stores with `lakana
artifact upload` has none of these (its run and commit are `null`) and is meant for testing, for
repair and for bringing along a model that exists already; Lakana is not a general file store. See
[upload](../reference/artifacts.md#upload).

Storing a version takes three steps: `lakana artifact upload` opens an **upload**, sends the
files, and commits. If the process is killed (a lost connection, a power cut, `kill -9`), nothing
withdraws the upload. It stays open, holds one of the artifact's 16 upload places, and keeps the
files it stored safe from deletion until it expires, one hour after it began by default. You see
it when a new upload is refused: ``models.churn has 16 uploads in progress, the most allowed at
once. Commit one, withdraw one you began (`lakana artifact uploads list models.churn` shows yours,
`lakana artifact uploads abort models.churn UPLOAD_ID` withdraws one), or wait for one to expire
(they expire after 3600 seconds), then try again.``

Most of the time you can simply wait. Withdraw an upload when you cannot.

## List your open uploads

The `artifact uploads list` subcommand of `lakana` shows your own open uploads, newest first,
across the environment. Give an artifact's name to see only its uploads. The example runs in an
environment where a process was killed in the middle of an upload of `models.abandoned_model`:

<!-- source: docs/examples/train-and-predict/run.sh#uploads-list -->
```bash
lakana artifact uploads list --env dev
```

<!-- output: train-and-predict#uploads-list -->
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ upload_id                            ┃ artifact               ┃ created_at                  ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ a8906dd2-aca6-46da-88cb-d8368776a48e │ models.abandoned_model │ 2026-10-07T04:54:44.223736Z │
└──────────────────────────────────────┴────────────────────────┴─────────────────────────────┘
Not shown: expires_at. Use --output json to see it.
Withdraw one with `lakana artifact uploads abort NAME UPLOAD_ID --env dev`.
```

Only your own uploads are listed. Another person's are never shown, and a service account sees
its own. A task sees the uploads of its own attempt. The list says when each began; a terminal
that is too narrow for the time it frees its place by itself (`expires_at`) hides that column and
says so, and `--output json` always has it. It does not say how many files were sent. An upload
frees its place one hour after it began unless the operator changed
`LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS`.

## Withdraw one

Copy the id from the first column and give it, with the artifact's name, to `uploads abort`. The
example reads the id into `UPLOAD_ID` first:

<!-- source: docs/examples/train-and-predict/run.sh#uploads-abort -->
```bash
lakana artifact uploads abort models.abandoned_model "$UPLOAD_ID" --env dev
```

<!-- output: train-and-predict#uploads-abort -->
```text
Withdrew upload a8906dd2-aca6-46da-88cb-d8368776a48e of dev/models.abandoned_model
```

The place is free at once. Files the upload had already stored stay until Lakana's periodic
clean-up finds that no version and no open upload needs them. The audit log records the withdrawal
as `ARTIFACT_UPLOAD_ABORTED`, with the artifact's name and the upload's id. The list is now empty:

<!-- source: docs/examples/train-and-predict/run.sh#uploads-list-empty -->
```bash
lakana artifact uploads list --env dev
```

<!-- output: train-and-predict#uploads-list-empty -->
```text
You have no open uploads in dev.
```

## When it fails

| You see | What it means | What to do |
|---|---|---|
| `You have no open uploads in dev.` | You have none: the upload expired, was committed or withdrawn, or another person began it. | Nothing. If the artifact is full of other people's uploads, wait for them to expire or ask their owners. |
| A permission error (exit code 5) | You may not read the environment, or, for `abort`, may not write the artifact. | Ask for the permission. |
| `This upload has expired or does not exist; start the upload again.` | The id is wrong, the upload is another person's, or it expired or was committed meanwhile. One text for every case, so nothing is revealed. | List again and copy the id. |
| `--cursor and --all can't be combined` | You gave both. | Use one. |

With more than 100 uploads, `--limit` (1 to 500) sets the page size, the last line names the
`--cursor` for the next page, and `--all` follows every page. `--output json` gives `items` (each
with `upload_id`, `artifact`, `created_at` and `expires_at`) and `next_cursor`, or, with `--all`,
the list of items. Every option is in the [reference](../reference/artifacts.md#uploads-list).
