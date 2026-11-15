# Inspect and download artifact versions

After this guide you can see which artifacts exist, what versions each has and where they came
from, and download any live version to your own machine. You need read permission (`READ_TABLE`)
on the artifact, and the `lakana` command set up for your instance (see
[Getting started](../tutorials/getting-started.md)).

An **artifact** is a named file or directory that a run stored. Each run adds a numbered
**version**. The **current version** is the highest one whose files still exist. Each version has a
**manifest**: the list of its files with their paths, sizes and SHA-256 hashes. More in
[Artifacts: what they are and what they are not](../explanation/artifacts.md).

## 1. List the artifacts

The list command shows the artifacts you may read:

<!-- source: docs/examples/train-and-predict/run.sh#artifact-list -->
```bash
lakana artifact list --env dev
```

You see one row for each artifact:

<!-- output: train-and-predict#artifact-list -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ name        ┃ namespace ┃ current_version ┃ versions ┃ size  ┃ keep ┃ defined in                             ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ spend_model │ models    │ 1               │ 1        │ 639 B │ 2    │ forecasts/assets/models/spend_model.py │
└─────────────┴───────────┴─────────────────┴──────────┴───────┴──────┴────────────────────────────────────────┘
```

Each row has the name, the namespace, the current version, how many versions the artifact has had,
the size of the live versions, `keep` (how many live versions the project that declares it keeps;
empty keeps all) and `defined in` (the repository and file that declare it; empty for an artifact
you uploaded that no deployed project declares). The list shows 100 artifacts at most; a last line
`More results: ...` names the command for the next page. The examples on this page come from the
[tutorial](../tutorials/train-a-model.md). Add `--namespace models` to see one namespace, and `--env NAME` for an environment
other than your profile's default. An artifact you may not read is not listed. A namespace you may
not see gives an empty list, not an error.

## 2. See the versions

Give the versions list command the artifact's name, as `namespace.name`, to see its versions,
newest first:

<!-- source: docs/examples/train-and-predict/run.sh#artifact-versions -->
```bash
lakana artifact versions list models.spend_model --env dev
```

You see one row for each version. The times and run identifiers differ on your machine. A time or
a run identifier is never cut. If the window is too narrow for every column, the command leaves
columns out, least useful first (`file_count`, `size`, `run_id`, `deleted_at` and last
`manifest_sha256`), and says which on the line below the table. In a window that line tells you to
widen it or use `--output json`; where the output is not a terminal, it tells you only to use
`--output json`, as in the output below, which was made in a window of 120 characters and written
to a file:

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

A row has the version number, when it was stored, its size, its number of files, the start of its
`manifest_sha256` and, when you may see it, the run that stored it. A version that was deleted is
listed too, with `deleted_at`. Its files are gone. The last line says how many versions still have
files and their total size. The output above has no `file_count` column because the window was too
narrow for it, as the line above the last one says.

- **Numbers can have gaps.** A deleted version keeps its number, and a number is never used again.
  The next version is not always the highest number plus one.
- **`manifest_sha256` is the identity of the content.** Two versions with the same digest hold the
  same files. The command shows its first 12 characters, and leaves the column out last of all
  when the window is narrow. For the whole digest and the whole record, put `--output json`
  before the command name (`lakana --output json ...`).
- **No run is shown** for a version a person uploaded, and a task token never sees run
  identifiers.

If you have no versions listed, or the artifact does not appear, the run that produces it has not
stored a version yet. The messages you can meet are in "When it fails".

## 3. Download a version

Give the download command the artifact's name and a directory. Without `--version` it downloads
the **current** version; `--version 3` downloads version 3.

<!-- source: docs/examples/train-and-predict/run.sh#artifact-download -->
```bash
lakana artifact download models.spend_model ~/spend-model --env dev
ls ~/spend-model
```

The command names the file it made, and `ls` lists the folder (here one scikit-learn model saved
with `joblib`). Your folder is your own:

<!-- output: train-and-predict#artifact-download -->
```text
Downloaded dev/models.spend_model version 1 to /home/you/spend-model/model.joblib
model.joblib
```

The directory must not exist, or must be empty. Lakana writes the files there only when every one
has arrived and matches the manifest: if anything fails, nothing is left in the directory (a
directory that did not exist is not created, and one that existed is empty again). The command
prints `Downloaded <environment>/<name> version <n> to <path>`, as in the output above. For an artifact that is one file,
`<path>` is the file, for example `model.joblib`; for a directory artifact it is the directory.

Things to know:

- **`--version` is for exploring.** A task never uses it: a task reads the current version, or the
  version that an earlier task of **its own run** stored.
- **The copy is private to you.** Files are 0600 and directories 0700 (less your umask), and
  nothing is executable, because the files are access-controlled on the server and a copy that
  everyone can read would undo that. To let others read it, give them read permission on the
  directory and everything in it with `chmod` (recursively, mode `a+rX`). To remove it, delete the
  directory.
- **Check the disk first.** The command compares the version's size with the free space before it
  writes a byte, and says `The artifact needs <n> bytes but only <m> are free in <directory>; free
  some space or choose another directory.`
- **A big file takes time.** A download needs at least 64 KiB/s (about 0.5 Mbit/s). The server ends
  a slower one.
- **Loading a saved model runs code.** Before you open a downloaded model with `pickle` or `joblib`,
  read [A pickled model is code](../explanation/artifacts.md#a-pickled-model-is-code).

A copy made with the SDK option `read_only=True` (this is what a task gets) has files 0400 and
directories 0500. You cannot delete it until you give yourself write permission again:

<!-- not tested: removes a directory of the reader's choice -->
```bash
chmod -R u+w DIR && rm -r DIR
```

## When it fails

| You see | What it means | What to do |
|---|---|---|
| `models.spend_model has no version yet; run the asset that produces it.` | The artifact exists but holds no live version. | Run the project (or the producing asset) so that it stores one. |
| `models.spend_model has no version 7.` | There is no version with that number. | Look at the numbers in `artifact versions list`: they can have gaps. |
| `` Version 3 of models.spend_model was deleted; its bytes are gone. Read a version that exists: `lakana artifact versions list models.spend_model --env dev` shows which. `` | The version was deleted by a person or by `keep_versions`. | Download a version that is not marked deleted. |
| An error with exit code 5 (permission denied) | You may not read the artifact, or it does not exist: the answer is the same, on purpose. | Check the name, then ask the owner of the namespace for read. |
| `Artifact 'models.churn_modle' not found in 'dev'.` (exit code 6) | You may read the namespace, and there is no artifact with this name: usually a typing mistake. | Check the spelling against the list command. A name you may not read gives exit code 5 instead. |
| `` models.spend_model is a table, not an artifact. See `lakana table get models.spend_model`. `` (exit code 8) | The name belongs to a table, a view or an external table. | Use the commands for tables on it. |
| `<directory> already exists and is not an empty directory; give a new or an empty directory.` | The target has files in it. | Choose a new directory. |
| `The artifact needs <n> bytes but only <m> are free in <directory>; ...` | The disk is too small. | Free space or choose another directory. |
| `The server was busy with other downloads ... Nothing was kept; try again in a minute.` | The server allows 32 file downloads at a time and they were all taken for about 30 seconds. | Try again in a minute. |
| `The connection to the server ended before <path> was complete; nothing was kept. ...` | The connection broke, or the server cut a download slower than 64 KiB/s. | Download again, over a faster or more stable connection. |
| `The stored manifest of version 3 does not match its digest, so this version is damaged on the server and downloading it again will not help. Tell the operator, and store the artifact again …` | The record of the version on the server is damaged. Nothing is kept. | Tell the operator, then store the artifact again (run the asset that produces it, or `lakana artifact upload`). Downloading again does not help. |
| An `ArtifactCorrupt` error about a file: `The downloaded file <path> does not match its manifest (size or SHA-256); download it again.` | What arrived is not what the version recorded. Nothing is kept. | Download again. If it repeats, tell the operator that the stored files are damaged. |

A download killed in the middle can leave a hidden directory named `.<name>.lakana-partial-<random>`
next to the target (or inside it, when the target was an existing empty directory). It holds an
unfinished copy. Remove it, and download again.

Every option and message is in the [reference](../reference/artifacts.md).
