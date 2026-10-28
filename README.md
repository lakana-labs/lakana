<p align="center">
  <img src="docs/assets/lakana-logo.png" alt="Lakana" width="220">
</p>

# Lakana

<p align="center">
  <a href="https://github.com/lakana-labs/lakana/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/lakana-labs/lakana/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://pypi.org/project/lakana/"><img alt="PyPI" src="https://img.shields.io/pypi/v/lakana"></a>
  <a href="https://pypi.org/project/lakana/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/lakana"></a>
  <a href="LICENSE"><img alt="Licence: Apache-2.0" src="https://img.shields.io/badge/licence-Apache--2.0-blue"></a>
  <a href="docs/agent/testing.md#the-coverage-ratchet"><img alt="Coverage 100 %" src="https://img.shields.io/badge/coverage-100%25-brightgreen"></a>
  <a href="https://docs.astral.sh/ruff/"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="https://docs.astral.sh/uv/"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/lakana-labs/lakana"><img alt="OpenSSF Scorecard" src="https://api.scorecard.dev/projects/github.com/lakana-labs/lakana/badge"></a>
</p>

**Documentation: [https://lakana-labs.github.io/lakana/](https://lakana-labs.github.io/lakana/)**

Lakana is an open-source data lakehouse for small and medium teams: governed and reproducible
by default, simple enough to run on one machine, professional enough to run an organisation's
data platform. It covers analytics, data science and ML in SQL and Python, with code in git and
every table, feature and model traceable to a commit. It runs anywhere containers run, on
premises or in the cloud, and scales by adding workers, not by distributing queries.

Principles:

- **Governed by default:** catalog, row and column policies, secrets, audit and lineage in one
  install.
- **Reproducible:** production runs only deployed, versioned code.
- **Two languages:** SQL and Python, nothing else.
- **What works, not what is possible:** one proven way to do each thing.
- **Easy to use,** with documentation that is part of the product.
- **Usable by AI agents:** an MCP server is planned before v1.0.0.
- **Self-hosted and flexible:** from a laptop to a cluster, with no hosted service to depend on.

What is in scope, what is not, and who it is for:
[Scope and philosophy](docs/site/explanation/scope.md). The plan to v1.0.0:
[Roadmap](docs/site/explanation/roadmap.md). If you already run SQL files on DuckDB and wonder why
you would change, or why Apache Iceberg, see
[Where Lakana fits, and why Apache Iceberg](docs/site/explanation/where-lakana-fits.md).

> A lakana is a Malagasy outrigger canoe: a narrow dugout hull, one float and a simple
> sail — very little boat, taken a long way by a tiny crew. That is the idea here.

> **Pre-beta.** The packages are on PyPI as 0.x releases so that people can try Lakana and tell
> us what is missing. The API and the data layout may change between 0.x releases, and Lakana is
> not ready for production data yet. The public beta follows the web interface and data modelling.

`lakana-core`, `lakana-server`, and `lakana-worker` are
substantially built — catalog, storage, authorization, policy, secrets, audit, the query
executor, and the platform REST API are all real. `lakana` is the SDK and CLI on top of them,
covering every platform route. There is no web interface yet: you work with Lakana through the
CLI, the SDK or the REST API, and the interface is planned before the public beta. See
[Scope and philosophy](docs/site/explanation/scope.md) and
the [Roadmap](docs/site/explanation/roadmap.md) for where this is going.

## Getting started

**Solo, on your own machine, with no Docker and no Postgres:** the
[Getting started](docs/site/tutorials/getting-started.md) tutorial takes you from installing
Lakana to a built table, with the output of every command. It installs from PyPI:

```bash
pip install "lakana[server]"   # or: uv tool install "lakana[server]"
```

**A small team, sharing one instance — Docker Compose:**

```bash
pip install lakana
lakana bundle init
cd lakana-deploy && docker compose up -d --wait
lakana sql "SELECT 42"
```

See [Deploying with Compose (team mode)](docs/site/how-to/deploy-with-compose.md) for
prerequisites, exposing it beyond localhost with TLS, scaling workers, upgrading, and backups.

## Loading data

A data source is a short Python asset in git that names a **connection** (an address and a
secret, registered once per environment with `lakana connection create`) and reads it with a
helper of `lakana.ingest`: files from directories, S3, GCS, Azure and HTTPS, PostgreSQL, SQLite,
MariaDB and MySQL databases, and REST APIs. On a schedule it replaces, appends or merges into a
bronze table, and in the Compose bundle its task reaches only the sources it declares.
`lakana init --template ingest-files`, `ingest-sql` or `ingest-rest` writes a project that does
it; `lakana load <file> <table>` loads a local file into bronze once, for exploring. See
[Load data from a source](docs/site/how-to/load-data-from-a-source.md).

## Python assets

SQL transforms, Python at the edges: a Python asset receives its declared inputs as polars
DataFrames and returns a polars DataFrame or an Arrow table, which replaces its table or is
appended or merged into it; see
[Projects and deploys](docs/site/how-to/projects.md#python-assets). With `output="artifact"` it
instead stores a file such as a trained model as a numbered version that another asset reads; see
[Artifacts](docs/site/reference/artifacts.md). `lakana init --template python` writes the sample
with one Python asset and its lock, ready to deploy; see
[Build a table with Python](docs/site/tutorials/python-sample.md).

```python
# assets/gold/station_scores.py  ->  table gold.station_scores
import polars as pl
from lakana import asset


@asset(inputs={"readings": "bronze.readings", "stations": "bronze.stations"}, retries=2)
def station_scores(readings: pl.DataFrame, stations: pl.DataFrame) -> pl.DataFrame:
    """Temperature spread per station over the last week of readings."""
    recent = readings.filter(pl.col("ts") > pl.col("ts").max() - pl.duration(days=7))
    scored = recent.group_by("station_id").agg(score=pl.col("temp_c").std())
    return scored.join(stations, on="station_id")
```

The inputs are tables other assets build (here two SQL assets, `bronze.readings` and
`bronze.stations`). The full walk-through, with those two files, the `pyproject.toml` and the
deploy steps, is in `docs/site/how-to/projects.md`, "Python assets".

The project also needs a `pyproject.toml` that depends on `lakana[assets]` and a committed
`uv.lock`. In the Compose bundle each Python task runs in a container of its own that reaches
the server and its declared connections only; in local mode Python assets run as processes of
the worker's user and are not sandboxed. Either way one instance is one trust domain, so deploy
only code you would run on the worker yourself; see
[Run Python tasks in containers](docs/site/how-to/run-python-tasks-in-containers.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Contributions require a Contributor License Agreement, a licence grant: you keep your
copyright. See [`CONTRIBUTING.md`](.github/CONTRIBUTING.md) for what it means in plain words.

## Governance

- [`CONTRIBUTING.md`](.github/CONTRIBUTING.md) — workflow, gates, and code conventions
- [`CODE_OF_CONDUCT.md`](.github/CODE_OF_CONDUCT.md) — expected behaviour in project spaces
- [`SECURITY.md`](.github/SECURITY.md) — how to report a vulnerability
- [`SUPPORT.md`](.github/SUPPORT.md) — support is best effort: no service level, no warranty, no response times
- [`docs/legal/CLA.md`](docs/legal/CLA.md) — the Contributor License Agreement
  ([corporate version](docs/legal/CLA-CORPORATE.md))
- [`docs/legal/TRADEMARK.md`](docs/legal/TRADEMARK.md) — how the project name and logo may be used (not registered as trade marks)
