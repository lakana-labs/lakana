# Architecture

One page. The authoritative documents are:

- `docs/site/explanation/scope.md` — what the platform is for and what it leaves out
- `docs/site/explanation/roadmap.md` — the milestone sequence
- `docs/security/threat-model.md` — the security design
- the code itself: the package boundaries below are enforced by import-linter

Read this page for orientation; the design decisions and their reasoning live in those
documents and in the commit history.

## Components

```
lakana-server ──▶ lakana-core
lakana-worker ──▶ lakana-core
lakana        ──▶ (nothing internal)
```

`lakana-server` and `lakana-worker` never import each other; they communicate only by writing
and reading `query_runs` and `deploy_runs` rows through `lakana-core` (`import-linter`'s
`layers` contract in `.importlinter` makes this a lint failure, not just a convention). As
of M4b, all four packages are real, substantially built — the M1–M3 work described below
has landed, `lakana` (the SDK) covers every platform route with a contract test to prove it,
and `lakana` also now contains the CLI built on top of it (details below).

- **`lakana-core`** (`packages/lakana-core`, import `lakana_core`) — domain and persistence.
  111 modules across:
  - `models/` — the SQLAlchemy models for the whole metadata schema: `Environment`,
    `Namespace`, `Table`, `Principal`, `Group`/`GroupMembership`, `Grant`,
    `PersonalAccessToken`/`TaskToken`, `RowFilter`, `ColumnMask`, `Secret`, `QueryRun`,
    `LineageEdge`, `Repository`, `DeployRun`, `JobVersion`, `EnvironmentDeployment`,
    `Run`, `TaskRun`, `TaskAttempt`, `SchedulerLease`, `AuditEvent`, the artifact store's
    `ArtifactVersion`, `ArtifactUpload`, `ArtifactBlob` (M5c2b), and `CheckResult` (M5d-a: one
    row per check and task run; a count, never a row of the table).
  - `migrations/` — Alembic migrations for that schema (`versions/`).
  - `catalog/` — `CatalogService` and the Iceberg REST operations: table/namespace CRUD,
    the compare-and-swap `commit_table`, data-file listing (`datafiles.py`), schema
    inference, and `external_location.py` (the `raw/<dir>` claim scheme for `EXTERNAL`
    tables).
  - `identity/` — principals, groups, PATs, and task tokens (`principals.py`,
    `groups.py`, `pats.py`, `task_tokens.py`, `tokens.py`), all SHA-256-hashed and
    `hmac.compare_digest`-verified.
  - `grants/` — `GrantAuthorization` (the real `AuthorizationService` implementation),
    role templates, task-token scope narrowing, and grant revocation.
  - `policy/` — row filters and column masks: the sqlglot-validated restricted
    expression subset (`expressions.py`), and the per-`(principal, table)` resolver.
  - `secrets/` — envelope encryption: `KeyProvider`/`EnvKeyProvider` (`keys.py`),
    AES-256-GCM encrypt/decrypt bound to the row (stored format 2, `crypto.py`), and the
    write-only `SecretService`.
  - `artifacts/` — the artifact store (M5c2b): `manifest.py`, `store.py`, `service.py`
    (`ArtifactService`) and `sweep.py`; see "The artifact store (M5c2b)" below.
  - `audit/` — the append-only `AuditEvent` log, `AuditService`, and
    `AuditingAuthorization` (wraps any `AuthorizationService` to audit every denial).
  - `storage/` — the `Storage` protocol over fsspec (`FsspecStorage`), and
    `StorageResolver`/`ResolvedStorage` (per-environment storage root resolution,
    path confinement, id-suffixed managed-table locations).
  - `projects/` — the M5b project pipeline (git itself runs only in the worker): discovery,
    front-matter and `lakana.yaml` parsing (`discovery.py`, `frontmatter.py`,
    `project_config.py`, `yaml_loader.py`), SQL parsing and validation (`parse.py`,
    `issues.py`, `limits.py`), `manifest.py`, `dag.py`, `diff.py`, the URL/ref/local-path
    validators (`git_input.py`), `RepositoryService` (`repositories.py`),
    `DeployService` (`deploys.py`), and the registered package indexes (M5c2c): the
    `package_indexes` table's `PackageIndexService` and `registered_indexes`
    (`package_indexes.py`), the URL rules and `KnownSources` (`package_sources.py`) and the lock
    and `pyproject.toml` checks (`lockfile.py`).
  - `net/` — outbound HTTPS to addresses someone else named (#15). `egress.py` holds the checks
    every such client shares: URL shape, the refusal of internal addresses (`is_internal`), the
    one name lookup under a deadline (`lookup`), connecting to the checked address
    (`pinned_request`), the operator's TLS and proxy (`tls_and_proxy`, never the environment),
    trying each address in turn (`each_address`), one deadline (`run_with_deadline`) and the
    reduction of any failure to a fixed reason (`classify`). The worker's webhook client
    (`lakana_worker.egress`) is built on it and keeps its own reasons, settings and test hook.
    `https_json.py` fetches one JSON document of at most 64 KiB from an allowed host name over
    https on port 443, from public addresses only and without redirects.
  - `ci_oidc/` — verification of CI providers' OIDC ID tokens (#15): `config.py`
    (`CiOidcConfig`, `LAKANA_CI_OIDC__*`, a field of the server's settings), `jwks.py`
    (`JwksCache`: discovery and the issuers' keys), `verification.py` (`IdTokenVerifier`) and
    `errors.py` (`IdTokenRefused` with a stable `RefusalCode`); `rules.py`
    (`CiTrustRuleService`, the claim matching of the exchange and of `explain`), `exchange.py`
    (`CiTokenService`: the exchange, single use of each `jti`, verification of a `lakana_ci_`
    token, `resolve_ci_scope`, `ci_scope_for_run`) and `scope.py` (what a CI token may ask the
    `DEPLOY` checks for). The server's `POST /api/v1/auth/oidc/token` and
    `/api/v1/ci-trust-rules` call it. See "CI OIDC verification" and "CI OIDC exchange and CI
    tokens (#15)" in `security.md`.
  - Top-level modules: `auth.py` (`AuthorizationService`, `PrincipalRef`, `Securable`,
    `TaskScope`, `CiScope`), `bootstrap.py`, `config.py`, `db.py`, `errors.py`, `identifiers.py`
    (`NamespacePath`, path-component validation), `iceberg_json.py`, `lineage.py`, and
    `query_rewrite.py` (the policy-rewriting query rewriter — parse, AST blacklist,
    per-table CTE substitution).

  **Never imports `fastapi` or `starlette`** (`import-linter`'s
  `core-is-framework-free` contract).
- **`lakana-server`** (`packages/lakana-server`, import `lakana_server`) — the FastAPI control
  plane. `app.py` builds one `FastAPI` app and registers both API surfaces: the platform
  REST API (`api/platform/` — `environments`, `namespaces`, `tables`, `queries`,
  `principals`, `groups`, `tokens`, `task_tokens`, `grants`, `row_filters`,
  `column_masks`, `secrets`, `audit_events`, `artifacts` (M5c2b), `package_indexes` (M5c2c), one router module each) and a
  spec-compliant subset of the Iceberg REST Catalog (`api/iceberg/` — `router.py`,
  `route.py`, `schemas.py`, `encoding.py`), sharing one auth path (`deps.py`). Also
  owns `settings.py` (`Settings`, read from `LAKANA_*` env vars),
  `auth_rate_limit.py` (`AuthAuditLimiter`, bounding failed-authentication audit
  writes), and `errors.py` (exception-to-HTTP-response mapping). **Never imports
  `pyiceberg` or `fsspec`** (`import-linter`'s `server-goes-through-core` contract,
  with `lakana_core.** -> pyiceberg`/`fsspec` excused as the one legitimate indirect
  path) — everything storage- or Iceberg-shaped goes through `lakana-core`.
- **`lakana-worker`** (`packages/lakana-worker`, import `lakana_worker`) — the DuckDB query
  executor and dequeue loop. `poller.py` claims and runs one queued item at a
  time (a query run, a deploy or a task; the one exception is under "The run engine"
  below), of the kinds the worker takes (`WorkerPool`, "Worker pools" below); `pipeline.py`'s `run_one` ties together parsing, authorization, catalog
  resolution, rewriting, execution, and result recording for a single query;
  `executor.py` is the worker-facing API of query execution (what a query is given, which
  limits apply, which error each end of a child becomes) and holds no connection: every
  statement runs in a child process (M5c3a). `query_child.py` is that child and the only module
  that imports `duckdb` (DuckDB 2.0; no ambient object-store credential, no persistent secrets,
  and only the locations of the tables a query reads open to it); `query_process.py` starts it, reads its Arrow
  stream, judges the result complete and stops it by "ask, wait, kill"; `query_protocol.py` is
  the request and the error document they exchange. `__main__.py` is the `python -m
  lakana_worker` entry point. `index_credentials.py` (M5c2c) turns a registered package
  index's secret into the `UV_INDEX` entry of an environment build and writes its messages.
  **The only package allowed to import `duckdb`**, and within it only `query_child.py`: the
  `only-the-query-child-imports-duckdb` contract in `.importlinter`.
  Never imports `lakana-server`.
- **`lakana`** (`packages/lakana`, import `lakana`) — the SDK. As of M4a, it is complete:
  `_config.py` (three-source config resolution: explicit arguments,
  `LAKANA_URL`/`LAKANA_TOKEN`/`LAKANA_PROFILE`, then the profile file), `errors.py` (the
  `LakanaError` hierarchy and HTTP status → exception mapping), `_http.py`
  (`HttpTransport`: bearer auth, path-parameter quoting, GET retries), `_routes.py`
  (the declarative `ROUTES` table — one `Route(method, path)` per platform route, keyed
  `"<resource>.<action>"`, e.g. `"tables.append"`), and `client.py` (`Client`, the entry
  point, plus its `.sql()` convenience method). `resources/` holds one module per
  platform resource (`environments`, `namespaces`, `tables`, `queries`, `principals`,
  `groups`, `tokens`, `task_tokens`, `grants`, `row_filters`, `column_masks`, `secrets`,
  `audit_events`, `artifacts`) — each a thin class over `HttpTransport` that looks up its routes in
  `ROUTES` by string key and converts JSON to/from the typed dataclasses in `_models.py`.
  Depends on neither `lakana-core` nor `lakana-server` (`import-linter`'s
  `sdk-is-independent` contract) — it only ever speaks HTTP.
  `packages/lakana-server/tests/sdk/test_sdk_contract.py` is the seam that keeps
  `_routes.py` from drifting from the server it targets: it compares `ROUTES` against
  `lakana_server.app.create_app(...).openapi()` in both directions (every SDK route exists
  on the server; every platform route — excluding Iceberg REST, which this SDK does not
  cover — has an SDK method) and checks that every `ROUTES` key is actually referenced
  from `resources/`, so a route can't go stale in either direction without a test
  failing. As of M4b, `packages/lakana` also contains the `lakana` command-line interface
  (`lakana.cli`, built with Typer + Rich), the `lakana` package's only other top-level
  concern. The CLI is built entirely on the SDK above it — it never talks HTTP directly —
  so it inherits the same `sdk-is-independent` boundary. `lakana.cli._profiles` is the sole
  writer of the profile file (`lakana._config` remains its sole reader).

The dependency direction is a one-way DAG, **enforced in CI by `import-linter`**
(`.importlinter`'s `[importlinter:...]` sections, checked by `uv run lint-imports`)
rather than by convention alone.

## Local mode (M4c)

`lakana server start --local` (`lakana.cli.server`, `lakana.cli.local`) runs a complete Lakana on
one machine with no Docker and no Postgres. `lakana` itself supervises two separate OS
processes — `lakana-server` (its console script wraps `uvicorn.run(create_app(settings), ...)`)
and `lakana-worker` (its console script wraps the existing dequeue loop) — never one
in-process worker: "in-process worker" in older text means one *command*,
not one process, so user SQL still never runs inside the API. The catalog is SQLite in WAL
journal mode (`lakana_core.db.create_engine` turns on WAL and a busy timeout on every SQLite
**file** connection, not in-memory SQLite or Postgres), so the server and worker can share
one database file without "database is locked" errors, and storage is a `file://` lake
under the data directory. The worker has a memory watchdog (`LAKANA_WORKER__MEMORY_LIMIT`, default 4GB; Linux measures the
worker and its descendants, macOS the worker only, Windows nothing). A worker that exceeds it first kills its largest query child or Python task process tree (Linux), and exits
with code 75 only if it is still over the limit afterwards or nothing endable was running, and the supervisor restarts it (at most 3 times in 5 minutes), printing one line
why; the supervisor passes the environment through unchanged.

The CLI finds both console scripts through `importlib.metadata` entry points
(`lakana.cli.local._scripts.find_script`) — it reads package metadata and imports neither
`lakana_server` nor `lakana_worker`, keeping `sdk-is-independent` intact. Startup order is
`lakana-server migrate` (applies Alembic migrations and exits), then `lakana-server` (the
supervisor polls its unauthenticated `GET /healthz` until it answers `200` or a 30 s
timeout passes), then `lakana-worker`. Both children's output streams to the terminal,
prefixed `server |` / `worker |`, drained on background threads so a chatty child can't
block the supervisor. Shutdown (Ctrl-C/SIGTERM, and on POSIX SIGHUP — e.g. closing the
terminal — unless it's already `SIG_IGN`, so `nohup` still works) sends `SIGTERM` to every
started child, `migrate` included, through the supervisor's own in-memory process handles
— never by signalling a PID read back from a pidfile — waits up to 10 s against one shared
deadline, then kills stragglers. `SIGINT` also gets `signal.default_int_handler` installed
explicitly, so a backgrounded `start` (which inherits `SIGINT` as `SIG_IGN` from a
non-interactive shell) still stops on `kill -INT`. All three signals are set to `SIG_IGN`
for the duration of the final `stop()` itself, so a second interrupt can't abort cleanup
partway through. `start` exits 0 once shutdown completes, whether or not a straggler needed
`SIGKILL` — 0 means the CLI's own shutdown ran to completion, not that every child answered
`SIGTERM` alone.

See `docs/agent/security.md` for the data directory, master key and bootstrap-credential
handling, and `docs/security/threat-model.md` for the local-mode threat row.

## Team mode (M4d)

`lakana bundle init` (`lakana.cli.deploy`) generates a Docker Compose bundle for a small team
sharing one instance, instead of the one-machine, no-Docker topology local mode runs.
Where local mode is one SQLite file and a `file://` lake supervised as two OS processes by
the CLI itself, team mode is nine Compose services (with `launcher` and `egress`, the Python
task containers' launcher and the builds' egress proxy), plus two optional ones (`worker-interactive` and `worker-pipeline`, no replica until `.env` says so), each in its own container, built from
one image (`Dockerfile`, repo root — the same image runs the server, every worker, and the
one-shot setup job, differing only in the command):

```
postgres  ──(healthy)──▶ migrate ──(completed)──▶ server ──(healthy)──▶ setup
seaweedfs ──(healthy)──▶ bucket  ─────────────────(completed)──────────▶ setup
launcher  ──(healthy)──▶ egress
launcher  ──(healthy)──▶ worker, worker-interactive, worker-pipeline
                          migrate ──(completed)──▶ worker (× LAKANA_WORKERS)
                          migrate ──(completed)──▶ worker-interactive (× LAKANA_WORKERS_INTERACTIVE, 0)
                          migrate ──(completed)──▶ worker-pipeline (× LAKANA_WORKERS_PIPELINE, 0)
```

`postgres` (catalog) and `seaweedfs` (S3-compatible object storage, via the SeaweedFS S3
gateway) start independently; `bucket` (one-shot, creates the `lakana` bucket) waits on
`seaweedfs`'s healthcheck, and `migrate` (one-shot, `lakana-server migrate` — the identical
command local mode's supervisor runs before starting its own `lakana-server`) waits on
`postgres`'s. `server` and `worker` (scaled to `LAKANA_WORKERS` replicas) both wait on
`migrate` completing successfully; so do `worker-interactive` and `worker-pipeline`, which are
`worker` plus `LAKANA_WORKER__TAKES` (YAML anchors in `compose.yaml`) and 0 replicas by default. `setup` — a one-shot job with no CLI-side supervisor
equivalent, since team mode has no single process orchestrating the others — waits on
`server`'s healthcheck and `bucket`'s completion, then creates the `dev` environment, its
storage secret, and the binding between them entirely over the REST API (`lakana.cli.deploy.
setup`, built on the SDK, never a direct database or storage credential), mirroring what
local mode's own first-run bootstrap does by calling `lakana-core` in-process. `lakana bundle
init` (`lakana.cli.deploy._init`) is the generator: it writes `compose.yaml` (a static
template) and a fresh `.env` (secrets included) into a target directory, and — like local
mode's own `init`-adjacent bootstrap — refuses to overwrite a bundle that already exists,
since doing so would silently rotate the secrets master key. See `docs/site/how-to/deploy-with-compose.md`
for the full reference and `docs/agent/security.md`/`docs/security/threat-model.md` for the
security posture this topology implies (no host ports for Postgres/SeaweedFS, `.env` as the
root of trust, non-root uid 10001 except the `launcher`, which holds the Docker socket, TLS from
an operator's reverse proxy).

**Worker shutdown (M5c1 and M5c3b):** on `SIGTERM` (tini, PID 1, forwards it at once) the
worker stops claiming, finishes the item it is running and returns, and a running *task run* is
released after `LAKANA_RUN__DRAIN_GRACE_SECONDS`; the bundle's `stop_grace_period: 45s` covers that
for a SQL task and for a Python task: its process is asked to stop only when that grace has run
out and then gets `LAKANA_RUN__KILL_GRACE` (10 s) to exit, so with the defaults its release takes
about 32 s. A venv build in progress is stopped when that grace has run out, like a running
process (`KeeperStop` reaches `venvs.acquire` and `DockerEnvironments.acquire` as `stop`, #291).
A worker killed before the
release leaves the task to the scheduler's reclaim, like any worker that dies.
A task whose worker dies outright is reclaimed by the scheduler, which also fails the task's
orphaned write `QueryRun`. Since M5c3b the same sweep ends the ad-hoc queries and deploys such a
worker held, after about a lease (60 s), `FAILED` with the cause `worker_lost`; a write that had
already committed ends `SUCCEEDED`. Nothing stays `RUNNING` for ever. User view:
`docs/site/how-to/stop-a-query.md`, "A worker that is lost".

## The write path and persisted lineage (M5a)

A query can write its result into a table instead of returning it. A request with a `target`
becomes a `QueryRun` with write fields (`write_mode`, `target_name`, `target_table_id`,
`incremental_key`, `evolve_schema`, and — once finished — `rows_written`, `snapshot_id`,
`watermark`, `schema_diff`). The worker parses, expands views, resolves and rewrites the SELECT as
for any query, then streams the Arrow batches of the query child (`executor.stream_query`, UTC
session zone) into `CatalogService.write_batches`. The batches reach the writer before the
supervisor can prove the child's result complete, so the commit happens only after the stream has
ended normally (exit status 0 and its end marker); a stream that does not end that way fails the
run, and the writer deletes the files it wrote (after reading the table again to see that the
commit did not land: `CatalogService._settle_failed_write`). `write_batches` plans the schema before reading a batch, writes Parquet files
under the target's location one bin at a time (`catalog.writes.DataFileWriter`, pyiceberg's
`write_file`), and commits once through `commit_table`: a fast append, or for `replace` one
`OVERWRITE` snapshot. The `TABLE_WRITE` audit event and one `lineage_edges` row per source ride in
the same transaction. `create` publishes the table with its first snapshot in one insert. Arrow
uploads use the same service (`PUT …/data` → `CatalogService.replace`). Both Arrow routes take
`expected_snapshot_id=<id>|none` (409 on a mismatch): replace pins with a `SnapshotPin`, as the SQL
path does, and append adds an `AssertRefSnapshotId` requirement; `none` on a `PUT` also creates a
missing table.

Views are `tables` rows with `table_type = VIEW` and a `view_definition`; `mode: view` plans them
in the worker (`describe_query`) and stores them without running them. `LineageService` walks
`lineage_edges`. An edge with `source_kind = connection` (migration 0049) names a connection in
`source_table_id`/`source_name`: a write by a bound task token (`append`, `write_batches`, an
artifact commit) adds one per connection in `BoundTask.connections`, the deployed manifest's
list read by `_resolve_bound` through `read_entry_connections`, never from request input
(`catalog.connection_lineage`). The walk shows it as a `CONNECTION` node only to a
`READ_ENVIRONMENT` holder without a task token, and never walks past it. Maintenance is a `QueryRun` with `write_mode = MAINTAIN`: the worker calls
`CatalogService.maintain`, which expires snapshots through `commit_table(action=MAINTAIN_TABLE)`
and deletes confined orphans older than `older_than_hours` (default 72, at most 87600)
(`catalog.maintenance`). Expiry is pinned to the metadata it read, so a concurrent change aborts
the run with the report's `conflict: true` ("table changed during maintenance, retry"); the
metadata pointer is re-read just before orphan deletion, which is skipped if it moved. Lineage is
a bounded walk (at most 500 nodes, `truncated` past that when a readable node is left out).

A deploy (M5b) is a `deploy_runs` row. `poll_once` claims the oldest queued item across
`query_runs` and `deploy_runs`. `lakana_worker.deploy.run_deploy` re-authorizes `DEPLOY`,
decrypts the repository's credential from the target environment's secret scope, fetches the
commit with `lakana_worker.git.fetch_commit` into a private temp directory, runs
`lakana_core.projects` (discover → parse → validate → manifest → diff), uploads the `git archive`
tarball to `<env root>/_artifacts/code/<repo>/<sha>-<sha256>.tar.gz` (the digest in the name
means a racing deploy of the same SHA can never replace an object whose digest was recorded;
an archive a failed deploy leaves is removed by `lakana_core.projects.code_sweep` from the same
`housekeeping` as the artifact sweep, once no project version names it (by location, or by commit and digest), it is over seven days old and no
deploy is queued or running on its storage root)
and records the project version, the pointer compare-and-swap and the audit event in one transaction
(`DeployService.record_deploy`). A rollback skips the fetch and only moves the pointer.

## The run engine (M5c1)

A run is a `runs` row with one `task_runs` row per selected asset, created by
`lakana_core.runs.service.RunService` (the server never executes anything). Every worker runs these
things:

- **the scheduler thread** (`lakana_worker.scheduler.SchedulerLoop`): about once a second it tries
  to hold the scheduler role (`lakana_core.runs.leader.LeaderElection` — a Postgres session advisory
  lock plus the `scheduler_lease` heartbeat row; on SQLite the heartbeat row alone, taken by a
  conditional update). The holder loads every active run (`store.load_snapshots`), computes the
  transitions with the pure `scheduler.tick` (readiness, backoff, expired leases, cancellation,
  `upstream_failed`, finalization) and applies them in per-run transactions of compare-and-swaps
  (`store.apply_transitions`) — a missed swap rolls back only that run's transaction, and the
  next tick re-reads it; the other runs of the tick stay committed. Lease and heartbeat times
  are the database's clock (`lakana_core.db.db_now`), never a worker's, so clock skew between
  workers cannot expire a live lease. On shutdown the worker
  stops the loop and joins its thread so the role is resigned.
- **the dequeue loop** (`lakana_worker.poller`): queries and deploys first, then the next `ready`
  task (`lakana_core.runs.tasks.claim_task`). `lakana_worker.tasks.run_claimed` verifies the code
  tarball once per run per worker (`lakana_worker.task_integrity.verify_code`; the task log lives in
  `lakana_worker.task_logs`), checks the run principal, inserts a `QueryRun` already
  `RUNNING` with `task_run_id` and binds it as the task's `query_run_id` (guarded by lease owner
  and claim counter), creates the asset's missing namespaces under that run context, and runs the write
  through `pipeline.run_one` inside an `executor.cancellation` block. A `LeaseKeeper` thread (`lakana_worker.task_lease`)
  renews the 60 s lease every 20 s for the claimed attempt, sees a cancel request, a lost lease or the
  SIGTERM drain, and stops the query child ("ask, wait, kill") instead of interrupting a
  connection; it is stopped before the outcome is recorded. The outcome
  is classified by exception type, recorded with the redacted `QueryRun.error_message` only (plus
  `QueryRun.error_cause`, a `QueryErrorCause` value set by `pipeline.failure_cause` from the same
  exception, returned as `error_cause` by the query-status route and as `QueryFailed.cause` by the
  SDK; the Python harness treats `not_found`, `permission_denied` and `result_too_large` of an
  input read as permanent (`inputs` phase only), everything else, including `invalid_query`,
  `out_of_memory` and no cause, as retryable; the causes are `not_found`, `permission_denied`,
  `result_too_large`, `invalid_query` (SQL syntax via `sql_syntax.describe_syntax_error`, and a
  DuckDB binder, catalog, parser, conversion or constraint error), `out_of_memory` (DuckDB's
  `OutOfMemoryException`), `timeout`, `cancelled` and `internal` (a DuckDB internal or fatal
  error, a storage error, a conflict, anything unclassified)), by
  `record_attempt` (retry with backoff, an uncounted conflict retry or release, or a final
  state), conditional on still holding the lease; the free-text log goes to
  `_artifacts/runs/<run_id>/<asset>.log`.
- **the notification thread** (`lakana_worker.notify.DeliveryLoop`, M5d-c): delivers the rows of
  `notification_deliveries` that `_apply_run` queued in the transaction that finished a run (event
  `failed` or `recovered`) and the `test` rows of `notify-test`. It never runs inside the task
  loop or the scheduler tick. It claims **one** due row per short transaction (`FOR UPDATE SKIP
  LOCKED`, a lease of 60 s from the database's clock), decrypts the webhook secret only for that
  attempt, builds the payload (`lakana_core.notifications.payload`) and sends it through
  `lakana_worker.egress` (the only code that sends a webhook, built on `lakana_core.net.egress`;
  see "Webhook egress" in `security.md`). A 2xx
  answer ends it `delivered`, a permanent 4xx or the fifth attempt `failed` (the audit event
  `NOTIFICATION_FAILED`), anything else is retried after about 30 s, 2 min, 10 min and 30 min.
  Nothing in it writes to the run. `__main__` starts it unless `LAKANA_WORKER__NOTIFICATIONS` is
  false or the worker has no usable secrets master key (a warning says so), and the SIGTERM drain
  stops it.

**Workers, the heartbeat and the sweep (M5c3b).** Every worker registers a row in `workers`
(`lakana_core.models.Worker`, migration `0022`) and keeps its `heartbeat_at` fresh from a thread of
its own (`lakana_worker.heartbeat.WorkerHeartbeat`, every `LAKANA_RUN__RENEW_SECONDS`, again after
5 s when a write failed; the database's clock, as for leases). A worker is alive while its
heartbeat is younger than `LAKANA_RUN__LEASE_SECONDS`; that one definition lives in
`lakana_core.runs.workers` (`alive_ids`, `without_alive_worker`, `forget_lost`, which keeps a lost
worker's row for the larger of an hour and the lease). Every `query_runs` and `deploy_runs` row a
worker starts carries its id (`worker_id`), and a worker whose own heartbeat is a lease old claims
nothing and stops its query. The tick of the scheduler thread has a second step after the task
transitions: `store.end_lost_runs`, the sweep, which ends the `RUNNING` query and deploy runs that
no alive worker holds (one conditional `UPDATE` per run; the evidence of a committed write,
`rows_written IS NOT NULL`, decides `SUCCEEDED` or `FAILED`; candidates are read `FOR NO KEY
UPDATE SKIP LOCKED`). Only the leader sweeps, and `SchedulerLoop` withholds the lease for
`renew + 5 s` after a gap of more than half a lease between its own ticks and as a new leader.
The write fence in `ProtectedWriteGuard.record_pending` keeps a late worker from committing a run
the sweep ended. The other enders and their order are listed in `docs/agent/security.md`
("Every ender, in one place"). `lakana_core.runs.waiting` computes the wait reason of a queued
query or a waiting task (`query_wait_reason`, `task_wait_reason`, `alive_workers` and `with_work`,
which read the workers as a `WorkerSnapshot` that counts them for one kind of work, `queued_times`,
`ahead_of`); it stores nothing and is evaluated when a status or a listing is read. The listings
are `QueryService.list_queries` and `RunService.list_workers` (routes `queries.list` and `workers.list`).

**Worker pools (M5c3c).** `LAKANA_WORKER__TAKES` (`all`, `interactive`, `pipeline`) is a
worker's `WorkerPool`, handed by `main` to `poll_forever` → `poll_iteration` → `poll_once` and to the
`serve_queries` callback of a Python task. The one rule that classifies a query run lives in
`lakana_core.runs.pools`: it is pipeline work when `task_run_id IS NOT NULL` or `write_mode` is
`MAINTAIN`, interactive otherwise (`is_pipeline`/`is_interactive` for SQL, `kind_of` for a row; two
explicit conditions, never `~`, because both test `IS NULL` first, so neither is ever `NULL`). Deploy runs and task
runs are pipeline work by their table. `all` selects as before (one statement across both kinds,
oldest first, then deploys by age, then a task); `interactive` selects interactive query runs and
claims no deploy and no task; `pipeline` selects pipeline query runs, deploys and tasks. While a
worker supervises a Python task, `serve_queries` selects query runs of **its own kinds only**, so
the supervising pipeline worker serves the task's input query itself (it carries `task_run_id`) and
never needs an interactive worker, and it leaves older ad-hoc queries to one. The scheduler loop,
the heartbeat, the claim gate (`may_claim`), the idle worker's ender and the leader's sweep do not
depend on `takes`.
`workers.takes` (migration `0023`, string, default `all`) is written by `workers.register` and by
the heartbeat's re-registration; `workers.alive_takes` reads the alive workers' values, and
`takes_kind` is false for a value outside the three (it selects nothing).

**The cap on a principal's running queries (M5c3c).**
`LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` (`auto` or a number) is resolved once per poll by
`pools.cap_for(setting, n)`, where n counts the alive workers that take interactive work
(`pools.interactive_workers`): `auto` is n for 1 or 2, n - 1 from 3 on, and 1 for none. The poll
hands the number to `run_one(running_cap=)`; `_next_query(capped=)` leaves out the principals at the
cap (`running_by_principal`), and the claim, `pipeline._start`, takes an advisory transaction lock
per principal (`pools.lock_principal`: key `CAP_LOCK_KEY` and 32 bits of the principal's id, bounded by
`SET LOCAL lock_timeout`, `CAP_LOCK_TIMEOUT_SECONDS`) and then runs a start update that carries the
count condition (`pools.under_cap`). A refused claim, by the condition or by the lock timeout,
leaves the run `QUEUED` and says nothing; after a lock timeout the polling loop (`poller.PassedBy`) also passes that principal's ad-hoc runs by for 30 s, and writes that time to `principals.query_passed_by_until` (`pools.pass_by`, migration `0041`), which the wait reason reads (`lock_timeout`, #95), so that a status read names the lock instead of `starting`. Counted: the principal's `RUNNING` interactive query runs,
in every environment. The wait reasons use the same resolution (`QueryService` reads
`pools.cap_for`), and say `user_limit` only while a worker that takes interactive work is free.

**Python tasks (M5c2a).** `run_claimed` dispatches on the manifest entry's `kind`; an entry without
one (a project version deployed before M5c2a) is SQL. A Python task goes to `RunEngine.python`
(`lakana_worker.python_tasks.PythonRuntime`), and this is the one exception to "one item at a
time": the worker's loop handles one item at a time — a query run, a deploy or a task — but while
a Python task's subprocess runs (supervised by the `lakana-python-task` thread), the loop keeps
claiming **query runs only**, of its own kinds (`poll_once(only_queries=True, pool=...)`), so the task's own input queries are
served by the worker that runs it. No second task and no deploy is claimed meanwhile, and nothing
once the drain is set. Three threads, each with its own database session:

- the *supervisor* prepares the attempt, unpacks the verified code into a private per-attempt
  directory under `<worker cache>/attempts/` (there is no shared code cache; the worker creates
  `attempts/` at start with `prepare_private_directory`, never the image, because Docker's
  copy-up into a new shared volume races between workers), re-checks the lock
  against this worker, gets the venv, reads the target table's state, mints a token bound to the
  attempt, and calls the runner. It starts the process and outlives it. On every path out it
  revokes the token, releases the venv and removes the attempt directory. A worker that dies
  mid-task leaves its directory: `AttemptSweeper` (`attempt_sweep.py`), called before every poll
  through `poll_forever(maintenance=...)`, removes direct children of the private `attempts/`
  directory that match `attempt-[a-z0-9_]{8}`, are real directories owned by the worker's user,
  have been unchanged for longer than a task can live (`token_ttl` plus the venv build timeout)
  and can be `flock`ed exclusively without blocking, through `remove_private_tree`. A worker holds
  `LOCK_SH` on a descriptor of its attempt directory (`O_CLOEXEC`) from creation until the attempt
  is removed, so a live attempt of any worker sharing the cache is never swept and the lock dies
  with the process; the age is the second guard.
  `DeploySweeper` (`deploy_sweep.py`) does the same for `lakana-deploy-<8 chars>` checkouts
  directly under the temp directory (not private, so each entry is checked for type and owner): a
  deploy holds `LOCK_SH` on its checkout for its whole run (`hold_checkout`); the sweeper takes
  `LOCK_EX|LOCK_NB` and re-checks the inode (`lock_exclusive_if_same`, shared with `AttemptSweeper`),
  after an age guard of at least 24 hours (`MIN_AGE_SECONDS`, for workers on older code that hold no
  lock). Every rule is decided on the opened descriptor (`fstat`), and a directory whose lock fails for
  any reason but being held is kept at any age and logged once. It runs at start-up
  (`sweep_deploy_checkouts`) and from the same `maintenance` hook, and never looks at `lakana-spill-*`.
  Venv lock files are never unlinked: unlinking one lets a paused evictor lock an orphaned inode
  and delete a venv another worker leases (review R1);
- the *lease keeper* is M5c1's, unchanged: renewal and recording stay guarded by owner and claim
  counter. `KeeperStop` turns its cancel, lost lease or drain into the runner's stop signal;
- the *main thread* serves queries, finishes the one in hand after the supervisor has ended, and
  only then records the outcome through the same `record_attempt`.

The worker does not trust the process's result record. For a table asset, success requires that the worker itself
finds the target present and changed (table id and current snapshot, read in-process before and
after; for an asset with checks the "before" is the task run's baseline, see "Checks: the task in
two phases (M5d-a)" above; for an artifact asset see below);
`rows_written` is the task's own report and only displayed. Classification order: a lost lease
records nothing; a write that landed is a success; then a cancel or a drain (seen by the keeper or
re-read from the run) wins over whatever the process said, because a bound token stops working
the moment its attempt is cancelled or reclaimed; then the timeout, the harness's `conflict` flag,
`retryable: false`, and otherwise a transient failure. The venv build's and the process's output
(redacted, control characters replaced, 1 MiB per attempt) goes into the attempt's log block.

**Artifact assets (M5c2b).** Three additions to the above, all in `python_tasks.py`. The supervisor
resolves every artifact input to a version in-process, before the process starts
(`run_input_version`: the highest live version a task run of this run committed, else the current
one) and re-checks each input's kind against the catalog (`kind_of`) in both directions: the kinds
recorded at deploy can be wrong, so the catalog has the last word. The description sent on stdin is
version 2: `output`, `input_kinds` (every input), `input_versions` (the artifact inputs) and
`work_dir`, the attempt directory, where the harness makes `inputs/` and `output/` itself. An
entry whose `output` is neither `table` nor `artifact`, and a manifest newer than
`MANIFEST_VERSION`, fail permanently. The success rule: an artifact attempt succeeds only if the
server holds a live `artifact_versions` row with exactly this task run and this claim
(`attempt_version`); the record's `version` and `manifest_sha256` are hints that are never stored
or logged, a version an earlier claim committed never counts, and a lost lease records nothing. A
version a process committed before it crashed is not a success (the retry commits its own). An
artifact upload's `conflict` is an ordinary transient failure: it gets no free immediate retry. The
worker never adds a version itself.

**Python appends and merges (M6).** The description is version 3 for an asset that appends,
merges or declares a connection (`write`, `key`, `watermark` `{column, value}`, `connections`
`{name: {kind}}`), version 2 for every other asset, so an older harness keeps running those. For an
append or merge with a watermark on an existing table, `_supervise` reads `read_watermark` as the
run principal after `_target_state` and before minting; its pin must equal that state (else a
`ConflictError`, the free retry), and the upload is pinned to the same snapshot. The platform does
not filter the function's rows; `ctx.watermark()` hands it the value. The harness uploads an append
with `tables.append` (a first run with `tables.replace` and `"none"`), a merge with `tables.merge`,
and checks a merge's repeated keys and the composite cap itself, with Python wording. Success rule
addition: an append or merge whose table is unchanged succeeds when the record claims 0 rows and no
snapshot (nothing new). `lakana.context.current()` holds the attempt's client and connections until
`main` has written the record and the trace.

A worker that is killed (SIGKILL, OOM) takes its children with it on Linux: the harness and the
`uv` of a venv build both ask the kernel for `PR_SET_PDEATHSIG` (the build through a small
launcher that then `execv`s uv, `venvs._LAUNCHER`) and check that their parent is still the worker
that started them. Processes those start themselves (a child of user code, the build backend of a
source distribution) are not covered.

Scaling: the scheduler role can move to its own process later without schema changes; fair
queueing (M5c3) and cron (M5d) plug into the tick.

**Checks: the task in two phases (M5d-a).** User documentation:
`docs/site/how-to/check-your-data.md`, `docs/site/reference/checks.md`. Security:
`docs/agent/security.md`, "Checks and the rollback (M5d-a)".

- **Discovery.** `lakana_core.projects.checks` finds `checks/<ns…>/<table>/<name>.sql`, applies the
  deploy rules (`check_issues`), builds the `CheckSpec`s of the manifest (`manifest_checks`,
  `checks_of`, which reads a stored manifest as untrusted) and the wrapper `count_query`
  (`SELECT COUNT(*) AS failing FROM (<check>) AS lakana_check`). A manifest with a `checks` list has
  version 4 (`MANIFEST_VERSION`; a project without checks keeps `VERSION_WITHOUT_CHECKS`, 3, so that
  a worker of the previous release refuses a manifest whose checks it would skip). `source_check_tables`
  maps each checked table that no asset produces to the selected assets that read it.
- **Tasks.** A check of a table the project builds adds no task: the asset's task has a second
  phase, and `TaskRun.has_checks` says so. A check of a table the project only reads gets one task of
  kind `source_check` per table (`RunService`, with an edge to every selected asset that reads it);
  it has only the second phase and no rollback. `ClaimedTask.kind` carries the kind to the worker.
  A table the project builds that carries the `keep` note (`kept_tables`) and is read by a
  selected asset without being rebuilt gets the same kind of task (`kept_check_tables`): a pass
  clears the note, a failure leaves it as it is and stops the readers (`kept_check_message`). It
  is told apart by "its table is an asset of the manifest" (`TaskRunView.kept_check`). When the
  project version has no check for the kept table any more, the task's one result and its message
  are `ERROR_KEPT_NO_CHECKS` (only a rebuild clears the note), not `ERROR_NO_CHECKS`; a check
  of it that was refused another project's kept table is named as a check that could not run.
- **The note on a kept table (`on_failure: keep`, migration `0025`).** Three nullable columns on
  `tables`: `kept_run_id`, `kept_task_run_id`, `kept_check` (the text K2 shows, "and N more"
  included); no foreign keys, so the note outlives its run. `lakana_core.runs.kept` owns it.
  *Where `keep` is allowed:* `check_issues` refuses it at deploy on a check of an asset of
  `materialize: incremental` (a rebuild only adds rows, so the table could never pass again) or
  `view` (no data to keep); an asset of `materialize: table` and a table the project only reads
  take it.
  *Who sets it:* only the task that built the table, in the check phase, when the verdict has a
  failing `keep` check (`note_kept`, its own transaction right after the verdict, under the same
  claim check; a worker lost in between is covered by the next claim, which runs the checks again
  and never the build). A `source_check` task never sets or moves a note, and neither does a
  `keep` check that could not run because it read another kept table, except that a table that
  carries a note already has it moved to the new run with `tables.kept_unchecked` (migration
  `0030`), so that its state text says no check could run on the data. *Who clears it
  (`clear_kept`, one `TABLE_KEPT_CLEARED` event, `why` of `KEPT_WHY`):* a check phase of the table
  whose `block` and `keep` checks all pass (`checks_passed`), the check task of a later run of the
  project that builds the table, or a rebuild by a project version that has no checks for it
  (`rebuilt_without_checks`, a step of its own that never fails the build; in rare cases the note
  survives and the next build clears it). A set-back follows it (migration `0031`):
  `tables.kept_earlier` is a JSON list, newest last, of the states the table had before the
  current one (a note a clear removed too; an entry with no `run_id` when the data had none; at most
  `KEPT_EARLIER_LIMIT`, the oldest drops). *Every state is a verdict on a snapshot:*
  `tables.kept_snapshot_id` is the snapshot the current state is about (the marked run's write,
  from the task run's stamp; or the snapshot a passing check judged when it cleared, or
  `KEPT_SNAPSHOT_UNKNOWN`), and each entry carries its own `snapshot_id`. `note_kept` pushes the
  state it replaces unless the same task run set it (another verdict of one task run is the same
  data); with no mark and no earlier verdict the pushed state is "no mark" on the stamp's snapshot
  before the write. `clear_kept` (through `_clear_row`, which reads the row `FOR UPDATE`) pushes the mark it removes,
  with the snapshot that mark was a verdict on, and records the snapshot its checks judged; the check
  phase's clear is tied to the note it began with (`KeptSeen`, read in the statement that reads the
  written table) and removes only that one, in `record_attempt`'s transaction; the same transaction
  records the passing snapshot on an unmarked table that has a history (`record_clean_in_attempt`,
  which pushes the unmarked state it replaces), and a rebuild without checks records the snapshot it
  built. `_pushed` drops an older entry for the same snapshot. Migration `0031` marks an unmarked table with a write in
  flight as unknown (-1), because a clear before the upgrade may have judged it.
  `CatalogService.roll_back` (every set-back: a failed `block` check, `set_back_only`, the baseline
  set-back of a failed Python build) calls `restore_kept` in the transaction of the pointer swap,
  after it (the pointer is a column of the table's row, so the row is locked by then; the restore
  reads it `FOR UPDATE`). It acts when the row's verdict snapshot is not the snapshot the table is set
  back to, whoever set it: the newest entry for that snapshot becomes the state and the entries above
  it are dropped (they describe data that is gone). An entry with no `run_id` means no note, the only
  case in which readers are let in again. With no entry (a row from before `0031`, history that ran
  out, an unreadable entry, an unknown snapshot) the table gets a mark of unknown checks
  (`tables.kept_unknown`, migration `0032`; `kept_run_id` and `kept_task_run_id` name the run that was
  set back, `kept_check` is empty), with its own state and refusal text; like any mark it is cleared by
  passing checks. A table with no mark and no verdict (`kept_snapshot_id` null) is left alone, a mark
  that names another run or task run stays, and a first build keeps its mark (`kept_not_restored`).
  The audit goes in the `TABLE_ROLLED_BACK` detail (`kept_restored`, `kept_unknown`, `kept_not_restored`): a new
  event type would need an enum migration for nothing the rollback event cannot say. *Why the check task of a later
  run has the kind `source_check`:* a new kind would be built as an asset by a worker of the
  release before; `source_check` is the kind such a worker already runs without a rollback. The
  read path is in `docs/agent/security.md`, "The kept note on the read path".
- **A task fails closed when the manifest and the task disagree.** `read_manifest_checks` reads a
  stored manifest as untrusted and says which entries it could not read (`unreadable_tables`, and
  `unplaced` for an entry that names no table of the manifest, a `checks` value that is not a
  list or a version-4 manifest without an entry, which counts against every table; an entry whose
  name is not `<table>/<check>` or repeats is unreadable too, because results are recorded by
  name). `RunService`
  sets `has_checks` for such a table, so its task goes through phase two, where an extra blocking
  result `<table>/(unreadable)` (`ERROR_UNREADABLE_CHECK`) is recorded beside the checks that can
  be read, as `<table>/(none)` (`ERROR_NO_CHECKS`) is for a task with checks and none to run. The
  other way round, a claimed task whose `has_checks` is false while the manifest lists checks for
  its table, or for a table it reads that has no `source_check` task in the run, comes from a
  server older than the worker; `_dispatch` refuses it before anything is written
  (`_older_server_refusal`).
- **Phase one, the write.** As before, with one addition: the write's commit transaction (the
  fence's, `ProtectedWriteGuard.record_pending`) records `QueryRun.snapshot_before` and, for a task
  with checks, stamps the task run *written* (`TaskRun.written_*`: table, snapshot before, snapshot
  after, `publishes`). A Python asset cannot stamp with its commit, so `record_baseline` records the
  table's state once before the first process of the task run (the *baseline*), each write of the
  task run's own chain advances `own_snapshot_id` (the *tip*, `_advance_own_tip`), and the stamp is
  written after the process ends. A write notes its result through `_record_write`; `append` and
  an Iceberg REST commit, which reach `commit_table` without one, are noted there
  (`ProtectedWriteGuard.note_own_commit`: claim form, no fence, an added snapshot that becomes the
  head, rows from its summary's `added-records`). The rollback names the tip as "after".
- **Phase two, the checks** (`lakana_worker.task_checks.check_phase`, under a lease keeper of its own).
  `lakana_worker.checks.count_check` runs each check's count through `pipeline.run_one` as the run's
  principal: a read `QueryRun` inserted `RUNNING` with `task_run_id`, never bound as the task's
  `query_run_id`, with no result file (the count returns to the worker through `on_result`).
  `lakana_core.runs.checks` keeps the results (`record_result`, one `CheckResult` per check, a
  `CHECK_FAILED` audit row per failed blocking check), judges them (`verdict`) and words every
  message (module constants; the worker writes, the server shows). `lakana_core.projects.checks`
  and `lakana_core.runs.checks` are imported by the server and the worker; only the worker counts.
- **The verdict.** Every check runs, also after a failure. Only `warn` checks failed: the task
  succeeds, with a note. A blocking check failed or errored: `CatalogService.roll_back`, then the
  task fails for good. A transient failure of a count retries while attempts remain, and is
  `errored` on the last one.
- **The rollback's order** (`CatalogService.roll_back`): authorize and check the write guard under
  the claim form of the run context; decide by what the catalog finds (the table's current
  snapshot must still be the run's tip: the requirement); upload the new metadata file; move the
  pointer; and, in that same transaction, set or leave the `unpublished_run_id` marker, clear the
  written stamp, record `TABLE_ROLLED_BACK` and put the outcome on the failed results. It locks the
  table's row and then the task run's, like a write, and holds no row lock across an upload. A
  failure of the database never leaves it undecided: the outcome is read back from the rollback's
  own audit row. The tag `lakana-check-failed-<run>` keeps the failed snapshot for
  `FAILED_SNAPSHOT_KEEP_DAYS` (7), and `lakana check rows` reads it for a user (next bullet).
- **Looking at the rows of a check.** A result stores a count and the id of the snapshot
  the count read (`checked_snapshot_id`), never a row. `lakana check rows` is
  `POST /environments/{env}/runs/{run_id}/checks/rows`, answered by
  `lakana_core.runs.check_rows.submit_check_rows`: it checks two rights (read the run, submit a
  query), finds the result among that run's results by the check's name, refuses a caller who may
  not read every table the check's SQL names (read again from the SQL), and queues an ordinary
  ad-hoc `QueryRun` of the caller whose `sql_text` is the check's SQL from the run's manifest and
  whose `check_result_id` (migration `0026`, no foreign key) is the **binding** to the result. The
  worker's source resolution (`lakana_worker.pipeline._resolve_sources`) is the only place that
  acts on the binding: it reads the result once, and for the one source whose catalog row has the
  result's table id it calls `CatalogService.load_table_at_check` instead of `load_table`, which
  returns a `LoadedAt` (the table's metadata, the snapshot and the snapshot's columns). That is the
  **pin**: the table is read at the checked snapshot, only the columns the table still has are
  declared, and the caller's row filters and column masks are applied by the usual rewrite. Every
  other source is read as it is now. The read does not need the table to be published, and it is
  the only one that does not. A pipeline task is refused in `submit_check_rows` and again in
  `load_table_at_check`. The run's `QUERY_COMPLETED` event carries `check_result_id`, which no
  other query's event has. The user documentation is `docs/site/how-to/check-your-data.md`,
  "Look at the rows that failed"; what a reviewer must know is in `docs/agent/security.md`, "The
  query that shows the rows of a check".
- **The tag's removal.** `CatalogService.maintain` asks `catalog.maintenance.old_failed_tags` for
  the tags with the failed-check prefix whose snapshot is more than seven days old (a tag has no
  creation time; the snapshot's timestamp is the age, and the comparison is strict), removes each
  ref and expires its snapshot in the same commit as the rest of the expiry, whatever `keep_days`
  and `keep_last` say, unless another ref names the snapshot or a remaining ref's history
  contains it. `MaintenanceReport.tags_removed` carries the names to the audit row
  (`TABLE_MAINTAINED`) and to the CLI line. After that, `load_table_at_check` raises
  `CheckedSnapshotGoneError`, and the count in `lakana check list` is all that remains.
- **What maintenance spares.** `CatalogService.maintain` passes
  `_snapshots_running_tasks_need` to `select_expired_snapshots` as `spare`: the
  `written_snapshot_before` of every task run that is not terminal and wrote this table, and every
  such task run's `baseline_snapshot_id` that is among the table's snapshots (a baseline has no
  table id; snapshot ids are random 63-bit values). A spared snapshot is neither expired by age nor
  taken with an old failed-check tag, so the set-back of a running task still finds its state. An
  Iceberg REST commit that removes snapshots is not maintenance and is not covered.
- **A lost worker.** A task with checks whose write is in the table is *unchecked* until a worker
  records the verdict (`store.awaiting_checks`, `TaskSnapshot.unchecked`): the sweep and the
  worker's own recording do not count it succeeded or failed, its lease reclaim puts it back to
  `pending` uncounted, and the next claim finds it *written* and runs the checks only
  (`task_checks.before_build`). A claim that finds a landed rollback in the results ends the task failed from
  them. The three tests in `test_checks_races_integration.py` hold this on PostgreSQL.
  A task that may have written but is not stamped (a Python asset with checks) and whose lease is
  lost on its last attempt, or in a cancelled run, goes back `ready` with `task_runs.setting_back`
  instead of ending; the next claim only sets the table back (`task_checks.set_back_only`). The
  cancel loop does the same, without a reclaim, for a waiting task of a cancelled run that awaits
  its checks or has checks, a baseline and no stamp (`TaskSnapshot.baselined`); its statement
  requires those facts and the claim it saw, and a plain cancel requires their absence.
  A live worker hands a task back the same way, from `record_attempt` under the claim it holds
  (`tasks._must_be_set_back`, decided on the locked row with `awaiting_checks` and
  `may_have_written`): a cancel seen after the write (stamped, or a baseline without a stamp), and
  a memory stop that ends for good a task that may have written (`record_memory_failure`; a stop
  that leaves attempts, or a stamped task, is unchanged). The stop's message waits in
  `task_runs.error_message` for the set-back claim, which ends the task with it; the scheduler's
  put-back clears that column so that an earlier attempt's text is never taken for the reason.

### Failure codes of tasks (#145)

- **One code per ending, stored with the text.** `task_runs.failure_code` and
  `task_attempts.failure_code` (nullable `String(32)`, migration `0040`) hold a
  `lakana_core.runs.failures.FailureCode` value next to `error_message`, written by the same
  statements: `record_attempt(failure_code=...)` for a worker's ending and
  `TaskTransition.failure_code` / the lost-attempt update in `store._apply_task` for the
  scheduler's. A success, a release and a retry that is not an ending store none; a cancel is always
  `cancelled`; a failure recorded without a code is `internal` (`tasks._stored_code`). A put-back
  for a set-back clears the code with the text, and the worker that sets the table back ends the
  task with a code of its own (`task_checks._set_back_code`: the stored memory stop's, else
  `worker_lost`, else `cancelled`).
- **The worker names the code where it names the kind.** `TaskFailure(kind, message, code)` takes
  the code as a required argument, a failing `TaskOutcome` carries `failure_code`, and a
  `RunOutcome(succeeded=False)` of the Python runner does too;
  `tests/test_failure_code_sites.py` reads the worker's source and fails when a call site leaves
  it out. A query's exception becomes a code through the query run's own cause
  (`task_outcome.failure_code_of`, over `pipeline.failure_cause`, plus `conflict`).
- **One table of sentences.** `FAILURES` in `lakana_core/runs/failures.py` maps each code to its
  cause and next step; `docs/site/reference/why-did-it-fail.md` has the same sentences and a test
  compares them. Rows from before the codes show `unknown` (`stored_failure`), which is never
  stored. The REST/SDK/CLI shapes are `failure` on tasks and attempts and `failed_tasks` on runs.

## Python assets (M5c2a)

User documentation: `docs/site/how-to/projects.md`, "Python assets". The run-time mechanics (threads, the
poll loop, classification) are under "The run engine (M5c1)", "Python tasks (M5c2a)".

| Unit | Package | Purpose |
|---|---|---|
| `lakana.assets` | `lakana` | `@asset` and `AssetSpec`. |
| `lakana.runner` | `lakana` | The harness: runs one asset inside the task's venv. Imports only the SDK. |
| `lakana_core.projects.python_assets` | core | `ast` discovery of one file into a `ParsedAsset`. |
| `lakana_core.projects.lockfile` | core | `pyproject.toml` and `uv.lock` validation. |
| `lakana_core.identity.task_tokens` | core | Bound tokens: mint for a task attempt, resolve, revoke. |
| `lakana_core.grants.authorization` | core | Scope rules for bound tokens. |
| `lakana_core.runs.lockdown` | core | The claim-form run context. |
| `lakana_worker.python_support` | worker | Whether this worker can run Python assets; the cache directory. |
| `lakana_worker.runners` | worker | `Runner`, `LocalProcessRunner`, `DockerRunner`, `unpack_code`. |
| `lakana_worker.launcher_client` | worker | The worker's side of the launcher's socket: one request per connection with the launcher's token; `LauncherUnavailable`, `LauncherRefused`, `LauncherTimedOut`, `LogSinkFailed`; `claim_owner` names the worker for the launcher's labels. |
| `lakana_worker.docker_environments` | worker | Environments built by the launcher, one volume per project, lock, `pyproject.toml`, Python and index set. |
| `lakana_worker.launcher` | launcher (own service) | The only holder of the Docker socket: request schema, container specs, the Docker Engine client, the run/build/stop/status/cleanup operations, the Unix-socket server (`lakana-launcher`), and the builds' egress proxy (`launcher.egress`, the `egress` service) with its tokens (`launcher.egress_token`); standard library only (contract `launcher-stands-alone`). Security: `security.md`, "Task isolation: the launcher". User documentation: `docs/site/how-to/run-python-tasks-in-containers.md`. |
| `lakana_worker.venvs` | worker | The venv cache. |
| `lakana_worker.python_tasks` | worker | One Python attempt: prepare, supervise, classify. |
| `lakana_worker.tasks`, `poller` | worker | Dispatch by manifest `kind`; query-only polling during a Python task. |
| `lakana_worker.task_checks` | worker | The check phase of a task: count, record, verdict, rollback. |
| `lakana_worker.task_lease`, `task_outcome` | worker | The lease keeper and its loss; how an attempt ended (`TaskOutcome`, `TaskFailure`). |

**The runner protocol.** `Runner.run(task: PythonTask, on_log, stop: StopSignal) -> RunOutcome`.
`LocalProcessRunner` runs the harness as a subprocess of the worker; `DockerRunner` asks
the launcher to run it in a container of its own and implements the same protocol
(`Runner.isolated` is true for it only, and a protected environment needs it);
`LAKANA_RUN__RUNNER` (`local` by default, or `docker`) picks one at the worker's start, and with
`docker` `PythonRuntime.environments` (`DockerEnvironments`) replaces the local venv cache. `run` blocks until the process is reaped and its output drained; call it from a thread
that outlives the process, because the parent-death signal is tied to the thread that started it.

**One attempt.** claim, then the `lakana-python-task` thread: verify the code tarball and unpack
it into a private per-attempt directory (`<worker cache>/attempts/<attempt>/code`, read-only,
removed at the end; there is no shared code cache), re-check the lock against this worker, read
the target table's snapshot, get the venv (`<cache>/venvs/<sha256 of lock and pyproject.toml>-py<x.y>-<platform>`), mint
the bound token, run `python -P -m lakana.runner` (task description on stdin, result record in
the attempt directory), then revoke the token, release the venv and remove the attempt directory.
The lease keeper renews throughout and turns a cancel, a lost lease and a drain into the runner's
stop signal.

**Why the harness is in the SDK.** Rule 2: user code that we execute ourselves gets no side
channel. The venv contains `lakana` and polars, never `lakana-core` or `lakana-worker`, and
import-linter forbids the SDK to import them.

**One kind per manifest entry.** A project version's manifest (version 3) records `kind` per asset
and `lock_sha256` per project version; an entry without `kind` is SQL, so project versions deployed before
M5c2a run unchanged. Version 3 (M5c2b) adds `output`, `keep_versions` and `input_kinds` to a
Python entry; an entry without `output` is a table, and the diff reads both through
`entry_output`/`entry_keep_versions`, so an upgrade alone is no change. A Python asset reaches the DAG as a `ParsedAsset` with its inputs as
`references`, so graph building, cycle checks, environment validation and run policy treat both
kinds alike.

## The artifact store (M5c2b)

User documentation: `docs/site/reference/artifacts.md` (facts) and
`docs/site/explanation/artifacts.md` (why). Security: `docs/agent/security.md`, "The artifact store
(M5c2b)". A Python asset declared `@asset(output="artifact", keep_versions=N)` stores a file or a
directory as a numbered version; another asset reads it; the worker succeeds only on a version this
attempt stored.

**The catalog.** An artifact is a row of `tables` with `table_type = ARTIFACT` (migration
`0018_artifact_store.py`), so one name means one thing and grants, ownership, orphans and lineage work
by the table id. Three tables hang off it, all cascading with the row:

| Table | Holds |
|---|---|
| `artifact_versions` | One row per version: `(artifact_id, version)` unique, `manifest_sha256`, `manifest_json`, size, file count, `is_directory`, the producer (`task_run_id`, `claim`, `job_version_id`, `commit_sha`, `created_by`) and `deleted_at`/`deleted_by`. Never edited; deleting sets `deleted_at`. |
| `artifact_uploads` | An open upload: `principal_id`, the manifest, `task_run_id` and `claim`, `expires_at`, and `committed_version` once committed (a committed row stays until it expires, so a repeated commit is idempotent). |
| `artifact_blobs` | Primary key `(artifact_id, sha256)`, size: the files stored for this artifact. "Which hashes are missing" and "is every file stored" are one query. |

A lineage edge uses the version number in `source_snapshot_id` / `target_snapshot_id` when that end
is an artifact; the node's `kind` says which it is.

**Code, by package.**

| Package | Unit | Owns |
|---|---|---|
| core | `lakana_core.artifacts.manifest` | The path and manifest rules, the canonical manifest and its digest, `parse_manifest` with the limits. |
| core | `lakana_core.artifacts.store` | Storage paths (`blob_path`, `manifest_path`), `lock_artifact`, the reference queries (`unreferenced_hashes`), `kind_of`, `run_input_version`, `attempt_version` (worker-only: they authorize nothing). |
| core | `lakana_core.artifacts.service` | `ArtifactService` (authorization, upload, commit, read, download, delete, drop, abort, `keep_versions`) and `BlobUpload`. |
| core | `lakana_core.artifacts.sweep` | The sweep. |
| core | `storage.base` / `fsspec_storage` | `StreamWriter`, `Storage.open_write`, `Storage.read_chunks`: multipart upload on object stores, temporary name and rename locally. |
| core | `catalog`, `runs.lockdown`, `grants.authorization`, `projects.*` | Table operations refuse an artifact; `WriteOperation.ARTIFACT_VERSION`, `DELETE_ARTIFACT_VERSION`, `DROP_ARTIFACT`; `BoundTask.output`/`keep_versions`; manifest version 3 and the deploy checks. |
| server | `lakana_server.api.platform.artifacts` | The eleven routes. It streams a body to `BlobUpload` and a file from storage; the limiters live here. |
| sdk | `lakana._artifact_manifest`, `_artifact_tree`, `resources.artifacts`, `cli.artifacts`, `assets`, `runner` | Scan and local copy of the manifest rules (the SDK imports nothing of core; a parity test in `packages/lakana-worker/tests` keeps both copies equal), `client.artifacts`, the commands, `output_dir()`, the harness. |
| worker | `python_tasks`, `runners`, `__main__` | Version resolution, the success rule, the description sent to the harness, the sweep's housekeeping thread. |

**The layout** under the environment's storage root: `artifacts/<artifact id as 32 hex digits>/blobs/<sha256>`,
`artifacts/<id>/manifests/<version>.json`, and the marker `artifacts/<id>/.dropped` while a drop is in
progress. The id, not the name, so a renamed or recreated name never reaches old bytes. Files are
content-addressed **per artifact**; nothing is shared between artifacts (sharing would leak whether
another artifact holds a file and would make deletion depend on objects the deleter cannot see). The
directory `artifacts/` is reserved: `confine_to_root` refuses a table location inside it.

**The task flow.**

1. **Deploy** reads the decorator with `ast` (`output`, `keep_versions`), tells the kind of every
   input (`DeployService.artifact_inputs`: from the catalog for what the deployer may read, from the
   other repository's current deploy otherwise) and records them in manifest **version 3**
   (`output`, `keep_versions`, `input_kinds` on a Python entry; an entry without `output` is a
   table). It refuses a SQL asset that reads an artifact, a name that clashes with another kind, a
   self-input and a parameter name the task cannot store a copy under.
2. **The worker's `_prepare`** builds the run principal, resolves each input **as that principal**
   (`permits(READ_TABLE)` first, then the catalog), re-checks the kinds against the
   catalog, and for an artifact input picks the highest live version a task of this run stored, else
   the current one (`run_input_version`). It sends description version 2 (`output`, `input_kinds`,
   `input_versions`, `work_dir`) to the harness and mints a bound token (declared inputs, own asset,
   `output`, `keep_versions`).
3. **The harness** (`lakana.runner`) makes `inputs/` and `output/` in the attempt directory, downloads
   each artifact input through `client.artifacts.download(..., read_only=True)` into
   `inputs/<parameter>`, calls the function, pins what it returned by descriptor, and uploads it with
   `client.artifacts.upload(..., sources=...)`: begin, the missing files, commit. The commit applies
   `keep_versions` from the token's bound scope.
4. **The worker's success rule**: an artifact attempt succeeded only if
   `attempt_version` finds a live `artifact_versions` row with exactly this task run and claim. The
   record's `version` and `manifest_sha256` are hints. A version stored by an attempt that then died
   stays live; the attempt is recorded as failed and retried.

**The sweep.** Run in a thread of its own by the worker that holds the scheduler role, every
`LAKANA_RUN__SWEEP_INTERVAL` seconds (`housekeeping` in `lakana_worker.__main__`); see
"The artifact store (M5c2b)" in `security.md` for what it deletes and what it never does. The same
function then runs the table location sweep (`sweep_table_locations`, see "Table locations" in
`security.md`) and the code archive sweep (`sweep_code_archives`: a minute of its own), with the
same `now` and rotation, and a failure of one does not stop the others.

**Settings.** `ArtifactsConfig` (`LAKANA_ARTIFACTS__MAX_FILE_BYTES`, `MAX_BYTES`, `MAX_FILES`,
`UPLOAD_TTL_SECONDS`), read by the server and the worker; the info route sends the three size limits
so the SDK refuses a tree before it sends a byte. File uploads share `LAKANA_APPEND__MAX_CONCURRENT`
and `LAKANA_APPEND__READ_TIMEOUT_SECONDS` with table appends; downloads have a limiter of their own.

## Boundary invariants worth remembering

- **The REST API is the only way in.** No component — not the CLI, not a future UI, not
  user code — gets a privileged side channel around it.
- **Environment is a required parameter on every core operation.** `lakana-core` never
  guesses an ambient default; the server resolves it from the request.
- **Write-then-CAS ordering is mandatory** for catalog commits: Iceberg metadata is written
  to object storage before the pointer is committed, so an orphaned metadata file is
  recoverable garbage but a dangling pointer would be corruption.
- **Synchronous core** — no `async def` in `lakana-core`; FastAPI endpoints are declared
  `def` so Starlette dispatches them to its own threadpool.
- **Artifact bytes: rows first, bytes second.** A deletion changes the database under the artifact's
  lock and removes objects afterwards; a file becomes visible and gets its row under that lock. A
  crash leaves orphan bytes for the sweep, never a live version without bytes.
- **Table locations: record first.** A create commits its location's `CLAIMED` record before the
  first file and publishes it to `LIVE` in the transaction that inserts the row (the fence); the
  location sweep deletes only locations whose record is `ABANDONED` or `DROPPED`, never a directory
  without a record. A drop makes the record a tombstone in the transaction that deletes the row
  (`lakana_core.catalog.tombstones`): due after `LAKANA_TABLES__UNDO_DAYS`, with the row's snapshot
  and the ids of the policies it revoked, which `undrop_table` restores under the record's lock.
  An expiry (`lakana_core.catalog.retention`, step 1 of the location sweep) is the same drop
  (`tombstones.drop_row`) by the system, decided again under the environment's and the table's row
  locks. Deleting an environment (`lakana_core.catalog.environment_deletion`) is the same drop for
  every table in it; the row stays, marked `deleted_at` and hidden from every lookup by name
  (`LIVE_ENVIRONMENT`), until the sweep has purged its last record and deletes it.
- **SQLite and Postgres both work** for `lakana-core` — no dialect-specific SQL, so the unit
  tier can run against in-memory SQLite while the integration tier uses real Postgres.
