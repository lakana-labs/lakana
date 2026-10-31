# Roadmap

This is a plan, not a promise. Order and content change as we learn. The issues and milestones
on GitHub are the source of detail; this page gives the shape. What Lakana is for, and what it
will not do, is in [`scope.md`](scope.md).

Releases before v1.0.0 may change the API. **v1.0.0 is the API freeze.**

## Done

Foundation, catalog, security, SDK and CLI (M0 to M4), materialisation (M5a), projects and
deploy (M5b) and the run engine (M5c1). Python assets (M5c2a) followed in 0.9.0.
The artifact store (M5c2b) followed in 0.11.0: a run stores a trained model
or another file as a numbered version tied to its commit, and another asset reads it. See
[Artifacts](../reference/artifacts.md).
Private package indexes and the delegation rule (M5c2c) are built in 0.12.0:
an administrator registers a package index, with a password if it needs one, and a project's
Python assets install their own packages from it. A git dependency is not supported: it is
refused with a message that says to publish it to an index. The permissions that hand out access,
manage groups and issue tokens now work only for the `ADMIN` role. See
[Use a private package index](../how-to/private-package-index.md) and the
[upgrade notes](upgrades.md#notes-for-0120).
The first half of M5c3 (M5c3a) is built too: every query runs in a process the worker can stop,
with a hard time limit, a cancel (`lakana query cancel`, or Ctrl-C) and a memory limit that ends one
query before it stops the worker. See [Stop a query](../how-to/stop-a-query.md).
The second half (M5c3b) is built as well, in 0.12.0: a query or a deploy can no
longer stay `RUNNING` when its worker is gone. Each worker registers and writes a heartbeat; the
platform ends what a lost worker held after about a minute, with the cause `worker_lost`, and a
write that was already committed ends `SUCCEEDED`. Every wait shows its reason (a `Waiting:` line
in the CLI, `waiting_for` in the SDK and the REST API), and `lakana query list` and `lakana worker
list` show what is queued and running and which workers are alive. See
[Why is my query or task waiting?](../how-to/why-is-it-waiting.md), [A worker that is
lost](../how-to/stop-a-query.md#a-worker-that-is-lost) and the [upgrade notes](upgrades.md#notes-for-0120).
The third part (M5c3c) is built too, in the same release, and with it the whole of M5c3: a
worker takes interactive work (ad-hoc queries), pipeline work (deploys, tasks and table
maintenance) or both, set with `LAKANA_WORKER__TAKES`, and the Compose bundle has two optional
services for them. One user runs only so many ad-hoc queries at once
(`LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL`; by default one worker is kept for others once three
workers take ad-hoc queries). A query that waits for either reason says so: `no_worker_of_kind`
and `user_limit`. A worker still runs one query at a time; several queries per worker (#81) come
after v1.0.0, and a wait reason for a deploy is issue #92. See [Separate workers for ad-hoc
queries and
pipelines](../how-to/deploy-with-compose.md#separate-workers-for-ad-hoc-queries-and-pipelines) and the
[upgrade notes](upgrades.md#workers-for-one-kind-of-work-and-a-limit-per-user).
The first part of M5d (M5d-a1) is built: a project keeps data quality checks under `checks/`, as
SQL files that return the rows that are wrong. A run counts them after it writes a table, keeps the
result, and a check that fails sets the table back to what it was before the run and stops the
assets that read it; a table built for the first time is left not published. A check can warn
instead, or keep (`on_failure: keep`): the failing data then stays in the table, marked as
kept, and nothing that reads it is built until its checks pass. A project sets the default for its
checks in `lakana.yaml`. The failing rows can be looked at with `lakana check rows`. See
[Check your data](../how-to/check-your-data.md), [Checks](../reference/checks.md) and the
[upgrade notes](upgrades.md#data-quality-checks).
Jobs and their triggers (M5d-b) are built too: a job is a named selection in `lakana.yaml` that
starts by hand, through the API from CI, or on a cron schedule in a time zone. A scheduled run
writes as a bound service account; a start that meets an active run waits, one start per job;
after downtime a job runs once, for the latest missed time. See
[Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md) and the
[upgrade notes](upgrades.md#jobs-and-the-triggers-that-start-them).
Notifications (M5d-c) complete M5d: a job can name a webhook, and a run that fails, and the
first run that succeeds after it, send a message in `json`, `slack` or `teams` format. The worker
sends only to a public `https` address unless an operator allows a host, tries up to five times,
and never changes the run. See [Get told when a job fails](../how-to/get-told-when-a-job-fails.md)
and the [upgrade notes](upgrades.md#notifications-for-jobs). 

Housekeeping for data that is no longer wanted shipped in 0.17.0. A table, or every table of an
environment, can be given a retention and then expires (#87, PR #227), so that development tables
do not stay for ever; nothing expires unless a retention is set. A dropped table
keeps its files for an undo period (7 days by default) and then the worker deletes them; a
deleted environment's tables follow the same rule (#94, #213), and `lakana table undrop` and
`lakana env restore` bring them back until then. Every table's location is recorded before
anything is written, so files that never landed can be found and removed (#90), and
`lakana env orphaned-locations` lists and removes the directories left without a record (#229).
The audit log records when a manager views other people's queries or a table's readers, and
query text in other people's events needs `READ_QUERY_TEXT` (#88); a successful sign-in is written at most once per
credential and window (#217). Deploying from CI without a stored secret, through OIDC token
exchange (#15), was built in three parts that did not wait for the web interface: 0.17.0
verified a CI provider's ID token (#244); the exchange, trust rules and enforcement, and then
the client, how-tos and templates followed in 0.18.0.

0.18.0 shipped the rest of that work and the first operating and contract items. CI deploys
without a stored secret, through OIDC token exchange, from GitHub Actions and from GitLab CI
(#15 is complete: parts 2 and 3 are #247 and #258). `/readyz`, `lakana-worker healthcheck` and
stable log codes tell an operator whether an instance is ready and why not (#205, #207); disk use
is shown per environment and the worker's package cache trims itself (#206). The storage root
and the webhook allow-list are hardened (#231, #243, #260). The REST API is declared a versioned
contract with a CI schema check, and the promise starts at v1.0.0 (#7); routes carry
descriptions (#268). A guide and a tutorial show how to move SQL transformations (#61), and the
output blocks in the documentation are checked (#54). The next element was the move to DuckDB 2
(item 2 below).

0.19.0 (2026-10-22) moved query execution to DuckDB 2.0, a long-term-support release supported
until at least October 2027 (#96 is closed). A few SQL forms behave differently; see the
[notes for 0.19.0](upgrades.md). The query sandbox was hardened for DuckDB 2. A store
certificate that cannot be verified now fails at once with a message that says why (#277). The
next elements were M7b, task isolation (#38), then M6 and the public beta (M7c,
#33).

0.20.0 (2026-10-23) completed M7b, task isolation (#38 is closed). Each Python task runs in a
container of its own, started by a launcher that alone holds the Docker socket. An environment
build reaches only its package indexes, through an egress proxy. Two changes are breaking: the
Compose bundle uses the docker runner by default, and a protected environment runs Python tasks
only in containers. See [Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md)
and the [notes for 0.20.0](upgrades.md#notes-for-0200). The next elements are the open
dependency updates and a batch of small issues, then M6 ingestion, M8d data modelling, M7 web
interface and sign-in, a small private test round on a hosted instance, M7c (the public beta,
#33), M8, M8b, M8c, M9, M10 and v1.0.0.

M6, ingestion (#1), shipped with 0.21.0 (see the
[notes for 0.21.0](upgrades.md#notes-for-0210)). An administrator registers
a source once per environment as a named connection (#85), and `lakana connection test` checks
its address and credentials; a Python asset declares the connections it reads, fetches their
credentials at run time (#23), and under the docker runner its task reaches exactly their
endpoints, through the egress proxy or a relay of its own (#308), trusting the operator's
certificate authority (#292). The helpers in `lakana[assets]` read files from directories, S3,
GCS, Azure and HTTPS (#26), PostgreSQL, SQLite, MariaDB and MySQL (#27) and REST APIs, with
OAuth2 (#28); assets replace, append or merge. `lakana load` loads a local file into bronze
(#29), and `lakana init --template` starts a project from a tested asset for files, SQL or REST,
with a recipe for SQL Server (#56). The next elements are M8d data modelling, M7 web interface
and sign-in, a small private test round on a hosted instance, M7c (the public beta, #33), M8,
M8b, M8c, M9, M10 and v1.0.0.

## To v1.0.0

The order up to the public beta is: M6 ingestion (built), M8d data modelling, M7 web interface and
sign-in, a small private test round on a hosted instance, and then M7c, the public beta. Dates
follow the plan. The reasons: the first audience, teams without a senior data engineer, needs
the web interface and real sign-in from the start; the strict data-model rules (a declared kind,
grain, key and references, with generated checks, #250) should exist before anyone builds projects
on Lakana; and the interface then shows model metadata from its first version. Ingestion (M6)
came first, after task isolation (M7b): a new team's first job is getting its data in, so the
beta ships with the ingestion helpers and named connections.

1. **M5d, triggers, data quality checks and notifications (built).** Runs start by hand, on a schedule
   or through an API call. A check, a SQL file that must return no rows, can block, keep or
   warn, and a project can set the default in `lakana.yaml`; jobs start by hand, through the API
   or on a cron schedule (both built: see above). A job notifies a webhook when a run or a
   check fails and when it recovers (built). Triggers by other jobs or by events
   come after v1.0.0. Done when a failing check stops a downstream build, its result is kept, and the
   failure is announced.
2. **The move to DuckDB 2 (built, 0.19.0).** Lakana runs on DuckDB 2.0, a long-term-support
   release supported until at least October 2027. It was a migration, not a milestone, tracked in
   #96. The worker requires `duckdb>=2.0,<3`; what changed for SQL is in
   [Upgrade policy](upgrades.md).
3. **M7b, task isolation (built, 0.20.0).** Each Python task runs in a container of its own,
   started by a launcher that alone holds the Docker socket, so a task cannot read the worker's
   credentials or another project's environment. Its memory and CPU limits are enforced by the
   container runtime, and a stopped task leaves no process behind. An environment build reaches
   only its package indexes, through an egress proxy. The Compose bundle uses the docker runner
   by default, and a protected environment runs Python tasks only in containers. See
   [Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md).
4. **M6, ingestion (built, next release).** An administrator registers a source once as a named connection (#85), per
   environment, with its address and credentials; a connection is managed through the CLI and the
   API, and in the web interface from M7. A Python asset declares the connections it uses in
   its header, and an ingestion task reaches only those connections and nothing else on the
   network (#308). Helpers in `lakana[assets]` read files (S3, GCS, Azure, HTTPS and
   directories), SQL databases (PostgreSQL, SQLite, MariaDB and MySQL) and REST APIs. The write modes
   are `replace`, `append` and `merge` (an upsert); `history` follows in M8d. `lakana load`
   loads a local file into bronze, and stays as a convenience for exploration and tests. A
   connection is only a name for an address and a secret: Lakana runs no connectors, and the
   asset's helper does the reading. Done when a new team loads data from each kind of source
   with a short asset, merges a changing source into a table, or loads a local file with
   `lakana load`: the Compose smoke test runs the three templates on a schedule against a file
   directory, a PostgreSQL database and a REST API outside the stack, and every helper has a
   tested recipe.
5. **M8d, data modelling (epic #249).** Decided on 2026-10-20; on 2026-10-24 it moved before
    the public beta, after M6 and before M7. Opinionated support for medallion layers (bronze, silver, gold) and
    Kimball-lite star schemas, with optional wide tables. The parts: model metadata in the asset
    header (#250); translated labels and descriptions with a main language (#251); layer rules,
    opt-in and enforced at deploy once declared (#252); the write mode `history` (SCD type 2) (#253); `merge`
    (upsert) came earlier, with M6; a template and a guide (#254); and measures as
    metadata only, with no metrics layer (#255). Decided on 2026-10-28: the model metadata as a
    graph, with lineage, impact and join-path queries (#374), and a business glossary of terms
    linked to tables, columns and measures (#375). Done when a new team can start from the
    template, build a star schema with a history dimension, and see its layers and labels in the
    catalog.
6. **M7, web interface and sign-in.** Comes after M8d, so the interface shows model metadata
   (kind, grain, key, references, layers and labels) from its first version. A web
   interface with OIDC sign-in, or an invite link and a passkey for teams without an identity
   provider. It shows the catalog, lineage, runs with logs and check results and deploys, and has
   a one-query SQL editor, administration of users, groups, grants, tokens and connections,
   loading a file, and a first-run tour. No file browser, dashboards or notebooks. The Compose
   bundle can set up a TLS proxy, because passkeys need HTTPS. Done when a new user can sign in and answer a question
   without the CLI. A second security review is due when the interface and sign-in arrive. M7 is designed first (#66): a spec and its slices come before any screen, built around what the
   first audience needs, and the private test round checks it before the public beta. After
   M7, a small private test round on a hosted instance comes before the beta.
7. **M7c, public beta.** The repository is open and the packages are on PyPI as a pre-beta from
   0.12.0; the beta is the 0.x release that says it is ready to try, planned for about early December 2026.
   The beta includes the web interface (M7) with sign-in by OIDC, or by an invite link and a
   passkey for teams without an identity provider, and the data-modelling features of M8d, after a
   small private test round on a hosted instance. The CLI, the SDK and the REST API stay. Tokens
   remain for the CLI, the SDK and automation. The API may still change. Done when a stranger can
   install it from PyPI and follow the documentation to a working instance, a second person can
   sign in through the interface, a new team can load data from a file, a SQL database and a
   JSON API with the M6 helpers, and model it with the M8d template.
8. **M8, ML.** Experiment tracking, the model registry, and the offline feature store, all
    queryable and tied to commits. The registry is the artifact store of M5c2b. It adds named
    pointers such as `production`, pinned versions that a consumer can name, and metrics on a
    version. Done when a model can be traced to the
    exact code, data and parameters that produced it.
9. **M8b, agents.** An MCP server so AI agents can operate Lakana under the same authorization
    and audit as a person. The server exposes the graph queries of M8d (lineage, impact and
    join paths) as tools. Done when an agent can run a deploy and a query and both appear in
    the audit log under its own identity.
10. **M8c, BI tools.** A read-only governed SQL endpoint over the PostgreSQL wire protocol, with
    a token as the password. A service account is refused on a table that has a row filter or
    column mask; a dashboard on restricted data reads a table built for its audience. Done when
    each tool on the tested list connects and sees only what its identity may.
11. **M9, performance and multi-host workers.** Measured query performance, and workers that
    run on more than one host. Done when adding a host increases throughput as documented.
12. **M10, hardening and v1.0.0.** Security review, upgrade paths, documentation pass and the
    API freeze. Done when the release criteria are met and v1.0.0 is tagged.

## After v1.0.0

- Model serving, starting with a discussion of whether and how.
- Kubernetes and a Helm chart.
- Arrow Flight SQL, for Python, R and other clients.
- More databases for ingestion, starting with the ones teams ask for most.
- SFTP as a file source.
- Data contracts: the schema, freshness and checks a published table promises its consumers.
- Cloud file-sharing services as file sources.
- No-code loads: configuring a recurring load without writing an asset (#86). Nothing no-code
  is in scope up to v1.0.0.

## Out of scope

Distributed query execution, real-time streaming, AutoML and GPU scheduling, hosting notebooks,
languages other than SQL and Python, custom roles, a plugin ecosystem and registry, an online
feature store and a semantic layer. The reasons are in [`scope.md`](scope.md). Unstructured data
is parked.
