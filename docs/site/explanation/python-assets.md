# Python assets: what they are for and how far they are trusted

Why a project has Python assets next to its SQL, and when one instance is one trust domain. The
steps are in [Projects and deploys](../how-to/projects.md#python-assets); the rules are in
[Python assets (reference)](../reference/python-assets.md).

**SQL transforms, Python at the edges.** Joins, filters and aggregations belong in SQL assets.
The platform reads their dependencies and column lineage, runs them on DuckDB and enforces
limits and policies there. A Python asset is for what SQL cannot do at either edge of a
pipeline: ingestion (no inputs, returns rows from an API or a file format SQL cannot read) and
end use (scoring with a model, calling a library, training). If you can write it as a `SELECT`,
write it as a SQL asset.

**An incremental Python asset filters its own rows.** For a SQL asset that appends, the platform
wraps the `SELECT` and keeps only the rows past the watermark, because it can read the query. It
cannot read what a Python function does, so it hands the function the watermark instead
(`ctx.watermark()`) and writes every row the function returns. The function knows its source:
an API's `since=` parameter, a file's modification time or a column of an export. Filtering there
also means a run reads only what is new. The cost is that a mistake in the filter adds rows twice,
which a `merge` on a key would absorb: it updates a row it already has rather than adding it.
Details in [The asset's context](../reference/python-assets.md#the-assets-context).


## Isolation: one instance is one trust domain

**`LocalProcessRunner`, which runs Python assets, is not a sandbox.** A task runs as the
worker's operating-system user. It can read everything the worker can read: the worker's own
settings and credentials (its database and storage access among them), the files in its home
directory, and anything it can reach on the network. The venv cache is shared between tasks and
writable by them, so one task can change the environment another task will use. The scrubbed
environment, the private temporary directory and the task token limit accidents; they do not
stop code that means harm.

So one instance is one **trust domain**: everyone who may deploy Python code to it is trusted
with everything the worker can read. Deploy only code and dependencies you would run on the
worker itself, and run one instance per client or per group of people who do not trust each
other. Under the local runner, treat a Python asset like code you run on the worker's machine
yourself.

**With `LAKANA_RUN__RUNNER=docker`** (the Compose bundle's default) each task runs in a container
of its own and cannot read the worker's credentials or another project's environment: see
[Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md). Containers share
the host's kernel, so this isolates projects, not code written to escape it; a task still talks to
the server with its token, and a package built from source still runs its build code next to the
build's index credentials. The local runner, and local mode (`lakana server start --local`), keep
the one trust domain described above.
