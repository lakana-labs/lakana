# Limit how many versions are kept

After this guide an artifact asset keeps only its newest few versions, and you know when older
ones are deleted and what stays. You need an artifact asset (see
[Train a scikit-learn model and score with it](models-with-scikit-learn.md)).

Every run of an artifact asset stores a new **version**. Without a limit every version is kept, and
their files use storage. `keep_versions` tells Lakana how many to keep.

## Set `keep_versions`

Add `keep_versions=N` to the decorator, where N is a whole number from 1 to 2147483647. This keeps
the newest three versions:

```python
from pathlib import Path

import polars as pl

import lakana
from lakana import asset


@asset(inputs={"train": "gold.training_set"}, output="artifact", keep_versions=3)
def churn_model(train: pl.DataFrame) -> Path:
    """The churn model; the three newest versions are kept."""
    path = lakana.output_dir() / "model.bin"
    path.write_bytes(b"...")  # write the real model file here
    return path
```

Like every `@asset` argument it must be a literal, written out in the file. Commit and deploy as
usual. Leave `keep_versions` out to keep every version. `keep_versions` on an asset that does not
have `output="artifact"` is refused at deploy.

## When older versions are deleted

- **When a run stores a new version.** After the new version is stored, any live version beyond the
  newest `keep_versions` is deleted, in the same step. With `keep_versions=3`, a run that stores
  version 7 deletes version 4 and everything older.
- **Not before the next run.** Changing `keep_versions` and deploying deletes nothing yet. The new
  value is used when the next run stores a version. If you lower it from 10 to 2, that run deletes
  all but two versions at once.
- **Only a run prunes.** A version that a person stores with `lakana artifact upload` never
  deletes anything, even when the asset sets `keep_versions`.
- **Only live versions count.** A version you deleted by hand does not count towards N.

## What is deleted and what stays

A deleted version loses its **files**, except a file that a kept version still has, and Lakana
cannot bring them back. If your bucket keeps old object versions, a deleted file stays in the
bucket until the bucket's own rule removes it. The version keeps:

- its record: number, size, who and which run stored it, and when it was deleted and by whom (the
  principal that stored the newer version);
- its place in the lineage of everything that read or wrote it.

The numbers are never used again. Files are removed right after the run stores its version, or
shortly after by Lakana's periodic clean-up (the person who runs the instance finds how it runs in
[The sweep](deploy-with-compose.md#the-sweep)).

## See which artifacts keep how many

`lakana artifact list` shows `keep` for each artifact: the `keep_versions` of the asset that
declares it, and empty for an artifact that keeps all its versions or that no deployed project
declares. `defined in` names the file.

## See what was deleted

The `artifact versions list` subcommand shows every version, with `deleted_at` on the deleted ones.
The audit log records each pruned version as `ARTIFACT_VERSION_DELETED` with `reason` set to
`keep_versions` and the id of the task run (the `audit list` subcommand with `--event-type`).

## Know the consequences

- **Pruning needs no `DROP_TABLE`.** Whoever may deploy the asset controls how much of its history
  stays, and **a deleted version cannot be brought back.** Choose N with that in mind.
- **A failed run can still prune.** If the run stores its version and then fails, for example the
  process is killed after the version was stored, the task shows as failed or timed out, that
  version stays live, and the version before it is already deleted. With `keep_versions=1` you
  then have the new version only. Use at least 2 when you want the last good model to survive a
  bad run. Until you delete it, everything outside the run (another run, a person who downloads the
  current version) reads that version as the current one.
- **A consumer may find a version deleted.** (The **consumer** is an asset that reads the artifact;
  the **producer** is the one that stores it.) A task in another run (or another project) that
  resolved its input just before the producer stored a new version can find its version gone. It
  fails with `` reading input 'model' (models.churn_model, version 3) failed: Version 3 of
  models.churn_model was deleted; its bytes are gone. Read a version that exists: `lakana
  artifact versions list models.churn_model --env dev` shows which. `` and is tried again, and the next attempt
  reads a version that exists. A consumer that starts after the pruning reads the current version.
  A larger N makes this rare.
- **Overlapping runs and a small N.** A consumer reads the newest version that a task of **its own
  run** stored. If another run stores a version in between, and N is so small that this prunes the
  version of your run before your consumer resolves it, the consumer finds no live version of its
  own run and reads the **current** version, which another run stored. Choose N larger than the
  number of runs of this asset that can be going at the same time.

The full list of options is in the [reference](../reference/artifacts.md). To delete one version
yourself, see [Delete an artifact version](delete-an-artifact-version.md).
