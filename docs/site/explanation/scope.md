# Scope and philosophy

This page explains what Lakana is for, what it deliberately does and does not do, and who it is
built for. It is the current statement of scope; the plan for getting there is in
[`roadmap.md`](roadmap.md). Both pages are living documents and change as decisions change.
If you already run SQL files on DuckDB, [Where Lakana fits, and why Apache Iceberg](where-lakana-fits.md)
says what Lakana adds to such a stack, what it lacks, and when to stay with what you have.

## What Lakana is

> Lakana is an open-source data lakehouse for small and medium teams: governed and reproducible
> by default, simple enough to run on one machine, professional enough to run an organisation's
> data platform. It covers analytics, data science and ML in SQL and Python, with code in git and
> every table, feature and model traceable to a commit. It runs anywhere containers run, on
> premises or in the cloud, and scales by adding workers, not by distributing queries.

A lakehouse keeps data as open table files on cheap storage and puts a catalog, access control
and a query engine on top. Lakana packages those parts, built on DuckDB, Apache Iceberg and
PostgreSQL, so that a small team gets them in one install instead of assembling them.

## Principles

- **Governed by default.** The catalog, row and column policies, secrets, audit and lineage come
  in one install. Nothing is an add-on you have to remember to turn on.
- **Reproducible.** Production runs only deployed, versioned code. You can always say which
  commit produced a table.
- **Two languages.** SQL and Python, nothing else.
- **What works, not what is possible.** One proven way to do each thing. A feature needs a clear
  user before it is built.
- **Easy to use.** Many of the teams Lakana is for have no senior data engineers. Every feature is
  designed for someone meeting it for the first time: sensible defaults, one obvious way, error
  messages that say what to do next.
- **Documentation is part of the product.** The docs must be good enough that such a team
  succeeds without outside help. A feature without its user documentation is not finished.
- **Usable by AI agents.** Lakana ships an MCP server before v1.0.0, so an agent can operate it
  under the same authorization and audit as a person.
- **Self-hosted and flexible.** A laptop, a single server, a cluster or a cloud account, with no
  dependence on a hosted service.
- **SQL transforms, Python at the edges.** Joins, filters and aggregations are SQL assets. Python
  is for getting data in and for end uses such as training and scoring.

## In scope

| Area | What Lakana covers |
|---|---|
| Analytics | SQL assets, incremental tables, lineage, governed queries |
| Data modelling | Medallion layers and Kimball-lite star schemas, the history write mode (`merge` comes with ingestion in M6), translated metadata, measures as metadata (milestone M8d, built after ingestion and before the web interface and the public beta, #249) |
| Data science | Python assets, reproducible environments, exploration through the SDK (including from your own notebook) |
| ML | Training and batch scoring as assets, versioned model artifacts, experiment tracking, a feature store |
| Operations | Runs, schedules, retries, audit, secrets, access policies, table and environment retention (0.17.0, #87), deploys from CI without a stored secret (0.18.0, #15), health checks and stable log codes (0.18.0, #205, #207), queries on DuckDB 2.0 with a hardened sandbox (0.19.0, #96), each Python task in a container of its own (0.20.0, #38) |

How the larger pieces are meant to work:

- **Ingestion.** A data source is a Python asset in git. Helpers in `lakana[assets]` cover files
  (S3, GCS, Azure, HTTPS and directories), SQL databases (PostgreSQL, SQLite and MariaDB) and
  REST APIs, and a load can replace, append or merge. More databases and SFTP follow after v1.0.0. For a
  quick start, `lakana load <file> <table>` loads a local file without writing code; it stays as a
  convenience for exploration and tests. From M6 an administrator can register a source's address
  and credentials once under a name in an environment (a named connection, #85); a Python asset
  declares the connections it uses, and an ingestion task reaches only those (#308); connections are managed through the CLI and the API, and in the
  web interface once that exists. The platform does not run connectors of its own: a connection
  is a named address and secret, and the asset's helper does the reading. Configuring a
  recurring load without writing an asset (no-code loads, #86) is an idea for after v1.0.0
  only.
- **Data modelling.** Lakana is opinionated about how tables are laid out: medallion layers
  (bronze, silver, gold) and Kimball-lite star schemas, with wide tables allowed where a star is
  too much. Model metadata sits in the asset header (#250), labels and descriptions can be
  translated with a main language (#251), and layer rules are opt-in first and enforced later
  (#252). The write mode `history` (SCD type 2) builds tables whose rows
  change (#253); `merge` (upsert) comes earlier, with ingestion in M6, and a template and a guide start a new team (#254). A measure is metadata on a
  column, not a computed metric (#255). All of it comes before the web interface and the public beta (epic #249), so that nobody builds projects on Lakana before the rules exist and the interface shows model metadata from its first version.
- **Data quality.** A check is a SQL file that must return no rows. It either blocks (the table is
  set back to what it was before the run, and what reads it is not built) or only warns, and its
  results are kept as a history. Nothing more elaborate than that. See
  [Check your data](../how-to/check-your-data.md).
- **Model artifacts.** A Python asset can store a file or a directory, such as a trained model, as
  numbered versions tied to the commit of the run, and another asset reads it. Access follows the
  grants of tables: a grant on a namespace covers its models, and a row filter or column mask
  cannot apply to a file, so a model reveals what it was trained on to everyone who may read it.
  A version comes from a run; a person can also upload one, for testing, repair or a model that
  exists already, but Lakana is not a general file store. See
  [Artifacts: what they are and what they are not](artifacts.md). This is built (M5c2b).
- **Experiment tracking.** Parameters, metrics and artifacts of each run are stored as ordinary
  tables and artifact versions, queryable in SQL and tied to the commit of the run. The model
  registry is the artifact store; M8 adds named pointers such as `production`, pinned versions and
  metrics on a version.
- **Feature store.** Feature definitions are assets in git, stored as tables, with point-in-time
  correct joins for training. This is the offline half; a low-latency online store is not planned
  until a user needs one.
- **Sign-in.** Tokens for the CLI, the SDK and automation, and an administrator issues one for
  each further user with the CLI. For people, the public beta brings the web interface with
  OpenID Connect (OIDC), or an invite link and a passkey for small teams that have no identity
  provider. No passwords.
- **Web interface.** There is none yet. It is built after ingestion (M6) and data modelling
  (M8d) and before the public beta, because the first audience, teams without a senior data
  engineer, needs it and real sign-in from the start, and because it then shows model metadata
  from its first version. It is designed first (#66), before any screen, around what that audience
  needs, and the private test round on a hosted instance checks it before the
  beta. The interface (M7) is the project's first front-end code. A second security review is
  due when it and sign-in arrive.
- **BI tools.** Before v1.0.0, a read-only SQL endpoint that speaks the PostgreSQL wire protocol,
  with a token as the password, and a tested list of tools. A BI tool that runs on a server
  connects with one stored identity, so a row filter could not apply to each viewer. The endpoint
  therefore refuses a service account on any table with a row filter or column mask; a dashboard
  on restricted data reads a table built for its audience. A person connecting with their own
  token gets their own filters and masks.
- **Scale.** DuckDB runs on a single node per worker. You scale by adding workers, also on more
  than one host. Kubernetes (Helm) support comes after v1.0.0. Each query runs in a process of
  its own that the worker can stop: the time limit is hard, a user can cancel a query, and on
  Linux a worker over its memory limit ends the largest query or Python task before it stops itself (M5c3a). A
  worker still runs one query at a time (several queries per worker, #81, come after v1.0.0).
  A worker takes interactive work (ad-hoc queries), pipeline work (deploys, tasks and table
  maintenance) or both, and one user runs only so many ad-hoc queries at once, so that one heavy
  user cannot take every worker; a query that waits for either reason says so (M5c3c). Each worker registers and sends a heartbeat. A query or a deploy whose worker is lost
  (killed, powered off, cut off from the database) is ended after about a minute and shows the
  cause `worker_lost`: nothing stays `RUNNING` for ever, and nothing is run again by itself. A
  write that had already been committed ends `SUCCEEDED`. The minute holds when another worker
  can take over the scheduler's role: PostgreSQL is asked to drop the connection of a worker
  that lost power or its network, which is expected to free the role within about a lease (`lakana scheduler
  status` shows who holds it). Every wait
  shows its reason (M5c3b).
- **Task isolation (shipped in 0.20.0).** Local mode runs tasks as processes of the worker; the
  Compose bundle runs each in a container of its own by default (`DockerRunner`, M7b), started by
  a launcher that alone holds the Docker socket. This isolates projects but shares the host's
  kernel. A protected environment runs Python tasks only in containers.

## Out of scope, by decision

These are choices, not gaps. Each keeps the platform small enough to understand and run.

- **Distributed query execution.** Compute is single-node per worker, which is what keeps
  Lakana simple to run and to reason about.
- **Real-time streaming.** No streaming or message-queue ingestion; Lakana works on batches.
- **AutoML and GPU scheduling.** These need a different kind of platform.
- **Hosting notebooks.** See the notebook rule below.
- **Languages other than SQL and Python.** Two languages keep the tooling and the lineage
  tractable.
- **Custom roles.** Users cannot define their own roles; the built-in roles and grants stay
  easy to audit.
- **A plugin ecosystem and registry.** One proven way beats many unvetted ones.
- **An online feature store.** See the feature store above.
- **A semantic layer.** Metrics definitions belong in your SQL assets. A measure recorded as
  metadata (#255) is documentation and is not computed.

**Parked:** unstructured data, meaning governed volumes for files. It may return if teams need it.

## Postponed

**Model serving** is neither in nor out of scope yet. It will be discussed after v1.0.0.

## The notebook rule

The platform does not host or run notebooks. Production code lives in git as SQL and Python
files, so that what runs is reviewed, versioned and tested. This does not stop anyone from
exploring: you can use your own notebook, on your own machine, and reach Lakana through the SDK
under your own identity and the same policies as anyone else. What you build there becomes
production when it is committed to git and deployed.

## Who it is for

In order of priority:

1. **One person or a small team that is responsible for an organisation's data** and already
   works with git, SQL and Python: at a company, a start-up, a consultancy that builds for its
   clients, a research institute, or a department of a larger organisation. They want full
   control of their data platform and can run it themselves. This is who the documentation and
   the tutorials speak to first.
2. **Those who run Lakana for others:** a consultancy, a freelance data engineer, an IT service
   provider, an internal IT department or a computing centre that installs and operates an
   instance for people who use it but could not run it. An organisation without anyone who
   works with git and SQL is served this way, not directly.
3. **Individuals** who are experimenting with analytics, data science and ML and want a
   realistic place to start, and **teaching**: a platform a student can run on a laptop and
   read end to end.
4. **Also served:** academic teams and research groups; public-sector and regulated
   organisations with data-sovereignty requirements; and agencies.

The size of the organisation does not decide the fit; the skills of the people who run it do.
"Easy to use" therefore means easy for someone who knows SQL and git, not for someone who knows
neither. Lakana is an open-source project: it offers no support contract, and whoever operates
an instance, for themselves or for others, does so on their own account.

## Who it is not for

- Workloads at petabyte scale, or ones that need queries spread across many nodes.
- Teams that want a hosted service rather than running their own.
- R-first groups. They can still read the Iceberg tables directly with their own tools.
