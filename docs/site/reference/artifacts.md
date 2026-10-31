# Artifacts

This page lists every option, command, limit and message of the artifact store, to look up. After
reading it you can say what an asset must return to store an artifact, what a version is, which
limits apply, and what each command, SDK method and REST route does. For why artifacts work this
way, see [Artifacts: what they are and what they are not](../explanation/artifacts.md).

An **artifact** is a named file or directory of files that Lakana stores for you, for example a
trained model. Each time you store it again, Lakana keeps a new numbered **version**. An artifact
has one name in the catalog, like a table: `models.churn_model` is `<namespace>.<name>`, and a
name is either a table or an artifact, never both. A version is described by its **manifest**: the
list of its files, each with its path, its size and its SHA-256 hash.

## The `@asset` options

An asset becomes an artifact asset with `output="artifact"`. Everything else about a Python asset
stays as described in [Projects and deploys](../how-to/projects.md), "Python assets".

| Option | Values | Default | Meaning |
|---|---|---|---|
| `output` | `"table"` or `"artifact"` | `"table"` | `"table"`: the function returns a `polars.DataFrame` (or a `pyarrow.Table`) that becomes the table. `"artifact"`: the function writes files into `lakana.output_dir()` and returns a path, and each run stores one new version of the artifact. |
| `keep_versions` | an integer from 1 to 2147483647 | not set: keep every version | Only with `output="artifact"`. After a run stores a new version, versions beyond the newest `keep_versions` are deleted. See [Limit how many versions are kept](../how-to/limit-artifact-versions.md). |
| `decimals` | `"float"` or `"exact"` | `"float"` | For every Python asset, table or artifact. `"float"`: a SQL decimal column of an input table reaches the function as a `Float64` column. `"exact"`: it arrives as a `Decimal` column of `decimal.Decimal` values. See [Python assets](../how-to/projects.md#python-assets). |

Options that do not apply to an artifact asset:

- `materialize` and `columns` are refused. The decorator raises `materialize does not apply to an
  artifact asset; remove it` (or `columns does not apply ...`). A deploy says `argument
  'materialize' does not apply to an artifact asset; remove it` (or `'columns'`), naming the file
  and line.
- `write`, `key` and `watermark` are refused the same way (`argument 'write' does not apply to an
  artifact asset; remove it`), except `write="replace"` and `key=[]`, which change nothing: each
  run stores a new version of the artifact. `incremental_key` is not an `@asset` argument at all,
  so a deploy refuses it as `unknown argument 'incremental_key'`.
- `keep_versions` on an asset without `output="artifact"` is refused. The decorator raises
  `keep_versions applies only to an asset with output="artifact"`. A deploy says `argument
  'keep_versions' applies only to an asset with output="artifact"; remove it or set
  output="artifact"`, naming the file and line.
- `decimals` other than `"float"` or `"exact"` is refused. The decorator raises `decimals must be
  'float' (Python floats, the default) or 'exact' (decimal.Decimal values), got 'fixed'`. A deploy
  says `argument 'decimals' must be 'float' or 'exact', got 'fixed'`, naming the file and line.
- `output`, `keep_versions` and `decimals` in the front matter of a SQL file are refused: `` `output` applies
  only to a Python asset, not to a SQL file; declare the asset in a `.py` file with
  `@asset(output="artifact")`, or remove `output` from this block``.

`inputs`, `description`, `owner`, `tags`, `retries`, `retry_delay` and `timeout` work as for a table
asset. Like every `@asset` argument, they must be literals in the file.

### What the function returns

An artifact asset gets an empty directory from `lakana.output_dir()`, writes its files there, and
returns a `pathlib.Path` that lies inside it:

- a **file** inside the directory: the artifact is that one file, stored under the file's name;
- **the directory itself**, or a **subdirectory** of it: the artifact is that directory with
  everything below it.

`lakana.output_dir()` returns a private, empty directory of this task attempt. You can call it at
the top of the module or inside the function. Outside a task, or in an asset that produces a table,
it raises `RuntimeError`: `lakana.output_dir() is available only while an asset declared with
output="artifact" runs as a task`.

An asset that stores or reads an artifact needs a worker on Linux or macOS. On another system the
task fails, before anything is made, with `an artifact asset needs a platform that opens files relative to a directory without following links
(Linux or macOS); run the worker there`.

Returning anything else fails the task for good, without a retry. The messages say what to return:

| The task fails with | Cause | Next step |
|---|---|---|
| `an artifact asset must return a pathlib.Path inside lakana.output_dir() (a file, or the directory itself), got <type>` (for a string it adds `; wrap it in pathlib.Path`) | The function returned a string, `None`, a DataFrame or another type. | Return `pathlib.Path(...)`. |
| `the asset returned <path>, which is not inside lakana.output_dir(); write the artifact's files there and return that directory or a file in it` | The path points elsewhere. | Write into `lakana.output_dir()`. |
| `the asset returned <path>, which does not exist; write the file before returning its path` | The file is not there. | Write the file first. |
| `the asset returned <path>, which passes through a symbolic link; an artifact holds only regular files and directories` | A link on the way. | Return a real file or directory. |
| `The directory has no files; an artifact must contain at least one file.` | The directory is empty. Empty subdirectories are never stored. | Write at least one file. |

The same rules as for `lakana artifact upload` apply to what is stored: see "What a path may be".
Do not leave a thread or process of your own running that changes the output directory, or calls
`os.chdir`, while the function returns: the upload can fail or store something else than you
expect. The harness checks that the directory it stores is the one it was given, but the check
is by name and your own code can defeat it (a known limit, tracked as #67).

### What a consuming asset receives

When an input of an asset names an artifact, its parameter receives a `pathlib.Path` instead of a
DataFrame. The asset that receives it is the **consumer**; the asset that stores the artifact is
the **producer**:

- a **file**, when the producer returned a file. The path ends in the file's name.
- a **directory**, when the producer returned a directory.

The path points to a local copy that the task owns. It is read-only (files 0400, directories 0500)
and is removed when the attempt ends. To change something, copy it elsewhere.

Table inputs of the same asset still arrive as DataFrames. A table asset or an artifact asset can
read an artifact. Two rules are checked at deploy:

- A SQL asset cannot read an artifact. A deploy says `` `models.churn_model` is an artifact; only a
  Python asset can read it.`` A query that names one says `models.churn_model is an artifact; SQL
  cannot read it. Only a Python asset can read an artifact: declare it in the asset's inputs.`
- An artifact asset cannot read its own artifact: `an artifact asset cannot read its own artifact
  (models.churn_model); remove it from inputs`.

The parameter that reads an artifact must be an ASCII identifier of at most 128 characters (letters,
digits and underscores, not starting with a digit), because the task stores the copy in a directory
of that name. Two such parameters may not differ only in case. A deploy refuses anything else and
says to rename the parameter, in the function and in `inputs`.

Which version a consumer reads: the newest live version that a task of **its own run** stored, and
otherwise the current version (see "Versions"). The consumer's lineage records that version.

### The two assets, as code

The [tutorial "Train a model and score with it"](../tutorials/train-a-model.md) has a complete
example that runs. This is a different example from the tutorial's: the first asset stores a small
JSON file; the second reads it. The block is parsed for syntax, not run.

```python
import json
from pathlib import Path

import polars as pl

import lakana
from lakana import asset


# assets/models/spend_threshold.py  ->  artifact models.spend_threshold
@asset(inputs={"orders": "silver.orders"}, output="artifact", keep_versions=5)
def spend_threshold(orders: pl.DataFrame) -> Path:
    """The spend above which a customer counts as a big spender."""
    path = lakana.output_dir() / "threshold.json"
    path.write_text(json.dumps({"threshold": orders["amount"].quantile(0.9)}))
    return path


# assets/gold/big_spenders.py  ->  table gold.big_spenders
@asset(inputs={"rule": "models.spend_threshold", "customers": "gold.customers"})
def big_spenders(rule: Path, customers: pl.DataFrame) -> pl.DataFrame:
    """Customers whose spend is above the threshold."""
    threshold = json.loads(rule.read_text())["threshold"]
    return customers.filter(pl.col("monthly_spend") > threshold)
```

With `layers:` in `lakana.yaml`, an artifact asset must be under `assets/bronze/`,
`assets/silver/` or `assets/gold/`, and follows that layer's `reads`; a layer whose `write` is
empty refuses it ([`layers`](lakana-yaml.md#layers)).

### Failures while a run goes

These are the texts a task or a run shows, as the code prints them. `models.x` stands for the
artifact's name, `'svc-run'` for the run principal's name.

| You see | What it means | What to do |
|---|---|---|
| `models.x has no version yet; run the asset that produces it` | An artifact input has no live version. The task is not retried. | Run the producer first, or run both assets in one run. |
| `models.x cannot be read by the run's principal 'svc-run', or does not exist; grant read on it or check the name` | The principal that runs the project may not read the artifact, or the name is wrong. The answer is the same on purpose. Not retried. | Check the name, then grant read on it. |
| `models.x is a table, but this deploy recorded it as an artifact; redeploy so that the asset reads it as a table` (or `a view`, `an external table`) | When the consumer was deployed, the name was an artifact (or was taken to be one because another repository deploys it as one); now it is a table. Not retried. | Redeploy the consuming repository. The asset then gets a DataFrame. |
| `models.x is an artifact, but this deploy recorded it as a table; redeploy so that the asset receives a path` | When the consumer was deployed, the name was a table; now it is an artifact. Not retried. | Redeploy the consuming repository. The asset then gets a path. |
| ``models.x is a table, not an artifact. Drop it (`lakana table delete models.x`) or give the asset another name and redeploy.`` (or `a view`, `an external table`) | An artifact asset has the name of a table, view or external table that appeared after its deploy. Not retried. | Drop the table with the command the message gives, or give the asset another file name and deploy. |
| `reading input 'model' (models.x, version 3) failed: <reason>` | The download of an input failed; the reason is the server's or the SDK's text. A 404, such as `` Version 3 of models.x was deleted; its bytes are gone. Read a version that exists: `lakana artifact versions list models.x --env dev` shows which. ``, is retried, and the next attempt resolves the version again. A refusal (403, for example a grant that was revoked after the task was prepared) is not retried. A stored manifest the client rejects (damaged, or over a limit such as a path it refuses) is not retried either: `The stored manifest of version 3 does not match its digest, so this version is damaged on the server and downloading it again will not help. Tell the operator, and store the artifact again (run the asset that produces it, or use `lakana artifact upload`).` | Read the reason. For a deleted version, nothing: the retry picks a version that exists. For a rejected manifest, tell the operator and store the artifact again. |
| `Task 'models.x' produces a table; it cannot add an artifact version. To store an artifact, declare the asset with output="artifact".` | Code in a table asset called `client.artifacts.upload` for its own name. | Declare the asset with `output="artifact"`, or do not store an artifact from it. |
| `Task 'models.x' produces an artifact; it cannot write a table. Write the artifact's files into lakana.output_dir() instead.` | Code in an artifact asset tried to write a table of its own name. | Write files into `lakana.output_dir()` and return the path. |
| ``models.x has 16 uploads in progress, the most allowed at once. Commit one, withdraw one you began (`lakana artifact uploads list models.x` shows yours, `lakana artifact uploads abort models.x UPLOAD_ID` withdraws one), or wait for one to expire (they expire after 3600 seconds), then try again.`` | The artifact has 16 open uploads. A task attempt is retried. A new attempt of a task first deletes its own earlier open uploads and those of that task run's earlier attempts, so only uploads of people and of other task runs can still fill the 16 places. | Wait for the retry, list your uploads and withdraw one you began, or wait up to the upload lifetime. |
| `the project version's manifest has version 5, but this worker understands versions 1 to 4; update the worker to the platform version that deployed it, or redeploy with this worker's version` (the numbers differ) | The worker is older than the platform that deployed the project. Not retried. | Update the worker, or redeploy with the worker's version. |

A task whose process is killed or times out **after** its version was committed is shown as failed or
timed out, and the version stays live. Its retry stores a newer one. See "Versions".

## What a path may be

A version is a list of files, each with a path inside the artifact. The SDK checks these rules
before it sends a byte, and the server checks them again, so a hand-made client meets the same
refusals. A path:

- is relative, with `/` between its parts, and has no leading `/`;
- has no empty part and no part that is `.` or `..`;
- has no backslash and no control, format, surrogate or line-separator character;
- has no part that contains `:` (for example `ckpt:2024`) or ends in `.` or a space;
- has no part named like a Windows device (`CON`, `PRN`, `AUX`, `NUL`, `COM0` to `COM9`, `LPT0` to
  `LPT9`, the same with the superscript digits `¹`, `²` and `³` in place of the digit, `CONIN$`,
  `CONOUT$`), in any case, with or without an extension: `nul.txt` is refused;
- is at most 1024 bytes long (UTF-8) and has at most 32 parts, each at most 255 bytes;
- is unique when upper and lower case and Unicode forms are folded together: `Model.bin` and
  `model.bin` in one artifact are refused, and so are two directories that differ only that way,
  and a name that is both a file and a directory.

A single-file artifact has exactly one file, and its path is a file name without `/`.

A refusal names the path and what to do, for example `ckpt:2024 must not contain ':'; rename it.`

What the upload refuses on disk, naming the path:

- a **symbolic link** anywhere in the tree: `<path> is a symbolic link; an artifact holds only
  regular files and directories. Copy what it points to instead.`
- a **device, socket or pipe**: `<path> is not a regular file (a device, a socket or a pipe); an
  artifact holds only regular files.`
- a file that is **hard-linked from outside** the directory: `<path> is hard-linked from outside
  the directory; copy the file instead.` A single file you name that has another hard link says
  `<path> has another hard link, so its content is reachable under a second name; copy the file
  and give the copy.`
- a file that **changed while it was read**: `<path> changed after it was scanned, so nothing was
  stored from it. Wait until whatever writes it has finished, then run the upload again.`
- a file whose **read failed** while it was sent: `<path> could not be read: <the operating
  system's reason>. The upload was stopped and can be tried again once the file is readable.` (a disk error, a network file system that went away).
  This is not the message above: nothing waits for a writer to finish.

Everything else in a directory is stored, **hidden files such as `.env` included**. Point the upload
at a directory that holds only the artifact. An empty directory is not stored, because a version
is a list of files.

## Limits

An operator sets four limits with environment variables of the server. A request over a limit is
refused before the file is sent, and the message names the setting.

| Setting | Default | Meaning |
|---|---|---|
| `LAKANA_ARTIFACTS__MAX_FILE_BYTES` | 2147483648 (2 GiB) | Largest single file of a version. |
| `LAKANA_ARTIFACTS__MAX_BYTES` | 10737418240 (10 GiB) | Largest total size of one version. |
| `LAKANA_ARTIFACTS__MAX_FILES` | 10000 | Most files in one version. |
| `LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS` | 3600 (one hour) | How long an upload may stay open, and how long its commit can be repeated. |

The server and the worker must read the same `LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS`. How to set
them: [Deploying with Compose](../how-to/deploy-with-compose.md), "Storage for artifacts".

When one is exceeded, the CLI and the SDK say, for example:

- `<file> is <n> bytes, more than the 2147483648 bytes one file may have (the server's operator
  sets this limit, LAKANA_ARTIFACTS__MAX_FILE_BYTES). Store a smaller file, or split it, or ask the
  operator for a larger limit.`
- `The files add up to more than the 10737418240 bytes one artifact may have (the server's operator
  sets this limit, LAKANA_ARTIFACTS__MAX_BYTES). Store fewer or smaller files, or ask the operator
  for a larger limit.`
- `The directory has more than the 10000 files one artifact may have (the server's operator sets
  this limit, LAKANA_ARTIFACTS__MAX_FILES). An artifact is one model or dataset: point the upload at
  the directory that holds just that, or pack many small files into one archive, or ask the
  operator for a larger limit.`

A client reads the three size limits from the server once, so it refuses a tree before it sends
anything. A server answer to a hand-made request names the same setting: `The artifact has <n>
files, more than LAKANA_ARTIFACTS__MAX_FILES (10000). Store fewer files, or ask the operator to
raise the setting.`

Limits that are fixed:

| Limit | Value | What you see |
|---|---|---|
| Open uploads of one artifact | 16 | ``<name> has 16 uploads in progress, the most allowed at once. Commit one, withdraw one you began (`lakana artifact uploads list <name>` shows yours, `lakana artifact uploads abort <name> UPLOAD_ID` withdraws one), or wait for one to expire (they expire after 3600 seconds), then try again.`` A commit, a withdrawal and expiry free a place. So does a new attempt of a task: before it counts, it deletes the open uploads of its own task run that carry its own claim number or a lower one (its earlier uploads and those of earlier attempts, which can never commit). Uploads of people and of other task runs, and uploads that were committed, stay. |
| Version number | at most 2147483647 | `<name> has reached the highest version number (2147483647); store further versions under a new artifact name.` |
| Request that opens an upload (the manifest) | 16 MiB | `The manifest request is larger than 16777216 bytes. It lists every file with its path and hash: store fewer files or give them shorter paths (the limit is fixed).` (413). The SDK raises `PayloadTooLarge` at once, and a task fails for good: it is not retried. |
| Manifest requests read and decoded at once, per server process | 2 | A request over the limit is answered 503 with `Retry-After: 1` and the text `Too many concurrent uploads; retry shortly.`, before its body is read. A slow writer holds one of the two places while its body arrives, for at most `LAKANA_APPEND__READ_TIMEOUT_SECONDS`. The SDK asks again to open the upload: one second after each refusal, up to five tries in all (it does not read `Retry-After`). |
| Concurrent downloads of files, per server process | 32 | A request over the limit is answered 503 with `Retry-After: 1`. The SDK asks again for about 30 seconds (six tries), then fails with a message that the server was busy with other downloads. |
| Slowest download | 64 KiB/s | A file gets 60 seconds plus its size at 64 KiB/s. A slower connection is cut, and the SDK says `The connection to the server ended before <path> was complete; nothing was kept.` and names this rate. |

Files are sent through the server. Uploading a file takes one of `LAKANA_APPEND__MAX_CONCURRENT`
slots (default 4, shared with table appends) per server process: when all are taken the server
answers 503 of type `AppendConcurrencyLimitError` and the SDK waits and sends the file again, up to five tries in all. It waits as long as the answer's `Retry-After` says when that is a whole number of seconds (at most 10 seconds each time), and one second when the header is absent, is not a whole number of seconds, or is a date: the server's `Retry-After` is 1 for the busy answer and 5 for `StorageUnavailableError`. Every 503 on a file is sent again, also the server's `StorageUnavailableError` and a proxy's: files are addressed by their hash, so a second copy of a stored file is answered "already had it" and does no harm. Opening an upload is asked again only after the busy answer, as a proxy's 503 may follow an upload the server already opened. A file upload that sends no byte for
`LAKANA_APPEND__READ_TIMEOUT_SECONDS` (default 300) is refused with 408.

## Versions

- Versions are numbered from 1 for each artifact. A number is never used twice, and numbers can
  have gaps (a version that was deleted keeps its number, and so does one whose commit failed
  after the number was chosen). The next version is not always the highest number plus one.
- The **current version** is the highest version whose files still exist. `artifact versions list`
  and the SDK show deleted versions too.
- A version is **immutable**: Lakana never changes its files. The same files give the same
  `manifest_sha256`: the SHA-256 of a canonical list of the paths, sizes and file hashes. Two
  versions with equal digests have equal content.
- A file that an artifact already stores (the same bytes) is not sent again. After `Stored ...`,
  a second line such as `Sent 1 file; none were already stored.` or `Sent 1 file; 2 already stored.`
  tells you how many were new.
- Dropping an artifact and creating the name again starts at version 1, as a new artifact.
- A task that stored its version and was then killed or timed out (the process ended after the
  commit) is shown as failed or timed out, and its version **stays live**: Lakana does not delete
  it. The task is retried under the asset's policy and its retry stores a newer version. With
  `keep_versions=1` the previous version is already deleted at that point, so until a retry
  succeeds only the killed task's version is left.

What a version records:

| Field | Meaning |
|---|---|
| `version` | The number. |
| `size_bytes`, `file_count`, `is_directory` | Total size, number of files, and whether it is a directory or one file. |
| `manifest_sha256` | The digest of the manifest. |
| `created_at`, `created_by` | When and by whom (the principal's name). |
| `commit_sha`, `project_version_id`, `run_id`, `task_run_id` | For a version a run stored: the git commit of the deployed code, the deploy (project version), the run and the task. `null` for a version a person uploaded (see [upload](#upload)). A task token sees `null` for `project_version_id`, `run_id` and `task_run_id`: it reads what it consumes, not the identifiers of other runs. |
| `deleted_at`, `deleted_by` | For a deleted version: when, and by whom (for a version removed by `keep_versions`, the principal that stored the newer one). |
| `files` | Path, size and SHA-256 of each file. Shown for one version, empty for a deleted one. |

## Commands

`--env NAME` picks the environment. Without it the profile's default is used. `--output json`
(a global option of `lakana`, placed before the command) prints JSON instead of text. A command
that needs a name takes `NAMESPACE.NAME`, such as `models.churn_model`.

| Command | What it does | Needs |
|---|---|---|
| `lakana artifact list [--namespace NS]` | Lists the artifacts you may read: name, namespace, current version, how many versions, size of the live versions. | `READ_TABLE` on each |
| `lakana artifact versions list NAME` | Lists the versions, newest first: number, time, size, file count, the start of the digest, the run, when deleted. The last line says how many versions have bytes and their size. A narrow window leaves columns out, least useful first, and says which. The whole digest is in `--output json`. | `READ_TABLE` |
| `lakana artifact download NAME DIRECTORY [--version N]` | Downloads a version (the current one without `--version`) into a new or an empty directory. | `READ_TABLE` |
| `lakana artifact upload NAME PATH` | Stores a file or directory as a new version. Creates the artifact on first use. | `WRITE_TABLE`, and `CREATE_TABLE` for a new name |
| `lakana artifact versions delete NAME N [--yes]` | Deletes the files of version N and says how many files and bytes it freed, and how many it kept and why. | `DROP_TABLE` |
| `lakana artifact drop NAME [--yes]` | Drops the artifact: every version, its files and its grants. | `DROP_TABLE` |
| `lakana artifact uploads list [NAME]` | Lists your own open uploads, newest first: id, artifact, when begun, when it expires. | `READ_ENVIRONMENT`, and `WRITE_TABLE` on each artifact |
| `lakana artifact uploads abort NAME UPLOAD_ID` | Withdraws an upload that was begun and never finished. | `WRITE_TABLE` |

`upload`, `versions delete` and `drop` also take `--break-glass --reason "<why>"` for a protected
environment (see "Permissions").

### list

<!-- source: docs/examples/train-and-predict/run.sh#artifact-list -->
```bash
lakana artifact list --env dev
```

You see one row per artifact:

<!-- output: train-and-predict#artifact-list -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┓
┃ name        ┃ namespace ┃ current_version ┃ versions ┃ size  ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━┩
│ spend_model │ models    │ 1               │ 1        │ 639 B │
└─────────────┴───────────┴─────────────────┴──────────┴───────┘
```

An artifact you may not read is not listed, and a namespace you may not see gives an empty list.

### versions list

<!-- source: docs/examples/train-and-predict/run.sh#artifact-versions -->
```bash
lakana artifact versions list models.spend_model --env dev
```

You see one row per version, newest first. A time or a run identifier is never cut. If the window
is too narrow for every column, the table leaves columns out, least useful first: `file_count`,
`size`, `run_id`, `deleted_at` and last `manifest_sha256`. A line below the table names them. In
a window it ends with "Widen the terminal or use --output json." Where the output is not a
terminal (a pipe or a file, as for the output below, which was made in a window of 120 characters
and written to a file) it ends with "Use --output json to see it." The whole digest and the whole
record are in `--output json`:

<!-- output -->
```text
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ version ┃ created_at                  ┃ size  ┃ manifest_sha256 ┃ run_id                               ┃ deleted_at ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ 2       │ 2026-10-15T04:29:51.269544Z │ 639 B │ 420bf73218ec    │ 5ffb69b9-7cea-49f8-a277-1d9fab9a5c09 │            │
│ 1       │ 2026-10-15T04:29:37.608004Z │ 639 B │ 420bf73218ec    │ 08e77e55-6054-40f4-a659-6d2992b8bb48 │            │
└─────────┴─────────────────────────────┴───────┴─────────────────┴──────────────────────────────────────┴────────────┘
Not shown: file_count. Use --output json to see it.
Total: 2 versions with bytes, 1.2 KiB
```

A deleted version shows its `deleted_at`. If an upload says that the server did not confirm its
commit, look here for a version with the `manifest_sha256` the message names (with
`--output json` if the table leaves the digest out). If the message says
the connection failed before the commit was sent, no version was stored: check the network and the
profile's URL, then run the upload again.

### download

<!-- source: docs/examples/train-and-predict/run.sh#artifact-download -->
```bash
lakana artifact download models.spend_model ~/spend-model --env dev
ls ~/spend-model
```

The command names the file it made, and `ls` lists the folder:

<!-- output: train-and-predict#artifact-download -->
```text
Downloaded dev/models.spend_model version 1 to /home/you/spend-model/model.joblib
model.joblib
```

`DIRECTORY` must not exist, or must be empty. The files appear there only when all of them have
arrived and match the manifest; if anything fails, nothing is left there. The command prints
`Downloaded <environment>/<name> version <n> to <path>`: for an artifact that is one file, `<path>`
is the file, as above. The copy is private to you: files 0600 and directories 0700 (less your umask), nothing
executable. To open it to other people, give them read permission with `chmod` (recursively, mode
`a+rX`). A read-only copy (a task's input) is removed after you give yourself write permission
again with `chmod` (recursively, mode `u+w`). See [Inspect and download versions](../how-to/inspect-and-download-artifact-versions.md).

A download that is killed can leave a directory named `.<name>.lakana-partial-<random>` next to
the target, or, when the target was an existing empty directory, inside it. It holds an unfinished
copy. Remove it and download again.

### upload

**What an upload is for.** An artifact is the output of an asset, and that is the normal way a version
comes to exist: it then has a run, a commit and lineage behind it. A version that a person uploads
has none of these: its `commit_sha`, `project_version_id`, `run_id` and `task_run_id` are `null` (the
version list shows no run), only `created_by` names the person, and `lakana artifact upload`
records no sources for lineage. Use it for testing, for repair (storing a version again after
damage) and to bring along a model that exists already. Lakana is not a general file store, and
nothing more is planned around uploads for now.

<!-- source: docs/examples/train-and-predict/run.sh#upload -->
```bash
lakana artifact upload models.scratch_model ./model-output --env dev
```

Here `./model-output` is a directory with one file in it, and `models.scratch_model` is a new name,
a test artifact for the examples of this reference and of [Delete an artifact
version](../how-to/delete-an-artifact-version.md). Use a name of your own: an upload to the name of
an artifact that exists stores a new version of it.
The command prints the version it stored with the number and the size of its files, then how many
files it sent:

<!-- output: train-and-predict#upload -->
```text
Stored dev/models.scratch_model version 1 (1 file, 33 B)
Sent 1 file; none were already stored.
```

The command never deletes older versions: only a run prunes, with the asset's `keep_versions` (see
[Limit how many versions are kept](../how-to/limit-artifact-versions.md)). The namespace must
exist; otherwise the command fails with `Namespace models not found in 'dev'.`

If the upload fails, or the server refuses the commit, the command withdraws its upload, so it does
not keep one of the artifact's 16 places. If the server's answer to the last request is lost,
the command says that the version may or may not exist, and names the `manifest_sha256` to look
for with `lakana --output json artifact versions list` (the table can leave the digest out in a
narrow window; the JSON never does). Run the upload again only if that version is not there.

### versions delete

<!-- source: docs/examples/train-and-predict/run.sh#delete-version -->
```bash
lakana artifact versions delete models.scratch_model 3 --env dev --yes
```

The example removes version 3 of `models.scratch_model`, the test artifact from `upload`, which
has three versions. Put the name of **your** artifact there. `--yes` skips the question, so check
the name before you press Enter. It is for scripts: without it, the command asks you to confirm and,
with no terminal, refuses. See [Delete an artifact version](../how-to/delete-an-artifact-version.md).

The command prints what the delete did, for example `Deleted version 3 of dev/models.scratch_model.
Freed 1 file (33 B).`, and a second sentence when files stayed: `Kept 1 file (33 B) that version 2
also uses.` A file stays for one of three reasons, the same words in `--output json`, the SDK and
the REST answer:

| `reason` | Meaning |
|---|---|
| `other_version` | A version that is not deleted has the file. `version` is the newest such version, and is `null` unless you may also read the artifact (`READ_TABLE`). |
| `open_upload` | An open upload of the artifact lists the file. The answer names neither the upload nor who began it. |
| `left_to_sweep` | This request did not remove the file and Lakana's periodic clean-up decides: its budget of 200 storage deletes or 5 seconds was spent, a storage delete failed, a manifest could not be read, or storage holds manifests the catalog does not know. |

`--output json` has `freed_files`, `freed_bytes`, `kept_files`, `kept_bytes` and `kept`, a list of
`{reason, files, bytes, version}`, next to the version's own fields. Files count once however many
paths of the version point at them. A server older than this release does not send the counts; the
command then says so.

### drop

<!-- source: docs/examples/train-and-predict/run.sh#drop -->
```bash
lakana artifact drop models.scratch_model --env dev --yes
```

The example drops `models.scratch_model`, the test artifact from `upload`. Put the name of **your**
artifact there. As for a version, `--yes` skips the question, so check the name before you press
Enter. See
[Delete an artifact version](../how-to/delete-an-artifact-version.md#drop-the-whole-artifact).

The server removes the files inside the request, so an artifact with thousands of files can take
minutes (the SDK waits up to ten). A reverse proxy in front of the server must allow a request that
long, or you see a 504 for a drop that still finishes.

### uploads list

<!-- source: docs/examples/train-and-predict/run.sh#uploads-list -->
```bash
lakana artifact uploads list --env dev
```

The example runs where a process was killed in the middle of an upload of
`models.abandoned_model`; see [List and withdraw an open
upload](../how-to/withdraw-an-artifact-upload.md) for its output.

Lists the uploads you began and did not finish, newest first, across the environment; give an
artifact's name (`lakana artifact uploads list models.churn`) to see only its uploads. Only your
own are listed, never another principal's, and the answer does not say that others exist. A
task attempt sees the uploads of its own attempt. An upload in an artifact you may no longer
write is left out, because you could not withdraw it. The list does not say how many files an
upload has sent. A line below the table names the next step, `uploads abort`.

`--limit` (1 to 500, default 100) is the page size, `--cursor` continues from the `More:
--cursor ...` line of the previous page (with the same artifact name), and `--all` follows every
page (not together with `--cursor`). `--output json` prints `items` (each `upload_id`,
`artifact`, `created_at`, `expires_at`) and `next_cursor`; with `--all` it prints the list of
items.

### uploads abort

<!-- not tested: needs the id of an upload that was begun and not finished; the how-to page runs it -->
```bash
lakana artifact uploads abort models.churn_model 3f2b9c1e-5a4d-4c8e-9b1a-0d6e7f8a9b0c --env dev
```

You rarely need this, because `upload` withdraws its own upload when it fails or you press Ctrl-C.
An artifact allows 16 open uploads. An upload whose process was killed is found with `uploads
list`; if you do not withdraw it, it frees its place after the upload lifetime, one hour by
default. Only the principal that began an upload can withdraw it, and only while it is open. Files
it already stored stay until Lakana's periodic clean-up finds that nothing needs them.

### What other `lakana` commands show

- `lakana table get NAME` shows the artifact's name, namespace, kind `ARTIFACT` and id, and
  points to `artifact versions list`.
- `lakana table list NAMESPACE` includes the artifacts of the namespace.
- `lakana table lineage NAME` shows an artifact as `<name> [artifact]`.
- `lakana table delete NAME` refuses an artifact and says to use `lakana artifact drop`. `table
  append`, `table replace`, `table maintain` and a rename refuse it too. Row filters and column
  masks cannot be attached to it.
- The Iceberg REST catalog does not show artifacts.
- `lakana audit list --event-type ARTIFACT_DOWNLOADED` reads the events listed under "Audit events"; it needs the permission to read the audit log.

## SDK

`client.artifacts` has the methods below. This block is parsed for syntax; the types are in
`lakana`. Each takes the environment name first and the dotted `"namespace.name"` second.

```python
class Artifacts:
    def upload(
        self,
        environment: str,
        qualified_name: str,
        path: str | os.PathLike[str],
        *,
        sources: Mapping[str, int | None] | None = None,
        break_glass_reason: str | None = None,
        limits: ScanLimits | None = None,
    ) -> ArtifactVersion: ...

    def list(self, environment: str, namespace: str | None = None) -> list[Artifact]: ...

    def get(self, environment: str, qualified_name: str) -> Artifact: ...

    def versions(self, environment: str, qualified_name: str) -> list[ArtifactVersion]: ...

    def version(
        self, environment: str, qualified_name: str, version: int | None = None
    ) -> ArtifactVersion: ...

    def download(
        self,
        environment: str,
        qualified_name: str,
        directory: str | os.PathLike[str],
        *,
        version: int | None = None,
        read_only: bool = False,
    ) -> ArtifactDownload: ...

    def delete_version(
        self,
        environment: str,
        qualified_name: str,
        version: int,
        *,
        break_glass_reason: str | None = None,
    ) -> DeletedArtifactVersion: ...

    def drop(
        self, environment: str, qualified_name: str, *, break_glass_reason: str | None = None
    ) -> None: ...

    def abort_upload(self, environment: str, qualified_name: str, upload_id: str) -> None: ...

    def list_uploads(
        self,
        environment: str,
        qualified_name: str | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ArtifactUploadPage: ...

    def iter_uploads(
        self, environment: str, qualified_name: str | None = None, *, page_size: int = 100
    ) -> Iterator[ArtifactUpload]: ...
```

- `version(...)` without a number returns the current version, with its files.
- `versions(...)` returns every version without its files and is not paged.
- `download(...)` with `read_only=True` makes files 0400 and directories 0500, as a task gets.
  The result's `path` is the directory, or for a one-file artifact the file.
- `upload(...)` returns the new version with `files_sent` and `files_reused` set, and
  `deleted_versions` listing what `keep_versions` removed.
- `list_uploads(...)` returns one page of your own open uploads, newest first: `items` (each an
  `ArtifactUpload` with `upload_id`, `artifact`, `created_at`, `expires_at`) and `next_cursor`;
  `iter_uploads(...)` yields them all. Withdraw one with `abort_upload`.
- `delete_version(...)` returns a `DeletedArtifactVersion`, which is an `ArtifactVersion` with
  `freed_files`, `freed_bytes`, `kept_files`, `kept_bytes` and `kept` (a list of `ArtifactKept` with
  `reason`, `files`, `bytes` and `version`) added; code that only reads the version works as
  before. The counts are `None` from a server older than this release, and a `reason` you do not
  know means the same as `left_to_sweep`.
- `sources` (upload) records what the version was made from, for lineage: a table, view or
  artifact name mapped to the artifact version that was read, or `None` for the current version
  at commit time. Pass the number when the code must name the exact version it read. For a table
  write, `client.tables.append(...)` and `client.tables.replace(...)` take `source_versions`, a
  `Mapping[str, int]` of name to version number (a value cannot be `None` there), and a separate
  `sources` list of names for sources whose version you do not name. The REST routes take a
  repeatable `source_version=<version>:<name>` and a repeatable `source=<name>`; one write names at most 64
  of the two kinds, each. A source you may not read is refused. A version that was deleted after the code read it is still accepted as a source, and the lineage records its number; a version number that never existed is refused.
- Errors are the usual ones from `lakana.errors`: `PermissionDenied`, `NotFound`, `Conflict`,
  `ValidationError` (a path or limit problem, with the message above), `ArtifactCorrupt` (a
  download that does not match its manifest) and `LakanaConnectionError`.
- `limits` (upload) changes what the local scan looks at before it gives up. Leave it out: the SDK
  asks the server for the operator's limits.
- `upload` and `download` are the only methods that move file bytes. Nothing holds a whole file in
  memory.

## REST

Twelve routes under `/api/v1/environments/{env}`. `{path}` is the namespace and `{name}` the
artifact. The CLI and the SDK use only these, and the SDK also reads the artifact limits from the
server's info route (`GET /api/v1/info`).

| Method and path | Action | Required permission |
|---|---|---|
| `POST /namespaces/{path}/artifacts/{name}/uploads` | Begin an upload: send the manifest; the answer lists the files the store does not have. Creates the artifact on first use. | `WRITE_TABLE`, and `CREATE_TABLE` for a new name |
| `PUT /namespaces/{path}/artifacts/{name}/uploads/{upload_id}/blobs/{sha256}` | Send one file. | `WRITE_TABLE` |
| `POST /namespaces/{path}/artifacts/{name}/uploads/{upload_id}/commit` | Turn the complete upload into the next version. Repeating it for the same upload returns the same version (the sources of the first commit stand). | `WRITE_TABLE`, and `READ_TABLE` on each source |
| `GET /artifacts/uploads` (optional `name`, `limit`, `cursor` queries) | List your own open uploads, newest first: `{items: [{upload_id, artifact, created_at, expires_at}], next_cursor}`. `name` is `namespace.name`; `limit` is 1 to 500 (default 100); `cursor` is the previous `next_cursor`. | `READ_ENVIRONMENT`, and `WRITE_TABLE` on each artifact |
| `DELETE /namespaces/{path}/artifacts/{name}/uploads/{upload_id}` | Withdraw an open upload. | `WRITE_TABLE` |
| `DELETE /namespaces/{path}/artifacts/{name}/versions/{version}` | Delete one version's files. The answer is `200` with the version and, added to its fields, `freed_files`, `freed_bytes`, `kept_files`, `kept_bytes` and `kept` (`reason`, `files`, `bytes`, `version`), as under `versions delete`. | `DROP_TABLE` |
| `DELETE /namespaces/{path}/artifacts/{name}` | Drop the artifact. | `DROP_TABLE` |
| `GET /artifacts` (optional `namespace` query) | List the artifacts you may read. | `READ_ENVIRONMENT`, then `READ_TABLE` for each |
| `GET /namespaces/{path}/artifacts/{name}` | Show one artifact. | `READ_TABLE` |
| `GET /namespaces/{path}/artifacts/{name}/versions` | List the versions, without files. | `READ_TABLE` |
| `GET /namespaces/{path}/artifacts/{name}/versions/{version}` | Show one version with its files; `{version}` is a number or `current`. | `READ_TABLE` |
| `GET /namespaces/{path}/artifacts/{name}/versions/{version}/blobs/{sha256}` | Download one file. | `READ_TABLE` |

The routes that begin an upload, send a file, commit and delete (a version or the artifact) take
`break_glass_reason` as a query parameter; withdrawing an upload does not, because it writes
nothing. In the blob download route `{version}` must be a number, not `current`. A caller who may
not use a name gets the same 403 whether or not the artifact, the version or the file exists, and a list shows only
what the caller may read. The download route answers `GET` only, ignores a `Range` header and
sends the whole file with its size as `Content-Length`.

## Permissions

An artifact is governed like a table, with the table permissions.

| Permission | On an artifact |
|---|---|
| `READ_TABLE` | List and read versions, download files. |
| `WRITE_TABLE` | Add a version; list and withdraw your own uploads. |
| `CREATE_TABLE` | Create the artifact by storing its first version. |
| `DROP_TABLE` | Delete a version or drop the artifact. |

The built-in roles carry them as for tables: `VIEWER` and `ANALYST` read; `DATA_SCIENTIST` also
creates and adds versions; `DATA_ENGINEER` also deletes and drops; `ADMIN` has all.

- A grant on a namespace covers its tables **and** its artifacts. Grants do not tell a table from
  a model, so anyone who may read a namespace may read the artifacts in it.
- **Row filters and column masks cannot be attached to an artifact**: `'models.churn_model' is an
  artifact; row filters and column masks cannot be attached to an artifact. Whoever may read an
  artifact reads all of it.` Read why this matters in
  [Artifacts: what they are and what they are not](../explanation/artifacts.md).
- **A task token** (the credential of a running task) can read the artifacts that its asset declares
  as inputs and add versions to its own artifact. It can **never delete a version or drop an
  artifact**, whatever its principal may do: `A task token cannot delete a version or drop an
  artifact, and neither can a running task or query. Do it as a person or a service account with
  DROP_TABLE on the artifact (...), or set keep_versions on the asset to prune old versions when a
  new one is committed.` A task whose asset produces a table cannot add a version, and one that
  produces an artifact cannot write a table.
- A **protected environment** accepts a new version only from a run of the deployed code. A person
  with `MANAGE_ENVIRONMENT` can add a version, delete a version or drop an artifact there with
  `--break-glass --reason "<10 to 500 characters>"`; it is audited. The refusal says ``Environment
  '<name>' is protected: only runs of the deployed project version may write. See `lakana run`.``
  `keep_versions` pruning by a run needs no reason, because it belongs to adding the new version.

## Audit events

Five events, with these keys in their `detail`:

| Event | When | Detail |
|---|---|---|
| `ARTIFACT_CREATED` | The first version of a new name is begun. | `artifact`, `artifact_id`, `principal` |
| `ARTIFACT_VERSION_ADDED` | A version is committed. | `artifact`, `version`, `manifest_sha256`, `size_bytes`, `file_count`, `task_run_id` (`null` outside a run), `principal` |
| `ARTIFACT_DOWNLOADED` | A person or service account fetches a file of a version, **once per principal and version within the upload lifetime** (one hour by default), not once per file. The token a run gives its task writes none. A download that fails halfway is recorded too. | `artifact`, `artifact_id`, `version`, `file_count`, `size_bytes`, `principal` |
| `ARTIFACT_VERSION_DELETED` | A version is deleted, by a person or by `keep_versions`. | `artifact`, `version`, `reason` (`requested` or `keep_versions`), `size_bytes`, `freed_files` (files made eligible for removal, not files removed), `principal`; `task_run_id` for `keep_versions`; `kept_reason` when no file was freed because a stored manifest was damaged or unknown |
| `ARTIFACT_DROPPED` | An artifact is dropped. | `artifact`, `artifact_id`, `versions` (how many were live), `principal` |

A write in a protected environment also records `BREAK_GLASS_WRITE` or `PROTECTED_WRITE_DENIED`,
as other writes do. A commit that is repeated after a lost answer may add one more
`BREAK_GLASS_WRITE` row (up to four for one SDK upload).

## Where the files are

Files are stored under the environment's storage root, in `artifacts/<artifact id>/blobs/<sha256>`;
a manifest is stored in `artifacts/<artifact id>/manifests/<version>.json`. The id is the
artifact's id as 32 hexadecimal digits. The directory `artifacts/` of a storage root is reserved:
a table location inside it is refused. A file is stored once per artifact. Nothing is shared between
artifacts. A deleted environment keeps its artifacts, hidden, and `lakana env restore` brings them
back with it. Once the worker removes the deleted environment after its undo period, the artifact
rows go from the catalog and their files stay in storage.
