# Train a scikit-learn model and score with it

After this guide you have two Python assets in one project: one trains a scikit-learn model on
every run and stores it as an artifact, and one loads the newest model and writes a table of
scores. You need a project with Python assets that already deploys (see
[Projects and deploys](projects.md), "Python assets"), a table to train on, and a table to score.
The example uses these tables and columns:

- `gold.training_set`: the numbers `tenure_months` and `monthly_spend`, and `churned` (0 or 1, the
  answer the model learns).
- `gold.customers`: `customer_id`, `tenure_months` and `monthly_spend`.

The asset that stores the model is the **producer**. The asset that reads it is the **consumer**.

The model is an **artifact**: a file that Lakana stores and numbers for you. Each run stores a new
**version**. What artifacts are and how they are governed is in
[Artifacts: what they are and what they are not](../explanation/artifacts.md).

!!! warning "Loading a saved model runs code"
    A model saved with `joblib` or `pickle` is a program. Loading it runs whatever the file
    contains. Only declare artifacts as inputs when you trust everyone who can store a version of
    them. Read [A pickled model is code](../explanation/artifacts.md#a-pickled-model-is-code)
    before you give a colleague write access to a namespace of models.

## 1. Add the libraries

Add `scikit-learn` to `[project] dependencies` in the project's `pyproject.toml`, next to the
`lakana[assets]` line (the one you uncommented in [Projects and deploys](projects.md), "Python
assets", step 4):

```toml
[project]
name = "analytics"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["lakana[assets]==<version>", "scikit-learn>=1.5,<2"]
```

The rest of the file stays as it is. `joblib` comes with scikit-learn. scikit-learn and the
packages it needs (`numpy`, `scipy` and `joblib`, about 60 MB in all) come from the public package
index, like `lakana` itself (see [Projects and deploys](projects.md), "Python assets", step 4).
Lock the packages. `uv lock` needs a
network connection to the public package index, and so does the worker's first build of the
project's environment:

<!-- source: docs/examples/train-and-predict/run.sh#lock -->
```bash
uv lock
```

You commit `pyproject.toml` and `uv.lock` with the assets in step 4. The tutorial
[Train a model and score with it](../tutorials/train-a-model.md) runs these steps, a scikit-learn
model included, in a test on every change.

Both assets of one project run from the same lock, so they use the same scikit-learn version. That
matters: a model saved by one version of scikit-learn may not load in another. If the
producer and the consumer live in two repositories, each has its own lock. Pin the same scikit-learn
version in both.

Two repositories also need an order. Deploy the producer's repository first and run it once, then
deploy the consumer's:

- A deploy of the consumer **before** the producer has deployed fails with `table
  'models.churn_model' not found`.
- After the producer's deploy and **before** its first run, the consumer deploys, but its run fails
  with `models.churn_model has no version yet; run the asset that produces it`.
- If the name later changes from an artifact to a table, or the other way round, the consumer's run
  fails with `models.churn_model is a table, but this deploy recorded it as an artifact; redeploy so
  that the asset reads it as a table`, or `models.churn_model is an artifact, but this deploy
  recorded it as a table; redeploy so that the asset receives a path`. Redeploy the consumer.

## 2. Write the asset that trains

`assets/models/churn_model.py` is the artifact `models.churn_model`, because the file's place in
`assets/` gives the name, as for tables. The asset reads a table, trains, writes the model into
the directory that `lakana.output_dir()` returns, and returns the path of the file:

```python
from pathlib import Path

import joblib
import polars as pl
from sklearn.linear_model import LogisticRegression

import lakana
from lakana import asset

FEATURES = ["tenure_months", "monthly_spend"]


@asset(inputs={"train": "gold.training_set"}, output="artifact")
def churn_model(train: pl.DataFrame) -> Path:
    """A logistic regression that predicts churn, trained again on every run."""
    model = LogisticRegression()
    model.fit(train.select(FEATURES).to_numpy(), train["churned"].to_numpy())
    path = lakana.output_dir() / "model.joblib"
    joblib.dump(model, path)
    return path
```

- `output="artifact"` says that this asset stores a file, not a table.
- The function must return a `pathlib.Path` that lies inside `lakana.output_dir()`. A file
  returns one artifact file; the directory itself returns everything in it.
- Everything else of a Python asset works as before: `retries`, `timeout`, `owner` and so on.

In a project with `layers:` in `lakana.yaml`, every asset lives in a layer: write the model as
`assets/gold/churn_model.py` (`gold.churn_model`) and read it as `gold.churn_model`. See
[Keep layers apart](keep-layers-apart.md).

## 3. Write the asset that scores

`assets/gold/churn_scores.py` reads the model and a table, and returns a table. The parameter
that names the artifact receives a `pathlib.Path` to a local, **read-only** copy of the model
file, not a DataFrame:

```python
from pathlib import Path

import joblib
import polars as pl

from lakana import asset


@asset(inputs={"model": "models.churn_model", "customers": "gold.customers"})
def churn_scores(model: Path, customers: pl.DataFrame) -> pl.DataFrame:
    """The chance that each customer churns."""
    fitted = joblib.load(model)
    features = customers.select(["tenure_months", "monthly_spend"]).to_numpy()
    chance = fitted.predict_proba(features)[:, 1]
    return customers.select("customer_id").with_columns(churn_chance=pl.Series(chance))
```

The producer returned a file, so `model` is the path of that file. If a producer returns a
directory, the parameter is a directory.

The parameter name of an artifact input (`model` here) must be letters, digits and underscores, at
most 128 characters. The deploy refuses any other name and tells you to rename it.

## 4. Deploy and run

Commit both files and the new `uv.lock`, and deploy, as in "Deploy, dry-run, roll back" in
[Projects and deploys](projects.md). The tutorial
[Train a model and score with it](../tutorials/train-a-model.md) shows the same steps, from the
first commit, with what you will see. Then run the project:

<!-- source: docs/examples/train-and-predict/run.sh#run -->
```bash
lakana run --env dev
```

The run builds `models.churn_model` first and then `gold.churn_scores`, because the second asset
declares the artifact as an input. The scoring task reads the version that the training task of
the **same run** stored.

The first run builds the project's environment, which takes a while. A later run reuses it.

## What you see

- After the run, the `artifact list` subcommand of `lakana` shows `models.churn_model` with its
  current version, and `artifact versions list` shows one version per run, newest first. See
  [Inspect and download versions](inspect-and-download-artifact-versions.md).
- The `table lineage` subcommand, for `gold.churn_scores`, shows `models.churn_model [artifact]` as an
  input.
- Each run adds a version. To keep only the newest few, see
  [Limit how many versions are kept](limit-artifact-versions.md).

## When it fails

| You see | What it means | What to do |
|---|---|---|
| `` `models.churn_model` is an artifact; only a Python asset can read it. `` at deploy | A SQL asset names the artifact in `FROM`. | Read the model in a Python asset, as above. |
| `` `models.churn_model` already exists in dev as a table; drop it first, or choose another name. `` at deploy | A table has the artifact's name. | Choose another file name, or drop the table with the `table delete` subcommand. |
| `parameter 'model' reads the artifact ... rename the parameter ...` at deploy | The parameter name is not a plain identifier. | Rename it in the function and in `inputs`. |
| `table 'models.churn_model' not found` at deploy | The consumer's repository was deployed before the producer's. | Deploy the producer's repository first, run it, then deploy the consumer. |
| `models.churn_model has no version yet; run the asset that produces it` | The consumer ran before any version existed (for example the run did not include the producer). | Run the producer first, or run both together. |
| `models.churn_model is a table, but this deploy recorded it as an artifact; redeploy so that the asset reads it as a table`, or `models.churn_model is an artifact, but this deploy recorded it as a table; redeploy so that the asset receives a path` | The name changed kind after the consumer was deployed. The task is not retried. | Redeploy the consumer's repository. |
| ``models.churn_model is a table, not an artifact. Drop it (`lakana table delete models.churn_model`) or give the asset another name and redeploy.`` (or `a view`, `an external table`) | A table was created with the producer's name after it was deployed. | Drop the table with the command the message gives, or choose another file name and deploy. |
| `models.churn_model has 16 uploads in progress, the most allowed at once. ...` | Many uploads of the artifact are open. The task is retried. | Wait for the retry. See the [reference](../reference/artifacts.md), "Limits". |
| `an artifact asset must return a pathlib.Path inside lakana.output_dir() ...` | The function returned something else. | Return the `Path` of the file you wrote. |
| `` reading input 'model' (models.churn_model, version 3) failed: Version 3 of models.churn_model was deleted; its bytes are gone. Read a version that exists: `lakana artifact versions list models.churn_model --env dev` shows which. `` | A newer run pruned the version while this task started. | Nothing: the task is tried again and picks a version that exists. If it keeps failing, run again. |
| `models.churn_model cannot be read by the run's principal 'svc-run', or does not exist; grant read on it or check the name` | The principal that runs the project has no `READ_TABLE` on the artifact, or the name is wrong. | Check the name, then grant read. See [Projects and deploys](projects.md), "Run principals". |
| `InconsistentVersionWarning` in the task's log, or an error from `joblib.load` | The model was saved by another scikit-learn version. | Use the same version in the producer and the consumer. |

More messages and every option are in the [reference](../reference/artifacts.md).
