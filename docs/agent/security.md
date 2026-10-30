# Security

Referenced from `CLAUDE.md` when you are touching authorization, policy, secrets, or the
query path. The living, milestone-updated threat model — read it before implementing anything in this
area, not just this summary — is `docs/security/threat-model.md`. For where authorization and the
query path sit relative to `lakana-core`/`lakana-server`, see `docs/agent/architecture.md`.

## Rules of thumb (M3f)

- **Grants, row filters, and column masks bind to a table's or environment's id, never its
  name.** Never add a code path that matches one of these against a securable's *name* —
  a rename or a drop-and-recreate under the same name must not inherit access.
- **Never accept or honour a caller-supplied table location.** Lakana assigns every managed
  table's location itself (`ResolvedStorage.table_location`); `create_table` and
  `commit_table` refuse a caller-supplied `location`/`SetLocationUpdate` outright, and
  `write.data.path`/`write.object-storage.path`/`write.metadata.path` are refused as table
  properties for the same reason. Do not add a path that lets any of these through, even
  confined to the storage root.
- **A new managed table's location is claimed before its first file, and published behind a
  fence.** Any new path that writes a new managed location goes through
  `CatalogService._prepare_table` (which commits the `CLAIMED` record) and `_publish_table` (which
  runs `publish_location` in the transaction that inserts the row and rolls back when it moves
  nothing). Never write a file under a location without a record, never insert an `ICEBERG` row
  without the fence, and never delete table files outside `lakana_core.catalog.location_sweep` or
  `_release_location` (`lakana_core.catalog.orphaned_locations` deletes only through the sweep's
  purge). See "Table locations" below.
- **An `EXTERNAL` table claims an exclusive `raw/<dir>` directory**
  (`lakana_core.catalog.external_location`), not an arbitrary glob. Any new code that lists
  or resolves an external table's files must go through `parse_external_location`/
  `check_claim_free`, never `fnmatch` against a raw pattern directly.
- **A task token may only ever perform the actions in `TASK_TOKEN_ACTIONS`**
  (`lakana_core.grants.authorization`). If you add a new `Action`, decide explicitly whether
  a task token should ever be allowed to invoke it — the default for anything not added to
  that set is refusal, and that default is deliberate.
- **A CI token may only ever `DEPLOY` on its own environment** (`GrantAuthorization._ci_scope_refusal`),
  and each `DEPLOY` check decides what that allows (`lakana_core.ci_oidc.scope`). A new `DEPLOY`
  check must decide it too, and go into `test_ci_token_enforcement.py`'s `_DEPLOY_CHECKS`; see "CI
  OIDC exchange and CI tokens (#15)".
- **Environment creation and storage changes are instance-scoped, not environment-scoped.**
  `create_environment` and `update_environment_storage` authorize
  `Action.MANAGE_ENVIRONMENT` at instance scope; do not change either to authorize against
  the target environment's own name, which would let a grant for a not-yet-existing name
  create or re-root it.
- **Every identity or policy mutation records an audit event, in the same transaction as
  the change.** Adding a new mutation to `GrantService`, `SecretService`,
  `CatalogService`, `PatService`, `TaskTokenService`, `GroupService`,
  `PrincipalService`, or `CiTrustRuleService` without an accompanying `AuditService.record` call
  is a gap, not a style choice — see "Audit log" below for the event types and detail shapes already in
  use.

## Syntax errors name only the reader's own SQL

`lakana_core.sql_syntax.describe_syntax_error` quotes up to 40 characters where the parser
stopped, so it is only for SQL the reader wrote (the worker parses it first, in `check_blacklist`).
Anything parsed from **stored or generated** text must never reach a reader through it. The
parse sites under `packages/*/src` are:

- reader's own query (`check_blacklist`, `extract_table_refs`, `extract_lineage` in
  `pipeline.run_one`): the syntax message, with line and column, to the reader;
- a stored view's SQL in `query_rewrite._expand_into`: `View 'gold.v' could not be read. Ask its
  owner to recreate it.` — the view the reader named, none of its text, since the reader may
  lack READ on what it references; the two re-parses of the expanded statement
  (`pipeline._resolve_and_rewrite`) say the same when the statement contains a view;
- a stored view's SQL in the view diagnosis (`check_blacklist` in `pipeline._diagnose_views`,
  so that no view's text reaches DuckDB unchecked, also when the guard has become stricter since
  the view was created): `VIEW_REFUSED_BY_GUARD`, which names the view and asks its owner to
  recreate it, none of its text, for a refusal and a syntax error alike;
- stored row filters and column masks (`PolicyResolver`, `_substitute_context_functions`): a
  `ValidationError` that names no text; on the ordinary read path it is inside the opaque
  resolution wrapper, so the reader sees `Resolving the tables this query reads failed
  (ValidationError).` and nothing more;
- generated reader statements (`rewrite_query`, which hold the policies): "A stored row filter or
  column mask on a table this query reads could not be read. Ask an administrator to correct or
  remove it." — only after the reader's tables were authorized, and it names no table;
- deploy-time parsing (`projects/parse.py`, `deploys.py`) and write-time policy validation
  (`policy/expressions.py`): text the deployer or administrator wrote themselves.

A `RecursionError` from the parser is the query's nesting depth: cause `invalid_query`, "nested too
deeply", permanent for a SQL task. Every string from sqlglot is cut to 2,000 characters before it
is processed and the module uses no regular expression, because a running pattern is not
interrupted by the query timeout; the message is at most 300 printable characters.

## Policy expressions are type-checked when they are created

A row filter or column mask runs on every stored value, so one that needs an implicit conversion
fails every query it covers, on the first value that cannot be converted (GHSA-32q2-rpmr-586j).
`lakana_core.policy.expression_types.check_expression_types` refuses that at creation, in the same
place that checks the columns exist (`_check_columns_exist` in `row_filters.py`,
`_check_column_exists` in `column_masks.py`), against the schema whose id the policy records, so the
types checked and the `schema_id` stored are one read. Rules: a comparison is accepted only when
DuckDB's implicit cast cannot fail. Both sides are of one kind (text; date/timestamp; otherwise
the same type; a `timestamptz` only with a `timestamptz` column, because the read does not pin the
time zone). For numbers, a literal must be exactly representable in the column's type (integer
range, `decimal(p,s)` places and digits with no exponent, finite and in range for floats), and two
columns must be integers, `float`/`double`, or decimals of the same precision and scale. A
comparison with `NULL` is refused. `AND`/`OR`/`NOT` and a filter as a whole need booleans; a minus
sign goes only before a number literal; `substr` takes whole-number literals in the 32-bit range;
`||`/`md5` need text. A mask must keep its column's type: `query_rewrite._masked_projection` wraps
it in `TRY_CAST(<mask> AS <type>)` when the mask row has `type_checked` (set on every mask created
since migration 0036; false for older ones, which a read leaves as written). The target is a
structured `sqlglot` `DataType` from `lakana_core.policy.cast_types`: for an Iceberg table from
the column's `pyiceberg` type, for a raw-zone table from the stored Arrow type name through a
closed list of the exact names pyarrow prints (decimals from two integers). It is never parsed
from a type string, because a raw-zone struct's field name is free text. A column of any other
type (struct, list, map, nanosecond timestamp, unknown) has no cast, and creation allows only
`NULL` or the column itself as its mask. Creation accepts only values that cast cannot fail on. The check and the cast apply to Iceberg and Parquet tables only (`policy.columns.type_checks_apply`, from `TableDescription.file_format`): a CSV or JSON table has no stored schema, DuckDB infers its own types at read time, so its filters are checked against the subset only and its masks are stored with `type_checked` false. A type the table does not declare in a form the check knows is refused (fail
closed, never guessed). The only conversion a policy may contain is a typed `DATE '...'` or
`TIMESTAMP '...'` literal, which `validate_restricted_expression` accepts only for a string literal
that is a real date or moment in the exact `YYYY-MM-DD[ HH:MM:SS[.ffffff]]` form; it is a constant, so it
cannot fail on a stored value. Any other `CAST`, `TRY_CAST` or `::` stays outside the subset.
A comparison of a date with a timestamp is accepted, and the read compiles its date side to
`TRY_CAST(<date> AS <timestamp type>)` (`query_rewrite._compiled_row_filter`, from the column
types in `TableSource.mask_types`): a date outside the timestamp range becomes `NULL`, the
comparison is unknown and the row hidden, never an error (#178). This is fail-closed only because
the filter subset has no `IS NULL`, `IS DISTINCT FROM` or `COALESCE`: adding one of those to the
subset must revisit it. A new
node type added to the subset needs a rule in `_Checker.type_of`; one without a rule is refused
(`its type could not be determined`). Existing policies are not re-checked; the read path is
unchanged and still replaces the engine's text with a fixed message.

## Names in log lines and in CLI output

A table, namespace, artifact, environment, asset or file name can hold a line break or an escape
sequence, so it can forge a log record or recolour a terminal.

- **Log calls** (`lakana-core`, `lakana-server`, `lakana-worker`): pass such a value through
  `lakana_core.identifiers.shown` (ASCII-escaped, 200 characters). That includes a path or an
  object key built from a name (a code tarball's path holds the repository's name). Ids, counts,
  enum values and `type(exc).__name__` need nothing.
  `packages/lakana-core/tests/test_log_untrusted_names.py` walks the syntax trees of
  `packages/*/src` and fails on a call of `logger`, `log` (and the `_`-prefixed and upper-case
  spellings) at any level whose message or any other argument or keyword value (an f-string, `%`
  formatting, `.format(...)`, `+`, `extra=`) contains `.dotted()`, an attribute `.name` or a
  variable `asset` outside a call to `shown`. It does **not** catch a name assigned to a variable
  on an earlier line (`name = row.name` then `logger.warning("%s", name)`), a logger with another
  name (`self.logger`, `logging.warning`), `logger.log(level, ...)`, and it cannot see inside an
  exception: `logger.exception` prints the traceback with the exception's text (the formatter below
  escapes control characters there, on output), and core still
  raises some exceptions with a raw `ref.dotted()` in their message. Read each new log call; extend
  the guard's lists when a new kind of name appears.
- **Every record, in one place.** `lakana_core.log_setup.SafeFormatter` (installed by
  `configure_logging`) and `lakana_server.log_config.uvicorn_log_config()` (uvicorn's loggers do
  not reach the root handler) escape control characters in the **final** text of a record:
  message, exception text, traceback, stack. Same notion and form as the CLI's `safe_text`
  (C0 except the line feed, DEL, C1, the bidirectional controls, the Unicode separators; `\x1b`,
  `\u202e`; a tab is four spaces, a lone surrogate `\ud800`; the set is `log_setup.UNSAFE`); a backslash is **not** doubled, so `shown()` and `%a` output stays as it is. The
  line feed is kept for tracebacks, but every line after the first starts with
  `    | ` (`CONTINUATION_PREFIX`), so no text can start a line that looks like a record (a time,
  a level, a logger name). The formatter never raises (a bad `args`, a failing `str()` or `repr()`
  give a line with the formatter's own header, the call site and the traceback, ending in a constant
  string at worst). A record is cut at 1 MiB of text (`MAX_RECORD_CHARS`) with a count. It exists only where Lakana installed it: an embedding process with its own
  root handler keeps its own formatting, and **`shown()` at the call sites stays necessary**.
  Query child processes (`lakana_worker.query_child`) configure no logging; the worker logs the
  end of their stderr with `%a`. Uvicorn's colour is off (an escape sequence).
  **What it promises:** line integrity and no raw control characters. **What it does not:** field
  integrity inside a line; uvicorn passes a request's query string raw into its quoted request
  line, so a client can still write text that looks like a field there. With
  `LAKANA_DATABASE__ECHO=true` SQLAlchemy adds a handler of its own; `lakana_core.db.create_engine`
  gives it the same formatter.
- **CLI** (`lakana.cli._output`): `inline` makes one name safe for a line; `done` and `emit` of a
  plain string pass the whole text through `safe_text`, which escapes every control character and
  keeps the lines. Escaping is idempotent, so `done(state, f"Deleted {inline(name)}")` is escaped
  once. The data fields of JSON output are never escaped by us (JSON encodes them itself); the
  human-readable `message` of `done` without a result object is the same sentence as in table mode,
  so its names are escaped.

## Request bodies: a JSON limit, authentication first, a limit per streamed route

Every body is one of two kinds.

| Route class | Routes | Limit |
|---|---|---|
| JSON body (a pydantic model, or an Iceberg REST request) | every route with a body model | `Settings.request_body.max_bytes` (`LAKANA_REQUEST_BODY__MAX_BYTES`, 1 MiB), `BodyLimitMiddleware`: 413 before FastAPI reads a byte, before authentication; one rule: a numeric `Content-Length` over the limit is refused at once; otherwise the bytes are counted as they arrive and reading stops at the limit, whatever the headers say (`Content-Length` beside `Transfer-Encoding: chunked`, a non-numeric length, no header), so at most the limit is held; a client that disconnects part-way is passed on, not waited for |
| Streamed, read by the route itself | table append and replace (`POST`/`PUT .../tables/{name}/data`), artifact manifest (`POST .../artifacts/{name}/uploads`), artifact blob (`PUT .../uploads/{id}/blobs/{sha256}`) | each its own: `Settings.append.max_bytes`, `MAX_MANIFEST_REQUEST_BYTES` (16 MiB), the manifest's file sizes. Marked with `@streams_its_own_body`; `tests/test_body_limit.py` lists them, so a new streamed route fails that test until it is listed |
| No body | everything else | none needed |

A new route whose body is JSON gets the limit and the next rule with no work. A route that streams its
own body must carry `@streams_its_own_body` and its own bound.

**Authenticate before the body is decoded.** FastAPI reads and decodes a JSON body before any dependency
runs. `AuthenticateFirstRoute` (`lakana_server/api/routing.py`, the `route_class` of every router,
including `IcebergRoute`) runs `get_principal` first for a route that has a body model and depends on it,
and stores the principal on `request.state` under the private name `PRINCIPAL_STATE_KEY`;
`get_principal` returns it, if it is a `PrincipalRef`, instead of verifying and auditing the
credential a second time. `test_body_limit.py` reads the running app (`create_app`), not the `ROUTERS`
tuple: every route with a body is an `AuthenticateFirstRoute`, and every route but `/healthz`, `/readyz` and
`/v1/config` depends on `get_principal`, however it got into the app. A request with no credential, or a wrong one, is 401 and is
audited and rate-limited as before. The limit above bounds what a caller *with* a credential can make
the server decode.

**A rejected secret body echoes nothing the caller sent.** The validation handler
(`errors.py`) drops each problem's `input`. A mapping key still reaches the error's `loc`, so the
secrets bodies validate `values` with `SecretValues` (`api/platform/schemas.py`), whose single message names
no key and no value. The storage `options` of an environment (`StorageConfigIn.options` on create,
`EnvironmentStorageUpdate.options` on `PATCH .../storage`) can carry inline S3 credentials too, so
both use `StorageOptions` / `OptionalStorageOptions`, which refuse a bad shape with one message that
names no key and no value. No other request body carries a credential: tokens and secrets are in
headers or are only ever returned. The application logs no request body.

## Local files `lakana load` reads: a capped child process (#353)

A small Parquet or Arrow file can decode to far more than its size (one value repeated by a
dictionary or by shared prefixes, views over one buffer, pages a footer hides), and its metadata
can lie, so no estimate made before reading bounds what one batch takes. A hand-written parser of
Parquet page headers was tried and dropped: each review found a new way past it.

- `lakana.cli._capped.read` runs `load.read_file` for Parquet and Arrow in a child process
  (`sys.executable -I -m lakana.cli._capped`, no shell) with `RLIMIT_DATA` set to the child's
  `VmData` after its imports, plus twice the upload limit, plus `HEADROOM` (256 MiB; 768 MiB at the
  default limit). The rows come back as an Arrow stream on its stdout. The CLI's own process is
  never capped. Unlike the worker (below), the child reads one file and exits, so memory that does
  not shrink is no reason against `setrlimit` here.
- **Why `RLIMIT_DATA`, not `RLIMIT_AS`.** `RLIMIT_AS` counts reserved address space: glibc's
  64 MiB malloc arena per pyarrow thread and the memory-mapped Arrow file. A 10 MiB file of
  48 columns was refused on a 20-core machine, and an mmap failure surfaced as "could not read".
  `RLIMIT_DATA` (Linux 4.7 and later) counts private writable memory only. Measured, it passes
  with or without `MALLOC_ARENA_MAX` and at 128 threads; the bound is also tighter (the least that
  let a 248 MiB table pass was 352 MiB beyond the child's base, against 576 MiB under
  `RLIMIT_AS`).
- The child's memory is also made independent of the machine: the system allocator
  (`ARROW_DEFAULT_MEMORY_POOL=system`; mimalloc failed at random under a cap), `MALLOC_ARENA_MAX=2`,
  `OMP_NUM_THREADS` and both pyarrow pools set to `THREADS` (2) before the cap.
- A `MemoryError` in the child (exit 4), a child ended by a signal, and glibc's abort when a
  thread cannot get its thread-local memory (exit 127 with "cannot allocate memory" on stderr,
  told by its words because no type crosses a process) are the size refusal ("... larger than
  one upload may be (N) once read", exit 2). A `CliError` crosses as JSON on stderr (exit 3).
  Anything else, a loader failure with exit 127 included, is an unreadable file naming the child's
  last stderr line.
- Where `/proc/self/status` cannot be read, the child sets no cap (the residual below) instead of
  failing.
- `read_file` lets `MemoryError` through (`pyarrow.ArrowMemoryError` is also an `ArrowException`),
  so it is never reported as "could not read".
- Still in the parent, cheap and parser-free: the metadata estimate and batch size, a dictionary
  or view column measured from its values' lengths before it is decoded (the size checked is the
  size added), and nested columns refused before any row is read (from the Parquet leaves'
  repetition, which catches extension types such as tensors).
- **Residual:** only Linux (4.7 and later) enforces `RLIMIT_DATA`. On macOS and Windows the file is read in the
  CLI's process with only the in-process bounds, and a crafted file can still take far more memory
  than the limit. It only threatens the user's own CLI process; the server enforces its own limit.
  CSV, JSON and spreadsheets are read in the CLI's process: their file size is checked against the
  limit first, and text does not expand that way.

## Query limits: a hard time limit, a memory target, the worker's memory stop

`QueryLimitsConfig` (`LAKANA_QUERY_LIMITS__*`) bounds a query. Each query runs in a process of
its own (`lakana_worker.query_child`, started by `lakana_worker.query_process`; M5c3a).
`timeout_seconds` is **hard**: at the limit the worker sends `SIGTERM` to the child's process
group, the child interrupts DuckDB, and `kill_grace_seconds` (10) later the worker kills the
group. The same sequence, "ask, wait, kill", serves a cancel of the task run the query executes
for, a cancel request on the query run and the worker's drain (for an ad-hoc query; a task's
query is released at drain by its lease keeper). `memory_limit` and `threads` are
DuckDB's own settings inside the child, and `memory_limit` can still be overshot. The result caps
are checked by the worker on the batches as they arrive. The time limit, a cancel and a shutdown
cover the query, including the time the worker spends between two batches (they are noticed on
every read, and the read that ends the stream evaluates them one last time); once the stream has
ended normally, storing the result or committing the write is not interrupted.

The worker has one **hard** limit by default: `LAKANA_WORKER__MEMORY_LIMIT` (4GB, or 90
percent of the container's). `setrlimit` was tried and rejected: `RLIMIT_DATA` and `RLIMIT_AS`
count mapped memory, which does not shrink after a query, so ordinary work failed and a real hit
crashed the worker at random. Instead a watchdog thread (`lakana_worker.memory`) measures resident
memory five times a second: the worker plus its live descendants (query children, Python tasks,
`uv`, and the members of the sessions they led) on Linux; the
worker process only on macOS; nothing on Windows. Over the limit (Linux only) it first kills the largest consumer, a query child or a running Python
task (by the resident memory of its process tree), through the object that owns the process
(`query_process.REGISTRY`, `runners.TASKS`), which signals the process group it started and knows
whether it has reaped it (the kill and the reap take one lock), so a reused process id is never
signalled. No ask and no grace: memory is the one reason that cannot wait. A query so ended fails
with `out_of_memory`. A task so ended gets `StopReason.MEMORY` and its own thread records a
transient failure (`task_ended_message`) through the normal outcome path; the watchdog writes no
task row, except at the stop below. A task in its venv build is not registered and so not endable
(#48). The worker and what runs beside go on. The kernel detaches a killed process's address space at once (its size reads 0 within a
fraction of a millisecond, before the child is waitable), so the wait of at most `RELEASE_SECONDS`
(2 s, polling every 10 ms, no lock held) only matters when the kill does not take; then the
watchdog measures again. The watchdog notes the query runs in flight, and the claim of an ended task, before it kills, and
the stop records against those too: the killed thread can leave `ACTIVITY` before the stop (and a
stopping worker's task thread records nothing), so the run would otherwise stay `RUNNING`. The
updates are conditional (`RUNNING` rows; `record_attempt` on the claim), so what the thread already
recorded is left alone and nothing is recorded twice; an ended task is always recorded transient
there, never as the certain blame. A failing
relief falls through to the stop. Only if the total is still over
the limit, or nothing endable was running, it records `out_of_memory`
against the query runs and task runs in flight (after killing the descendants, so growth stops
first), and exits with code 75;
Docker or the local-mode supervisor starts the worker again. A burst between two checks is still
not seen. The Compose container's `mem_limit`
(no swap) is the backstop. This stops a worker that uses too much, the watchdog sees a spike only
at its next tick, and the child's process boundary is not a security boundary (see "The query
child" below). The Docker runner for Python tasks (M7b, #38; Kubernetes #34) is separate.
Never present any of these as a security boundary.

- Spilling is decided by the request, never by DuckDB's text. Only local `file://` mode sends
  `spill = true`; every other mode sets `max_temp_directory_size = '0KB'`, so a query over
  `memory_limit` fails with DuckDB's `Out of Memory Error: failed to offload data block …`
  (the same on 1.5.5 and 2.0.0), which the child words as "the query ran out of memory and
  cannot spill to disk" plus the advice. DuckDB allows its own `temp_directory` in allow-list
  mode, so the limit, not a refused file system, is what keeps the spill off. Nothing is
  planned a second time: the `EXPLAIN` re-plan that once told a refused spill from a refused
  local read is gone, and with it two residuals (user SQL planned twice, and a hung HTTP
  request in the re-plan delaying a stop). In the Compose bundle the worker's `TMPDIR` is the
  container's writable layer, which is why spill stays off there; enabling it is its own issue.
  The child has no log of its own, so DuckDB's text is not logged.
- The write deadline covers the whole streaming phase, including Parquet encoding and storage
  uploads inside `write_batches`, so a slow store can trip it while DuckDB is idle; the write
  message says so. The clock runs in the worker and is looked at on every read of the child's
  stream, so a consumer that is slow between batches cannot hide a timeout past its next read,
  the one that ends the stream included. Once the stream has ended normally, writing the last
  file and committing the snapshot are not interrupted.
- Each query gets a fresh `lakana-spill-*` private directory (the child's `HOME` and `TMPDIR`
  and its spill directory `tmp` are inside it), removed when the query ends.


**The statement length is bounded before anything parses it.** `QueryLimitsConfig.max_statement_chars`
(`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`, 100,000) is checked at every entry of SQL text: `POST .../queries`
(a query and a write), the row-filter and column-mask `expression` of their create routes (all three through
`deps.check_statement`), and a SQL asset at deploy (`DeployLimits.statement_chars`, set from the same setting
in the worker's `main`). The parser's cost sits outside the query timeout; 30 MB of nested subqueries cost
19 s and 2.9 GB. A view is created from a stored write run or a deployed asset, so it has been checked at one
of those. A new route that takes SQL text calls `check_statement` before the service.

## Run principals (M5c1)

A run in a protected environment writes as `environment_deployments.run_as_principal_id`, never as
its trigger; `RUN` lets a principal start deployed code, not choose what it does or who it writes
as. Binding needs `MANAGE_ENVIRONMENT` and an active service account (`DeployService.set_run_as`),
and every bind, clear (of an existing binding; clearing an unbound deployment is a no-op) and
undeploy is audited in the same transaction. An unknown principal name and a non-service-account
answer with the same 404, so names cannot be enumerated. Undeploy deletes the pointer
and then checks for an active run in the same transaction; a run start locks the pointer with a
conditional no-op update (`DeployService.lock_deployment`), so the two serialize on Postgres and on
SQLite alike.

## Descriptions of tables and columns

| Action | Needs | Notes |
|---|---|---|
| Set or reset a table's or a column's description (`PATCH .../tables/{name}`, `PUT .../tables/{name}/columns/{column}/description`) | the table's owner (`Table.owner_id`), or `DROP_TABLE` on the table | There is no `MANAGE_TABLE` action; the manager's right is the existing `DROP_TABLE` (data engineer and admin roles). `DROP_TABLE` is in `TASK_TOKEN_ACTIONS`, so an unbound task token that holds it may describe, as it may drop; a bound one cannot. Both routes also need `READ_TABLE`, checked before anything is written (the answer is the table as the caller reads it; a column is also checked against the schema the caller may read). A refusal is `AUTHORIZATION_DENIED` as for any action. |
| Read | `READ_TABLE`, as the table itself | |

Audited in the same transaction (`TABLE_DESCRIBED`, `COLUMN_DESCRIBED`; `detail`: `table`, `column`, `old_length`, `new_length`, `reset`); the text is never in `detail`. A deploy writes only `description_project` and the project rows of `column_descriptions`; it never touches an edit (`catalog/descriptions.py`). Pins: `test_descriptions.py`.

## A table's model and its references (#250)

| Read | Needs | Notes |
|---|---|---|
| `TableOut.model` (`GET .../tables/{name}`, the listing, `lakana table get`) | `READ_TABLE` on the table | `CatalogService.describe_table`, then `model_metadata.model_view`. A reference is shown only when `permits(READ_TABLE)` holds on its target's table scope, asked **before** anything is looked up; otherwise it is counted in `hidden_references`. So a missing and an unreadable target look the same to a reader without the right, and `target_exists` (`true`/`false`) is only ever computed for a target the reader may read. The `ref_<column>` check of a hidden reference is left out with it (its outcome and error say something about the target). A shown `ref_` result also passes `ReferenceCheckFilter.hides`, which asks about the target **the result's run** declared, as table health does: after a header points a reference at a new target, a result computed against the old one stays hidden from a reader of the new target who may not read the old one, until a run replaces it. A stored target that is not a table name is counted as hidden. |
| `?model_kind=` on the listing | as the listing | The filter runs on the described tables the caller may read; `require_model_kind` refuses anything outside `MODEL_KINDS` with one text. |
| `GET .../tables/{name}/references` | `READ_TABLE` on the table, checked first (403 before the lookup) | `CatalogService.table_references`, `model_metadata.references_view`. Outgoing as above (`hidden_outgoing`). Incoming rows are found by the table's current dotted name (`ix_table_references_environment_target`) and kept only when the caller may read the **source** table; the rest are counted in `hidden_incoming`, never named. |

**Generated reference checks everywhere else.** `runs/hidden_checks.ReferenceCheckFilter`
withholds a generated `ref_<column>` result (its outcome, `failing_rows` and `error`) from a reader
who may not read the reference's target, in `CatalogService.table_health` (`hidden_checks`),
`RunService.get` (each task's `hidden_checks`), `RunService.checks` and `RunService.list_checks`
(the page's `hidden`), and `submit_check_rows` answers such a check with `CheckNotInRunError`,
the text of a check the run does not have. The target is the one the run's project version
declared for the check (`CheckSpec.generated.target`); the right is asked by name before anything
is looked up, as for a reference. A result whose target cannot be told (no readable entry, or a
target that is not a table name) is shown only to a reader with READ_TABLE on the whole
environment. `key_*` and hand-written checks are not filtered. A task's `error_message`, in
`RunService.get` and in each attempt of `RunService.task_log`, goes through
`runs/check_message.message_for_reader`. The check phase stores, beside the message, the state it
was built from besides the results (`check_message_state`: the rollback, the kept note's facts, the
refusals, the branch); `check_message` is the one builder the worker and the reader's view share.
For a reader with a withheld result, the message is built again from the visible results only,
after the state was shown to give back the stored text from every result. The scheduler clears
the state whenever it writes another text (`store._apply_task`), and `record_attempt` stores the
pair together. When the state does not give back the text (an earlier attempt whose results were
counted again), or the visible results need another kind of message, the reader gets
`WITHHELD_TASK_MESSAGE`; a text without a state is shown only when it names no withheld check.
`test_check_message.py` proves, over every outcome, mode and state, that the reader's text is the
worker's own text from the visible results. The other texts that name a check (#385):

- **A table's notes.** `not_published_message` leaves out every result `hidden_checks.result_hider`
  hides (as if it did not exist; with none left it names no check), in `table get`'s
  `state_message` and in `NotPublishedError`. The kept note's check text is rebuilt by
  `kept.shown_kept_check` from the visible `keep` failures of `Table.kept_task_run_id`, after
  the failures (all, or those not refused a kept table, as `note_kept` chose) were shown to give
  back the stored `kept_check`; otherwise, or with none visible, the note names no check. Without
  the results (the run deleted), a text that names a `ref_` check names none. `result_hider`
  asks a person as `ReferenceCheckFilter` does; for a **task** (a run context or a task token) it
  hides every generated `ref_` check, because what a task is told (the K4 refusal of a kept
  table, a not-published refusal) lands in its own message and log, which every reader of its run
  reads.
- **The task's free-text log.** It is read by byte offsets, so it is one text for every reader:
  the worker writes a generated `ref_` check's line as `check ref_<column>: outcome withheld from
  the log; see lakana runs show RUN --env ENV` (`reference_check_log_line`, the same words for
  every outcome; `task_checks._check_lines`), and the attempt's `error:` line
  is `check_message.message_for_log`: the message rebuilt without any generated `ref_` result, or
  `log_withheld_message` when it cannot be. Hand-written and key checks are unchanged. A log
  written before this release keeps its lines.
- **Notifications** go to an endpoint, not to a principal whose rights can be asked:
  `payload.facts_for_run` leaves every generated `ref_` check out of `failed_checks` and sets
  `reference_check_failed`, which the messages say as `A reference check failed; see lakana runs
  show …` (`json`: `"reference_check_failed": true`, only then). They never carried row counts.
- **The `CHECK_FAILED` audit event** (and `TABLE_KEPT`, `TABLE_ROLLED_BACK`, which name checks)
  is unchanged: the log is read only with `READ_AUDIT_LOG` on the instance (`AuditService.query`),
  which only the instance `ADMIN` role carries, or a grant of that one action on the instance to a
  reviewer. Either reads every table's events instance-wide by design (the log is not filtered
  per table at all), so a check's name and row count there reveal nothing beyond what that reader
  already sees. Revisit if the audit log is ever opened per environment or per table.

Pins: `test_hidden_reference_checks.py` in lakana-core and lakana-server,
`test_check_message.py`, `test_notifications_payload.py`, and the worker's
`test_the_log_gives_a_reference_check_no_count_and_its_error_no_name`.

**A task's wait reason (#385).** `RunService._waiting` names an unfinished upstream task only
when the reader may read its table (`may_read_every`, by name, before anything is looked up, so a
missing table and an unreadable one look the same) and counts the rest in
`WaitReason.hidden_upstream` (`Waiting for upstream tasks: a and 1 table you may not read.`), as
the project-version view drops the edges from a table the reader may not read. `upstream_count` stays the
total. The run's task list itself still lists every task by its table (it always did; the edge is
what was hidden). Pins: `test_a_wait_reason_names_only_the_upstream_tables_the_reader_may_read`,
`test_runs_waiting.py`.

**A failed deploy's error (#385).** `error_message` reaches every reader of the environment, but
some issues tell of an object only the deployer may read. Each such `ProjectIssue` carries
`targets` and `withheld`, the line a missing object gets (empty when a missing one makes none):

- `projects/model.resolve_references`: every issue raised after the lookup described the target
  (its kind, its key, its columns, that it is an artifact, an ambiguous name, a name too long to
  store), and the run-principal refusal, which `resolve_model_references` passes in as
  `refusal` so that it sits at its reference's place in path/column order. `withheld` is
  `missing_target_message(raw)` for the first problem of a reference and empty for the others: a
  missing target makes one line.
- `DeployService._kind_issues`: a SQL asset reading an artifact, and a Python parameter that
  reads one (`table 'x' not found`, or empty for the case-only group), for an artifact that is
  not the project's own (its project version shows every reader its own); a clash of the asset's
  name with an existing table, view, external table or artifact (empty: a missing object makes
  no clash). `validate_checks_against_environment`: a check reading a catalog artifact
  (`table 'x' not found`, in the very place a missing table's line takes).

The worker stores `issues.withheld_lines` beside the text (`deploy_runs.error_withheld_json`:
each line's **position**, the line, its replacement, its targets), and `DeployService._view`
passes the text through `issues.error_for_reader` for a reader who is not `run.principal_id`:
a line whose targets the reader may not all read is replaced **by position**, never by text (two
references to one target can make the same line), or left out when its replacement is empty;
with nothing left, the reader gets `WITHHELD_DEPLOY_MESSAGE`. The deployer and readers of the
targets see it whole. Pins: `test_two_references_to_one_hidden_target_read_like_two_missing_targets`,
`test_a_run_principal_refusal_reads_like_a_missing_target_in_its_place`, the artifact and clash
tests in `test_projects_deploys.py`. Residuals:

- The replacement of a SQL asset's or a Python parameter's artifact line stays among the asset's
  kind issues, after the not-found lines, where a missing input's `table 'x' not found` would sit
  among those (one per name, at its first reader); a reader who compares the order or the count
  of lines can tell the name exists. A replaced clash line leaves the deploy failed for a reason
  the reader is not shown.
- `generated_name_issues` ("A check named `ref_x` is generated for …") is raised only for a
  reference that resolved, and carries no targets; it needs the author to name a hand-written
  check like a generated one, and a successful deploy would reveal the same.
- An error stored before this release has no lines and is shown whole. In practice nothing is
  exposed by it: the reference issues come from #384/#386/#387, which no release contains
  (the last tag is v0.21.0); the artifact and clash lines are older and were shown whole before.
- When a run lists more than 50 issues, the `…and N more issues` tail still counts the lines a
  reader sees left out.

The rows are written only by a deploy, a rollback and a run's table creation
(`catalog/model_metadata.py`); REST, the SDK and the CLI only read them. References are held by
name, as the header names its target: a table renamed through the Iceberg REST catalog is no longer
the target of references to its old name, and a later table of that name is, as the reference's
generated check reads it. A generated reference check's `error` never names the target either:
the pre-check texts (`runs/model_precheck.py`) name only the asset's path and column, the count's
refused and missing cases are `ERROR_UNREADABLE`, other failures are the engine's error type or the
cause in words, and the one fixed text that names a table, the row filter or column mask failure,
is replaced by its variant that names none unless it names the checked table (`error_text(...,
checked_table=)`, which the worker passes for a reference check; `test_policy_read_errors.py`).
The renamed table keeps its model rows (keyed by its id) under its new
name until it is dropped. Pins: `test_describe_table.py`
(`test_a_missing_target_and_an_unreadable_target_look_the_same_to_a_reader`,
`test_incoming_references_count_hidden_sources`, the two rename tests), `test_tables.py` in
lakana-server, `test_undrop_restores_the_model_and_its_references`.

## Table health: who sees the readers

| Action | Needs | Notes |
|---|---|---|
| `GET .../tables/{name}/health` | `READ_TABLE` on the table | Checked first, so a missing right is `403` before anything is read. |
| The `readers` of that answer | `MANAGE_GRANT` and the `ADMIN` role at instance scope (`grants.admin.may_see_grants`) | The grants listing's rule asked as a yes or no: nothing is recorded as `AUTHORIZATION_DENIED` for a `False`, and the field is `null`, not an error. The helper lives in `grants/admin.py` because `test_admin_only_call_sites.py` allows the three actions nowhere else. |

Seeing `readers` that name anyone other than the caller writes `ACTIVITY_VIEWED` (`view: table_readers`, the table as scope, `filters: {days}`), once per caller and table per `LAKANA_VIEW_AUDIT__WINDOW_MINUTES`, before the answer is built; if that row cannot be written the request is a 503 and returns nothing (see the query-list bullet on the view audit). `readers` that are `null`, empty or only the caller write nothing.

`table_reads` holds who read what, by day; it is written only by the worker, in the transaction that completes a succeeded query (`catalog/health.py`, `record_table_reads`), and deleted by the housekeeping sweep after `LAKANA_TABLE_READS__KEEP_DAYS`. A Python task reads its inputs through the query API (`lakana.runner._read_input`), so its reads count under the run's principal. Pins: `test_pipeline_table_reads.py`, `test_table_health.py`, `test_tables_health.py`.

## Search: what a result may reveal

`GET .../environments/{env}/search` (`catalog/search.py`, `CatalogSearch`) ranks candidates in one SQL
union and applies the listings' rights to each row afterwards, in chunks, so nothing it returns, and
no text inside it, is read out for a caller who may not read it.

| Result | Needs | Notes |
|---|---|---|
| table, column | `READ_TABLE` on the table | `authz.permits` per table (cached per search), as `CatalogService.can_read_table` asks it. A column's name, type and description and a table's description, declared grain (`model_grain`) and kind (`model_kind`) are in the row only after this check; a snippet is cut from the shown description or grain of a permitted row. A grain or kind of a table the caller may not read matches nothing for them. Search does not match reference targets; if it ever does, a hit may name only a target the caller may read. |
| run | `READ_ENVIRONMENT` | Decided once, before the runs are looked for. |
| principal | `MANAGE_PRINCIPAL` at instance scope | The principal listing's rule as a yes or no; no results, not an error. |

An environment that does not exist is `404` only for a caller who passes `READ_ENVIRONMENT`; others get an
empty page, the same as for one that exists. `MAX_SCANNED` (5,000) bounds the candidates examined per
search so a caller with few rights cannot make one search read an environment. `table_columns` holds
names and types only, in the same transaction as the schema change; a column mask hides values, not
names, as `table get` does. Pins: `test_catalog_search.py`, `test_search.py`.

## Jobs: who may start, pause and resume (M5d-b)

A job is a named selection in the manifest; starting one is starting a run, so it has the run's
rules, not rules of its own.

| Action | Needs | Notes |
|---|---|---|
| Start by hand or through the API (`POST .../jobs/{job}/runs`) | `RUN` on the environment, checked first | The same call as `lakana run` (`RunService.prepare_start`): the run principal is the bound service account, or in a protected environment without one the refusal of `lakana run`; in an unprotected one the caller. A paused job can be started. `trigger` is `api` when the caller is a service account and `manual` otherwise, set by core from the principal, never taken from the request. A task token never reaches the route (`RUN` is not in `TASK_TOKEN_ACTIONS`). |
| Test a job's webhook (`.../jobs/{job}/notify-test`) | `DEPLOY` on the environment | Queues one delivery of the event `test` (no run). Audited in the same transaction (`NOTIFICATION_TESTED`; `detail`: `repository`, `job`, `environment`, `delivery_id`; never the URL or the secret). Reading it back (`GET .../notifications/{id}`) needs `DEPLOY`, `RUN` or `READ_ENVIRONMENT`: a test has no run, so the run-read rule alone would lock out the deployer who started it, and its `last_result` is one of the fixed reasons. A holder of the deploy right can therefore read any delivery in the environment; each holds only fixed reasons. The run view's `notifications` follow the run-read rule. |
| Pause and resume (`.../pause`, `.../resume`) | `DEPLOY` on the environment | `DEPLOY` is environment-scoped, not per repository: a holder can pause and resume the jobs of every repository there. Audited in the same transaction (`JOB_PAUSED`, `JOB_RESUMED`; `detail`: `repository`, `job`); pausing a paused job or resuming one that is not paused changes nothing and writes no event. A job without a schedule refuses a pause. |
| List and show (`GET .../jobs`, `GET .../jobs/{job}`) | `READ_ENVIRONMENT` | Filtered as the deploy views are: all or nothing for the environment. `show` is `list` filtered to one name; its `triggers` (schedule, on demand) are read from the same view. |
| A start after a deploy (`on_deploy: true`, #102) | `RUN` on the environment, of the principal that deployed (`deploy_runs.principal_id`), checked first | The worker calls `JobService.start(deployer, ..., trigger=DEPLOY)` after `record_deploy` committed (`jobs/on_deploy.py`), through the worker's `AuditingAuthorization`: a refusal is `AUTHORIZATION_DENIED` and becomes the outcome `not_started`/`lacks_run` on the deploy run (`deploy_runs.job_starts_json`); the deploy is never rolled back. Run principal, protection, waiting start and audit are a start by hand's (`RUN_STARTED`/`JOB_START_WAITING` with trigger `deploy`, actor the deployer; a waiting one is checked again as `manual`). Only a deploy whose pointer moved to another project version stores the starts (`record_deploy`, same transaction as the swap); a dry run, a rollback, a failed deploy and a deploy of the current commit store none. A CI deploy is checked **with** the token's scope (`run_deploy` passes `authorize_deploy`'s `CiScope` on). Its rule's opt-in `start_on_deploy_jobs` (default off; `CiTrustRuleService`, `ADMIN` only, audited, raises the revision) is the only grant: `start_jobs_after_deploy` marks the scope `starting_deploy_jobs` (never set from a request) and `GrantAuthorization._ci_deploy_job_start` then allows `RUN` on the scope's own environment, without the account's grants; without the opt-in `_ci_scope_refusal` refuses it and the job is `not_started`/`lacks_run`, with a message naming the setting. `RUN_STARTED` carries `ci_rule_id` and `allowed_by: start_on_deploy_jobs`; the immediate start resolves the token again after the deploy committed, right before each job's start (`_recheck`, `deploy_jobs_scope`: same revision, the rule's repository), never the scope of the claim, else that job and every later one is `not_started`/`lacks_run` and audited `JOB_START_DROPPED` (`requester_lacks_run`, actor none, `ci_rule_id`); a parked start stores `ci_rule_id` and `ci_token_id` and is resolved the same way when it starts, else dropped as `requester_lacks_run`. A rule-allowed start also needs the job to be `on_deploy` in the project version it runs (`RunService.prepare_start` raises `NotOnDeploy`), else it is dropped and audited `JOB_START_DROPPED` with reason `not_on_deploy` (a deploy by hand may have moved the pointer meanwhile). A CI request that merges into a waiting scheduled start keeps `ci_rule_id` and `allowed_by` in its `JOB_START_WAITING` detail (`test_ci_deploy_jobs.py`, `test_deploy_ci_token.py`, `test_ci_deploy_jobs_postgres_integration.py`). A deployer deactivated since the claim, checked before each start, starts nothing more (`refused`, audited `JOB_START_DROPPED` with `requester_inactive`), also with a bound run principal. The jobs are read from the project version made current (`version.manifest_json`). A start still `pending` a lease after `finished_at` is shown as `lost`, never retried. Pins: `test_jobs_on_deploy.py`, the worker's `test_deploy.py`, `test_jobs_integration.py`. |
| A scheduled start | none: no caller | The deploy that declared the schedule and the binding of the run principal are the authorization. It writes only as the bound principal (`_choose_run_as(..., bound_only=True)`), never as a caller, and refuses without one. `RUN_STARTED` is attributed to that principal. |

A start that meets an active run does not run. It becomes the one waiting start of the job
(`job_schedules.pending_start`), parked and audited (`JOB_START_WAITING`: `repository`, `job`,
`trigger`, `waits_for_run_id`, `merged`, `replaced`, attributed to the caller or, for a schedule,
to the bound principal) in one transaction. **Lock order is a deploy's:** the deployment's pointer
first (`lock_deployment`, which raises `DeploymentMoved` if it moved, and the start reads it again),
the manifest read after it, the schedule row `FOR UPDATE` last. A row is never created outside the
deploy sync: a job a deploy removed meanwhile is a 404 and leaves no row and no waiting start
(`test_jobs_integration.py` holds a start before its park and a deploy at its `DELETE`, on
Postgres). Merging: a start by hand that meets a waiting *scheduled* start only raises `merged`,
and the run is the scheduled one (attributed to the bound service account, `trigger=schedule`;
the person's request is in `JOB_START_WAITING`); any other start replaces the waiting one, so the
run is attributed to the latest requester, and `replaced` holds the one it displaced.

**A waiting start is checked again when it starts** (`JobService.start_waiting`, one transaction:
pointer, row, checks, run). For `manual` and `api` the requester must exist, be active and pass
`GrantAuthorization.permits(requester, RUN, environment)`; the run principal is chosen again by
`_choose_run_as` (a protected environment needs the binding; an unprotected one without a binding
runs as the requester). A scheduled start uses only the bound principal. A start that fails a
check, or whose job left the manifest or whose selection matches nothing, is cleared and
audited as `JOB_START_DROPPED` with a `reason` (`requester_inactive`, `requester_lacks_run`,
`no_run_principal`, `run_principal_unusable` (the bound principal is inactive or not a service
account), `job_removed`, `selection_invalid`, `unreadable`, `not_on_deploy` (a CI deploy's start
whose job is no longer `on_deploy` in the current version)). The system drops it, so the event
has no actor; the requester is in `detail.requested_by`. So a request of a principal who was
cut off never runs, never runs as the bound account, and never blocks the project's other
waiting starts. A busy project leaves the start alone. A binding that changes to another service
account runs the start as the new one; a deploy that changes the job's selection runs the new
selection (RUN is per environment, not per selection).

**The scheduler fires in two transactions** (`lakana_core.jobs.scheduler`, called by the leader
after the run engine's tick). `JobService.book_fire` takes the pointer, then the schedule row,
checks again that the job is in the current manifest with a schedule, is not paused and is due on
the database's clock, and records the fire (`job_fires`, unique on schedule and time,
`last_fired_for`, `next_fire_at`) before committing; the lock is released before
`JobService.start` runs in its own transaction, so the scheduler never holds the row while a
deploy waits for it. A time is consumed whatever follows. If its start then fails, the event
`JOB_START_DROPPED` (no actor, `trigger=schedule`, `scheduled_for`, a `reason`:
`run_principal_unusable`, `selection_invalid`, `job_removed` or `error`) says why; with no bound
principal nothing starts, nothing fires in a burst later, and `lakana job list` shows the state
instead of an event per day. `job_fires.run_id` is set when the run starts, also when a parked
fire starts later through `start_waiting`; it is null while the fire waits and when it never
ran. `JOB_START_WAITING` and `JOB_START_DROPPED` are built by one function
(`jobs/audit.py:start_detail`) and always carry `repository`, `job`, `trigger`, `requested_by`,
`scheduled_for`, `missed` and `merged`, which describe the start that waits (or is dropped),
plus `waits_for_run_id`, `request_trigger` and `replaced` (waiting) or `reason` (dropped).
`request_trigger` is the trigger of the request that was parked or merged; the actor of a
waiting event is its requester, so a person's request that merges into a scheduled start stays
on record there. A deploy or an undeploy that deletes a waiting start writes
`JOB_START_DROPPED` too (`job_removed`, `undeployed`; no actor), in the same transaction as the
deploy or the undeploy.

**The leader's step is bounded.** The run engine's tick comes first, so the job steps must not
hold it up. On PostgreSQL every transaction of the job steps runs `SET LOCAL lock_timeout`
(`LOCK_WAIT_SECONDS`, 2 s) when it begins: a stuck holder of the deployment's pointer (a
connection that stalled mid-deploy) makes a booking or a waiting start give up, with one warning
a minute per schedule, and the next tick tries again; a start that fails after its time was
booked is audited as above. After a lock timeout the tick leaves that project's other schedules
and waiting starts to the next one, so one stuck deployment cannot use the time of the others: `fire_due` hands the projects it found
stuck to `start_waiting_jobs(skip=...)`, and a project that timed out within the last minute is
served after the others.
The time of a tick is also checked between two schedules
(`TICK_BUDGET_SECONDS`), so a step takes at most the budget plus the schedule in progress.
A scheduled fire writes only as the bound principal; a waiting start by hand is checked again as
described above and, in an unprotected environment without a binding, runs as its requester.

## Protected-environment write lockdown (M5c1)

In an environment with `is_protected`, `lakana_core.runs.lockdown.ProtectedWriteGuard` refuses
every catalog mutation the caller's grants would otherwise allow (403, audited
`PROTECTED_WRITE_DENIED`; a caller without the grant gets the ordinary denial first, audited
`AUTHORIZATION_DENIED`, and learns nothing about protection), unless:

- **it runs inside a running task run of that environment.** The run context is
  `PrincipalRef.run_context`, which only `lakana_worker.pipeline.run_one` (for a SQL task's write
  run), token authentication (below) and `lakana_worker.task_checks._set_back` (the claim form, for the
  rollback after a failed check: "Checks and the rollback (M5d-a)" below) set. The worker sets it from
  `QueryRun.task_run_id` — a column no REST schema accepts — together with the executing
  `QueryRun.id` (`RunContext.query_run_id`). The guard re-reads the database: the task run is
  `running` and its current `query_run_id` is the claimed one (a superseded or zombie attempt
  holds nothing), its run is in this environment, `running`, not `cancel_requested`, and bound
  (`run_as_principal_id`) to the acting principal. It then allows only `write`/`append`/`commit`/`create_view`
  on the task's own asset, and `create_namespace` of an ancestor of it. The check runs again
  right before the commit (`ProtectedWriteGuard.record_pending`) with the task run and run rows read
  `FOR SHARE`, so a scheduler reclaim or a cancellation that lands between the first check and the
  commit either is seen (the write is refused, audited) or waits until the write has committed. Never add a request field,
  header or token claim that sets `run_context`; a task token is verified here, not trusted.
  M5c2a adds a second form: a task token bound to one Python task attempt carries
  `RunContext(task_run_id, claim)`, set by `TaskTokenService.verify` from the database (never from
  the request). The guard re-reads the task run (`running`, same `claim_count`), its run (this
  environment, `running`, not cancel-requested, bound to the acting principal) and, unlike the
  M5c1 form, holds the write to the task's own asset whether or not the environment is
  protected. A context with both forms or neither covers nothing. A query submitted with a bound token is stamped with
  `QueryRun.task_run_id`, and the worker rebuilds the claim form from the re-resolved token, so
  a write statement in it is judged by the same guard.
  **One exception to the cancel request.** A run asked to cancel refuses every task write, with
  one exception: a claim-form context passes when the task row has `setting_back`, the
  operation is `WriteOperation.COMMIT`, the target is the task's own asset, and the caller
  declared the commit the internal rollback of a set-back (`ProtectedWriteGuard.check(...,
  rollback=True)`, carried on `_RunWrite` into the `record_pending` re-check under lock). Only
  the catalog service says so: `roll_back` at both its checks, and `commit_table` with
  `internal_rollback`, which the REST router never sets. Setting the table back is the cancel's
  own work. A plain `commit_table`, whether from REST or the SDK, is refused with today's text.
  Two further facts keep REST and tokens out of the exception: `_resolve_bound` refuses a
  cancel-requested run, so no bound task token verifies or is minted there, and a set-back
  claim mints no token because the worker sets back in-process. The authority is the
  `task_runs` row the guard reads (`FOR SHARE` in `record_pending`), never the `RunContext`, and
  only the scheduler (`store._apply_task`, with its own evidence) and the worker's own record
  of its end (`record_attempt`, under the claim it holds, on the locked row's evidence: a cancel
  after a write, or a memory stop of a task that may have written) set that flag. Every other
  check stays: the claim counter (a worker of an earlier claim is fenced out), a `running`
  task and run, the run's principal and the task's own asset. Any other operation, or another
  table, is refused with today's text. The claim path (`_take`) and the lease keeper treat the
  flag the same way, so a cancel request neither stops the claim nor signals the keeper.
- **or it carries a break-glass reason** (10–500 characters, one line: a control, format, surrogate
  or private-use character (`Cc`/`Cf`/`Cs`/`Co` — newline, escape, bidi or zero-width) and a line
  or paragraph separator (U+2028/U+2029) are refused, since the reason is printed from the audit
  log; unassigned `Cn` is not, as it depends on the build's Unicode tables) and the principal holds
  `MANAGE_ENVIRONMENT` on the environment. Grants and policies still apply. `BREAK_GLASS_WRITE` is
  recorded once per (environment, target) per service instance, in the write's own transaction,
  right before the commit that lands it (`ProtectedWriteGuard.record_pending`) — never for a write
  the operation's grant check then refuses; a
  queued SQL write carries the reason on `QueryRun.break_glass_reason`, checked at submission and
  again (and audited) when the worker executes it.

The guard runs right after the operation's own authorization in every `CatalogService` mutation
(by design), so a caller without the grant gets the ordinary 403
or 404 and never learns whether the environment is protected:
`create_namespace`, `drop_namespace`, `update_namespace_properties`, `create_table`,
`create_external_table`, `append`, `write_batches`, `commit_table` (except
`action=MAINTAIN_TABLE`), `create_or_replace_view`, `drop_table`, `rename_table`; and of
`QueryService.submit_write`. **A new mutation method must call it too** — the Iceberg REST catalog
reaches the same methods and so is covered, and has no break-glass. Maintenance, reads and
governance (grants, policies, secrets, repositories, deployments) are not affected.
`commit_table(action=MAINTAIN_TABLE)` skips the guard only structurally: it accepts snapshot
expiry updates and the removal of a ref named like a failed check's tag
(`lakana-check-failed-<run id>`, which maintenance removes after seven days) and nothing else, and never lands a break-glass row an earlier write left pending.
Targets are compared as tuples of namespace parts and table name, never as dotted strings, so a
legacy dotted component (`["bronze", "x.y"]`) cannot pass for `bronze.x.y`. A refusal rolls back the caller's uncommitted state before it
audits `PROTECTED_WRITE_DENIED`, so a half-built write's audit and lineage rows never persist.

**A run context is verified in every environment, protected or not.** In an unprotected
environment the guard checks the same liveness (task run `running` with the claimed
`query_run_id`, run `running`, not `cancel_requested`, bound to the acting principal), at `check`
and again `FOR SHARE` in `record_pending`, but not the asset (the M5c1 form): the write's scope is
left to grants, as for any write there. The claim form (a bound task token) is held to the task's
own asset whether or not the environment is protected. Maintenance never reaches the guard, so a bound token's
restriction on that path rests on authorization (`MAINTAIN_TABLE` is not in its allow-list). A stale context is refused with the neutral "This task attempt was
superseded" (audited `PROTECTED_WRITE_DENIED`, `protected: false`), so a stalled worker can never
commit over the attempt that replaced it. A live claim-form attempt that reaches past its own
asset in an unprotected environment is not stale and is not called so: it gets `OTHER_ASSET_MESSAGE`
("Task '<asset>' may not touch '<target>' (<operation>): a task only writes its own asset. Do this
outside the task."), audited with `reason: other_asset`; an operation the task may not do to its
own asset (drop or delete an artifact, drop or rename a table) gets `OWN_ASSET_MESSAGE` ("Task
'<asset>' may not do '<operation>' to its own asset; a task may only materialize its own asset:
for a table, <list>; for an artifact, <list>. Do this outside the task." For a namespace that has
the asset's name the subject reads "a namespace with its own asset's name"; the list is the same
for every task, since the guard does not know the kind at that point), audited with
`reason: own_asset_operation`; in a
protected environment both still get the protected message. Writes without a run context in an unprotected
environment are not affected. `run_one` starts a run, and refuses one before execution (deleted or
inactive principal, invalid task token), only while it is `QUEUED` or `RUNNING`, and writes its
final status with `UPDATE … WHERE status = 'RUNNING'` (rolling back the attempt's own unflushed and
flushed fields when that matches nothing), so the scheduler's reclaim stamp (`FAILED`) and its
message always stand. A SQL task's write that committed before the reclaim got its locks, or
whose worker died before recording the attempt, is found by the reclaim through its evidence and
the task ends `succeeded`, not rerun ("A task whose write committed is not run again"
below; a task whose asset has checks is the exception: "Checks and the rollback (M5d-a)"). What remains is **at-least-once execution** for a Python task, whose own table writes
carry no evidence on a write run of the task, and for a worker that dies before its write
commits: such a task is rerun, so its writes must stay idempotent per attempt (`replace`,
incremental watermarks).

### The write fence (M5c3b)

A worker frozen for longer than the lease (a paused VM, SIGSTOP, a suspended laptop) wakes after
its run was ended and the user was told to run it again. It must not then commit a table write.
`run_one` therefore sets `PrincipalRef.write_fence = WriteFence(query_run_id, worker_id)` for
every run it executes, and `ProtectedWriteGuard.record_pending` holds the write to it: the
commit lands only while that query run is still `RUNNING` under that `worker_id`
(`IS NOT DISTINCT FROM`, so a run claimed without an id passes its own fence). Otherwise it
raises `WriteFenceRefused` (a `PermissionDenied` with `WRITE_FENCE_MESSAGE`), rolls back the
write's uncommitted work and audits nothing: whoever ended the run audited that.

- **Same transaction as the pointer swap.** `record_pending` runs right before the `commit()` of
  `commit_table`, `_publish_table` (create), the empty append and `create_or_replace_view`;
  nothing slow follows it. The fence is one `UPDATE query_runs ... WHERE id AND status =
  'RUNNING' AND worker_id IS NOT DISTINCT FROM ...` that also writes the evidence
  (`rows_written`, `snapshot_id`, `target_table_id`). On Postgres it takes the row lock until
  the commit, so a sweep or a cancel that ended the run either committed first (the fence sees
  it) or waits and then finds a run that has its evidence. On SQLite there are no row locks; the
  `WHERE` clause is the check and one process writes at a time. A write without a noted result
  falls back to `SELECT ... FOR SHARE`.
- **Evidence lives with the fence, not in `_record_write`.** `_record_write` runs before the
  metadata file is uploaded; an `UPDATE` of the run there would hold its lock across object-store
  I/O and block the memory watchdog, a cancel and the sweep. It only calls
  `ProtectedWriteGuard.note_result`; `record_pending` writes it. A `RUNNING` run with
  `rows_written IS NOT NULL` has committed its write.
- **Lock order:** task run, run, then query run (`store._apply_task` order). The fence is checked
  after the run context's verification, so a task's SQL write must pass both its `claim_count`
  check and its fence.
- **Not fenced:** maintenance commits, because running maintenance a second time is safe
  (expiry is a compare-and-swap on the metadata location; orphan removal re-reads the pointer
  and is skipped while a write into the table is `RUNNING`; it appends nothing); also
  `ensure_namespaces`, a Python task's own table write through the REST API (#49) and the
  orphaned result file of a late read. A refused write's own data files are deleted by the
  existing clean-up of `write_batches`/`_write_new_table`; only the metadata file that
  `commit_table` already wrote stays, as an orphan.
- **The clean-up of a failed write never deletes a write that landed.** The commit can
  reach the database and fail only on the way back (a dropped connection, code after
  `session.commit()`); the table's new snapshot then names the write's files. When the failure is
  not a `LakanaError` (those are raised before the commit) and the commit was sent
  (`CatalogService._commit_sent`, set right before `session.commit()` in `commit_table` and
  `_publish_table`), the three clean-ups (`write_batches`, `_write_new_table`, `append`) go
  through `CatalogService._settle_failed_write`: roll back, then read the table fresh on the new
  transaction (the stored `metadata_location` of the `tables` row, never the session's objects,
  then the metadata file: does it list the snapshot this write produced). **Landed:** keep every
  file, return the write's result (reported as committed). **Not landed:** delete as before.
  **The check fails or times out:** keep every file and raise `WriteOutcomeUnknownError` (read
  the table with `lakana sql`, `lakana query status`). A pipeline task counts it as a permanent
  failure (a `LakanaError`), so it is not retried on its own, and a worker's run that already
  shows the evidence is stamped `SUCCEEDED` anyway (below), which is what makes "look at the
  query" an honest instruction. The pointer is read **with `FOR SHARE`** on Postgres
  (`SET LOCAL lock_timeout`, `LOCK_TIMEOUT_SECONDS`): a plain read cannot see a backend that is
  still inside its `COMMIT`, so a commit that was sent, answered with an error and applied late
  would be judged "not landed" and its files deleted; the transaction that moves the pointer
  holds the row's lock, so the locking read waits for it and reads the final row (proved by
  `test_the_check_after_a_failed_commit_waits_for_a_commit_still_in_flight`). A timeout is
  "unknown", never "not landed". A **create** has no row to lock: if its failure came from the
  commit and the table is not visible, the location is kept and the outcome is unknown
  (an unreferenced location; there is no table for `lakana table maintain`, and nothing else
  removes it yet). A failure before the commit was sent, a write that produced no snapshot (an
  append of no rows) and a replace's old files need no check: a replace deletes no old file on
  any path. The check leaves no transaction open on the session. Orphans of a write into an
  existing table are removed by `lakana table maintain`. No residual remains for the plain
  commit race on Postgres; on any database, a commit applied later than the lock timeout after
  it was sent is the one case left, and then the outcome is reported as unknown.
- **Other locks on the run's row.** On Postgres the lineage edge's foreign key takes
  `FOR KEY SHARE` on the run's row when the edge is flushed, which is before the metadata file
  is read and written. That lock does not block a cancel's or the watchdog's `UPDATE`, and not
  the fence's own later `UPDATE` in the same transaction; it does make a
  `FOR UPDATE SKIP LOCKED` reader pass the row by (the sweep reads `FOR NO KEY UPDATE SKIP LOCKED`,
  which a key-share lock does not block). The exclusive lock proper is taken only by
  the fence, right before the commit.
- **A noted result belongs to one fence.** `note_result` remembers the fence in effect;
  `record_pending` uses the note only under that same fence, so a note can never become another
  run's evidence (the commit then passes on the check alone and writes none).
- **A request can never carry a fence.** Only `lakana_worker.pipeline._run_one` builds one;
  `test_no_run_context_on_the_wire.py` forbids `writefence` and `workerid` in any request and
  `WriteFence` in the server's sources.
- **The refusal's journey:** `task_outcome.classify` gives TRANSIENT for it (a SQL task is retried),
  `failure_cause` gives `worker_lost`, and `_keep_a_prior_stamp` leaves the status the ender wrote.

### The sweep of lost query runs (M5c3b)

The leading scheduler, on every tick (`store.scheduler_tick(..., lease_seconds)`, also when no
task has a transition), ends each `RUNNING` query run that no alive worker holds
(`workers.without_alive_worker`: the worker's heartbeat is a lease old or its row is gone; a run
without a worker id, claimed by a release that wrote none, once it started a lease ago, by the
database's clock). A wrong sweep fails a healthy query or tells a user to repeat a write that
landed, so:

- **One conditional `UPDATE` decides.** `SKIP LOCKED` only chooses candidates. The statement that
  ends a run carries `status = 'RUNNING'`, the evidence as it was read (`rows_written IS NULL`
  for `FAILED`, `IS NOT NULL` for `SUCCEEDED`) and "still no alive worker"; `rowcount` is
  checked, and a run that changed in between is left for the next tick. The ORM object is never
  the decision. The statement sets status, message, cause and `finished_at` only, never
  `rows_written`, `snapshot_id` or `target_table_id`.
- **The evidence is `rows_written IS NOT NULL`, `0` included** (an empty append, a view, an empty
  create), and nothing else: not `snapshot_id`, not `target_table_id` (set at submission). One
  predicate (`store.has_committed`, as SQL `store.committed_write`) is shared by the sweep, the
  cancel of a lost run, the memory stop and the task path. A committed write ends `SUCCEEDED` with no message
  (a user who runs it again would append twice); its run has no `watermark`, `schema_diff` or
  lineage summary, because only the worker computes them.
- **Every ender, in one place.** (1) The sweep, on the leader's tick, for a run no alive worker
  holds. (2) The idle worker (`store.end_orphaned_runs`, from `poller.poll_forever`) for runs that
  still carry its own id although it runs nothing: right after each item it ran and at most every
  5 s while idle, with the same statement and rule. It ends `RUNNING` deploy runs under its own id
  too, `FAILED` with `DEPLOY_LOST_MESSAGE` and no event, by the sweep's one statement
  (`store._end_deploy_where`) with `worker_id` as the condition; true because a deploy finishes
  in one transaction (`DeployService._hold` then `_finish`), so a failed final write published
  nothing. A deploy in flight is never ended: it runs inline in `poll_forever`'s own thread (the
  queries served beside a Python task are `only_queries`), and the ender runs only between items. (3) The task path (`store._apply_task`) for
  every `RUNNING` run of a task whose lease is lost: `SUCCEEDED` for one with the evidence, and
  the task with it, **except a task that awaits its checks** (`store.awaiting_checks`: the
  asset has checks, the task is not ended, and it is stamped *written* or its current write run
  shows the evidence): that task goes back to `pending`, uncounted, its attempt `lost`, its stamp
  kept, and every other reclaim statement requires that it does not await checks, so a write that
  lands between the snapshot and the statement turns a stale reclaim into a miss. A count of a
  check is a task's own `RUNNING` query run that is not its write, has no target and is not
  bound as the task's `query_run_id` (`store.is_check_count`): the
  sweep and the task path end it `FAILED` with `CHECK_LOST_MESSAGE`, never `SUCCEEDED`. (4) The memory stop, and (5) the cancel of a
  lost run, which share the predicate `committed_write`. After a gap in its own ticks (more than
  half the lease beyond the scheduler interval, or a new leader) the leader pauses ender (1), for
  queries and deploys, until `renew_seconds + 5 s` of database time have passed, so that healthy
  workers can beat first; ender (3) is not paused. An interval of half the lease or more is not
  a gap, or the sweep would never run.
- **The role of a leader that vanished.** The scheduler role is a PostgreSQL session
  advisory lock; a leader that disappears without closing its connection (power loss, a cut
  network on another machine than the database's, a paused VM) holds it until the server drops the
  backend, which with default TCP settings takes about two hours. `lakana_core.db.create_engine`
  therefore sets, on **every** connection of an engine whose dialect is PostgreSQL (the role's
  dedicated connection and all others, so a dead worker's row locks go too), `SET
  tcp_keepalives_idle`, `tcp_keepalives_interval`, `tcp_keepalives_count` and `tcp_user_timeout`
  (not as pg8000 `startup_params`: a startup parameter the server does not know, such as
  `tcp_user_timeout` before PostgreSQL 12, is fatal for the connection, while a refused `SET` is
  not; pg8000's `tcp_keepalive` flag is the other direction, the client probing the server), from
  the lease: idle `lease // 3`, interval `lease // 6`, two
  probes, user timeout `idle + 2 * interval` in milliseconds (20 s, 10 s, 2, 40 s with the default
  lease). A dead client is **expected to** be noticed by the server within about 40 s, on
  platforms and PostgreSQL versions that honour the settings (not on a unix socket). **Tested:** the
  values reach the server session (`pg_settings` on the role's connection and on a pooled one,
  `test_run_engine_integration.py`), and the unit tests for the derivation, the dialect gate and the
  failure path. **Not tested:** that a cut network frees the role; that needs the kernel to drop a
  connection, which one host and one container network do not produce deterministically. A server
  that refuses a setting costs one logged warning per engine and never the connection; each setting is
  issued and committed alone, so one refusal does not undo the others. No setting turns it off.
  SQLite frees the role by its heartbeat (10 s) and is not concerned. The other direction (the
  client noticing a dead server) is `pool_pre_ping`, unchanged.
  **Where the settings do nothing, without a warning:** through a connection pooler the probes
  test the pooler, not the worker (workers and server must connect to PostgreSQL directly; a
  pooler in transaction mode also breaks the role itself, a session lock); on a PostgreSQL host
  that is not Linux `tcp_user_timeout` has no effect and the keepalive settings alone decide; on a
  unix socket they do nothing, harmless as both ends share a machine. PostgreSQL before 12
  refuses `tcp_user_timeout` (that one does warn). **One live client can be cut:** one frozen
  for longer than the user timeout while a result larger than the socket buffers is in transit
  (the peer's window stays zero). **After a server-side cut** pg8000 raises a raw
  `ConnectionResetError` on the next call (not a `DBAPIError`, not caught by `pool_pre_ping`);
  `PostgresAdvisoryLock.acquire` treats it like a database error (drops the connection, not
  leader, one INFO line); other call sites are not changed and see the raw error once.
- **The claim holds a `RUNNING` run to its claimer.** `run_one`'s start `UPDATE` matches a
  `QUEUED` run, or a `RUNNING` one whose `worker_id` is this worker's (a SQL task's write run is
  inserted `RUNNING` under its worker's id). A claim that matches nothing executes and writes
  nothing. On PostgreSQL the dequeue's row lock already did this; on SQLite two workers can
  both pick a `QUEUED` row, and the loser's claim now finds it `RUNNING` under the winner.
  A final update refused while the stored run is still `RUNNING` (under another id) writes no
  `QUERY_COMPLETED`: the worker that holds it writes the one event.
- **Which statement ends which run.** A SQL task's own write (`task_run_id` set, `task_token_id`
  null) is the task path's: `store._apply_task` ends it when the task's lease is lost. While its
  task run is `RUNNING`, the write is the task's current one (`task_runs.query_run_id`) and
  `lease_expires_at >= now` (the database's clock) the sweep and the idle worker do not touch it,
  in the candidate read and in the ending `UPDATE`: the heartbeat and the lease are two threads
  with separate phases, and ending a write under a live lease made the task fail for good with
  its rows in the table. `cancel_query` already refuses a task's write and points to
  `lakana runs cancel`, so the cancel path needs no such condition. Once the lease is not live,
  the sweep and the idle worker end the write by the evidence rule like any other: `SUCCEEDED`
  with `rows_written`, otherwise the lease message and cause `worker_lost`; neither writes a
  `QUERY_COMPLETED` for it (the task's own worker does, if it is alive). Every other `RUNNING` run
  without an alive worker (an ad-hoc query, a Python task's input query, maintenance) is the
  sweep's. Both statements require `RUNNING`, so a run is ended once.
- **A task whose write committed is not run again.** (Not for a task whose asset has
  checks and whose write is in the table: that task is *unchecked* until a worker records its
  verdict, and none of the rules below ends it as `succeeded` or `failed`; see "Checks and the
  rollback (M5d-a)", "Every ender" for the rule.) When the lease of a `RUNNING` task is
  lost and its **current** write run shows the evidence (`TaskSnapshot.committed`, read in the
  snapshot's task query by an outer join on `task_runs.query_run_id`; `_take` clears the link at
  each claim, so an earlier attempt's run never counts), `tick` emits `SUCCEEDED` with `lost`
  instead of `PENDING`, `FAILED` or `CANCELLED`: the evidence wins over a cancel request and over
  "last attempt". `_apply_task` first locks the task's row (`SELECT ... FOR UPDATE`, a statement
  of its own) and then runs the statements, each with the evidence in its `WHERE`: `EXISTS` for the
  success, `NOT EXISTS` for every other reclaim. The lock comes first because a write holds the
  task's row `FOR SHARE` from its fence to its commit: a single `UPDATE` that waited for that lock
  would judge the evidence by the snapshot it took before the wait (Postgres re-checks the quals
  of a waited-for row only when the row changed), and could put back a task whose write had just
  landed (proved by `test_a_commit_that_lands_while_the_reclaim_waits_for_the_task_stops_the_retry`).
  The task (`succeeded`, `error_message` = `COMMITTED_BEFORE_LOST_MESSAGE`), its open attempt
  (`succeeded`, `rows_written` from the evidence, the same message) and every `RUNNING` run of
  the task with the evidence (`SUCCEEDED`; the rest `FAILED` with the lease message) end in one
  transaction, and downstream tasks proceed in the same tick. The worker never recorded the
  watermark, schema diff or lineage summary of such a task: they stay empty, not invented; its
  `finished_at` is the tick's time, so its duration includes the wait for the lease. The memory stop does the same through `record_attempt(on_committed=...)` (the
  attempt ends `succeeded` instead of `failed`/`timed_out`, not retried). So does the worker's
  own recording (`tasks._finish` passes `on_committed` for every outcome): a task whose current
  write run shows the evidence is recorded `succeeded` also when a stamp failure, a drain
  release, a cancel or a failure would have retried, released, cancelled or failed it. (For a task
  that awaits its checks, `record_attempt` ignores `on_committed` and puts the task back instead:
  see "Checks and the rollback (M5d-a)".) `tasks._close_write` ends a run with the evidence `SUCCEEDED` (two statements, the evidence in
  each `WHERE`), and `pipeline._keep_a_prior_stamp` lands a failure or cancel outcome of a run
  that has the evidence as `SUCCEEDED` (one conditional `UPDATE ... RETURNING`), clears its
  error fields and audits one `QUERY_COMPLETED` with that status. A worker that is alive
  after all and records its own outcome at the same moment races on the task's row lock in
  `record_attempt`: one of the two wins, the task ends `succeeded` once and no second attempt
  exists. An incremental write was never at risk unless its key is `NULL` in every row: the next
  run reads `max(key)` from the table itself (`read_watermark`) and writes only rows above it.
- **Passed by, not waited for.** Candidates are read `FOR NO KEY UPDATE SKIP LOCKED`: a run is
  skipped while another transaction holds it with a conflicting lock, in practice the write
  fence's `UPDATE` between the fence and the commit (microseconds, no I/O), and is seen on a later
  tick. The scheduler never waits for a row lock. The mode is not `FOR UPDATE` because the
  lineage edge's foreign key holds `FOR KEY SHARE` on the run's row during a write's metadata
  upload: a worker stopped there (SIGSTOP keeps its connection) would keep its run `RUNNING`
  until it woke. `FOR KEY SHARE` does not conflict with `FOR NO KEY UPDATE` nor with the
  `UPDATE` that ends the run; the sweep ends it, and the woken worker's fence meets a run that
  is no longer `RUNNING`. Proved on Postgres by
  `test_lost_workers_integration.py::test_a_write_held_before_its_commit_is_ended_and_then_changes_nothing`,
  which also asserts that the held worker has the key-share lock; do not change the lock mode
  without that test.
- **At most one completion event per run,** written by whoever ended it (`QUERY_COMPLETED`: the
  sweep, a cancel of a lost run, or the worker). Four exceptions have none: the memory stop (it
  records the failure and writes no event, as before); a run whose submitter was deleted; a
  Python task's input query that the task path ended because its worker is dead (that worker
  cannot write one); and a SQL task's own write ended by the sweep, the idle worker or the task
  path because its worker is dead, or by a live worker's own clean-up after its final update
  failed. A
  late worker whose final update is refused
  (`pipeline._keep_a_prior_stamp` returns `False`, and it holds to the worker id as well as the
  status) writes none when the sweep or a lost-run cancel ended the run; after a task's lease
  reclaim, which writes none, the worker's event stands.
- **A living worker counted lost** (after a suspend, or because its own view of its heartbeat is
  younger than the database's) has a seconds-old run ended. When it wakes, its status update, its
  commit (the write fence) and its completion event are all refused: one final status, one event.
- **The cancel and the memory stop use the same rule.** A cancel of a run no alive worker holds
  ends it at once: `CANCELLED` without evidence, `SUCCEEDED` with it (never `CANCELLED` over a
  committed write); an alive worker is asked to stop, as before. `record_memory_failure` runs two
  statements, `SUCCEEDED` for a run with evidence that is neither maintenance nor a SQL task's own
  write and `FAILED` `out_of_memory` for the rest, each with the evidence in its own `WHERE`; a
  commit between them leaves the run `RUNNING` for the sweep.
- **Passed-by rows.** The sweep does not touch `QUEUED` runs (the predicate has no status filter;
  the caller adds it) or runs of an alive worker.
- Tests: `test_runs_store.py` (the sweep, the two orders against a late heartbeat and late
  evidence, the task lease, the idle worker's orphans), `test_query_service.py` (the cancel),
  `test_pipeline_outcome.py` (the late worker, the memory stop, the orphan), `test_poller.py`,
  `test_scheduler.py` (the guard after a gap), `test_main.py`.

### Deploy runs (M5c3b)

The same sweep ends a `RUNNING` deploy run (deploy, dry run or rollback) no alive worker holds
(`store._end_deploy_run`, one conditional `UPDATE` on status and "no alive worker"; `FAILED` with
`DEPLOY_LOST_MESSAGE`, no audit event, as `DeployService.fail` writes none; candidates `FOR NO
KEY UPDATE SKIP LOCKED`, and no foreign key points at `deploy_runs`). The other half is the
check at the four finishing points of `DeployService` (`record_deploy`, `record_dry_run`,
`execute_rollback`, `fail`): `_hold` locks the run's row (`SELECT … FOR UPDATE`; on SQLite a
write that changes nothing comes first, because `sqlite3` begins a transaction at the first write
and a bare `SELECT` would leave a gap) and refuses with `DEPLOY_ENDED_MESSAGE` unless the run is
`running` under the caller's worker id, in the same transaction as the pointer swap, the
project version, the status and the audit event. Consequences to keep:

- **No evidence rule for deploys.** The finish is one transaction, so a deploy either committed
  (its run is no longer `RUNNING`; the sweep's `UPDATE` matches nothing) or changed nothing
  visible. Files in the object store (the code archive) are written before the transaction and
  are orphans if it never happens; `sweep_code_archives` removes one only when no project version
  names it, it is over `ORPHAN_BLOB_GRACE` old and no deploy is queued or running on its storage root.
- **`_hold` comes first.** Moving the check after the writes would not work: the `SELECT`
  autoflushes the caller's own `status = SUCCEEDED` and reads it back. The unique-violation branch
  of `record_deploy` rolls back, which releases the lock, and calls `_hold` again.
- **`fail` accepts a `QUEUED` run** (nobody has claimed it; an inactive principal is failed
  before a worker takes the run), but never overwrites an ended one.
- **A stale worker does not stop a running deploy.** Only the sweep and the check at the finish
  end a lost deploy.
- Tests: `test_projects_deploys.py` and `test_deploy_hold_sqlite.py` (the finishing points and the
  hold, on both databases, including a late rollback that would move the pointer back),
  `test_runs_store.py` (the deploy sweep), the worker's `test_deploy.py` (a worker that wakes
  late) and `test_lost_workers_integration.py` (both orders of sweep and hold on Postgres).

## Run execution (M5c1)

- **Code integrity:** before the first task of a run on a worker it reads `code_uri` (which must
  lie under the environment's storage root) and compares its SHA-256 with `code_sha256`; a
  mismatch, a missing object or an escaping path fails the task permanently, flags the run for
  cancellation and records `CODE_INTEGRITY_FAILED`. A storage error is retried.
- **Run principal at execution:** the worker re-reads the run principal; a deactivated or deleted
  one fails the task (and `run_one` refuses an inactive principal again, closing the window
  between the check and the query). In the check phase of a task whose table was built, a
  principal that is deactivated (its row exists) makes every check `errored`
  (`ERROR_NO_PRINCIPAL`) and the table is rolled back; a principal whose row no longer exists,
  or a run without one, cannot start the phase (see "What is not defended" under the checks). A
  `source_check` task fails like a build in both cases. The write run is bound as the task's `query_run_id` before
  any namespace is created, so only the current attempt carries a usable run context.
- **Leases:** a worker renews and records an outcome only while it still owns the lease and the
  claim (`task_runs.claim_count`, which only grows — the attempt number repeats after an uncounted
  retry; `renew_lease`'s and `record_attempt`'s condition, and the scheduler's reclaim), so a reclaimed task is never finished twice; the renewal thread
  is stopped before the outcome is recorded. Timeouts, a lost worker and storage/network errors
  are retried; validation errors, permission refusals and integrity failures never are.
- **Failure text:** failures are classified by exception type only. `run_one`'s returned
  exception is unredacted and can carry storage locations; only the stored, redacted
  `QueryRun.error_message` reaches `task_runs`, `task_attempts` and the log. For a check not
  even that reaches a `CheckResult`: see "Checks and the rollback (M5d-a)", "What a result may
  hold".
  A refusal is re-checked against `runs.cancel_requested` (the protected-write guard refuses the
  instant it is set, before the lease keeper sees it): a cancelled run's task ends `cancelled`, not
  permanently failed. A claim that only sets the table back is not stopped by the request
  (`cancel_kind` returns nothing for it).

## Python tasks and bound task tokens (M5c2a)

User documentation: `docs/site/how-to/projects.md`, "Python assets". This section is what a reviewer of
code in this area has to know.

- **Deploy never runs repository code.** `lakana_core.projects.python_assets` reads a `.py`
  asset with `ast`; `lakana_core.projects.lockfile` parses `pyproject.toml` and `uv.lock` as
  TOML. Nothing is imported, resolved or installed at deploy. Discovery refuses every decorator
  but the single `@asset(...)`, so a file that deploys is a file the harness selects the same
  function from.
- **A Python task is an SDK client** (rule 2). `lakana.runner` lives in the SDK and imports
  nothing of core, server or worker (import-linter's `sdk-is-independent`). It reads through
  the query API and writes through the Arrow replace route with `expected_snapshot_id`; an
  artifact asset (M5c2b) reads and writes through `client.artifacts` instead. It is
  started as `<venv>/bin/python -P -m lakana.runner`; it imports what it needs first, then
  *appends* the repository root and `src/` to `sys.path`, so a repository file cannot shadow
  the standard library or an installed package.
- **Bound tokens.** The worker mints one token per attempt (`TaskTokenService.mint_for_task`,
  no `MINT_TASK_TOKEN` check: the worker is the platform), bound to `(task_run_id, claim)`.
  `resolve_task_token_scope` accepts it only while that task run is `running` with that
  `claim_count` and its run is `running` and not cancel-requested, so a reclaimed, finished,
  cancelled or superseded attempt's token is dead at once. The worker also revokes it on every
  path out of the attempt. No REST schema accepts `task_run_id` or `claim`; the 0016
  downgrade deletes bound tokens. Scope rules: see "The authorization seam" (`BOUND_TASK_TOKEN_ACTIONS`,
  `_check_bound_scope`) and the lockdown section (the claim-form `RunContext`).
- **The token's path.** Minted in the worker, written to the child's stdin, held in the child's
  memory. Never on a command line, never in the environment, redacted from the task log
  (the whole token and its secret part, including across a flush boundary) and from the result
  record's message.
- **`LocalProcessRunner` is not a sandbox, and one instance is one trust domain.** The task
  runs as the worker's operating-system user. It can read what the worker can read (the
  worker's own configuration and credentials, its home directory, the network the worker
  reaches) and it can write the shared venv cache, so one task can poison the environment of
  another task, of another project, or of another client's task, that has the same lock and `pyproject.toml`
  (the venv key covers both). The
  scrubbed environment (`PATH`, `HOME`, `TMPDIR`, `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`,
  `POLARS_OOC_SPILL_DIR` and, when the worker has a memory limit, `POLARS_OVERRIDE_TOTAL_MEMORY_MB`
  and `POLARS_OOC_DISK_BUDGET_MB`: a directory and sizes, no secret),
  the private per-attempt directory and the process group the supervisor kills are hygiene.
  What bounds a task is its token. Therefore everyone who may deploy Python code to an
  instance is trusted with everything its worker can read; run one instance per client. Never
  describe this runner as isolation in code, docs or messages. Local mode and
  `LAKANA_RUN__RUNNER=local` keep this, and the trust domain includes holders of
  `MANAGE_CONNECTION` there: a connection test dials from the worker's host (any address the
  connection names; paths under the ingestion roots), a probe a data engineer without `DEPLOY`
  did not have before. A `DockerRunner` worker runs a test through the launcher's `check` op
  instead (see "Task egress: connections, the proxy and the relay (M6)"): the task image, the
  task's network rules for that one connection, so it reaches only what such a task could.
- **`DockerRunner` isolates projects, not hostile code.** Each task runs in a container that the
  launcher (`lakana_worker.launcher`) starts with fixed fields (`requests.run_container_spec`,
  `_host_config`): uid 10001 in group 10003 (`TASK_USER`; builds run as 10002:10002, so a task
  cannot change its environment), `CapDrop: ALL`, not privileged, a read-only root filesystem,
  code and environment read-only (`/code`, `/env`), a private `/work` (a work-volume subpath) and
  a `/tmp` tmpfs (`noexec,nosuid,nodev`, 64 MiB), memory (no swap), CPU and pids limits, an init
  process, private IPC and cgroup namespaces, no log driver, `no-new-privileges` where Docker
  allows it (off on snap Docker, detected by a probe at start), and a network of its own
  (internal, no IPv6, `gateway_mode_ipv4/6=isolated`) shared with the `server` service only,
  plus the `egress` service and the task's relay when its asset declares connections (see "Task
  egress: connections, the proxy and the relay (M6)" below). The token is on stdin only. A task reaches every port the server's container listens on, and
  the REST API with its own token, as before. Containers share the kernel. `Runner.isolated` is
  true for this runner only; `PythonRuntime` refuses a mismatch of runner and environments, and
  `_prepare` refuses a protected environment's task when the runtime is not isolated (read once
  per attempt, when it is prepared: an attempt already past `_prepare` when the environment is
  protected runs to its end).
- **The environment build runs code too.** A package built from source (an sdist) runs its PEP 517 backend as the worker's user, with network access, and sees the
  index and proxy credentials passed to the build. The worker runs `uv sync --frozen --no-config`: the project's `[tool.uv]` settings and
  any `uv.toml` (a system-level one on the worker's host too) are not read at build time (a
  project's `https-proxy` or `allow-insecure-host` would otherwise send an index credential to a
  host the project chooses; pinned against the real uv in `test_index_credentials.py`). They shape
  the lock on the author's machine only. Still read: the lock, and the index definitions in
  `[[tool.uv.index]]` (a credential in such a URL goes to that index's own host only). A root
  `uv.toml` is still refused at deploy and by the worker. The lock decides what is fetched, and the
  rule names indexes, not download hosts: each package's `source.registry` must be PyPI, the
  project's `wheelhouse` folder, the operator's `LAKANA_RUN__INDEX_URL` or a registered package
  index, and another index, a git source and a direct URL are refused at deploy and before every
  execution (`lockfile_issues`, `source_issues`). A wheel's or sdist's own URL in the lock is not
  checked: it may point at another host, uv downloads from it, and no credential is sent there
  (a credential is bound to its index's scheme, host and port). A URL the strict
  `comparable_index_url` rule refuses is not a registered index; the operator's index has its own
  lenient comparison (`KnownSources.is_known`). Local path and workspace dependencies are refused at deploy
  (`lockfile_issues`) and again before the build. Detached child processes of user code are not
  killed (no cgroup). The uv cache is trimmed above `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES`
  (`lakana_worker.caches`, #206): the worker deletes nothing itself, it runs `uv cache prune` and
  `uv cache clean <names>` (fixed argv, no shell, offline, `--no-config`) and ends a command at once
  when uv says another process holds the cache's lock, so a build of another worker on the shared
  volume is never interrupted; no trim starts while this worker builds (`builds_in_progress`). A
  package name comes from the cache's own directories, which a task under `LocalProcessRunner` can
  write (one trust domain), so it is passed only when it matches the normalised-name pattern (never
  a leading `-`). Environments hold hard links or copies of the cache's files, never symbolic links,
  so removing an entry never breaks a running task's environment. `GET
  /api/v1/environments/{name}/usage` reads only the catalog database (sizes measured by the
  leader's sweep from one metadata file per table, at most 200 per sweep), so it cannot make the
  server walk a bucket; it needs `MANAGE_ENVIRONMENT` on the environment because the totals cover
  tables and tombstones the caller may not see. The attempt
  sweep deletes only old, unheld direct children of the private `attempts/` directory with the
  worker's attempt-name pattern, via `remove_private_tree`; a link or foreign name is left alone. The venv cache directory must be owned
  by the worker's user, mode 0700 and not a symlink (`VenvCacheUnsafe`, a permanent failure).
- **Untrusted inputs of the worker:** the code tarball (digest re-checked, unpacked fresh for
  every attempt with no links and no special files into a read-only directory inside the
  private attempt directory), the result file the child writes (opened `O_NOFOLLOW`, regular
  files only, 64 KiB, shape-checked, and never believed: success needs the worker to find this
  attempt's own write, #49: a snapshot on the current branch, newer than the attempt's start,
  whose summary carries `lakana.task-run-id` and `lakana.task-claim`; `CatalogService.commit_table`
  and `_write_new_table` set both from the claim-form `RunContext` only and remove them from every
  other added snapshot, so no client, Iceberg REST included, can claim an attempt's write; a table
  created without rows is proven by its location's `holder_run_id`, and only the current claim of a
  task run can commit, `record_pending`), the child's output (redacted, control characters replaced, 1 MiB per
  attempt in all), uv's output (redacted of proxy and index credentials).
- **The artifact harness has two residuals that are recorded, not closed (#67):** the upload
  works with `os.fchdir` into the directory it pinned, and the check that `inputs` is still the
  directory the harness made is by path. Both hold against code that swaps a directory by name, not
  against code of the task running at the same time (a thread that calls `os.chdir` redirects the
  scan). It adds nothing to what in-process code can already do with the task's rights. Details
  under "The artifact store (M5c2b)", "Residuals of the harness".
- **Review checklist for changes here:** a new exit path must still revoke the token, release
  the venv lease and remove the attempt directory; a new action for bound tokens must be added
  to `BOUND_TASK_TOKEN_ACTIONS` *and* handled in `_check_bound_scope`; nothing may put the
  token into `argv`, the environment, a log line or an exception message; user code must never
  be able to choose its own failure classification (the `conflict` flag is set only by the
  harness's upload path).

**Artifacts and bound tokens (M5c2b).** An artifact is a row of the same name registry as a
table, so the same two rules cover it: a bound token reads only declared inputs (`READ_TABLE`) and
writes only its own asset (`CREATE_TABLE`, `WRITE_TABLE`), never `DROP_TABLE`.
`BoundTask.output` and `BoundTask.keep_versions` come from the project version manifest, read through
`entry_output` and `entry_keep_versions` as untrusted input (a wrong type, case or range reads as
`table` and as "keep all"). The lockdown has three operations for artifacts:
`WriteOperation.ARTIFACT_VERSION` is materialising, so a task may do it to its own asset;
`DELETE_ARTIFACT_VERSION` and `DROP_ARTIFACT` are not, so a bound task token never may, and in a
protected environment they need a break-glass reason from a holder of `MANAGE_ENVIRONMENT`. The
guard also enforces the output kind, for a run context of either form, on the task's own asset:
it reads the task's manifest entry in the run's project version (`python_entry`, then `entry_output`;
a missing or malformed entry counts as `table`), so a task that produces a table is refused
`ARTIFACT_VERSION` ("Task '<asset>' produces a table; it cannot add an artifact version. To
store an artifact, declare the asset with output=\"artifact\".") and a task that produces an
artifact is refused every table write ("Task '<asset>' produces an artifact; it cannot write a
table. Write the artifact's files into lakana.output_dir() instead."), at `check` and again under
lock at `record_pending`. The worker's in-process (query-run) form never adds a version on its own
asset, whatever the entry says: a version arrives over REST with the bound token. The refusals
are audited as `PROTECTED_WRITE_DENIED` with `reason: output_kind`.

**Reading an artifact.** Every read route authorizes `READ_TABLE` on the name
before it looks anything up, so a caller without the privilege gets one 403, with one text,
whether or not the artifact, the version or the file exists; the list shows only artifacts the
caller may read (an empty list, never an error, for a namespace they cannot see). A task token
(bound or not) gets a version without `task_run_id`, `run_id` and `project_version_id`;
`commit_sha`, `created_by` and `deleted_by` stay. A download is audited by the service, once per principal and
version per upload lifetime, before the first byte is sent, so a download that later fails is
audited too; a bound token writes none. The body is streamed with the manifest's size as
`Content-Length`, no database transaction open, a deadline of 60 s plus the size at 64 KiB/s (the
server ends a download slower than that), no slot of the append limiter and a slot of its own
(at most 32 files at once per process; one more is a 503 with `Retry-After: 1`, which the SDK
asks again); an object whose length is not the manifest's, or a storage failure after
the first byte, ends the response unfinished (the connection is cut and the client sees an
incomplete body). The SDK treats the manifest it receives as hostile: it validates every path
with its own copy of the rules (a part is at most 255 bytes), writes into a staging
directory without following a link and moves the finished tree to the target only when every
file matches its size and SHA-256; an existing empty target is never replaced. A
person's copy is private (files 0600, directories 0700, less the umask) because the contents are
access-controlled on the server; `read_only=True` seals it (0400/0500) for a task's inputs
.

## DockerRunner: the launcher client and environments (M7b, #38)

- **The worker talks to the launcher only through `lakana_worker.launcher_client`.** One request
  per connection, the launcher's token read (bounded, ASCII) from the file next to the socket and
  added to every request. Every answer is untrusted input: anything outside the protocol (not
  JSON, nested too deeply, a log that is not base64, a line over 1 MiB) is "runner unavailable",
  never another exception. A refusal (`LauncherRefused`) is permanent; an unreachable launcher or
  a Docker failure (`LauncherUnavailable`) is transient and names the `launcher` service and
  `LAKANA_RUN__LAUNCHER_SOCKET`; a log sink that raises (`LogSinkFailed`) stops the container.
- **`DockerRunner` sends the task description on the container's stdin and nowhere else**: the
  token never appears in another request field, the environment or a command line. The
  description is the local one with the container's paths (`/work/result.json`, `/work`) and no
  parent pid. The container's output is redacted like a local task's, a token split across
  Docker frames or log lines included; the result record in `<attempt>/task` is read with the
  same distrust (`O_NOFOLLOW`, regular file, bounded) and a stale one is removed before the run.
  Docker's `OOMKilled` sets the memory stop reason, and so does exit 137 (SIGKILL) on a run the
  launcher neither timed out nor stopped: the flag comes from a containerd event that is lost
  when the container goes first. A cancel set first wins.
- **A stop is asked for until the launcher confirms it, and the worker does not wait forever.**
  The launcher's `stop` answers `stopped: true` only when Docker stopped the container, found it
  not running or gone, or the run has no container yet; a failed or refused Docker call is
  logged and answered `false`. `DockerRunner` asks again on `false` (also before the launcher
  registered the run): the first retry at the next poll, then a gap that doubles up to 5 s
  (`runners.stop_retry_gap`). Its launcher call has a deadline of the task's timeout, the kill
  grace and `runners.DEADLINE_MARGIN_SECONDS` (900 s: the launcher's image pull and wait
  margin); past it the client closes the connection (`LauncherTimedOut`), which makes the
  launcher kill and remove the container, and the task fails as timed out naming the launcher.
- **A build stops with its task (#291).** The supervisor's `KeeperStop` reaches the build as
  `stop`: `DockerEnvironments.acquire` asks the launcher to stop the build by id and owner, with
  the same retries as a run, and once the launcher confirmed it waits at most `stop_wait` (30 s,
  for an image pull the launcher cannot interrupt) before the task ends without the result; the
  launcher kills the build container (grace 0), removes the private cache and the environment
  volume and marks nothing complete. A build queued behind another build of its environment
  stops waiting (`_LOCK_POLL`). The local runner's `venvs.acquire` polls `stop` with the size
  watch, kills uv's process group, removes the build directory and publishes nothing; a wait for
  another task's build lock ends too. `VenvBuildStopped` is classified by the keeper
  (`_refuse_if_stopped`): cancelled, released or lease lost, never a build failure.
- **An environment belongs to one project.** Its key (`docker_environments.environment_key`) is
  the SHA-256 of the project's id, the lock digest, `pyproject.toml`'s digest, the Python version
  and the credential-free index set; the launcher adds the image to the volume's name. The
  project is in it because a lock pins every index package by hash but not the files of a path
  source or a vendored package in the repository: without it, a project could commit another
  project's exact `uv.lock` and `pyproject.toml` beside its own copy of a vendored package, and
  the other project's tasks would run that code with their own token (PR 2 security review, I4).
  The cost is one build per project for a lock several projects share. Within one project, a
  vendored package changed without a change to `uv.lock` or `pyproject.toml` still reuses the
  built environment, as the local venv cache does.
  The project id the worker passes is `python_tasks.environment_owner(repository_id,
  environment_id)`, a UUIDv5 of the repository and the environment, never a name: one
  repository deployed to dev and prod gets two environments, so a dev deployer who changes a
  vendored package without changing the lock cannot hand that code to prod's tasks and prod's
  token. A project deleted and registered again gets a new repository id and a new key.
- **The worker's start with `LAKANA_RUN__RUNNER=docker`** (`__main__._python_runtime`): one
  `LauncherClient` on `LAKANA_RUN__LAUNCHER_SOCKET` serves `DockerRunner` and
  `DockerEnvironments`, both owned by `launcher_client.claim_owner`: the host name and a random
  token kept in `launcher-owner` in the worker's cache directory, locked (`flock`) for as long as
  the worker runs. A second live worker on the same directory takes `launcher-owner.1` and so
  on, so two workers that share a host name never share an owner: the `cleanup` one asks for at
  its start removes only what an earlier process of the same worker left, never the other's
  running tasks. Without a writable directory the token is random and nothing is kept (the
  launcher's deadline label still removes leftovers). The worker asks the launcher once at
  start to remove what its earlier process left (`cleanup`), and starts anyway if the launcher
  cannot be reached. The attempt directory is
  `tempfile.mkdtemp(prefix="attempt-")` directly under the work directory, which must be the
  mount of the launcher's work volume: the launcher mounts `<attempt>/code` and `<attempt>/task`
  by name (`requests._ATTEMPT` accepts mkdtemp's names; a test pins the agreement). The local
  runner refuses a project whose `python:` names another Python than the worker's own
  (`no_runtime`). `LAKANA_RUN__HTTPS_PROXY` is refused at start with the docker runner (the
  egress proxy is the way out; a company proxy is `LAKANA_EGRESS__UPSTREAM_PROXY`).
- **The trust mount** (#292). With `LAKANA_RUN__CA_BUNDLE` the worker builds the trust bundle
  once at start, in memory (`lakana_worker.trust.build_trust_bundle`): Mozilla's roots (certifi)
  plus only the `CERTIFICATE` blocks of the operator's file, each parsed as X.509 and written
  again canonically, so anything else pasted into the file never reaches a container. The file
  is read once (`O_NONBLOCK`, regular file, at most 1 MiB): judging and copying the same bytes
  leaves no check-to-copy swap. A file with a private key (PEM or PGP), without a certificate,
  with a damaged one or with a `TRUSTED CERTIFICATE` block stops the worker, because every task
  can read the bundle. The worker, not the launcher, writes the bytes into each attempt's
  `trust/trust.pem` (`install_trust`: a file, never a link; a planted link at `trust` is
  refused, one at `trust.pem` replaced without being followed), and the run or build request
  carries only `trust: true`; the launcher mounts `<attempt>/trust` read-only at `/trust` and
  sets `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE`. Read-only because a task owns its attempt's
  files: only the mount stops it from rewriting what a later build of the same attempt trusts
  (`test_docker_trust_integration.py` proves `EROFS`). Under the docker runner nothing is
  written to the worker's cache, so replicas sharing it never read each other's bundle; the
  local runner writes `trust-<sha256[:16]>.pem` there, named by its content, never deleted.
- **A build gets only what its lock needs**: the credentials of the registered indexes it uses
  (an index without a credential is a host to reach, not an entry of `UV_INDEX`), the operator's
  index, and the `host:port` of each of those indexes for the egress proxy. Credentials are decrypted only when
  a build runs, never for an environment the launcher reports complete; the build's output is
  redacted for every form of every credential before it reaches the task log.
- **A build's only way out is the egress proxy** (`launcher/egress.py`, `lakana-egress`,
  standard library; Squid and tinyproxy are GPL). The launcher attaches the `egress` service to
  each build network and puts a token in the build's `HTTPS_PROXY` (`egress_token.mint`):
  HMAC-SHA256 with `egress.key` (32 random bytes, hex, mode 0640, in the `egress-key` volume the
  proxy mounts read-only) over a purpose (`build`), the build network's /29, exact host:port
  pairs (`requests.build_targets`: exactly the request's `targets`, the `host:port` of every index
  the build may use, with a credential or without, from `docker_environments.build_targets`;
  parsing refuses a credential or an operator index whose pair is not among them; #301) and an
  expiry (the build's timeout and a minute, counted after the image pull). The proxy keeps no
  state and knows nothing of builds: it tunnels `CONNECT host:port` only when the token
  verifies, the client's address is in the token's network (a token copied into another
  container is refused), and the pair is in the token; host matching is exact on one spelling
  (lower case, no trailing dot, no IPv6 literal, no leading-zero port). Directly it never
  connects to loopback, link-local (cloud metadata), multicast, unspecified, reserved or known
  metadata addresses, nor to a network its own container is attached to (read from
  `/proc/net/route`: the Compose network and every build network; a table it cannot read means
  no direct connection at all, 503, and so does any line it cannot parse or an IPv6 table that
  exists but cannot be read; a missing IPv6 table means IPv6 is off), nor to a range of
  `refused.json` (below), resolving once and connecting to a checked address (no DNS rebinding);
  other private addresses stay reachable, because a company index often is one. The head must
  arrive whole within 10 s and 16 KiB, strictly CRLF, one `Proxy-Authorization`, at most 64
  lines; at most 256 connections (503
  beyond, then closed without a thread), and at most 32 from one client network
  (`LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK`, #302): the most specific attached network that
  holds the client's address (a build's /29), or the address alone, counted from the accept,
  before the head, so idle connections count; past the cap 503 naming it, and past the cap and
  four answering threads the network's connections are closed at once (`process_request`), so a
  flood takes only its own threads. Builds set `UV_CONCURRENT_DOWNLOADS=16`
  (`requests.BUILD_DOWNLOADS`) to stay within it; a tunnel ends idle after 5 minutes and at the token's
  expiry. A released build's /29 rests until its token's expiry plus a minute before any network
  gets it again, running and resting networks counting together against the network cap. The
  rest lives in the launcher's memory, so every launcher start writes a new `egress.key`
  (`egress_token.rotate_key`, replaced in one step): no token from before the restart is valid.
  The proxy needs no restart: it compares the key file's inode, modification time and size at
  each connection and reads it again when they change; a failed read is not remembered, so the
  next connection tries again. `mint` refuses a token longer than `verify` accepts (the launcher
  answers the build with a refusal). With the docker runner an `http://`
  `LAKANA_RUN__INDEX_URL` is refused at worker start (`RunConfig`): the proxy tunnels https
  only. `LAKANA_EGRESS__UPSTREAM_PROXY` chains a company proxy, which then resolves and
  decides. Tasks with connections get tokens of purpose `task` through the same proxy (see
  "Task egress: connections, the proxy and the relay (M6)" below).
  In the Compose bundle the proxy runs as uid and gid 10001 with the base interpreter
  (`python -I -S`, standard library only) and reads `/run/lakana-egress/egress.key` (0640,
  root:10001, in a set-group-id directory 2750 root:10001) from the `egress-key` volume, which
  only `launcher` (writes) and `egress` (read-only) mount.
- **Docker's gateways and the operator's ranges.** The Docker host answers on the gateway of
  every Docker network (172.17.0.1 on the default bridge, one per Compose network), outside the
  networks the proxy is attached to, so a host service listening on `0.0.0.0` was reachable
  through NAT. The launcher lists `GET /networks` (all networks, no label filter) at start and
  at every network open (task, build, check: `_open_network`, after the network exists and
  before anything joins it) and writes every `IPAM.Config[].Gateway` (IPv4 and IPv6, as /32 or
  /128), `LAKANA_LAUNCHER__EGRESS_REFUSED` (comma-separated CIDRs or addresses, at most 64; a
  malformed entry or host bits set stops the launcher naming the entry; an IPv4-mapped entry is
  written as its IPv4 form, because `forbidden` maps addresses to IPv4 first) and its own network
  range, merged and sorted, to `refused.json` next to `egress.key` (temporary file and rename,
  0640 in the key's group, only when the content changed). Listing and writing happen under one
  lock, so an older listing never replaces a newer one. A failed listing stops the start; later
  it keeps the last set and logs; a `Gateway` that does not parse is logged naming its network.
  `dial` also refuses the Docker host's well-known names (`egress.DOCKER_HOST_NAMES`:
  `host.docker.internal`, `gateway.docker.internal`, `docker.for.mac.host.internal`,
  `docker.for.mac.localhost`, `docker.for.win.localhost`; any case, trailing dot) before any
  resolution, for every purpose, direct or upstream: on Docker Desktop they resolve to the host
  outside every gateway. The proxy reads the file like the key (again when its
  inode, time or size change; a failed read is not remembered) and refuses every address in it
  through `dial` (`forbidden(..., refused=...)`, checked before the test hook `reachable`, so
  no hook ever opens a refused range), direct or before an upstream proxy,
  for builds, tasks and connection tests alike; a missing or malformed file means no connection
  at all, 503 naming `docker compose logs launcher`. Each relay gets the same list, current at
  its start, in its plan.
- **Incident response.** When an environment may be poisoned (a malicious dependency or vendored
  package was deployed) or a worker may be compromised:
  1. Stop the workers (`docker compose stop worker`), so nothing builds or runs meanwhile.
  2. Remove the stack's task and build containers and their networks; the commands are in the
     docstring of `lakana_worker.launcher.service` (filter
     `label=lakana.launcher.stack=<project>`, where `<project>` is the Compose project name of
     the stack, not a Lakana project).
  3. Remove the built environments and the package caches:
     `docker volume rm $(docker volume ls -q --filter label=lakana.launcher.stack=<project>)`.
     Every environment is rebuilt from its lock on next use; a shared cache refills from the
     indexes.
  4. Rotate what a build or a task could have seen: the credentials of the registered package
     indexes the affected projects use, and `LAKANA_RUN__INDEX_URL`'s password if it has one.
     Restarting the launcher writes a new `launcher.token`; to make the cache key new as well,
     delete `cache.key` in the launcher's state volume before the restart (the shared caches
     then start empty).
  5. Redeploy the fixed project, then start the workers again. Review the audit log and the
     task logs of the affected projects for what ran meanwhile.

## Task isolation: the launcher (M7b, #38)

User documentation: `docs/site/how-to/run-python-tasks-in-containers.md`. The launcher is the one
remaining privileged component: it holds the Docker socket, which is root on the host. In the
Compose bundle it runs as its own service with `network_mode: none`, `cap_drop: [ALL]`, a
read-only filesystem and uid 0 only to open the socket, on the base interpreter
(`python -I -S`) from its own copy of its code (`/opt/lakana-launcher`), so no `.pth` file of a
dependency runs next to the socket. It listens on a Unix socket in the `launcher-socket` volume,
which only it and the workers mount; `launcher.token` next to the socket (0640, new at every
start) authenticates a worker, compared with `hmac.compare_digest`.

- **Nothing the caller sends reaches Docker as-is.** `requests.parse_request` accepts one JSON
  line of at most 512 KiB with exactly the fields of one of five operations (`FIELDS`: run,
  build, stop, status, cleanup), refuses repeated keys, and checks each value against a pattern
  or the operator's ceiling (`LAKANA_LAUNCHER__MAX_MEMORY_MB`, `MAX_CPUS`, `MAX_PIDS`, a week of
  timeout). `requests.run_container_spec` and `build_container_spec` fix every field that matters
  (user, capabilities, mounts, network, entrypoint, environment). Image references come only from
  `LAKANA_LAUNCHER__IMAGES`; host paths only from `LAKANA_LAUNCHER__INGESTION_ROOTS`, bind-mounted
  read-only and non-recursive at `/ingest/<name>`, only the roots a run names (the worker names
  the roots of the task's `directory` and `sqlite-file` connections). The settings
  refuse `/` and anything under or holding a system directory (`settings.FORBIDDEN_ROOTS`), the
  Docker socket's and the launcher's socket's directories; at start a probe container per root
  refuses proc, sysfs and devtmpfs mounts, sockets, pipes and device nodes. Docker resolves a
  symlinked root on the host, so the operator rules are documented in the how-to: a root is not
  a symlink, it and its parents are writable only by root, and the Docker group's id is none of
  10001 to 10003 (`_check_socket_group` refuses 10002 and 10003 and a world-writable socket).
  `test_launcher_requests.py` holds every hostile shape of the design trial.
- **Docker's subpath check is the second layer.** Code and scratch are subpaths of the work
  volume (`<attempt>/code`, `<attempt>/task`, the attempt name matched by `requests._ATTEMPT`);
  Docker refuses a subpath that leaves the volume through `..` or a link
  (`test_a_subpath_that_is_a_link_out_of_the_volume_is_refused_by_docker`, docker tier).
- **Standard library only, nothing else of Lakana** (import-linter contract
  `launcher-stands-alone`; `test_launcher_imports.py` keeps it off the worker's other modules).
  Any change to `lakana_worker/launcher/` gets a separate security review.
- **The network.** Each task or build gets a /29 from `LAKANA_LAUNCHER__NETWORK_RANGE`
  (`10.207.0.0/16`, private, a /24 or larger), internal and with an isolated gateway; at most three
  quarters of the range's /29s are in use or resting at once, and the live runs together hold at
  most `LAKANA_LAUNCHER__MAX_TOTAL_MEMORY_MB`. Both refusals are `EngineError`, which the worker
  reports as "runner unavailable" and retries. Engine 28 is required, and at start the launcher
  creates an isolated network and checks that Docker kept the option. A range that overlaps a
  route of the host's LAN shadows that route for the host: the how-to tells operators to choose
  a free range.
- **Builds.** A build container gets the code read-only, its environment volume and a uv cache:
  shared between builds with the same index set and credentials only when the build is wheels
  only (`no_build`), else private to its environment; never mounted into a task. It gets the
  credentials of the registered indexes its lock uses that have one (the worker decides which,
  `require_use` first), the operator's `LAKANA_RUN__INDEX_URL` with its password, and a per-build
  egress token (see "DockerRunner: the launcher client and environments" above for the proxy).
  A registered credential makes the build wheels only; the operator's index password does not.
  Build-time code of a dependency (an sdist, without a registered credential) sees the operator's
  index password and the token and can reach the granted hosts; it cannot reach anything else. The
  completion marker of an environment lives in the launcher's own volume (`launcher-state`), out
  of every build's reach.
- **Cleanup.** A container and its network are removed on exit, stop, timeout and a closed
  connection (a dead worker); `Launcher.shutdown` removes them on SIGTERM; each carries
  `lakana.launcher.deadline`, which a reaper enforces; at start the launcher removes everything
  carrying its stack label. It keeps the 10 most recently used environments
  (`KEEP_ENVIRONMENTS`) and removes a cache idle for 14 days.
- **Residuals, accepted:**
  - the shared kernel;
  - `no-new-privileges` off on snap Docker;
  - task images are named by tag and pulled once when absent: a tag repointed in the registry
    before the first pull is what runs (the release attests the images; the launcher does not
    verify attestations);
  - a task's network is the server's too: it reaches every port the server container listens on,
    and the REST API with its own token (as before). A task with connections reaches their
    endpoints as well, and nothing else (the residuals of that are under "Task egress" below);
  - a compromised worker holds the launcher token: it can start allowed images with its own code
    as uid 10001, mount any environment volume read-only by key, build into an environment not yet
    complete, stop a run of another worker only if it learns that run's id, and use any
    credential the worker can decrypt. `stop` and a closed connection act only on a run
    registered under the same owner (`_same_owner`; another owner's id gets the answer an unknown
    id gets), but the owner is **not** a secret: it is `<host>.<16 hex>` from `launcher-owner` in
    the worker's cache directory (`launcher_client.claim_owner`), and in the bundle every replica
    mounts the same `worker-cache` volume as the same user, so any worker can read every other
    worker's owner. What protects another worker's run is its id: 128 random bits
    (`uuid.uuid4().hex`) that `DockerRunner` and `DockerEnvironments` make per task and per build
    and never store or log (only the launcher logs it). The owner check closes the case where a
    `run` or `build` repeating a live id with another owner, refused as a duplicate (which tells
    only that the id is live), ended the first run when its connection closed. It cannot choose an image, a host path, a capability or a network;
  - while the launcher is down, tasks fail as "runner unavailable" and are retried; containers it
    started keep running until it starts again and removes them (their tokens are revoked by the
    worker when its call fails);
  - no disk quota for `/work`, environment volumes or caches (`LAKANA_RUN__VENV_MAX_BYTES` is
    checked after a build);
  - the network range can shadow a LAN route (above);
  - a launcher bug is host root.
- **Egress residuals, accepted:**
  - a direct connection may reach private addresses outside the networks the proxy is attached
    to, Docker's gateways and `LAKANA_LAUNCHER__EGRESS_REFUSED` (a company index often is one).
    The Docker host's other addresses (LAN, VPN or Tailscale, `virbr0`, Docker Desktop's
    `192.168.65.0/24`) are refused only when the operator lists them; the docs tell operators to
    list every one and to bind host services to loopback. A network made after the last listing
    is refused from the next network open on;
  - with `LAKANA_EGRESS__UPSTREAM_PROXY` the upstream proxy decides after the proxy's local
    check (numeric hosts, and for tasks and connection tests names that resolve here). A name is
    resolved twice, here and at the company proxy, so a split-horizon or rebinding name can pass
    the local check, and a name that does not resolve here is not checked at all. This is
    inherent: a CONNECT to the checked IP would break the name-based allow-lists of company
    proxies;
  - a tunnel whose writes block can outlive its token's expiry by up to the 300 s idle timeout.

### Task egress: connections, the proxy and the relay (M6)

User documentation: `docs/site/how-to/run-python-tasks-in-containers.md`, "Connections: what a
task reaches", and `docs/site/reference/connections.md`, "What a task reaches, and how". The
M7b posture (a task reaches the server only) stays the default for every task without
connections; a connection opens exactly its endpoints and nothing else.

- **The worker decides the routes, from the deployed manifest.** `python_tasks._connection_plan`
  reads the connections the bound entry declares (`BoundTask.connections`, never request input)
  in one select and computes each one's endpoints with core's
  `lakana_core.connections.endpoints.endpoints` (the same function the server's fetch uses). Only
  an isolated runner sends routes: `egress` (host:port pairs of `PROXY_KINDS`, at most
  `MAX_EGRESS` = 64, the token's pair capacity; with the deploy's 16 connections of at most two
  endpoints the real ceiling is 32 pairs, or the token's 8192-character payload for very long
  host names, which the launcher refuses with `EGRESS_TOKEN_TOO_LONG` and the worker turns into a
  permanent failure naming the asset and the split), `relay` (one `RelayRoute` per endpoint of
  `RELAY_KINDS`, listen ports from 15001, at most 16) and `roots` (the root names of
  `MOUNT_KINDS`, at most 16). Over a cap the attempt fails permanently naming it, before any
  process starts. The task gets the routing in its stdin description (`grant`, `relay_port`,
  `root_path`); the SDK's `fetch` compares the fetched kind with the routing's and the endpoints with the `grant` and raises the
  retryable `ConnectionChanged` when a connection changed during the attempt, caching nothing.
- **The launcher enforces them.** A run with `egress` gets the `egress` service on its network
  and a token of purpose `task` bound to the run's /29 and to its timeout and kill grace plus a
  minute, in `LAKANA_EGRESS_PROXY` (never `HTTPS_PROXY`, which libraries read unasked); a token
  copied into another task's container is refused (`this token belongs to another client
  network`), and a host:port outside it gets 403 naming `@asset(connections=[...])`. A run with
  `relay` gets its relay (below). A run with either while `egress` is not running is refused
  before any network or container exists (`EGRESS_MISSING`, a permanent failure with its own
  text). Every refusal of an address is the same `INTERNAL` reason, with the task's next step
  (`give the connection an address outside the Lakana stack (lakana connection update)`).
- **The connection check (`check` op).** Under the docker runner `ConnectionTestLoop`
  (`connection_tests.py`) asks `LauncherClient.check` with exactly the routes a task with that one
  connection would get, built by `python_tasks.connection_plan`, which `_connection_plan` uses
  too (a test compares both per kind): the `egress` pairs of a proxy kind, one relay route per
  endpoint of a database or `tcp` kind (ports from 15001, `relay_port` in the routing), the root of a mounted kind (`root_dir` `/ingest/<root>`). The worker's trust bundle is
  written into a private `attempt-check_*` directory (0700, under the work directory, removed when
  the check ends; `AttemptSweeper` sweeps a dead worker's); the launcher refuses a check naming any
  other attempt, so a check never mounts a task's. The worker claims a test for
  `LEASE_SECONDS` (`CHECK_WAIT_SECONDS` + 30, 120 s), so no other worker retakes a running check. The launcher (`Launcher.check`,
  `check_container_spec`) runs the check the task image carries, `CHECK_ENTRY` =
  `/opt/lakana-check-env/bin/python -I -B /opt/lakana-check/check.py`; the request has no image, command,
  environment, code or limit field, only `python` from `LAUNCHER__IMAGES`. The container runs as the
  task user with the fixed `CHECK_LIMITS` (256 MiB, 0.5 CPU, 64 pids, 30 s, kill grace 1 s), a
  16 MiB `/tmp`, no `/code`, `/env` or `/work`, only the attempt's `trust` subpath and the allowed
  roots, read-only. Its network holds no server (the connection and its secret travel on stdin): the
  egress service joins with a token of purpose `connection-test` when it has pairs, minted after the
  image pull, and its relay starts when it has routes; `stop`, the abandon watcher and `cleanup`
  treat a `check` name and its relay like a run's. A check's relay plan has `verdicts: true`: the
  relay reports a successful dial (`connected HOST:PORT`) as well as a refusal, and the launcher
  waits up to `RELAY_VERDICT_SECONDS` (20) for that verdict before it removes the relay, joining
  its reader before it closes the stream. The worker keeps at most 64 KiB of output; a
  `lakana-relay: refused` line decides the outcome first (source refusals map to `dns`, `refused`,
  `timeout`, `not_allowed`; the upstream proxy, the routing table and unknown texts to `failed`),
  then the last answer line, and through the relay an `ok` without the relay's `connected` line is
  `timeout` (the relay accepts before it dials, so the check's own `ok` proves only the relay); it
  masks the message again before storing it; no output reaches a log line.
  `test_connection_test_outcomes` proves the outcomes per route class on a real engine. A
  refusal, an unreachable launcher or no answer within `CHECK_WAIT_SECONDS` (90) records `failed`
  naming the launcher's log. `/opt/lakana-check/check.py` is a root-owned copy of
  `lakana/ingest/_check.py`, one file that never imports `lakana` or pyarrow: its module-level
  imports are the standard library's, and each credential step imports its driver inside itself
  from `_check.DRIVERS` (`adbc_driver_postgresql`, `adbc_driver_sqlite`, `pymysql`,
  `cryptography`; the `http` step adds `httpx` with its code); the AST guard `test_the_check_imports_only_the_standard_library_and_its_drivers` keeps
  it so. The check environment `/opt/lakana-check-env` is a venv on the image's own interpreter
  holding exactly the root `check` dependency group of `uv.lock` (`dist/check-requirements.txt`,
  exported by each workflow and by `docker_stack.build_task_image` before the build; installed
  `--require-hashes --no-deps --only-binary :all:`; tests derive the group and the image's import
  smoke from `DRIVERS`), owned by
  root and read-only (`chmod -R a-w`), installed before the setuid strip. `-I` keeps the script's
  directory, the environment and user site-packages out of its path (the venv's own site-packages
  stay), and `-B` writes no bytecode. An image without the environment is `failed` with
  `NO_CHECK_IN_IMAGE`: Docker's init exits 127 (`Init` is set), or an engine that starts the entry
  point itself fails the start, which reaches the worker as `LauncherUnavailable` naming
  `CHECK_ENTRY[0]`. Under the local runner the check runs on
  the worker's interpreter; a driver that does not import is `failed` with `UNAVAILABLE`, never an
  `ok`. Credential steps: PostgreSQL logs in through `conninfo` (every value quoted, a NUL refused;
  `sslrootcert` from `root_certificates`, shared with `sql.read`) and executes `SELECT 1` without
  reading it; MariaDB logs in through PyMySQL with the check's own TLS context or `ssl_disabled`
  (never PyMySQL's plaintext fallback), `local_infile=False`, on a socket the check opened (the
  relay under Docker); SQLite opens `file:<path>?mode=ro`. **A GCS key has one destination:** its
  `token_uri` must be `https` and its host and port a `token` endpoint of the connection (core
  gives `oauth2.googleapis.com:443`), else `credentials` and nothing is signed or sent, under either
  runner; the `token_uri` is parsed once, strictly (no whitespace or control character, no user, a
  port that parses and is declared, never 0); the RS256 assertion (read-only scope, 600 s) goes
  there over the check's own `_exchange` (the CONNECT tunnel of `LAKANA_EGRESS_PROXY` when set,
  verified TLS, no redirect followed, 64 KiB read), as the access token goes only to the
  `GCS_URL` listing. The key, the assertion and the token never enter a message, nor does the
  key's `token_uri` (a refusal names at most its host, and a port other than 443). **An `http`
  secret** is checked with one `GET` of the base URL over the same `_exchange`, with the
  connection's `ca_certificate`; a header value outside printable ASCII is `credentials` before
  anything is sent (a line break would add a header); `oauth2` posts the client credentials
  (`client_secret_post`) only to an `https` `token_url` whose host and port are an endpoint of
  the connection (`token`, or `service` for a token URL on the base URL's host and port, which
  core's `endpoints` declares once), else `failed` and nothing is sent; the basic header and the
  access token are masked like the secret. Every message is
  masked and its control characters escaped (`_safe`, a copy of `safe_name` a test keeps equal). No
  module constant of `_check` is named like a credential (a test): CodeQL's
  `py/clear-text-logging-sensitive-data` reads such a name as a secret (alert #66 was a message
  constant named `MARIADB_NO_SECRET`).
- **What a task cannot reach, connections or not**: another task's network (each run its own
  /29; the relay and the proxy refuse the launcher's whole range), the Compose stack (attached
  networks; the relay sits on the outward network, the stack's `default`, so SeaweedFS's 18333 and
  Postgres are refused by construction), loopback in every spelling, link-local and metadata, the
  gateway of every Docker network and the Docker host's well-known names, and the ranges of
  `LAKANA_LAUNCHER__EGRESS_REFUSED`.
- **Proved on a real engine** by `test_docker_task_egress_integration.py` (docker tier, see
  `docs/agent/testing.md`): a declared HTTPS target through the proxy with the trust bundle, a
  database and a `tcp` route through the relay, an undeclared host, the metadata address, the
  stack's server, a service on the egress service's network in no refused range (the
  attached-network rule alone), the default bridge's gateway, a configured range (refused with it, reached
  without), a borrowed token, a client outside the task's network, and teardown after exit, stop
  and timeout. The tests reach their source on a network the proxy and relay are attached to
  through two test hooks that are never settings: `EgressSettings.reachable` and
  `LauncherSettings.relay_reachable` (carried as the relay plan's `reachable`, at most 4 IPv4
  networks, always present and empty in production; `load_settings` refuses a variable of that
  name). A refused range wins over both (`egress.forbidden`'s `refused`), so the hooks cannot
  open a gateway or an operator's range.
- **Residuals, accepted:**
  - containers share the kernel (as for every task);
  - the egress token is in the task's environment: any code of the task can use it for the
    declared pairs, until the run ends (bound to the run's network and lifetime, so a copy is
    useless elsewhere and afterwards);
  - a declared host can receive the declared credentials and anything the task reads; the
    asset's `connections` are reviewed with its code;
  - the Docker host's LAN, VPN and other addresses are reachable unless the operator lists them
    in `LAKANA_EGRESS_REFUSED` (the docs say to, and to bind host services to loopback);
  - `tcp` traffic is encrypted only if the team's driver encrypts it; the relay never reads it;
  - a relay holds 64 MiB and 16 connections per route; a task can exhaust its own relay, never
    another task's;
  - an ingestion root is mounted whole (`_ingestion_mount`): a task reads every path under a root
    its connection names, other connections' paths included; the how-to says to give each group
    of data that some tasks must not read a root of its own.

### The relay (M6)

A task's database connection does not use the proxy (a database speaks its own protocol): one
relay container per run listens on one port per connection and forwards to exactly that
connection's target.

- **Where it runs.** Its own container from the egress container's image id, started between the
  task network and the task: network, relay, task, and on every path the relay is removed before
  the network. Entry point is the launcher's copy under `/opt/lakana-launcher` on the base
  interpreter with `-I -S`; standard library only. It joins the task network at the fixed address
  `relay_address(subnet)` (the /29's last host, alias `lakana-relay`) and the egress service's one
  other network (`_outward_network`; the launcher fails naming them unless there is exactly one).
- **What it listens on.** Only that address, one port per route (plan: `address`, `subnet`,
  1-256 `refused` ranges, 0-4 `reachable` networks of the tests' hook, empty in production, 1-16
  `routes`). It closes any peer outside the /29 and allows at most 16
  connections per port (the 17th is closed with a line naming the limit). A port not in the plan
  is refused by the kernel. A connection whose thread cannot start (`RuntimeError` at the
  process limit) is closed with a line naming the cause and gives its slot back, so the port
  keeps serving.
- **The checks.** Each connection resolves its target once and dials the address that passed
  `dial`, the proxy's shared check: loopback in every spelling the C library reads (`127.1`,
  `0x7f.1`, `2130706433`, octal), link-local and the metadata address, multicast, unspecified,
  reserved, every network the relay is attached to (so the whole Compose stack: it sits on the
  outward network), and the plan's `refused` ranges: every Docker network's gateway,
  `LAKANA_LAUNCHER__EGRESS_REFUSED` and the launcher's whole task-network range, attached or not
  (`relay_plan` adds the range when missing). More than 256 ranges fail the run before the relay
  exists, naming `docker network prune`. A refusal is
  one line, `lakana-relay: refused <host>:<port>: <reason>; give the connection an address outside
  the Lakana stack (lakana connection update)`, which reaches the task's log. An unreadable table
  fails closed.
- **Upstream proxy.** The same check now runs before a company proxy is asked: a numeric host is
  judged (one no resolver reads gets 403), and for tasks, connection tests and the relay a name
  that resolves here to a forbidden address is refused, since a company proxy on the Docker host
  would otherwise reach the host's loopback and the stack. A name that does not resolve here goes
  upstream. Builds keep their own literal rule (`_allowed_host`) and are not name-checked.
- **Limits.** Idle time is a week (a database may be silent through a long query; the proxy keeps
  five minutes). Container: user 65534:65534, `CapDrop: ALL`, read-only root, no mounts, 1 MiB
  `/tmp`, 64 MiB, 0.25 CPU, 290 pids (`RELAY_PIDS`: 16 routes of 16 handler threads and one
  accepting thread each, the main thread, Docker's init, 16 to spare), no-new-privileges as
  detected, labelled kind `relay` with the
  run's id, owner and deadline (the reaper removes it like any labelled object; `cleanup` spares
  the relay of a live run). It must report `lakana-relay: ready` within 10 s or the run fails with
  `the task's relay did not start: <its last line>` and no task container exists.

## The artifact store (M5c2b)

User documentation: `docs/site/reference/artifacts.md` and `docs/site/explanation/artifacts.md`. The
threat model's section "Artifacts (M5c2b)" says what an attacker can and cannot do. This section is
what a reviewer of code in `lakana_core.artifacts`, `lakana_server.api.platform.artifacts` and the
SDK's artifact modules has to know. The paragraphs "Artifacts and bound tokens" and "Reading an
artifact" under "Python tasks and bound task tokens" above belong to it.

**The write path: three requests, three authorizations.** All of it is `ArtifactService`
(`lakana_core.artifacts.service`); the routes only move bytes.

1. `POST …/uploads` → `begin_upload`. Order: authorize `WRITE_TABLE` on the name, `_require_attempt`
   (a bound scope without a run context is refused before the guard), `CREATE_TABLE` if the name
   is new, `ProtectedWriteGuard.check(ARTIFACT_VERSION)`, the kind of the name, then
   `parse_manifest` (limits). Only then the row is created and the artifact lock taken. The route
   runs `check_can_upload` before it reads a body of up to 16 MiB, so an unauthorized caller
   costs a lookup. It stores the upload (principal, task run and claim, expiry), bounded to
   `MAX_OPEN_UPLOADS = 16` per artifact, and `record_pending` runs in the same transaction.
2. `PUT …/blobs/{sha256}` → `receive_blob` authorizes `WRITE_TABLE` and **calls no guard**
   . A superseded attempt can therefore store bytes until its upload expires; it cannot
   commit them (see the threat model). The body is hashed on the way to storage;
   `BlobUpload.finish` verifies size and hash, takes the artifact lock and, under it, makes the
   object visible and inserts the `artifact_blobs` row.
3. `POST …/commit` → `commit_upload` authorizes `WRITE_TABLE`, `_require_attempt`, validates the
   sources, `guard.check`, reads each source's `READ_TABLE` (`resolve_lineage_sources`), takes the
   lock, opens the upload (same principal, and for a task the same task run and claim), checks that
   every file has a row, numbers the version above every row and every manifest object storage has
   seen, inserts the version, the lineage edges and the audit event, prunes, and calls
   `record_pending` **inside the locked transaction**, so a reclaim or a cancel that lands in between
   is seen (`FOR SHARE`). A repeated commit by the same caller returns the same version and writes
   nothing; it ignores its `sources`.

`abort_upload` authorizes `WRITE_TABLE`, takes the lock and deletes the caller's own open upload row;
it calls **no guard** on purpose (it writes no version and no byte, and a person who began
an upload under break-glass must not need a second reason to withdraw it). A superseded attempt can
withdraw the upload begun under its own claim.

`list_uploads` authorizes `READ_ENVIRONMENT`, then lists only the caller's own open uploads:
same principal and the same `(task_run_id, claim)` pair `abort_upload` accepts, so a task attempt sees
its own and a person never sees a task's. A row whose artifact the caller may not `WRITE_TABLE` is
left out. It never reads a manifest and has no "all principals" form. `delete_version` names a
version number in a kept group only if the caller `permits` `READ_TABLE` on the artifact, and merges
the groups when it does not, so the split by version does not leak; it names no upload, principal or
path.

Two things the delete answer newly tells are **accepted, not closed**. (a) A caller with
`DROP_TABLE` and without `READ_TABLE` learns that some other live version shares N files and B bytes
with the deleted one: that another live version exists and, with a file size they may already know,
whether the file survives. The 16-place cap per artifact already says as much about others'
uploads and the deleter already got the deleted version's size, so the added bit is small.
(b) Any deleter learns that somebody has an open upload naming files of this version (a count and
bytes, no upload, principal or path). `list_uploads` is a new route (`GET /environments/{env}/artifacts/uploads`)
that is not a new disclosure: it returns only the caller's own open uploads, filtered in SQL (see
above), and a malformed cursor is a 422 that echoes nothing of it.

**The lock, and the two orders that must never be reversed.** `lock_artifact` is a no-op
`UPDATE tables SET name = name` on the artifact's row: a row lock on Postgres, the write lock on
SQLite. Commit, blob finish, deletion, drop and each sweep step take it before any read their decision
depends on, and read with `populate_existing`.

- **Rows before bytes when deleting**. The version is marked and the `artifact_blobs` rows
  of files that no live version and no unexpired upload names are removed in one locked
  transaction; only then is each object deleted, one at a time, under the lock again and only while
  the file still has no row. A crash leaves bytes without rows (the sweep removes them), never a row
  without bytes. Do not delete an object first.
- **A file becomes visible and gets its row under the same lock**. While the lock is held,
  an object named by a hash with no row is garbage and needs no grace period. Do not call
  `StreamWriter.commit()` outside `BlobUpload.finish`.

Pins: `test_artifacts_store.py` (`test_the_lock_matches_only_an_artifact`),
`test_artifacts_service.py` (`test_the_lock_is_taken_before_a_file_is_checked_and_made_visible`,
`test_a_superseded_attempt_cannot_commit_a_version`,
`test_a_claim_reclaimed_between_the_check_and_the_lock_is_refused_under_lock`),
`test_artifacts_deletion.py` (`test_a_file_stored_again_before_the_bytes_are_removed_is_kept`,
`test_a_delete_between_the_lookup_and_the_lock_of_a_commit_loses_no_file`,
`test_deleting_a_version_keeps_a_file_another_version_shares`,
`test_an_open_upload_protects_its_files_from_deletion`), `test_artifacts_sweep.py`
(`test_the_sweep_never_removes_a_file_a_live_version_or_open_upload_needs`).

**What builds a storage path.** Only `blob_path(artifact_id, sha256)` (accepts exactly 64 lower-case
hex digits), `manifest_path(artifact_id, version)` (takes an `int` the service chose),
`artifact_prefix` and `dropped_marker_path`, all from an artifact **id**. Never a name, a manifest
path or a caller's path: a manifest path is a label that no storage call receives. A stored manifest
is untrusted too (`checked_hashes` raises `UnreadableManifest`, which stops every deletion for that
artifact). Pins: `test_a_damaged_manifest_never_becomes_a_storage_path`,
`test_a_hostile_manifest_path_is_never_echoed_raw`,
`test_no_storage_path_reaches_a_response`. The directory `artifacts/` of a storage root is reserved:
`ResolvedStorage.confine_to_root` refuses a table location inside it, so no table's data can be taken
for an artifact's.

**Bound-token rules for artifacts.** `BoundTask.output` and `BoundTask.keep_versions` come from the
project version manifest through `entry_output` and `entry_keep_versions`, as untrusted input. The scope
rules are the table rules (`READ_TABLE` on declared inputs, `CREATE_TABLE`/`WRITE_TABLE` on the own
asset, no `DROP_TABLE`); the guard adds the output kind (a table task cannot add a version, an
artifact task cannot write a table) and refuses `DELETE_ARTIFACT_VERSION` and `DROP_ARTIFACT` outright.
`BoundTask` has no input **kinds**: a token can download an artifact at any declared input name,
even one declared as a table (accepted, see the threat model). Pins:
`test_grants_authorization_bound.py` (`test_a_bound_token_reads_only_declared_artifacts`,
`test_bound_actions_are_the_task_token_actions_without_drop`),
`test_identity_bound_task_tokens.py` (`test_a_bound_scope_carries_output_and_keep_versions`),
`test_runs_lockdown.py` (the `_TABLE_REFUSAL` and `_ARTIFACT_REFUSAL` tests).

**No task token deletes.** `ArtifactService._refuse_run` refuses every principal with a task scope
(bound or not) or a run context, after authorizing `DROP_TABLE`. **`_refuse_run` is the only layer
that covers every form**; the others cover some:

| Caller | Refused by |
|---|---|
| Bound task token | its role (`BOUND_TASK_TOKEN_ACTIONS` has no `DROP_TABLE`, so authorization fails first), `_refuse_run`, and the guard in every environment (the destructive operations are not materialising, and a claim-form context is limited to its own asset's materialising writes) |
| Unbound task token (minted over REST) | `_refuse_run` alone in an unprotected environment: `TASK_TOKEN_ACTIONS` contains `DROP_TABLE` (as for tables), and the guard returns early for a caller without a run context. In a protected environment the guard refuses it too (a task token cannot hold `MANAGE_ENVIRONMENT`, so no break-glass reason can pass) |
| The worker's query-run form (a run context without a claim) | `_refuse_run` alone in an unprotected environment, because the guard's verdict for that form is `True` there (`own_asset_only` is off). In a protected environment the guard refuses it |

`DATA_ENGINEER` carries `DROP_TABLE`, so a person with that role may delete: only a run is refused. Pins: `test_a_task_token_of_any_form_never_deletes`,
`test_deleting_needs_drop_table_and_a_task_token_never_has_it`.

**Retention is decided from the manifest**. `_prune` reads `p.task_scope.bound.keep_versions`
and nothing else: never a request, a setting or a stored manifest. A person, a service account and a
token without a bound scope prune nothing. It runs inside the commit's transaction as the committing
principal and needs no `DROP_TABLE`: **whoever may deploy an asset controls its history, and there is
no way back.** Pins: `test_keep_versions_prunes_at_commit_as_the_same_principal`,
`test_a_retention_that_is_not_a_positive_number_prunes_nothing`.

**Authorize before lookup, and what each error may contain**. Every method authorizes on
the table-scope securable of the name before it looks anything up; listing authorizes
`READ_ENVIRONMENT` and shows only artifacts for which `permits(READ_TABLE)` holds. A caller without
the privilege gets the same 403 for an artifact, a version and a file that exist and that do not. No
message for one principal contains a name, hash, size or version of an object it may not read. The
exceptions are the writer's own: `begin_upload` says `a file with this SHA-256 is already stored
with N bytes, not M` and returns `missing`, which tells a writer which hashes **its own** artifact
holds (accepted: per artifact). Worker side: `_resolve_inputs` asks `permits(READ_TABLE)`
as the run principal before it looks at the catalog; for an unreadable artifact input it fails with
one text that holds whether or not the object exists. **One exception, a residual:** a grant revoked
after the worker resolved the version and before the harness downloads it lets the server's 403
reach the task as `reading input 'x' (name, version N) failed: ...`, which names a version that the
principal could read when it was resolved (threat model, "A grant revoked after the worker prepared
a task"). Pins:
`test_an_unreadable_artifact_and_a_missing_one_are_refused_alike`,
`test_a_caller_without_read_gets_the_same_answer_for_missing_and_existing`,
`test_a_caller_without_write_gets_the_same_answer_for_missing_and_existing`,
`test_an_input_the_run_principal_may_not_read_answers_as_one_that_does_not_exist`,
`test_artifact_service_authorization.py`.

**The consumer's copy (`lakana._artifact_tree.TreeWriter`).** The SDK treats the server's manifest as
hostile: its own copy of the path rules, every path checked again, each parent's real path inside
the target, files opened `O_CREAT | O_EXCL | O_NOFOLLOW` under temporary names and renamed after
size and SHA-256 match, a staging directory moved into place, nothing left at the target on failure
. A person's copy is 0600/0700; a task's input is sealed 0400/0500 by descriptor
before the rename. Pins: `test_artifact_tree.py` (`test_a_link_inside_the_tree_is_never_followed`,
`test_sealing_never_follows_a_link_put_into_the_tree`,
`test_a_staging_directory_swapped_for_another_one_is_noticed_before_the_rename`,
`test_download_refuses_when_the_disk_is_too_small`).

**The scan (`lakana._artifact_manifest.scan`).** It walks a local tree by descriptor
(`O_NOFOLLOW`, no path is looked up twice), refuses links, devices, sockets, pipes and files
hard-linked from outside, a name the server would refuse, and a file that changes
while it is read; it counts entries before it hashes anything, so a wrong path (`/`) ends in
moments; it hashes while it reads, and the upload opens the file again below its directory and
re-hashes what it sends. Everything in a directory is stored, **hidden files included**. The rules
exist twice, parity-tested. Pins: `test_artifact_scan.py`
(`test_scan_refuses_a_symbolic_link_and_names_it`, `test_scan_refuses_a_hard_link_from_outside`,
`test_a_directory_replaced_by_a_link_during_the_walk_is_not_followed`),
`test_artifact_manifest.py`, `test_artifacts_manifest.py`
(`test_a_path_that_leaves_its_directory_is_refused`,
`test_paths_that_collide_after_folding_are_refused`).

**The sweep (`lakana_core.artifacts.sweep`).** It deletes bytes unattended. What a restore or a
failover would leave behind is kept (a prefix without a row and a marker; a manifest object the
database does not know, and the files it names), but a file without a row is not safe: it goes once
it is older than seven days, or at once when a deleted version's manifest names it, unless a manifest
object the database does not know, a live version's manifest or an unexpired upload names it (one check per batch with `unreferenced_hashes`, the files kept are counted in `named_files_kept` and
logged once per artifact; from a consistent catalog that state cannot arise). Absence of a row alone is no reason; age and a deleted
version's manifest are. For an artifact with a row: expired uploads;
blob rows that no live version, no unexpired upload and no manifest object unknown to the database
names, then their bytes; an object without a row only if a deleted version's manifest names it or it
is older than `ORPHAN_BLOB_GRACE` (7 days); `.part` objects older than `max(upload TTL, 3600 s)`;
`manifests/<n>.json` only for a version whose **row** has `deleted_at`. A manifest object the
database does not know protects its files (at most `MAX_UNKNOWN_MANIFESTS = 20` objects of at most
16 MiB; more or unreadable leaves the artifact alone for the run) and is warned about. A prefix
`artifacts/<id>/` without a row goes only with the `.dropped` marker that `drop_artifact` writes
first and deletes last. A live manifest that cannot be read leaves that artifact whole. It never
deletes outside `artifacts/<id>/` and decides each deletion again under the lock. Bounds: one
single-flight thread of the worker that leads, a 300 s deadline shared between
environments, five consecutive failures give an environment up for the run, listings are iterated
never collected. `LAKANA_RUN__SWEEP_INTERVAL=0` turns it off and the worker says so at start. Pins:
`test_artifacts_sweep.py`, `test_artifacts_sweep_evidence.py`
(`test_a_restored_database_does_not_cost_the_versions_it_forgot`,
`test_a_marked_rowless_prefix_goes_and_its_marker_goes_last`,
`test_a_rowless_prefix_without_a_marker_is_left_alone_counted_and_reported_once`).

**Residuals of the artifact store, accepted.** Each is stated with what an attacker needs in the
threat model's "Artifacts (M5c2b)"; the list here is for reviewers, so that nobody "fixes" one by
accident or believes it closed:

- A reader of a model may infer its training data; row filters and column masks cannot apply to a
  file, and a grant on a namespace covers its models.
- A pickled artifact executes code in the consuming task; whoever can store a version (deploy on the
  producer, or `WRITE_TABLE` on the name outside a protected environment) can run code in every
  consumer that loads it.
- `BoundTask` has no input kinds (see above).
- The one bit: the kind of an unreadable object that another repository deploys is visible to
  a deployer who names it (N4: a repository's manifest is an input to the kinds that other
  repositories record whenever their deployer cannot read the object). Squatting an unclaimed name
  existed before artifacts.
- `receive_blob` has no guard call: a superseded attempt can store bytes until the upload expires.
- A download that fails mid-stream is still audited (the audit comes before the first byte);
  `ARTIFACT_DOWNLOADED` is one row per principal and version within the upload lifetime, not per
  file.
- A delete request that timed out at the client can still reach the object store after the
  artifact's lock is released; the same holds when the server ends the database session during a
  slow delete. A failed storage removal leaves a deleted version's files and its manifest object
  until the sweep.
- One reader of a large artifact can hold all 32 download slots of a process (no per-principal
  share of the download slots exists; #12 is about append slots and a minimum upload rate). One
  writer can hold the upload slots (`LAKANA_APPEND__MAX_CONCURRENT`, shared with table appends): a
  blob `PUT` keeps its slot until its upload expires, at one byte per read timeout, and the
  writer can renew it; downloads have a minimum rate, uploads have none.
- A slow writer holds one of the two manifest decode slots while its body arrives (the read
  timeout at most); a principal holds at most one at a time, and a request that finds
  none free, or its principal's slot taken, gets 503 before its body is read.
- The sweep restarts an artifact's row pass at every run with a fixed time share: an artifact
  with more than about 200,000 distinct files is never finished (see the sweep module's "Known
  limits"; its summary line says so at the time limit).
- The worker's INFO log is visible to whoever reads its stderr: artifact names of
  every environment in the sweep's lines, and DuckDB's text for a refused spill file. No SQL, no
  parameter value, no token: with one exception since M5c3a: for an internal error of a query
  process the worker logs a WARNING with the end of that process's stderr (at most 2,000 bytes,
  escaped), which for an internal error of DuckDB itself holds DuckDB's message, the text the
  submitter is shown as well.
- This also means consumers outside the run (another run, a download) read a failed
  attempt's version as the current one.
- No disk quota on a task's output directory beyond what any task code can already write; the
  consumer's download checks the free space before it writes.
- An interrupted multipart upload leaves parts a listing does not show; the bucket's rule
  `AbortIncompleteMultipartUpload` is the control, and the operator sets it. A drop leaves empty
  `blobs` and `manifests` directories.
- A blob row whose bytes were lost from storage is not detected until a download fails; after a
  restore to a moment before a delete, re-running an asset with unchanged output does not repair it
  (the row says the file is stored); every version that has the file must be deleted first, and with
  manifest objects the database does not know a sweep must run before the re-run (and one that names
  the missing file must be removed).
- `keep_versions` deletes without `DROP_TABLE`, and a run that stored a version and then failed
  leaves it live with the previous one already pruned when `keep_versions=1`.
- The harness's residuals, below under "Residuals of the harness".
- Deleting an environment keeps its artifact rows, hidden, until the location sweep removes the
  environment's row after the undo period (#213); the rows then cascade and the bytes stay in
  storage, without a marker, so no sweep deletes them.
- Never point two deployments at one storage root: each would see the other's artifacts as leftovers
  (the marker rule keeps the sweep from deleting them, but nothing else protects them).
- `list_artifacts` and the version list are not paged; the download route answers `GET` only and
  ignores `Range`.

**Residuals of the harness (#67).** The harness uploads with `os.fchdir` to the pinned
directory and re-checks by path that `inputs` is still the directory it made before it downloads into
it. Both hold against code that swaps a directory **by name**, not against code of the task that runs
at the same time: a thread of the asset's code that calls `os.chdir` during the upload redirects the
scan (in the review's experiment in 76 to 86 % of bounded attempts), and one that swaps `inputs`
between the re-check and the download is not stopped (the asset's module is imported, so its code is
already running). It adds nothing to what in-process code can do with the task's own rights. Closing
it needs the scan, the upload and the download to work by directory descriptor (`dir_fd`): #67. Words
in code, messages and docs stay "by name" and "a check, not a guarantee"; never write that a link "is
never followed" without that qualification.

## Table locations: the claim, the fence and the sweep (#90)

User documentation: "The sweep" and "After a restore" in `docs/site/how-to/deploy-with-compose.md`.
This is the first part of the data-deletion design (drops as tombstones #94 and retention #87 build
on it). The code deletes table data unattended; the rule is the artifact store's: **a record exists
before the first file, and a directory without a record is never deleted.**

**The record.** `table_locations` (`lakana_core.models.table_location`): one row per managed
location, unique by absolute `location`, `CLAIMED | LIVE | DROPPED | ABANDONED`, the holder run,
`claimed_at`, `purge_after`. `table_id` has no foreign key on purpose (the record outlives a dropped
row); `environment_id` cascades, but a deleted environment's row stays until its last record is
gone (#213, below), so its tombstones are purged like any other. Migration `0042` backfills `LIVE` for every `ICEBERG` row with a metadata
file. A drop makes the record its tombstone (`DROPPED`, below); `0043` adds `dropped_table` and
`purge_started_at`.

**The claim.** `_prepare_table` ends with `claim_location`, which inserts and **commits** the
`CLAIMED` record; validation and refusal come before it, so a refused create claims nothing. Both
creates use it: `create_table` (the platform route and the Iceberg REST create, which writes metadata
before the row exists) and a create-mode write (`_write_new_table`, data files first). The holder is
`claim_holder`: the query run, else the worker's fenced run, else the run context's query run or task
run; `None` for a person.

**The fence.** `_publish_table` runs `UPDATE table_locations SET state='LIVE', table_id=… WHERE
location=… AND state='CLAIMED'` in the transaction that inserts the row and raises `ConflictError`
(`CLAIM_RELEASED_MESSAGE`) after a rollback when no row moved. The sweep moves a claim to
`ABANDONED` only under the same row's lock (`lock_location`, a no-op `UPDATE`: a row lock on
Postgres, the write lock on SQLite) and reads the row again under it. Whichever commits first wins;
the other sees it. Pins: `test_table_locations_postgres_integration.py` (both orders, two sessions,
two threads), `test_table_locations.py` (`test_the_fence_refuses_a_create_whose_claim_was_abandoned_meanwhile`,
`test_publish_location_moves_only_a_claim`), `test_table_location_sweep.py`
(`test_a_claim_published_between_the_listing_and_the_lock_is_left_live`,
`test_a_claim_abandoned_by_the_sweep_refuses_a_later_publish`).

**A failed create.** `_settle_failed_write` decides, for `create_table` too now. Known not to have
committed (a `LakanaError`, or anything before the commit was sent): `_release_location` makes the
claim `ABANDONED` (only from `CLAIMED`), deletes the files (`cleanup_location`, only objects strictly
below the location), and deletes the record only when every file is gone; a record that says `LIVE`
keeps every file. In doubt: the claim stays `CLAIMED`, the files stay, `WriteOutcomeUnknownError`
says the clean-up removes them once it has proven the commit did not land. A commit that landed
although it raised returns the table.

**The sweep** (`lakana_core.catalog.location_sweep`, in the worker's housekeeping, single-flight,
the artifact sweep's deadline, rotation and failure cap; `LAKANA_RUN__SWEEP_INTERVAL=0` turns it off
and the start-up line names it). Step 1 (expiries) is retention's: see "Retention" below. Step 2: a claim is resolved only when
its holder (a `QueryRun` or `TaskRun`) is in a final state, or, with no holder or one the catalog
does not know, after `LAKANA_TABLES__ABANDONED_GRACE_HOURS`; under the lock, `LIVE` if a `tables`
row's metadata lies under the location, else `ABANDONED` with `purge_after = now` and
`TABLE_LOCATION_ABANDONED` (no principal). Step 3: an `ABANDONED` or `DROPPED` record past
`purge_after` is purged only if its location is canonical, strictly inside `managed/` of its
environment's root and ends in `-<32 hex>`, and nothing in use is the location or lies inside it in
**any** environment (`location_in_use`: `LIVE`/`CLAIMED` records and `tables` rows; plus
`table_root_locations` with `nested_locations_within`; maintenance uses `other_table_locations`), and
`near_other_location` finds nothing, checked after those two (no other environment's root at,
inside, or from at or inside `managed/` around it, and none holding this root in its
`UNRECORDED_ZONES` (`raw/`, `artifacts/`, `_artifacts/`, whose files have no record); no `LIVE`/`CLAIMED`/`DROPPED` record other than its
own, and no table directory, at or inside it under any of the `readings` described in the #229
section below; an unreadable location on the same scheme fails closed): the spelling comparisons
above fail open on another spelling of the same place, so the purge counts `purges_blocked` and
logs `UNJUDGED_MESSAGE` instead. Before **each** delete,
under the record's lock, the record must still be purgeable and `location_in_use` false again; only
objects strictly inside the location with sane segments are deleted; the record goes last, with
`TABLE_PURGED` (`files`, `bytes`, `complete`); a pass that stops part-way records `complete: false`
and keeps the record. Directories under `managed/` that end in a table id, hold files and have no
record are counted (`unrecorded_directories`) and logged once per root and run, never deleted.

**Residuals, accepted.** A create whose claim was abandoned (its run ended, or the grace passed)
while its process still writes, for example a frozen worker that wakes up, can add files after the
purge: its commit is refused by the fence, and the files become a directory without a record. A
namespace created inside an abandoned location between the last check and a delete is not seen. A
delete in flight can outlive the lock when the database ends the session. After a restore, a create
that was in flight at the backup has a `CLAIMED` record: once the scheduler ends its run, the sweep
removes its files even if the table was finished after the backup (the docs tell the operator to
keep the sweep off and copy such directories first).

### Drops are tombstones; undrop restores exactly what the drop took (#94)

User documentation: `docs/site/how-to/drop-and-restore-a-table.md`. Code:
`lakana_core.catalog.tombstones`, `CatalogService.drop_table`/`undrop_table`/`list_dropped_tables`.

**The drop** (platform `DELETE`, `?purge=true`, the CLI, the Iceberg REST `DELETE` with either
`purgeRequested`) does, in one transaction: lock the `tables` row (a no-op `UPDATE`, so a commit
landing between the lookup and the delete is in the snapshot, not lost to it), take `now = db_now`,
`revoke_attached(..., table_id, now=now)` (every revoked row is stamped with exactly `now`), then
`tombstone`: only for an `ICEBERG` row whose derived root has a record that is `LIVE` **and names
this table's id**, under `lock_location`: `DROPPED`, `dropped_at = now`, `purge_after = now +
LAKANA_TABLES__UNDO_DAYS` (`now` for a purge or `UNDO_DAYS=0`), `revoked_policy_ids` = the
`RevokedPolicy` list, `dropped_table` = every mapped column of the row, of its `column_descriptions`,
of its `table_columns` and of its `table_references` (the rows its id cascades to); then
`TABLE_DROPPED` and the row delete.
The drop itself deletes no file any more (`purge=True` used to delete the metadata file in the
request). No tombstone, files kept: an `EXTERNAL` table, a view (no location), a table whose root's
record names another table (two legacy tables sharing a directory). A `DROPPED` record of another
table at the same root gets the later `purge_after`, never an earlier one, so the second table's
period is honoured too.

**The undrop** authorizes `DROP_TABLE` on the namespace scope (never on the old name's table scope:
a new table of that name must not lend its table grants) and `CREATE_TABLE` on the target name,
then the guard as `WriteOperation.CREATE_TABLE` (not a task's operation, so every task run context
is refused). It finds the newest `DROPPED` record of the name in the namespace, takes
`lock_location`, re-reads it, and refuses (rolling back first): not `DROPPED` or no snapshot (404),
`purge_started_at` set (409), `db_now >= purge_after` (409), namespace path gone (404), name taken
(409, `--as`), metadata file gone (409, `FILES_GONE_MESSAGE`). Then, in one transaction: the row from the snapshot (same id and metadata
location, namespace by path, the new name; an owner or description author deleted since becomes
`NULL`), its descriptions and columns, its declared references (with the environment of the
namespace it comes back into; a tombstone from before #250 has none), `restore_policies`, the record back to `LIVE` (cleared),
`TABLE_UNDROPPED` with the restored `{kind, id}` list, the guard's `record_pending`, commit. An
`IntegrityError` (a create won the name between the check and the insert, or the namespace was
dropped) rolls back and is reported as taken, or as a conflict to retry.

**Exactly the revoked policies.** `restore_policies` un-revokes a listed id only while its
`revoked_at` equals the record's `dropped_at` (the drop's own stamp), and sets `table_id` back (the
foreign key set it `NULL`) and the display-only `namespace`/`table`. A row revoked before the drop
was never listed; a row deleted since (its principal or group was deleted, cascade) is skipped; a
row stamped again since stays revoked. Revoking an already-revoked row is a no-op in the services,
so nothing else can change a listed row.

**The purge/undrop lock.** The sweep's `purge` first checks `location_in_use` (a restored table is
in use), then `_start`: under `lock_location`, the record must still be purgeable with
`purge_after <= now` (an undrop and a new drop since the listing moves it out), and it commits
`purge_started_at` before the first delete; each delete then rechecks the state under the same lock.
Undrop takes that lock first and checks the mark. So either undrop commits first (the sweep finds
`LIVE`, or a later `purge_after`, and deletes nothing) or the mark is committed first (undrop is
refused, even if the pass stops part-way: a started purge is never undone). The sweep's `now` is the
database time at its start, never later than the undrop's `db_now`, so a record the sweep finds due
is also past its period for an undrop. Pins: `test_table_undrop.py`,
`test_table_location_sweep.py` (`test_the_purge_is_marked_started_under_the_lock_before_its_first_delete`,
`test_a_record_no_longer_due_when_the_purge_starts_is_left_alone`), and the two-thread Postgres
tests in `test_table_locations_postgres_integration.py`.

**A dropped table nested inside another location.** `location_in_use` counts only `LIVE` and
`CLAIMED`, so `dropped_inside` (any `DROPPED` record strictly below the location, in any
environment) is checked too: before the mark, and again under the lock before each delete and
before the record goes. The outer purge then waits (`purges_waiting`, one warning per record and
run), stays due, and is retried by every later sweep until the nested table is restored (`LIVE`,
which blocks it as before) or purged itself. A nested `ABANDONED` record does not block: nobody can
restore it and its own purge is already due. Maintenance reads the other tables' locations with
`other_table_locations`: the `LIVE`, `CLAIMED` and `DROPPED` records and the roots of the other
`tables` rows, in **one** `UNION ALL` statement, so an undrop (DROPPED to LIVE plus the row) at the
same moment is in that snapshot either way; two queries let it fall between them. A dropped table
nested under the maintained table's `data/` is therefore never taken for its orphans. A record
leaves those states only through a purge, which deletes the files before the record, or as a
claim that never published turning ABANDONED (no table's data), and a create
claims before its first file, whose files are younger than the orphan cutoff; so maintenance does
not re-check per file under a lock (the sweep does, because it has no cutoff). Pin:
`test_a_nested_table_restored_while_maintenance_lists_locations_keeps_its_files`. As a last guard, undrop refuses
with `FILES_GONE_MESSAGE` when the snapshot's metadata file no longer exists, before anything is
written. Pins: `test_a_dropped_table_nested_inside_blocks_the_purge_until_it_is_restored`,
`test_the_outer_purge_follows_once_the_nested_drop_is_purged_itself`,
`test_a_table_dropped_inside_during_the_purge_stops_it`,
`test_a_nested_dropped_tables_files_are_kept_for_its_undo_period`,
`test_an_undrop_whose_metadata_file_is_gone_is_refused_and_restores_nothing`.

**Residuals, accepted.** A purge that is blocked after its mark (something in use appeared inside)
leaves a tombstone that can no longer be restored although its files are whole. The read history
(`table_reads`) of a dropped table is not restored. A grant to a group or principal deleted during
the period is gone with it. Tables dropped before this release have no tombstone; their directories
are only counted (`unrecorded_directories`).

### Retention: who may shorten, and the expiry by the system (#87)

User documentation: `docs/site/how-to/let-tables-expire.md`. Code: `lakana_core.catalog.retention`,
`CatalogService.set_table_retention`/`table_retention`/`set_environment_retention`/
`list_expiring_tables`, step 1 of the location sweep (`expire_due`).

**Rights (Q8 A).** `set_table_retention` computes the expiry before (`planned_expiry`, the rule
that applies now, exclusions ignored) and after (the new rule counted from now, since the change
bumps the row's `updated_at`). Shortened means: the new rule gives an expiry and the old one gave
none or a later one. The owner (`owner_id == p.id`) may do anything; otherwise a shortened change
authorizes `DROP_TABLE` on the table and any other change (later, or `never`) `WRITE_TABLE`. The
authorization goes through `authorize`, so a refusal is audited `AUTHORIZATION_DENIED` like every
other. A name with no table authorizes `WRITE_TABLE` before the 404, so a caller without it learns
nothing. The protected-environment refusal (`days`/`on` there) and the not-managed refusal come after
the authorization. `never` is allowed in a protected environment; like descriptions, retention is
catalog metadata, not data, so `ProtectedWriteGuard` is not called (an expiry never happens there
anyway). `set_environment_retention` authorizes `MANAGE_ENVIRONMENT` on the environment scope (so an
instance grant covers it), refuses `days` in a protected environment, and audits
`ENVIRONMENT_RETENTION_SET`. Audits: `TABLE_RETENTION_SET` (`days`/`on`), `TABLE_RETENTION_CLEARED`
(`never`), each with `shortened`, `previous` and `expires_at`, in the change's transaction.

**The clock (Q2 B).** Last activity = max(`created_at`, `updated_at`, the newest
`TableRead.last_read_at`). `updated_at` is the row's own `onupdate`: every commit's compare-and-swap
(`catalog/commit.py`) moves it, so do maintenance, a description or retention change, and an undrop
(which sets it to now). The snapshot's `timestamp-ms` is **not** used: the committing client writes
it, and a client that wrote an old one could make a table due at once. A rule by days counts from
the later of the activity and the rule's `retention_set_at` (the table's, or the environment's for
the default), so enabling a retention gives every table its full period. `on` is midnight UTC of the
day, whatever the activity; `check_rule` refuses today or earlier. Reads through Iceberg REST are not
recorded, and reads older than `LAKANA_TABLE_READS__KEEP_DAYS` are swept, so neither keeps a table.

**The expiry.** `expire_due` lists the candidates of one environment (managed, not `never`, with a
rule of their own or the environment's), computes each expiry with one query for reads, one for the
deployed manifests and one for names, and calls `expire_table` for the due ones, most overdue first,
up to the run's remaining `LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP` (shared by every environment), each
behind the sweep's `_tick` (deadline, shutdown) and `_guarded` (failure count). `expire_table` takes
the **environment's** row lock and then the **table's** (`lock_row`, a no-op `UPDATE` that keeps
`updated_at`), so a protection, a retention change or a commit in another transaction either
committed first and is seen, or waits for the drop. Under the locks it decides again from the
database: environment not protected; row still `ICEBERG`; `describe_retention` (with the deployed
assets read again) says due at the sweep's `now`; no `QueryRun` with a `write_mode` that is `QUEUED`
or `RUNNING` and names the table by `target_table_id` or `target_name` in the environment; no
non-terminal `TaskRun` of the table's dotted name in a run of the environment; and the location record
at the table's root is `LIVE` **and names this table** (`owns_live_record`), so the drop always leaves
a tombstone. A due table that fails one of the last two is counted `expiries_postponed`. Then
`tombstones.drop_row` (the code `drop_table` runs too: revoke with one stamp, tombstone, audit, row
delete) with `actor=None` (no `revoked_by`, no audit principal), `reason: retention`, the rule in the
detail, and `purge_after = now + max(LAKANA_TABLES__UNDO_DAYS, 1 day)`: an expiry can always be undone
for a day. The worker reads `LAKANA_TABLES__UNDO_DAYS` for this (the server and the worker must agree).

**Undrop of an expired table.** The restored row's `updated_at` is set to now, so it counts as
active and is not dropped again at the next sweep; an `on` day that has passed is removed (set by the
undropping principal, `retention_cleared: true` in `TABLE_UNDROPPED`). The snapshot carries the
retention columns; a `retention_set_by` deleted since becomes `NULL`.

**Never expired.** Protected environments (checked under the environment's lock, so protection wins
even over a retention set before it); assets of the current project version of any deployment in the
environment (dotted name in `manifest_assets`; a legacy dotted component can only make more tables
match, never fewer); a write queued or running; external tables, views, artifacts (only `ICEBERG` rows
are candidates); a table whose record is not its own `LIVE` one. Pins: `test_table_retention.py`.

**Residuals, accepted.** A write submitted after the check and before the drop's commit fails against
a missing table (the drop is undoable). A long REST append (no query run) that commits after the drop
is refused the same way. A deploy that adds the table as an asset concurrently can lose the race; the
table is a tombstone that undrop restores. The expiring listing and `table list` compute the clock per
request; the sweep decides again under the locks, so a listing can be stale but never makes a drop.

### Deleting an environment: tombstones, the restore, and the last purge (#213)

User documentation: "Delete and restore an environment" in
`docs/site/how-to/drop-and-restore-a-table.md`. Code: `lakana_core.catalog.environment_deletion`,
`CatalogService.delete_environment`/`restore_environment`/`list_deleted_environments`, and the end
of each environment's pass in the location sweep. Before this, a deletion cascaded every row and
left every file for ever (no record, so no sweep touched them).

**The deletion** keeps its refusals (`MANAGE_ENVIRONMENT` on the environment scope, protected 422,
deployed repositories 409) and runs in one transaction under the environment's row lock (`SELECT
... FOR UPDATE`, re-read). In this order: every `CLAIMED` record of the environment under its own
lock (`LIVE` when a row names it, an in-doubt create that landed, whose table is then dropped with
the others; else `ABANDONED` with the deletion's `purge_after`, so the create's publish fails);
every non-artifact row under its row lock through `tombstones.drop_row` (`reason:
environment_deleted`, the deleter as actor, one `now` for every stamp): a managed table becomes a
tombstone with its own policies, a view or external table leaves none (external files are never
deleted); then `revoke_attached(environment_id)` for what is left (environment and namespace grants,
artifact policies), listed in `environments.revoked_policy_ids`; task tokens of the name are
revoked; the row gets `deleted_at`, `deleted_by`, `purge_after` (`now + LAKANA_TABLES__UNDO_DAYS`,
`now` with `purge`) and `dropped_location_ids` (exactly the tombstones it made). Artifacts and
namespaces stay: the restore needs the namespaces, and an artifact has no tombstone of its own.
`ENVIRONMENT_DELETED` carries `tables`, `tables_without_tombstone` (a managed table whose root had
no record of its own: only uncatalogued, files kept, as a drop does), `purge_after` and `purge`.
`--purge` on a deleted environment moves its `purge_after` and every purgeable record's of it that
has not started to now, audited as a second `ENVIRONMENT_DELETED` with `already_deleted`.

**Hidden everywhere, by name.** `LIVE_ENVIRONMENT` (`deleted_at IS NULL`) is in every lookup of an
environment by name: `CatalogService._find_environment` (with the restore hint only for a caller
who `permits` `MANAGE_ENVIRONMENT`; anyone else gets the missing environment's message), the
`list_environments` filter, `_lock_unprotected_environment`, `identity` (so authorization resolves
no environment id and only an instance grant can apply), `QueryService`, search, secrets, project
lookups, artifact creation, the package-index and repository read checks, the worker's venv build,
and `StorageResolver.for_environment` (refuses one unless `include_deleted`, which only the
location sweep and the restore pass). The artifact and code sweeps skip deleted environments. The
name stays taken (`environments.name` is unique): a create of it is a 409 naming `lakana env
restore` and `lakana env delete --purge`.

**The fence against a create or undrop racing the deletion.** A create claims before its first
file and publishes with `_publish_table`: it flushes the row (SQLite's write lock), takes the
environment's row **share** lock (`lock_live_environment`), refuses with `ENVIRONMENT_GONE_MESSAGE`
when `deleted_at` is set, and only then runs the claim fence. The deletion holds the exclusive lock
while it resolves claims and drops tables, so a publish either committed first (the deletion, whose
listing runs after the claims step, drops its table too) or sees the deletion. A claim inserted
after the deletion (a create that found the environment earlier) is never published; the sweep
abandons it once its holder ended or its grace period passed. `undrop_table` takes the same share
lock before the tombstone's lock; `create_external_table` checks `deleted_at` on the row it locks.
The lock order is always environment row, then table row, then location record. Pins:
`test_environment_deletion.py` (`test_a_create_whose_claim_came_before_the_deletion_is_not_published`,
`test_a_claim_made_after_the_deletion_is_never_published`,
`test_an_undrop_while_the_environment_is_deleted_is_refused`).

**The restore** authorizes `MANAGE_ENVIRONMENT` on the environment scope, locks the row, and
refuses (rolling back, nothing restored): no row (404), not deleted (409), `db_now >=
purge_after` (409), any listed record missing, not `DROPPED`, marked `purge_started_at`, or not
stamped with the deletion's `deleted_at` (409), any metadata file missing (409), a name taken
(409). Each listed record is locked before anything is written, so the sweep's `_start` either
marked one first (refused) or finds the record `LIVE` afterwards. Then each table through the undrop
helpers (`restore_table`, `refresh_restored`, `restore_policies`, `revive`, one `TABLE_UNDROPPED`
with `reason: environment_restored`), `unrevoke` of the row's own list at `revoked_at ==
deleted_at`, the row unmarked, `ENVIRONMENT_RESTORED`. A table dropped before the deletion is not in
the list and stays a tombstone; task tokens stay revoked.

**The last purge.** The sweep lists deleted environments with the others, resolves their storage
with `include_deleted`, expires nothing in them, and never makes a claim in one `LIVE`. After the
environment's purges, `purge_environment` takes the row's lock (`lock_row`), decides again (still
deleted, `purge_after <= now`, no `table_locations` row of the environment in any state), revokes
anything still active, records `ENVIRONMENT_PURGED` and deletes the row; the database cascades
namespaces, artifact rows, secrets, runs and the rest. A record is created only with a claim or by
the removal of directories without a record (its `ABANDONED` reservation, below); either insert
takes the row's key-share lock, so a record cannot slip in between the check and the delete.
A nested or shared-root location still blocks a purge as before (`location_in_use`,
`dropped_inside`, in every environment); the row then waits. Pins:
`test_the_row_stays_until_the_last_record_is_gone`,
`test_a_live_table_in_another_environment_on_the_root_is_never_purged`.

**Residuals, accepted.** An environment-scoped admin who deleted an environment cannot restore it
(the deletion revoked that grant; an administrator can). Views and external tables are not
restored. A view created through a lookup made before the deletion can land in the deleted
environment (no files; restored with it or cascaded with the row). Queued runs of a deleted
environment fail at the worker (the environment is not found). The artifacts' bytes are left in
storage after the row goes. Environments deleted before this release left no row and no record;
their directories are only counted (`unrecorded_directories`) and removed on request (next section).

### Directories without a record: listed, deleted on request (#229)

User documentation: `docs/site/how-to/clean-up-files-left-by-older-releases.md`.
`CatalogService.find_orphaned_locations` and `delete_orphaned_locations`
(`lakana_core.catalog.orphaned_locations`; REST `GET/POST
/api/v1/environments/{name}/orphaned-locations[/delete]`; `lakana env orphaned-locations`) are
the only code that deletes a directory the catalog has no record of, and only when an administrator
asks.

**Who.** `MANAGE_ENVIRONMENT` on `Securable.instance_scope()`, checked before the environment is
looked up: the walk sees every environment that shares the root, so an environment-scoped admin
is refused (and audited as a denial). A deleted environment is accepted by name (its leftovers
are the point); an unknown name is a 404 after the check.

**What is listed.** Only what the sweep's own walk (`_Run.unrecorded_directories`: id-suffixed
directories under `managed/` that hold files, never entering a known location or an id-suffixed
directory) finds, and then only if `_eligible` holds: `_Run._confined` (canonical, strictly inside
`managed/` of this root, ends in `-<32 hex>`); not `_Run._in_use` (`location_in_use` plus
`table_root_locations` equal or nested, every environment); no `table_locations` row in **any**
state at, inside or around it; no table's directory around it (the derived root, or for a metadata
file outside `metadata/` the file's directory); and `near_other_location` (the sweep's check too)
finds nothing: no other environment's storage root at, inside, or (from at or inside this
`managed/`, so also a root that *is* `managed/`) around it, and none that holds this root in its
`raw/`, `artifacts/` or `_artifacts/` (`UNRECORDED_ZONES`; listed as kept), however spelled, and no record or table location in a non-canonical spelling (doubled
slash, `.`/`..`, scheme alias such as `s3a`/`FILE`, percent escape) that normalises to one of
these. Canonical has one definition, `resolver.normalised_path` (empty and `.` segments
dropped, a leading `//` included, which `posixpath.normpath` keeps; `..` unjudgeable), and a `file`
location must be absolute with an empty host: `_is_canonical_uri`, `is_within_location` and
`spelled_canonically` build on it, and `create_environment` refuses a root that is not canonical
(`check_storage_root`, 422 naming the spelling to use; relative and host `file` roots, percent
escapes, glob characters and scheme aliases refused too). It also refuses (#231,
`locations.refuse_root_clash`, 422 naming the other environment, its root, the directory and a
root of its own, `<outer root>/<name>` when that one would be accepted) a root that is, or lies
inside, another environment's `managed/` or `UNRECORDED_ZONES` directory, or whose own such
directory is or holds another environment's root, compared under every `readings` spelling of
both and against every `environments` row, deleted ones included; a shared root (equal under some
reading) and one nested elsewhere in another root stay accepted, and a stored root that cannot be
read at all is skipped (the checks above fail closed on it). `update_environment_storage` cannot
change a root. Existing environments in such a layout are untouched and keep failing closed: the
listing names the other root with `HELD_PREFIX` ("move this environment to a root of its own")
instead of `UNJUDGED_PREFIX`. Pins: `test_environment_root_placement.py`.

**The root of the disk.** `check_storage_root` refuses `file:///` in every spelling (`file://`,
`FILE:///`, `file:////`, `file:///.`, `file://localhost[/]`; `ROOT_DISK_MESSAGE`, 422, suggests
`file:///data/lakana`). Many sites build paths with `root.rstrip("/")`, which turns `file:///`
into `file:`, and `fsspec.core.url_to_fs("file:")` (also `"file://"`) answers the **working
directory**: `FsspecStorage.__init__` (`fsspec_storage.py`), `ResolvedStorage.table_location`
(`file:/managed/...`), `CatalogService` external-table claims and maintenance's `root_prefix`
(`service.py`), `code_sweep._code_uri` and its grouping key, `orphaned_locations.target`, the
worker's `deploy.py` (`code_uri`), `task_integrity.py`, `pipeline.py` and `python_tasks.py`
(root prefixes), and `orphaned_locations._ineligible`'s `spelled_canonically` check. **Residual:**
an environment created at `file:///` before this refusal keeps those spellings: its files land
relative to each process's working directory; the sweep never purges its records (`file:/...`
is not inside `file:///managed` for `_confined`), and the leftovers command lists nothing for it
(`target` keeps `file:`), so its table directories stay; the sweeps of unreferenced code archives
and artifact files still run for it, in the worker's working directory. Do **not** fix one of these sites in isolation: `root_base`
(`resolver.py`, used by `near_other_location`, `child_location`, the sweep's `_Run` and
`root_relative_path` since #231) is safe only because every other site still fails closed on such
a root; giving `orphaned_locations.target` its real root alone would let `--delete` purge
id-suffixed directories under the server's working directory. The fix for users is a new
environment on a directory (upgrade note). `near_other_location` compares every
root, record (`record_states`; never the purge's own) and table directory under all `readings`:
percent-decoded up to three times, case-folded and NFC, `normpath` and, for `file`, `realpath`
(symbolic links), object-store keys literal and with `..` resolved, `abfs` reduced to the
container; a location with no reading (relative or host `file`, no scheme) blocks every deletion on
its scheme. Roots stored before stay and meet this check. New table and namespace names refuse `%`
(`refuse_percent_in_new_name`, called only where such a name is created; stored names stay usable). Residual: two paths to one directory
that only the kernel knows (a bind mount, a network share mounted twice) are not seen; operators
must not point two roots at one directory that way. A canonical other root makes the directory
not listed; any other match lists it as kept, with a reason naming the location. Glob characters
are taken literally (storage names are literal). A directory whose
newest file is younger than `older_than_days` (1 to 3,650, default 30), or has a file of unknown
age, is `recent` and never deleted. The listing writes nothing and records no event.

**A root in another scheme (#260).** `check_root_scheme` (`resolver.py`), called by
`create_environment` before `check_storage_root`, refuses a root whose scheme, lower-cased and
mapped through `SCHEME_ALIASES` (`s3a`/`s3n` → `s3`, `abfss` → `abfs`), is not
`StorageConfig.scheme` (`ROOT_SCHEME_MESSAGE`, 422, names the root, both schemes and a root from
`ROOT_SCHEME_EXAMPLES`). The scheme comparison is total over the text before the first `://`:
`StorageConfig.scheme` is one of `file`, `s3`, `abfs` (the REST schema has the same `Literal`),
and every value but the storage's own (and its aliases) is refused, so `local://`, `local:///`,
`local:///data`, `asynclocal://`, `memory://` and every other fsspec protocol (`gs`, `az`, `hdfs`,
`https`, ...) are refused for every storage scheme, and so are `s3://` for `file` and `file://`
for `s3`. The scheme alone does not judge a root: fsspec reads `a::b` as filesystem `a` over `b`,
and `url_to_fs` keeps only the part before the first `::`, so a chain *after* the scheme
(`file:///data/a/managed::memory:/dev` is `/data/a/managed`; `s3://bucket/x::file:/etc` is S3
`bucket/x`) passed both checks and let a new root reach into another environment's `managed/`
(found in the review of #262: writing through it overwrote prod's files). `check_storage_root`
therefore refuses `::` anywhere in a root, first, whatever the scheme (`ROOT_CHAIN_MESSAGE`, 422,
"cannot be an fsspec chain"; a chain before the scheme, `simplecache::file:///…`, meets
`check_root_scheme` first and gets the scheme message). `FsspecStorage.__init__` refuses `::` too,
so a chained root stored earlier fails closed at every `for_environment` before `url_to_fs` reads
it. `url_to_fs` on the root is Lakana's only fsspec call that parses a URL: table, metadata and
external locations reach pyiceberg's `FileIO` (`PyArrowFileIO`/`FsspecFileIO` take `::` literally,
probed) or DuckDB, and artifact and code paths go through `FsspecStorage._full` and the
filesystem object's own methods, which take `::` as part of a name. A root without `://` (`local:`, `/data`) has no scheme to compare and
is refused by `check_storage_root` (`ROOT_UNJUDGEABLE_MESSAGE`). An alias of the storage's scheme
passes this check and gets the spelling to use from `check_storage_root`. Why: the stored row has no
scheme column; `StorageResolver.for_environment` derives the scheme from the root
(`_parse_scheme`), so the scheme a client sent was dropped, and fsspec reads `local:` /
`local://` as the working directory (the hazard of "The root of the disk") and `memory://` as the
process's memory. `lakana env create` already sent the root's own scheme (`urlsplit`) and refused
anything but `file`, `s3`, `abfs`; the REST API and the SDK did not. **Existing rows** are left
alone: one in a scheme `_parse_scheme` does not know fails closed at every `for_environment`
(`Unsupported storage scheme`, 422): the table-location, artifact and code-archive sweeps count it
as a failed environment and touch nothing, `lakana env orphaned-locations` (with and without
`--delete`) is refused, so nothing of it lands in the working directory. **Residual:** deleting
such an environment works, but its purge never comes (the sweep skips it, `ValidationError`, each
run), so the row and its name stay. One created as `s3` with a
`file://` root (or the other way round) cannot be told apart from a correct one, since the sent
scheme was never stored: it has always been, and stays, an environment of its root's scheme.
`check_storage_root` also refuses an `s3`/`abfs` root (aliases and case included) whose normalised
path is empty or starts with `/` (`s3://`, `s3:///data`, `s3://.`, `abfs:///x`;
`ROOT_BUCKET_MESSAGE`, 422, shows `s3://bucket/lakana` / `abfs://container/lakana`): s3fs drops
leading slashes, so `s3:///data` is bucket `data` and an `s3://` root puts tables in bucket
`managed`, while the location checks compare the spelling as written. Existing rows are left alone
and keep that behaviour. Pins: `test_environment_root_scheme.py`.

**How it deletes.** Per directory: measure the age again, `_eligible` again, then insert and commit
an `ABANDONED` record (`purge_after = now`) for the location. `location` is unique, so a create
that claimed it first wins (the insert fails: kept) and one that tries later is refused. Then
`_Run.purge` with `audited=False` (no `TABLE_PURGED`), unchanged: the same shape check, in-use and
`dropped_inside` checks, `purge_started_at`, and per file, under that record's lock,
`location_in_use` and `dropped_inside` again, only objects strictly inside with sane segments,
the record last. A purge that does not finish (blocked, waiting, a storage error, an unjudgeable
path, the deadline) deletes the record again, so the directory is unrecorded once more and the next
call resumes it. The call has a 20-second deadline (half for the walk). One
`ORPHANED_LOCATIONS_DELETED` per delete call, with the caller as principal: `environment`,
`older_than_days`, `count`, `files`, `bytes`, `locations` (the first 100), `more_locations`,
`kept`, `complete`. Pins: `test_orphaned_locations.py` (each record state, nested and enclosing
tables and records, other environments' roots, the two claim races, a record appearing inside
mid-deletion, the deadline, rights).

**Never.** Nothing outside `managed/` of the named environment's root, nothing without the
canonical id-suffixed name, nothing a record in any state names, holds or surrounds, nothing a table
of any environment (deleted environments and tombstones included) uses, holds or surrounds, and no
file newer than `older_than_days`.

**Residuals, accepted.** The sweep's own (a process of an older release still writing into the
directory; a namespace or an environment root created inside it between the last check and a
delete; a delete in flight outliving the lock). A crash after the reservation leaves its
`ABANDONED` record, and the sweep then finishes the deletion the administrator asked for (with its
own `TABLE_PURGED`); a sweep running during the call can do the same. After a restore of the
catalog from a backup, a table created after the backup has no record and is listed like a
leftover: the docs tell the operator to keep `older_than_days` above the backup's age.

## Checks and the rollback (M5d-a)

User documentation: `docs/site/how-to/check-your-data.md`, `docs/site/reference/checks.md`. How the
phases fit together: `docs/agent/architecture.md`, "Checks: the task in two phases (M5d-a)". This
section is what a reviewer of code in this area has to know. Looking at the failing rows reads
a table at a named snapshot; that read is `load_table_at_check`, under "The one reader that is
not refused" below.

**Who counts.** The run's principal (`Run.run_as_principal_id`), as an ordinary read: the worker
submits `SELECT COUNT(*) FROM (<check>) AS lakana_check` through `pipeline.run_one`, so the same
resolve, rewrite and credential scoping apply as for any query of that principal, including its
row filters and column masks. The count is a `QueryRun` inserted `RUNNING` with `task_run_id`, and
is **never bound as the task's `query_run_id`**: it is not the task's write run, so the
protected-environment lockdown does not cover it and no write fence applies (it writes nothing).
It stores no result file; the number comes back to the worker through `on_result`. A count is
recognised by `store.is_check_count` (a task's own run with no write mode and no target, not
bound as the task's write), which the cancel
route (a refusal with its own text), the sweep (`CHECK_LOST_MESSAGE`) and the task path use. Its
`QueryRun` keeps what any query run keeps (its SQL and redacted error), readable by the principal
that ran it and by `MANAGE_ENVIRONMENT`.

**What a result may hold.** A `CheckResult` holds a count, the check's name and path, the snapshot
that was checked, the rollback outcome, one boolean and an error. `policy_applied` is one yes or
no: a row filter or a column mask of the run's principal applied to **any** table the check read,
not only the checked one. `error` holds an error type from a closed list
(`_ENGINE_ERROR_TYPES`) and a fixed sentence, never the database's own sentence, which can quote a
value of the table ("Could not convert string 'Alice Smith' to INT32"); a count refused for a
table the principal may not read, or one that does not exist, gets one text that names neither
(`ERROR_UNREADABLE`), because the query path answers both the same way. A check that raised
unexpectedly gets the exception's class name only. No row of the table is stored anywhere in the
result, and nothing may add one. Whoever may read the run may read its results (`RUN` or
`READ_ENVIRONMENT`); an environment's listing needs `READ_ENVIRONMENT`; neither route is a new
action.

**What a generated check's error may hold.** A check generated from an asset's header
(`TABLE/key_unique`, `TABLE/key_not_null`, `TABLE/ref_<column>`; `CheckResult.generated`) is
pre-checked by the worker inside `_count_one`, right before its count
(`lakana_core.runs.model_precheck.model_problem`), because a deploy has no output types. The
pre-check reads names and types from `table_columns`, never a value. A problem makes the check
`errored` without a count, so `block` sets the table back; a database error while reading is a
transient failure, like a failed count. Its `error` is one of the module's fixed sentences,
filled with the header's own column names, the checked table's name, the check's path and the
checked table's own column type (a float key), plus, for a column that matches only in case, the
checked table's own column name. It never holds a value. A reference's text **never names its
target or a type and never says whether the target exists**: a reader of the run may not be
able to read the target. A target that is missing, one the run's principal may not read
(`READ_TABLE`, asked before the target's columns are read) and one without the referenced column
get one text (`REFERENCE_UNCHECKABLE`), which points at the check's path, the asset's header line;
a type mismatch (`TYPE_MISMATCH`) points there too. Same rule as `ERROR_UNREADABLE`.

**Who rolls back.** The worker, for a running task on its own asset, with the **claim form** of
the run context (`RunContext(task_run_id, claim)`, built in `lakana_worker.task_checks._set_back`): the
third place that builds a run context, beside `pipeline.run_one` (the write run's form) and token
authentication. The rollback is a write by the run's principal on the task's own asset: in a
protected environment the guard allows the commit of the asset the claim names and nothing wider,
and re-reads the task run's claim before and after. **There is no route.**
`CatalogService.roll_back` and `commit_table(internal_rollback=True)` are called from the worker
only; `test_no_route_can_roll_a_table_back` (`test_no_run_context_on_the_wire.py`) fails if
`roll_back(`, `internal_rollback` or `after_swap` appears in the server's sources or a request
schema has a field named like a rollback, and the neighbouring test forbids a `RunContext(`
anywhere in the server. The rollback decides by what it finds under the requirement that the
table's current snapshot still be the one the run wrote (its tip, for a Python asset), so it can
never discard another writer's commit: that is `changed_since`, and the table is left as it is.
A database error never leaves the operation undecided: whether it landed is read back from its own
audit row (`TABLE_ROLLED_BACK`, for this task run and this claim) and from nothing a later writer
can change. Bookkeeping (the marker, the stamp, the audit row, the results' rollback outcome)
follows the pointer swap, in the swap's transaction; a rollback locks the table's row and then the
task run's, like a write, and holds no row lock across an upload.

**The two commit rules.** Both live in `CatalogService.commit_table` and so cover the Iceberg REST
route, the platform routes and maintenance alike.

- **C1: no commit may leave a table that has a current snapshot without one** (`_NO_UNPUBLISHING`).
  pyiceberg 0.12 can remove the `main` reference, and Lakana's commit applied it, for anyone who
  may write the table; readers would see an empty table. `roll_back` alone is exempt
  (`internal_rollback`), because it unpublishes a table on purpose and marks it. The cost is an
  engine that removed `main` deliberately; none is known.
- **C2: a commit that is not maintenance may give a not-published table a current snapshot only by
  adding that snapshot** (`_NO_SIDE_DOOR`), never by pointing `main` at one that exists: the
  snapshot a failed check kept under its tag would otherwise publish failing data by a side door.
  A commit that does give the table a current snapshot clears `unpublished_run_id` in the
  statement that moves the pointer.

**The answer to an Iceberg REST commit or create.** The commit route needs `WRITE_TABLE` and the
create route `CREATE_TABLE`, never `READ_TABLE`, yet the answer is the table's metadata: every
snapshot with its manifest list and summary, every ref (a failed check's tag among them), the
properties. `CatalogService.commit_answer` decides what goes back; the router
(`commit_table`, `create_table`, `_to_table_response`) only shapes it. The whole metadata goes to
a caller who holds `READ_TABLE` and whom `load_table` would not refuse (it calls
`_require_published` and `_require_not_kept`, as the load does). A bound task token is no
exception: it may write its own asset and not read it, so its answer is reduced like anyone
else's (`test_its_iceberg_commit_to_its_own_asset_answers_no_history`). Everyone else gets
`_ANSWER_WITHOUT_HISTORY`: format version, uuid, location, last column, partition and
sequence ids (not the time of the last change), and only the current schema, the default partition spec and the default sort
order with their ids (earlier schemas would name dropped columns), which pyiceberg 0.12 parses
as metadata with no snapshot
(`test_a_writer_without_read_commits_with_pyiceberg_and_parses_the_answer`). Without
`properties`, pyiceberg's delete-after-commit reads as off (it reads the setting from the
answer), so dropping `metadata-log` deletes nothing on the client. A client left with the
reduced metadata builds its next commit on "no snapshot", which fails its requirement with 409;
a refresh needs `READ_TABLE`. The commit itself is unchanged: it lands or fails as before. The
load route still needs `READ_TABLE` and answers the whole metadata. **A failed requirement or
update follows the same rule**, decided by one helper, `CatalogService._may_see_state`, through
`_state_withheld`, which keeps the exception type and so the status:

- pyiceberg's requirement texts name the ref and the snapshot id found (or that the ref is
  missing), which would answer a probe of `main`'s head or of a `lakana-check-failed-*` tag; a
  non-reader gets `_CHANGED_SINCE_YOUR_METADATA` (409), worded for an SDK write that lost a race
  too (`test_a_failed_requirement_tells_a_writer_without_read_nothing_of_the_table`).
- `apply_updates`' texts print what they compared against: for a snapshot ref set to an unknown
  id, the id; for an added spec or sort order with a taken id, the stored one. (`remove-snapshots`
  with an unknown id used to list every snapshot with its manifest list and summary; it is now
  refused before that, below.) The two location confinements after it print the table's
  location and the storage root. A non-reader gets `_UPDATE_DOES_NOT_FIT` (422).
- `_NO_UNPUBLISHING` and `_NO_SIDE_DOOR` say whether the table has a current snapshot or is
  published. The load itself tells that to any `READ_TABLE` holder, so for these two the grant
  alone decides (`publication_only`).
- pyiceberg refuses to remove a property the table does not have, which next to the silent
  removal of one it has would tell which keys exist; for a non-reader, `commit_table` drops the
  absent keys from a `remove-properties` update first, so both succeed, as in the Iceberg reference
  implementation.

A bound task token on its own asset is a non-reader like any other, with one exception: a
**merge** into it. For a bound token whose asset is the target (`_bound_to_own_asset`),
`check_can_merge` checks `READ_TABLE` against the run principal's own grants with the token's
narrowing to its inputs removed (`replace(p, task_scope=None)`), after `WRITE_TABLE` has held the
token to its environment and its own asset (compared by parts). Only that narrowing is waived:
materialising the asset is the task's whole job and a merge cannot be done without comparing
against it. The principal itself must still hold `READ_TABLE`, so a task of a write-only
principal is refused exactly as that principal's personal token is
(`test_a_task_of_a_write_only_principal_may_not_merge`, core and server); otherwise the merge's
counts would tell such a task the asset's keys. The write guard's own-asset rule
(`WriteOperation.MERGE`) still confines the token to that one table, and every other table stays
refused. The row filter and column mask
refusal still runs against the principal the task acts for, so a task of a filtered or masked
principal cannot merge (`test_a_task_of_a_filtered_principal_may_not_merge_into_its_own_asset`,
core and server).

**An Iceberg REST commit never removes snapshots** (`_NO_SNAPSHOT_REMOVAL`, 403 for every caller).
`commit_table` refuses `remove-snapshots`, and `remove-snapshot-ref` for a ref named
`lakana-check-failed-*` (`_is_maintenance_update` is the one definition of what that is), unless
the action is `MAINTAIN_TABLE` or the call is the internal rollback. It runs right after the
authorization and before the table is looked up, so the text depends on nothing in the table and
a caller without READ learns nothing from it. Without the rule a writer could expire the
snapshot a running task needs to set its table back (`written_snapshot_before`, the baseline of
a Python task) or the one an old failed-check tag keeps, and the set-back then fails with the
table keeping the rejected data. Expiry is `lakana table maintain`, which spares what running
tasks and kept tags need. Removing any other ref, a branch or tag of the user's, is still a
normal commit; `main` can be removed and then falls under the rule that no commit leaves a
table without a current snapshot.

**What else a commit over REST may not do** (all in the one
pre-lookup check `_refusal_before_lookup` unless noted, and all exempt for `internal_rollback`):

- **Set a ref named `lakana-check-failed-*`** (`_NO_FAILED_TAG_CHANGE`, 403). Moving the real tag
  off the failed snapshot frees it to expire at once; a new or moved prefix tag, or a branch under
  the prefix (which `old_failed_tags` does not count), would make the next `maintain` force-expire
  a snapshot of the caller's choice or keep a name for ever. `roll_back` is the one setter.
- **Remove schemas or partition specs** (`_NO_SCHEMA_OR_SPEC_REMOVAL`, 403). pyiceberg refuses
  only the current one. Removing the schema of a running task's baseline fails its set-back on
  `SetCurrentSchemaUpdate(schema_before)`, removing the schema of a failed snapshot ends
  `lakana check rows` with `CheckedSnapshotGoneError`, and a removed id leaves `known_schemas`, so
  `_SCHEMA_ID_TAKEN` would no longer stop its reuse.
- **Assign another UUID** (`_UUID_CANNOT_CHANGE`, 422, in the loop beside `SetLocationUpdate`,
  every caller). The set-back, the SQL write path and `maintain` all require `AssertTableUUID`,
  so a changed UUID would fail every later write and the set-back, with no READ needed.
  Assigning the table's own UUID changes nothing and is allowed.
- **Make an earlier partition spec or sort order the default without READ**: the rule of the next
  paragraph, for `SetDefaultSpecUpdate` and `SetDefaultSortOrderUpdate` (a partition field is
  named like its column, so a dropped column's name would leak).

`_EARLIER_SCHEMA_NEEDS_READ` is also what a holder of READ gets for a table it may not load now
(not published, or kept from a task, which includes a task token bound to no task): "ask for READ"
is then not the next step, but the text is the one fixed text and leaks nothing.

**`set-current-schema`, `set-default-spec` and `set-default-sort-order` to an earlier layout need
READ** (`_EARLIER_SCHEMA_NEEDS_READ`, 422). The reduced answer of a writer without READ carries the current schema, so making an
earlier schema current (schema ids are small sequential numbers) would show it with the names of
the columns dropped since. In `commit_table`'s pass over the updates, a `SetCurrentSchemaUpdate`
whose id is neither `-1` nor the id of an `AddSchemaUpdate` earlier in the same commit is refused
for a caller for whom `_may_see_state` is false, whether or not the id exists, with one fixed
text. Adding a schema and making it current stays open to a writer. The rollback
(`internal_rollback`) is exempt: it sets the table's schema back as the worker, who need not
read the table.

**A schema change may not reuse a schema id** (`_SCHEMA_ID_TAKEN`, every caller, 422).
pyiceberg's builder accepts an `AddSchemaUpdate` with an id the table has, and the
`SetCurrentSchemaUpdate(-1)` after it then finds the older schema of that id, which brings a
dropped column back. A non-reader's client, which sees only the current schema, picks exactly
such an id once the table evolved back to an earlier schema
(`test_a_writers_pyiceberg_schema_change_on_a_reused_schema_id_is_refused`).

**The same for a partition spec and a sort order** (`_SPEC_ID_TAKEN`, `_SORT_ORDER_ID_TAKEN`, every
caller, 422). pyiceberg accepts an `add-spec` or `add-sort-order` under a taken id when the
content is identical, so for a writer without READ success against failure would tell whether a
guess of an earlier spec (field names included) is right, and a hit would let it make that spec
the default again. Both are refused whatever the content, so the answer does not depend on it.
The refusal does tell a writer without READ that the id is taken, as it does for schema ids, and
nothing of the content; the count of specs and orders is not treated as protected.
A pyiceberg client that wants a spec the table already has sends `set-default-spec` alone (which
needs READ for an earlier one).

**The not-published state.** `Table.unpublished_run_id` is set by `roll_back` in the transaction
that moves the pointer and cleared by the next commit that gives the table a current snapshot. The
one refusal point is `CatalogService._require_published`, called from `load_table`, which the query
path and the Iceberg load both use (the platform answers 409 through `NotPublishedError`; the
Iceberg load answers 404 with the same text, as for a view). `lakana table get` and `list` show
`state` instead. No route reads a table as Arrow; both Arrow routes are writes, and a write
publishes. `test_no_new_catalog_method_builds_a_loaded_table_without_a_decision` (in
`test_tables.py`) enumerates the `CatalogService` methods that call `_to_loaded_table` and fails
when a new one appears, so that its author has to choose between a reader's path (calls
`_require_published`) and a writer's. Grants, row filters and column masks on the table are not
touched; `table delete` works.

**The one reader that is not refused.** `CatalogService.load_table_at_check(p, ref,
check_result_id=...)` loads a table to read it at the snapshot a stored check result recorded,
for showing a person the rows a check found. In this order:

1. It authorizes `READ_TABLE` on the name before it looks anything up, like `load_table`: a
   caller without the grant gets the permission error whether or not the table, the result or
   the snapshot exists, and whatever state the table is in.
2. **It refuses every principal with a run context, and every task token** (`CHECK_ROWS_NOT_FOR_A_TASK`,
   a permission error), in either form and before the result is looked up. A task-kind token
   (`PrincipalRef.task_scope` set) bound to no task, one minted by hand over REST, has no run
   context and is refused all the same: it is meant for automation, not for a person's reads. A `block` check keeps failing
   rows from everything downstream; a task that holds the read grant would get them here after
   all. The read is ad hoc by design.
3. It reads the check result **in the environment of the name** and decides at once whether
   the result is **bound** to the named table: it exists there, has a table id, and the live
   row of the name has that id (`row.id == result.table_id`, **by id only**; the result's
   `table_name` goes stale on a rename). Not bound is `CheckResultGoneError` with **one text
   that names only the table the caller named** (`check_result_gone_message`), whether the
   result is missing, of another environment, of another table, without a table, or of a table
   that was dropped and created again; or the name holds an artifact, which no check result is
   ever of (`test_artifact_refusals.py` classifies the method as hiding an artifact). A caller who may read table A and holds the id of a
   result of table B learns neither B's run, nor whether that result has a snapshot, nor that
   it exists. **No text before this point may name a run or a check.**
4. Bound, and the result names no snapshot: `NoCheckedSnapshotError`. From here on the result
   is the table's own and a text may name its run.
5. The row must be a managed table with metadata, else `CheckedSnapshotGoneError`.
6. It calls `_require_not_kept`. With step 2 no caller can meet that refusal today; the call
   stays so that the kept refusal does not depend on step 2.
7. The metadata must still hold the snapshot and the schema the snapshot names, else
   `CheckedSnapshotGoneError`. The columns are never guessed from the current schema.

It does **not** call `_require_published`, and it alone may leave that out: the refusal exists
because a table without a current snapshot would read as empty, and this read never shows the
current state. The table and the snapshot come from the stored result and never from the caller,
so only a snapshot that a check read can be reached; a snapshot of a build that failed before
its checks cannot. It returns a `LoadedAt`, which is deliberately not a `LoadedTable` (`ty`
rejects it where one is expected): the table's current metadata, the snapshot's id, and that
snapshot's columns (a rollback sets the schema back, so the current schema can lack a column the
snapshot's files have). `LoadedAt.data_files` lists the files of that snapshot and of no other,
under the same path checks as the current snapshot (`list_data_files(..., snapshot_id=...)`);
its `is_valid_path` is a required argument, and the pin below reports a call without one.
Row filters and column masks are resolved by the query path for the reader, by the table's id,
as for any read. `test_no_other_caller_reads_a_table_for_a_task` lists the method by name with
its reason (`_READS_WHAT_IS_NOT_PUBLISHED`) and pins every call of it outside core; a new caller
fails there. It has one caller, the worker's source resolution, described next.

**The query that shows the rows of a check.** A person looks at the rows a check
found through a query run of their own that is bound to the check result
(`QueryRun.check_result_id`, no foreign key). What a reviewer has to know:

- **One function sets the binding:** `lakana_core.runs.check_rows.submit_check_rows`. It
  authorizes the read of the run first (`authorize_run_read`: `RUN`, else `READ_ENVIRONMENT`,
  the rule of `RunService`) and `SUBMIT_QUERY` on the environment second, and only then looks
  at anything, so a caller without both gets the same refusal whether or not the run, the
  check or the project version exists. It refuses a task (a run context, or any task token, bound
  to a task or not) with `CHECK_ROWS_NOT_FOR_A_TASK`. It looks for the result **among the results of that
  run only**, by the check's name: the caller never passes a result's id. No result:
  `CheckNotInRunError` (a `NotFoundError`). A result without a checked snapshot:
  `NoCheckedSnapshotError`, with a reason only where the result, the kind of its table or its
  SQL says which it is. No readable entry for the check in the manifest of the run's project
  version: `CheckSqlGoneError` (a `ConflictError`). Right after the result's entry is found,
  and **before** `NoCheckedSnapshotError` or anything else derived from the SQL, it refuses a
  caller who may not read every table the check's SQL names (`PermissionDenied`,
  `CHECK_ROWS_NOT_EVERY_TABLE`, which names neither the tables nor their number). The names are
  read again from the SQL (`projects.checks.check_sql_references`), never from the manifest's
  `references`: the stored manifest is untrusted. SQL whose tables cannot be listed is treated
  as no entry at all. The worker authorizes the read of every table again, as for every query.
  The query run is `QUEUED`, submitted by the caller, has no task run, and its `sql_text` is the
  check's SQL from the manifest, so only a caller who may read every table it names ever sees
  it. `DeployService.job_version` applies the same rule to the manifest's `checks`
  (`_visible_checks`): a check whose SQL names an unreadable table, or whose tables cannot be
  listed, loses `sql` and `references` and carries `hidden_references`, as an asset does.
  The reference targets of an asset's `model` name tables as well: `_visible_model` keeps only
  the targets the reader may read and counts the rest in the entry's `hidden_model_references`
  (`test_a_project_version_hides_model_references_the_reader_may_not_read`). At deploy, a
  reference target outside the project counts only if the deployer may read it, and a missing
  and an unreadable target get one text; when a run principal is bound, it must read every
  target the deployer resolved, and that refusal may name the target
  (`DeployService.resolve_model_references`).
  `test_only_the_rows_of_a_check_bind_a_query_run_to_a_check_result` (in
  `test_runs_check_rows.py`) enumerates every construction of a `QueryRun` in core, server and
  worker and fails when a new one appears or another one sets the field;
  `test_nothing_sets_the_binding_on_a_query_run_that_exists` fails on an assignment, an update
  or a request field of that name (it lists the two files that name the field in a statement
  and only read it: the worker's query path and the audit detail's key in `runs/store.py`).
  `QueryService.submit_query` has no parameter for it.
- **It is an ad-hoc run of its caller, not a run of a task.** `task_run_id` is `NULL`, so
  `pools.is_interactive` holds (the interactive pool, the caller's cap), the worker builds no
  run context for it (`_run_context`), and the sweep and the cancel route never ask
  `store.is_check_count` about it (both ask only for a run with a task run and without a
  token). It is **not** one of the two kinds of query run the worker inserts for a task, and
  the kept refusal's rule for a count (`kept._is_count`) cannot match it: that rule needs a
  run context, which this run does not have. Status, result and cancel follow the submitter
  rule of any query run.
- **The one place that reads a table that is not published** is
  `lakana_worker.pipeline._resolve_sources`, for a run with `check_result_id`. It reads the
  result once, in the run's environment, before any table; a result that is gone fails the
  query (`CHECK_RESULT_REMOVED`), it is never read as "no binding". A result is gone when
  its run was deleted, and also when its task was claimed again: `record_result` deletes and
  inserts, so the new attempt's result has a new id. The text names both and says to run the
  command again; the query run stores neither the run nor the check's name, so the command is
  named without arguments. For the source whose
  catalog row has the id the result names (**by id, never by name**) it calls
  `load_table_at_check` and lists the files and columns of that snapshot, under the same
  location and ownership checks as a current read; every other source goes through
  `load_table` exactly as before, so a second table the check joins is read as it is now and
  stays refused when it is not published. If no source has that id (the table was dropped and
  built again under the name), the query fails with the snapshot-gone text and nothing is
  read as it is now. The three refusals (`CheckedSnapshotGoneError`, `CheckResultGoneError`,
  `NoCheckedSnapshotError`) pass through `_resolve_sources_opaquely` like `NotPublishedError`
  and have the cause `not_found`; a caller without `READ_TABLE` on the table gets the opaque
  permission text first, as in any query. The snapshot recorded per table (`pins`) is the one
  that was read.
- **Which columns are shown: only those the table has now.** Row filters and column masks are
  resolved for the reader by the table's id, after the pin and for that source like for any
  other, and the rewrite applies them to the snapshot's files. But policies are set on the
  table **as it is now**: a mask can only be put on a column the current schema has. A column
  that only the checked snapshot has can be masked for nobody: the failed run added it and the
  rollback set the schema back; or a later run dropped or retyped it, and the mask that once
  covered it was revoked because it broke the reader's ordinary queries (a snapshot that a
  `keep` or `warn` failure left in the history stays readable this way). So **for every
  reader, the pinned source shows only the columns of the checked snapshot whose name, type
  and field id equal a column of the table's current schema**
  (`_columns_the_table_still_has`).
  Nothing is shown column-wise that the reader could not read in the table today. The rule
  does not look at the reader's policies on purpose: a rule that applied only to readers with
  a mask changed its answer when a stale mask was revoked. Types are compared as the worker
  spells them for the rewrite (the text of the Iceberg type), so a nested type is compared
  whole and a struct that gained a field is withheld. The field id tells two columns that
  were renamed into each other through the Iceberg catalog: names and types stay, and each
  now holds what the other held, so a mask on one would cover the wrong snapshot column.
  Lakana's own writes keep the id of a column they do not change (a replace, a replace or an
  append that adds a column: `test_lakanas_own_writes_keep_the_field_ids_of_unchanged_columns`),
  so a rebuild withholds nothing; a column that was dropped and added again has a new id and
  is withheld. Whether a column is required is not compared. The list reaches the
  database as the explicit column list of the one CTE that reads the files
  (`TableSource.declared_columns_only`; without it a reader with no policy would read the
  files with `*`). A table that is not published after a failed first build keeps the schema
  that build made, so all columns of its failed snapshot are shown. If the check's SQL names
  a withheld column, the query fails with `COLUMN_NOT_SHOWN`. That is told by the column the
  database's binding error says it could not find (`Referenced column "X"`, or, for a column
  written through a table alias, `<Kind> "alias" does not have a column named "X"`), compared
  without regard to case, never by a column it merely suggests; a failure worded otherwise keeps
  its own text. The error names
  a bare column and no table, so the text is given only when **no other source of the query
  has a column of that name** (compared without regard to case); otherwise the database's
  message stands. If no column is
  left, the query fails with `NO_COLUMN_LEFT`. Both have the cause `invalid_query`.
- **Files that lack a column (`pipeline._schema_mismatch_failure`), raw-zone reads.** An
  Iceberg read projects every file onto the schema it uses through DuckDB's field-id map
  (`v_<n>_schema`, `catalog.field_ids.read_fields`), so its files never lack a column and a
  binder error's candidates are the read's own columns. A raw-zone table's files are read by
  name, and a read under a row filter or a column mask selects **every declared column** in the
  one CTE over the files, so after one of them lacks a column every reader under any policy of
  the table fails, whichever column the policy names (the files of one glob differ, or the column is
  "not found" in the one file the read binds against). Both database texts list a file's
  columns, which may include one the caller may not see, so neither reaches the caller: the
  glob wording is replaced by a text naming the table(s) that declare the column, and the "not
  found" wording is too, for a table read through an explicit list. When the reader's own SQL,
  views expanded (`query_rewrite.named_columns`), names the column, either wording gives the same
  text (so file order does not matter): the database's first line with its `Candidate ...` lines
  cut (`_without_candidates`), plus a hint naming the projected tables that declare the column and
  saying their older files lack it. A "not found" for a table read with a plain `*` keeps the
  database's text. It only
  rewrites the message of a read that already failed. A field inside a nested column is read by
  name; `refuse_changed_nested_fields` refuses a read whose nested fields moved since a live
  file was written, by the schemas of the snapshot's ancestry, and names no file.
- **A failed read of a check's snapshot looks again (`pipeline._snapshot_gone_after_failure`).**
  Maintenance may remove the snapshot between resolution and read. After a failure that
  is not a worker stop, the run asks `_resolve_sources` again with the caller's own rights
  (READ_TABLE is authorized first, so a caller without it keeps the original text), and only a
  `CheckedSnapshotGoneError` replaces the message; any other outcome leaves it. It is skipped
  while the worker stops (the outcome is rolled back then). It never re-runs the query. The
  second look re-lists the manifests, which doubles the time to fail while storage is down.
- **A column that a policy reads must be the same column in the snapshot and in the table.**
  Policies are written for the table as it is now, bind to its columns by name only, and the
  pinned source is an older snapshot. So for the pinned source, every top-level column that
  a row filter of the reader reads, and every column that the expression of an applied mask
  reads (`policy.expressions.columns_of`, the leftmost part of a dotted name, compared
  without regard to case as the database does), must exist in the checked snapshot's schema
  and in the table's current schema **with the same field id**. Otherwise the read is refused
  before the query runs, with `POLICY_NEEDS_A_COLUMN` (cause `invalid_query`), naming the
  first such column by name. The reason: a column added or dropped since is missing on one
  side, and a column that was dropped and added again under its name, or renamed into
  another through the Iceberg catalog, holds other data under the old name, so the filter
  would pass the wrong rows and the mask would be built from the wrong values without any
  error. **A policy is never left out to make the read work.** One thing is skipped, because
  it protects nothing: **a column mask whose column is not among the shown columns** (added
  after the snapshot, or withheld), where the rewrite's refusal of an unknown mask column
  would block the read for no gain; its expression is then not looked at. A mask that differs
  from a shown column only in case is not skipped (it is meant for that column; the rewrite
  refuses). All of this is done where the worker builds the pinned `TableSource`; the rewrite
  is unchanged, and an ordinary query is untouched: a mask on a column the table lacks fails
  it as before.
- **A failed query is not diagnosed through its views.** For an ordinary query that read a
  view and failed with a validation error, `_diagnose_views` plans each view again to say
  which one broke. That planning reads every table **as it is now**, so for a bound run it
  would blame a view for a table that is not published (and quote the not-published text,
  which names `lakana check rows`, the command that is running). `_run_one` skips it when
  `run.check_result_id` is set; the query's own error stands.
- **The audit trail tells this read from an ordinary query.** The `QUERY_COMPLETED` event of
  a query run has the detail `sql_text`, `tables_touched`, `row_count`, `status`. For a run
  with `check_result_id` it has one more key, **`check_result_id`** (the id as text): the
  read reached a table that is not published, or a state that was set back, which an ordinary
  query naming the same table cannot. The key is absent, not `null`, for every other run, so
  their events are unchanged. One builder, `lakana_core.runs.store.query_completed_detail`,
  serves the three writers of the event (the worker's `_run_one`, its `_refuse`, and
  `record_query_completed` for the sweep and a cancel of a lost run). The run and the check
  are found from the id in `check_results` while the result exists.
- **A name that leads nowhere.** When the name the result stores for its table is not found
  (renamed or dropped), the query fails with the snapshot-gone text instead of an opaque "not
  found". `load_table_row` asks for the read grant on the name before it looks the name up,
  so a reader without it still gets the opaque permission text; the name only chooses the
  text and never decides which table is read at the snapshot.

**The tag of a failed check and its removal.** `CatalogService.maintain` (action
`MAINTAIN_TABLE`) removes every ref whose name starts with `FAILED_SNAPSHOT_TAG_PREFIX`
(`lakana-check-failed-`) when the snapshot it names is more than seven days old, and expires that
snapshot in the same commit, whatever the retention settings say, unless another ref names it or a
remaining ref has it as an ancestor. The prefix decides, not who created the tag. No commit over REST can set a ref with
the prefix, so only the rollback makes one; a tag that an earlier release let a user make by
hand is removed too, and a user's snapshot kept only by such a tag goes with it. No other ref, `main`
included, is ever removed by maintenance. The commit skips the protected-environment guard only
structurally, as above: `_is_maintenance_update` accepts snapshot expiry and the removal of a ref
with the prefix, and the commit route of the Iceberg REST catalog passes no action
(`test_commit_route_has_no_action.py` pins it), so a client of that catalog cannot reach this
exemption; `commit_table` also refuses such updates outside maintenance (above). Once the snapshot is gone, `load_table_at_check` answers `CheckedSnapshotGoneError`:
this is the usual way the rows of a block check stop being readable. A table that is dropped,
renamed or built again ends it too.

**The kept note on the read path (M5d-a2).** A table that failed a `keep` check carries
`Table.kept_run_id`. `CatalogService._require_not_kept`, called from `load_table` right after
`_require_published` (so after the read grant: a principal without it gets the permission error
and learns nothing about the note), refuses with `KeptAfterCheckError` (409 on the platform API,
404 on the Iceberg load, passed through `_resolve_sources_opaquely` like `NotPublishedError`;
permanent for a task) when the reader is a task, that is when its principal has a run context in
either form or authenticated with a task token bound to no task (no run, so no
standing, refused outright), and has no standing (`lakana_core.runs.kept.task_may_read`):

- **Only a run of the project that builds the table has standing:** the table is an asset of the
  manifest of the run's project version (`Run.job_version_id`). Another project's check of the table
  may be weaker than the check that failed, so neither its `source_check` task for the table nor
  anything below that task reads. With a `block` or `keep` check that task fails, its real
  failures first and then the refusal as its message; with only `warn` checks it succeeds, and
  its reader is refused one task later, with the refusal as its own message. A run of
  another environment has no standing either, whatever its assets are called.
- In such a run a task reads when (a) its own asset is the table (the check task of the kept
  table, a check phase, the rebuild), or (b) a task of the run whose asset is the table is among
  its upstreams, directly or through `upstream_json`: the scheduler makes a task ready only when
  every upstream succeeded, so the table's checks passed in that run or it was rebuilt and
  checked. "The run has a task for the table" is not enough: a sibling could run before it.
  `RunService._create` therefore also lists every selected asset, a kept table that the run
  rebuilds included, among the upstreams of the selected assets that read it through
  unselected views (`view_readers`).
- (c) **The count of a check reads** in such a run without (a) or (b): a count builds nothing,
  and two tables whose checks read each other would otherwise lock each other for good. A count
  is told by its query run, re-read from the database: the run context is the query-run form,
  the run is that task's, was submitted without a token and is a count by `store.is_check_count`
  (no write mode, no target, not bound as the task's write): the one definition, shared with
  the cancel route and the sweep.
  The worker inserts exactly two kinds of query run for a task without a token, a SQL task's
  write run (it has a target) and a check's count; a Python task's reads come with the claim
  form. Never give a read run of a task another purpose without revisiting this. The query
  that shows a person the rows of a check (`QueryRun.check_result_id`, above) is no third
  kind: it has no task run and no run context, and is an ad-hoc run of its caller.

A reader without a run context and without a task token is never refused (the table keeps the data
so that people can look at it); an unbound task token minted over REST is a task with no run and is
refused. The note is a guard for pipelines,
not an access control, and grants still decide who reads. The run context is not re-checked for
being live here, because it only narrows what its principal may read. A table without a note
costs no statement; a kept one costs a task two reads (its task run with the run and the
manifest; the tasks of its run) and the worker's form a third (the query run) and, for a count,
a fourth (whether the task's write is that run, in `is_check_count`) when neither (a) nor (b)
holds. Run writers cannot keep the table again while a run that passed its checks is
still reading it: one run per repository and environment is active at a time, and deploy
ownership gives a table one repository.

**What the refusal shows, and to whom.** The refusal in full (text K4) is the message of the
refused query run and of the task that read; it names the kept table, a check of the project
that builds it and that project's run id. A viewer of another project's run therefore learns
those three, as the not-published message already tells them a check name and a run id; the
table's name is in their own SQL. A check result's `error` for this refusal is `ERROR_KEPT`
and names none of them. A `keep` check that could not run because of this refusal sets no note
on its own table (the worker tells it by the type of what failed the count, in the claim that
counted), so that notes do not spread through checks. The exception is a table that
carries a note already: its rebuild replaced the data the note names, so the note moves to the
new run with `kept_unchecked` set, and readers stay refused.

The stack is the history of the table's verdicts, newest last: a new note, a clear and a passing
write on a table with a history each push the state they replace (`_pushed`; an older entry for the
same snapshot goes, so the newest verdict on a snapshot is the one found). Every state is a verdict
on a snapshot (`Table.kept_snapshot_id`, `snapshot_id` of an entry). A set-back (`restore_kept`, in
the transaction of the set-back) to a snapshot T acts when the row's verdict is not about T, whoever
set it, because another run's check may have judged and cleared the data the set-back removes:
it puts back the newest entry for T (mark or "no mark") and drops the entries above it, which
describe data newer than T. Readers are let in again only when that entry is "no note": the data
had passed its checks, or had none, as a verdict on that very snapshot. With no entry for T (a row
from before migration `0031`, history that ran out of `KEPT_EARLIER_LIMIT`, an entry that cannot be
read, a `-1` or `NULL` snapshot) the table fails closed: it gets a mark that says its checks are not
known (`Table.kept_unknown`, migration `0032`), naming the run and task run that were set back,
with its own state and refusal texts. It refuses readers like every mark and is cleared by the next
passing checks like every mark (`_clear_row` pushes it as an entry with `unknown`). A table with no
mark that never had a verdict (`kept_snapshot_id` is `NULL`) is left alone: no check judged it, and
a plain `block` rollback of a never-marked table must not mark it. A table that has a mark which
names another run or task run keeps it (another run's mark is about other data), unless that
mark is the verdict on the data being removed (below), and a first build
keeps the mark it has. The restore and the set-back are one transaction, so a set-back that fails
leaves the row as it was. The writing task's own clear runs inside `record_attempt`'s
`in_transaction`, so a task whose success is not recorded has not cleared the note and cannot be
set back to data that carried one.

**What a wrong "no mark" would cost, and where it can still happen.** A reader let in sees data
that failed its checks, so each path to "no mark" has to be a verdict on the snapshot the table
holds. The paths are:

- a clear (a passing check judged a snapshot, recorded as the verdict);
- a passing write on a table with a history (`record_clean_in_attempt`; also when the mark was
  already cleared by another run, since its own passing checks back it);
- a restore of a "no mark" entry for exactly the snapshot the table goes back to (an entry is the
  row's own state when it was pushed, and the newest verdict on a snapshot replaces older ones);
- a set-back to a snapshot the unmarked row is already a verdict on (`kept_snapshot_id == T`,
  nothing changes);
- a set-back of a never-judged unmarked row (`kept_snapshot_id` is `NULL`, nothing changes). That
  row can hold data whose `keep` checks did not pass without any mark: a rebuild whose every check
  was refused a kept table of another project and whose own table carried no mark sets
  none, and `note_kept` pushes, for such a row, the unmarked state of the snapshot before the
  write (no check judged it). The set-back gives back exactly the state readers had before the
  write, so nothing regresses; marking every such table on every failed `block` check would refuse the previous good
  data of every table that was never kept.

A write whose checks record no single judged snapshot leaves the verdict at an older snapshot, so
a later set-back to that write finds no entry and marks the checks unknown: a refusal that clears
itself with the next passing run, never a wrong "no mark". That is the accepted price of failing
closed. A clean record never claims a snapshot no check judged, and never overrides a failing
verdict that the stack holds for the snapshot it judged (a phase that counted before that verdict
cannot show that its own is newer). A set-back also acts on a mark
that names another run when that mark is the verdict on the data being removed
(`kept_snapshot_id == snapshot_after`: writers set back in the reverse order of their writes); a
mark that is a verdict on other data stays. A clear is tied to the note its checks began with
(`KeptSeen`, checked under the row lock in `_clear_row`), so a note another run sets while the
counts run is never removed by it.

`test_every_public_catalog_method_is_placed` (`test_catalog_kept.py`) reads the public methods of
`CatalogService` from the class: each either goes through the refusal or is listed with the
reason why a task gets no kept data from it, and a new one fails the test until it is placed.
`test_no_other_caller_reads_a_table_for_a_task` pins the callers of the refused read (the Iceberg
load, the query path, and the query path's read at the snapshot of a check), each with what it
is, and the modules outside core that read Iceberg metadata themselves. An `EXTERNAL` table is read without
`load_table` and never carries the note: only a task that built a table sets one.

**The written stamp.** For a SQL task whose asset has checks, `ProtectedWriteGuard.record_pending`
stamps the task run *written* (table, snapshot before, snapshot after, `publishes`) in the same
statement and transaction as the fence's check and the evidence of the commit, so the record and
the pointer swap land together or not at all. A write holds the task's row `FOR SHARE` from its
fence to its commit; a reclaim locks the task's row first (`FOR UPDATE`) and then judges the
evidence with new statements, which is why a commit that lands while a reclaim waits stops the
reclaim (`test_a_commit_that_lands_while_the_reclaim_waits_for_the_task_stops_the_retry`). A
rollback that lands clears the stamp in its own transaction.

**Every ender, the rule for a written, unchecked task**. A task whose asset has checks
and whose write is in the table (stamped, or its current write run shows the evidence, which
outlives the stamp) *awaits its checks* (`store.awaiting_checks`, one definition). Until a worker
records the verdict:

- the leader's tick (`_apply_task`) puts it back to `pending` uncounted on a lost lease, its
  attempt `lost`; no other reclaim statement may match it;
- `record_attempt` does not end it `succeeded` (that would let downstream build on unchecked data)
  nor `failed` (it cannot run out of attempts) on a retryable outcome, a drain, a timeout or a
  cancel that is not the run's; the next claim runs the checks only;
- the memory stop (`record_attempt(stopped=...)`) puts it back the same way;
- the sweep of lost query runs ends a count `FAILED`, never `SUCCEEDED`.

Only `lakana runs cancel` ends such a task otherwise. A worker that claims a task whose results
record a landed rollback ends it failed from them, without building and without checking
(`task_checks.before_build`).

**What is not defended.**

- **Readers between the write and the rollback** see the failing data, for as long as the checks
  take. Only writing where readers do not look and publishing after the checks would prevent it
  (issue #98).
- **A task lost after it may have written, in a cancelled run or on its last attempt** is not ended
  by the scheduler, nor is a waiting task of a cancelled run whose table may hold unchecked rows
  (it awaits its checks, or has checks, a baseline and no stamp): it goes back to `ready` with
  `task_runs.setting_back`, uncounted, and the claim
  after it (`worker.task_checks.set_back_only`) builds and checks nothing and only sets the table
  back (the same `roll_back` call). The write guard lets that one commit pass a cancel request
  (see the guard, above). A set-back claim that is lost is put back at most
  `MAX_SET_BACK_CLAIMS` times (`task_runs.set_back_claims`); then the task ends with
  `SET_BACK_LOST` and the table keeps what it holds. A zombie of the old claim fails on the claim
  counter. The sentences are in `lakana_core.runs.checks`, the wait sentence in
  `lakana_core.runs.waiting`.
- **A worker lost in every check phase** is claimed again once per lease without a bound. **A
  check phase that cannot start** (the manifest's version; a run's principal whose row no longer
  exists, or a run without one) is tried again with the task's backoff, which is capped at
  600 s, also without a bound. Each attempt is recorded, and only `lakana runs cancel` ends
  either. A deactivated principal is not this case: the phase starts, every check is `errored`
  and the table is set back.
- **A Python asset:** a rollback undoes only the task run's own chain of writes (baseline to tip);
  anything by someone else gives `changed_since` and leaves the table, also with this run's rows in
  it. Between a failed attempt and the next the table holds unchecked data; a table that cannot be
  set back keeps it, and so does a Python asset **without checks** after a lost worker (it records
  no baseline). A Python asset with checks, lost after its last attempt's write, is set back by
  the next claim (above). An asset that writes its own
  table through `client.tables.append` or an Iceberg commit is recognised as the writer:
  `CatalogService.commit_table` notes such a commit itself (`ProtectedWriteGuard.note_own_commit`)
  when it is not maintenance, not the rollback, nothing is noted for the target yet, the request
  carries the claim form and **no write fence** (a note under a fence would make `_hold_fence`
  write `rows_written`, which a reclaim reads as "committed"), and the commit adds a snapshot that
  becomes the head; a `set-snapshot-ref` to an existing snapshot is not the task's own. A zero-row
  first build followed by someone else dropping and re-creating the table cannot
  be told apart from the run's own.
- **A check proves what its SQL says** and, being counted, never raises an error that only its
  select list would.

## The authorization seam

A `Principal` model and an `AuthorizationService` interface exist **from M1**, with M1
shipping only a single bootstrap admin and an allow-all implementation behind that
interface. **M3b replaced the implementation; it did not insert new call sites.** The
call sites — every core operation taking a resolved principal — were load-bearing
architecture from the start, so that real authorization in M3b was swapping one
implementation for another, not retrofitting checks into code that never expected them.
`GrantAuthorization` (`lakana_core.grants.authorization`), backed by persisted grants on
hierarchical securables, five role templates, and task-token scope narrowing, runs at
every production call site in `lakana-server` and `lakana-worker`, including the worker's own
query-execution path. `AllowAllAuthorization` still exists — it remains correct for
bootstrapping (before any grant can exist to authorize it, see `lakana_core.bootstrap`) and
for tests that don't exercise authorization.

**Task-token scope narrowing reaches the worker's query-execution path, not just
submission.** `lakana_worker.pipeline.run_one` resolves the run's `task_token_id` (stamped
by `QueryService.submit_query` from `PrincipalRef.task_scope.token_id`) into a fresh
`TaskScope` via `_task_scope_for_run` before building the `PrincipalRef` it authorizes
each referenced table against, so `GrantAuthorization` sees the same narrowing at
table-resolution time that it already saw at submission — a token's declared asset scope
is never checked once and then forgotten. `_task_scope_for_run` and
`TaskTokenService.verify` share one implementation,
`lakana_core.identity.task_tokens.resolve_task_token_scope`, so the two can never drift
apart; `_task_scope_for_run` additionally passes `expected_principal_id=run.principal_id`,
since `QueryRun.task_token_id` has no foreign key and nothing else stops a token row's
`principal_id` from later pointing at a different principal than the one the run was
actually submitted under. A run whose token is missing, revoked, or expired, or whose
principal has since been deactivated, fails with `"Task token no longer valid."` before
any parsing. `QueryService.get_query_run` additionally scopes a task-token caller's reads
to runs submitted with that same token, so one token cannot read a run its principal
submitted through a PAT or a different token. Every audit event this failure path
produces is recorded before the transaction commits once, never in a second, separate
transaction.

**Scope narrowing covers namespace- and environment-scoped actions, not only table
reads.** `GrantAuthorization._check_task_scope` enforces three rules, in order, every time
a task-token-authenticated principal is checked:
- **An action allow-list.** `TASK_TOKEN_ACTIONS` (`grants/authorization.py`) is the *only*
  set of actions a task token may ever perform: `READ_ENVIRONMENT`, `READ_NAMESPACE`,
  `READ_TABLE`, `SUBMIT_QUERY`, `CREATE_TABLE`, `WRITE_TABLE`, `DROP_TABLE`. Anything else —
  including the three actions that work only with the `ADMIN` role (`MANAGE_GRANT`,
  `MANAGE_TOKEN`, `MANAGE_GROUP`), `MINT_TASK_TOKEN`, and every other namespace/environment mutation
  (`DROP_NAMESPACE`, `CREATE_NAMESPACE`, `MANAGE_NAMESPACE`, `MANAGE_SECRET`, ...) — is
  refused with `PermissionDenied("Task tokens may not <ACTION>.")` before the environment or
  asset-scope checks even run, regardless of what the token's own principal could otherwise
  do via its non-task-token grants. `MAINTAIN_TABLE` (M5a) is deliberately not in it —
  expiring snapshots and deleting files is not something an unattended task may do until
  M5d decides otherwise. `DEPLOY` and `MANAGE_REPOSITORY` (M5b) are not in it either: a
  deploy changes what prod runs, and registering a repository chooses which hosts the worker
  fetches from. `MANAGE_PACKAGE_INDEX` (M5c2c) is not in it either: registering a package index
  chooses which hosts a build contacts with a credential. `RUN` (M5c1) is not in it either: starting a run executes deployed code as the
  deployment's run principal, which an unattended task must not trigger.
- **Namespace coverage.** A `NAMESPACE`-kind securable must be covered by some declared
  asset-scope pattern: a `ns.*` pattern covers `ns` and everything nested under it; a
  table-shaped pattern (`ns.table`) covers `ns` itself, so a token scoped to one specific
  table can still navigate to (read) its containing namespace; a bare namespace pattern
  with no table and no wildcard (`ns`) is also accepted and covers `ns` the same way. This
  coverage is not specific to `READ_NAMESPACE` — `_check_task_scope` applies it to *every*
  `TASK_TOKEN_ACTIONS` member checked against a `NAMESPACE`-kind securable, which includes
  `READ_TABLE`: `CatalogService.list_tables` authorizes `Action.READ_TABLE` at namespace
  scope, so a bare `ns` pattern or a table-shaped `ns.table` pattern equally covers that call,
  which returns every table *name* in `ns` (the Iceberg REST route that lists tables returns that
  list as it is). The platform route `GET .../tables` then keeps only what the caller may read
  (`CatalogService.can_read_table`: tables, views, external tables and artifacts alike), so a task
  token sees there only the names its scope covers; it once failed
  with a 403 that named the first object outside the scope. Loading any table's data or schema is a separate, later
  `TABLE`-kind check against the same patterns, which a bare `ns` pattern never satisfies
  and a table-shaped `ns.table` pattern satisfies only for that one named table. A namespace
  outside every pattern's coverage is refused with `PermissionDenied("...'s task token does
  not declare namespace '<ns>'.")`. This matcher (`_matches_namespace_pattern`) inherits the
  same dotted-display-form ambiguity as the table matcher it sits beside — a namespace
  component containing a literal `.` can make two different namespaces compare equal.
  New names can no longer contain a `.`, so the ambiguity can only arise from a
  namespace or table created before that rule; those are deprecated (see
  `docs/agent/conventions.md`, "Names").
- **The existing table asset-pattern check**, unchanged, for `TABLE`-kind securables.

A token the worker mints for one Python task attempt is **bound** (M5c2a) and narrower still:
no `DROP_TABLE`; `READ_TABLE` only on the asset's declared inputs (and the tables behind an
input that is a view); `CREATE_TABLE` and `WRITE_TABLE` only on the asset's own table or artifact, whether
or not the environment is protected (a token belongs to one environment); no table listing. It
keeps `READ_ENVIRONMENT`, so it can read the deploys and runs of its environment. Its inputs are
read from the project version manifest on every request, never from the token's `asset_scope` strings. Tables are compared as tuples of parts,
and a manifest name that does not split into a valid namespace and table matches nothing.

**A bound token's lifetime, and what the worker believes (M5c2a,
`lakana_worker.python_tasks`).** The worker mints the token right before the process starts
(after the venv build, so no token waits on a build), for the asset timeout plus
`LAKANA_RUN__KILL_GRACE` plus 60 s. If `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` is below that, the
attempt fails permanently before anything is started, naming both values: a token that cannot
outlive its task is never minted (the worker also warns at startup when the cap is below what
the default timeout needs). It reaches
the harness on stdin only and is revoked (`revoke_for_task`) on every path out of the attempt:
success, failure, timeout, cancel, lost lease, drain, and an unexpected error in the worker. A
revoke that fails is logged with the key id and is harmless, because the token is valid only
while its task run is `running` with that claim. Neither the minted object nor the token string
is ever logged, and the process's output is redacted before it reaches the task log. The
process's result record is untrusted: a `succeeded` record counts only if the worker, reading
in-process, finds the target table present and changed, and a cancel, a drain or a lost lease is consulted
before a failure record, so the 401 a cancelled or reclaimed attempt gets is never recorded as
a permanent failure. A reclaimed attempt records nothing: recording stays conditional on lease
owner and claim counter, as in M5c1. The per-attempt directory (unpacked code, `HOME`, `TMPDIR`,
the result file) is private (0700), lives under a private parent, and is removed with
`remove_private_tree` once the process is dead.

**Navigation is broader than either check above by itself.** `CatalogService.list_namespaces`
authorizes `Action.READ_NAMESPACE` at *environment* scope, an `ENVIRONMENT`-kind securable
`_check_task_scope` never narrows by asset-scope pattern once the environment itself matches
— so a task token can list every namespace *name* in its declared environment regardless of
its asset scope, and (per the namespace-coverage bullet above) every table *name* in whatever
namespaces its patterns cover. Neither listing lets it load a table outside its declared
asset scope; that refusal is unchanged.

`MINT_TASK_TOKEN` needs the `ADMIN` role only to mint for another principal (M5c2a): a
holder mints task tokens only for itself; minting for another principal needs the `ADMIN` role at
instance scope (`lakana_core.grants.admin.holds_instance_admin`), and a refusal is audited as
`AUTHORIZATION_DENIED` with `reason: not_self`. Revoking over REST follows the same rule: a holder revokes only a token minted for itself; another principal's token, or a bound token a worker minted for a task, needs `ADMIN`, and the refusal is identical for an unknown key id. The three actions `MANAGE_GRANT`,
`MANAGE_TOKEN` and `MANAGE_GROUP` have their own rule, below.

**The three actions that need the `ADMIN` role (M5c2c).** Every operation guarded by
`MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN` goes through
`lakana_core.grants.admin.authorize_admin_only`, which asks the authorization service for the
action at instance scope and then requires `holds_instance_admin`. No role other than `ADMIN`
carries any of the three, and a grant below instance scope never satisfied these checks.
- **A refusal** is `PermissionDenied` with `admin_only_message`, audited as
  `AUTHORIZATION_DENIED` with `reason: not_instance_admin`, through the denial limiter
  (`AuditingAuthorization.record_denial`; a bare `GrantAuthorization` refuses without an audit
  row, as for every denial).
- **Granting them.** `GrantService.grant` refuses a single-action grant of the three at any
  scope, with a `ValidationError` (422) that says to grant the `ADMIN` role instead. An existing
  one is kept, has no effect, is named in one WARNING at server start
  (`ineffective_grants_warning(session)`: how many, at most twenty with id, action and subject,
  the `grant create` command for the kind of subject that occurs, and who must act now). The
  `lakana grant list` table shows the marker `no effect` in a `note` column with one explaining
  line under the table, followed by the command that revokes each marked grant with its whole id
  (the first five, then a count; a narrow table may still leave `id` out when subjects are ids); `--output json`, the SDK and REST
  (`GrantOut.note`) carry the full
  sentence `no effect: <ACTION> needs the ADMIN role`. Nothing is deleted on upgrade.
- **The bootstrap is the one exemption.** `AllowAllAuthorization` asks for no role: it creates
  the first administrator before one exists. Nothing else in `packages/*/src` constructs one,
  which a test pins.
- **A minted bootstrap token never reaches a log (#208).** When `bootstrap_if_needed` has to
  create the first administrator and no key id was supplied, it writes the token to
  `LAKANA_BOOTSTRAP_TOKEN_FILE` (`write_token_file`: `O_CREAT | O_EXCL | O_NOFOLLOW`, mode 0600
  whatever the umask, `fsync`ed, the file removed again if the commit fails) and the server
  logs, at WARNING, only the administrator's name, the key id and the path. The setting has no
  default (the server has no data directory of its own); unset in that case, start-up fails
  naming it, before any state changes. An existing file or symlink at the path is refused, not
  overwritten. A supplied key id already used by some PAT (a revoked one keeps its key id)
  fails start-up with `ConflictError`; no random token is invented. The supplied-credential
  path is unchanged: DEBUG, no token, no file. Audit of other token sources: PAT issue/mint
  returns the token once in the HTTP response only, task tokens go to the child's stdin, and
  `PatService`, `TaskTokenService` and the auth paths log no token; a test pins that the
  bootstrap emits no `lakana_pat_` string at any level.
- **A principal's own tokens** stay administrator-only: issuing, listing and revoking a personal
  access token needs `MANAGE_TOKEN` whoever it is for. This element adds no self-service; that
  belongs to the sign-in design (#32).
- **Call sites.** `test_admin_only_call_sites.py` enumerates the thirteen call sites (four in
  `grants/service.py`, six in `identity/groups.py`, three in `identity/pats.py`) and fails on
  any other module that names one of the three actions. A new call site must go through the
  helper and into that test's `_GUARDED`.
- **Not covered by the rule** (accepted, see "What is explicitly not defended"): a holder of
  `MANAGE_POLICY` can remove a row filter or column mask that applies to itself;
  a holder of `MANAGE_PRINCIPAL` can deactivate every administrator but the last, and nothing
  reactivates a principal; `MANAGE_ENVIRONMENT` with `DEPLOY` and `RUN` can bind any active
  service account, an administrator's included, as an environment's run principal. The run's
  token is a bound task token, confined to one environment, and never reaches the three; what
  it gains is data access as that service account in that environment. `MANAGE_POLICY` gains
  data access too (the rows or values a filter or mask hid). `DEPLOY` and `RUN` on an
  environment with a bound run principal, and `MANAGE_REPOSITORY` with both, reach the same
  data access. `MANAGE_PACKAGE_INDEX` can be granted as a single action; its holder cannot
  redirect a secret to a host of their choice, because a secret is bound to one use and only
  `MANAGE_SECRET` binds it (see "Package index credentials (M5c2c)"). Under `LocalProcessRunner` a
  holder who also has `DEPLOY` and `RUN` still reads what the worker reads (one trust domain):
  binding stops an index manager from redirecting a secret, not code that runs in the worker. Under
  `DockerRunner` a task container holds no credential, so there it holds against task code too
  (build-time code of an sdist still sees what its build holds). The rule is not retroactive:
  what a
  holder of one of the three created earlier (grants, memberships, tokens) stays.

**Mint hygiene and TTL cap.** `TaskTokenService.mint` rejects a malformed asset-scope
pattern before it can be minted: an empty or whitespace-only pattern, one with an empty
dot-separated component (`.*` splits to `["", "*"]`, `"bronze."` to `["bronze", ""]`,
`".a"` to `["", "a"]`), or one whose first component is a bare `*` (`*`, `*.x`, `*.*`) —
every shape that is malformed or fails to name one specific namespace, so it could never
usefully scope a token in the first place. None of these actually behave as a wildcard
that would match every namespace under the coverage matchers above — a pattern like `.*`
matches nothing, not everything, since no real namespace's dotted form starts with a
leading `.` — so they are rejected for being useless/confusing to declare, not because
leaving them minted would silently widen access. It also caps `ttl_seconds` at
`TaskTokenService`'s `max_ttl_seconds` (constructor parameter, default 86 400 seconds/one
day), so an unattended task's credential cannot outlive human review indefinitely just by
requesting a very long lifetime. In `lakana-server`, that cap is operator-configurable via
`Settings.task_tokens.max_ttl_seconds` (`lakana_core.config.TaskTokensConfig`, itself
`Field(gt=0)` so a non-positive configured cap fails at settings load, not by silently
rejecting every mint), read from `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` and threaded into
`TaskTokenService` by `deps.get_task_token_service`. The worker builds its own
`TaskTokenService` from the same setting to mint the bound tokens of Python tasks, so the value
must be identical on the server and the worker; a cap lowered on the server alone does not
apply to the worker's tokens. Both rejections raise
`ValidationError`, mapped to `422` over the REST API. `TaskTokenMintIn.ttl_seconds` (the
request schema) enforces only `gt=0` — `TaskTokenService.mint` is the single source of the
upper bound; do not reintroduce a static `le=` on the schema, which would silently
override an operator's configured cap above it.

**Bounding failed-authentication audit writes.** `get_principal`
(`lakana_server.deps`) does not record an `AUTHENTICATION_FAILED` `AuditEvent` for every
failed attempt unconditionally — that would let an attacker retrying a bad credential
flood the audit log one row per request. `lakana_server.auth_rate_limit.AuthAuditLimiter` is
a per-client (`request.client.host`) token bucket, refilling and capped at
`Settings.auth_audit.per_ip_per_minute` (`lakana_server.settings.AuthAuditConfig`,
`Field(gt=0)`, default 10) tokens per 60 seconds, read from
`LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE` and constructed once on
`app.state.auth_audit_limiter` by the lifespan. `get_principal` consults it only on the
failure path: a client past its rate still gets `401`, but no row is written for that
attempt; the number of attempts suppressed since that client's last recorded failure is
attached to the next row that *is* recorded, as `detail.suppressed` (present only when
nonzero), so the flood stays visible in aggregate rather than one row per attempt. The
recorded `key_id` is truncated to 64 characters, regardless of how long the caller's
presented `key_id` was. The limiter's client dictionary is bounded at 10 000 entries
(oldest evicted first) and guarded by a lock, since FastAPI runs `get_principal` in a
threadpool. `X-Forwarded-For` is not trusted for the client key — see the threat model's
"Explicitly not defended" for what that means for a proxied deployment.

**Bounding successful-authentication audit writes (#217).** `get_principal` records
`AUTHENTICATION_SUCCEEDED` only when `lakana_server.sign_in_audit.SignInAuditLimiter`
(`app.state.sign_in_audit_limiter`, built by the lifespan from
`LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES`, `lakana_server.settings.ServerAuditConfig`, default 15,
0 to 1,440; server only) admits it: once per credential per window, the window running from the
last *recorded* row, so a poller writes one row per window, not none. The next recorded row carries
`detail.suppressed` (only when nonzero). A suppressed success writes nothing and commits nothing in
`get_principal` (`PatService.verify`'s own `last_used_at` commit is unchanged). **Mechanism: in
process, not a database lookup** — a lookup would add a query to every authenticated request (and
an index on `detail->>'key_id'`), exactly the per-request database cost this exists to remove; the
price is that each server process records a credential once per window, and a restart records
again. **Key:** `(kind, key_id)` with `kind` `"pat"`, `"task"` or `"ci"` (a PAT, a task token and a CI token cannot share
a window), and the principal kept beside it: the same key id authenticating another principal is
recorded. Key ids are random and unique per table and a revoked token keeps its own
(`credential.py`, `unique=True`), so a revoked-and-recreated token is a new credential. **Every rule
can only add rows:** a failed attempt that parsed a key id (`forget`) makes that credential's next
success recorded (a token that works again after a refusal, e.g. after its principal was
reactivated, leaves a row; an attacker who knows a key id can at most cause one extra success row
per attempt, never hide one); a row whose write raised (`not_recorded`) makes the next success
recorded and counts this one; the dict is bounded at 10 000 credentials, least recently used
evicted, and an evicted credential is recorded at its next success (its pending count is lost);
only verified credentials are inserted, so an unauthenticated caller cannot grow it. Counts pending
when the process stops are not written (unlike `DenialAuditLimiter.drain`). Failures are
unchanged (`AuthAuditLimiter` above). `authenticate_before_body` already makes a request one
`get_principal` call, so a body route is not counted twice.

## Query isolation: credential scoping, not blacklisting (M2/M3)

**This is running code, not a plan.** The query executor
(`lakana_worker.executor`, whose connection lives in the query child `lakana_worker.query_child`
since M5c3a), the rewriter (`lakana_core.query_rewrite`), and the DuckDB
integration shipped in M2; M3c extended the rewriter with row filters and column masks.

Policy enforcement works by rewriting SQL — but a rewriter can be bypassed by a query
that reads bytes directly, e.g.
`SELECT * FROM read_parquet('s3://…/managed/gold/customers/…')`. Blacklisting function names
(`read_parquet`, `ATTACH`, `INSTALL`, `COPY … TO`) is a losing game: DuckDB ships new
file-reading functions regularly, and the list is always one release behind.

**Known gap, narrowed by allow-list mode.** In local `file://` mode there is no cloud
credential to scope. The query process runs in the same allow-list mode as everywhere else
(see "Allow-list mode" under "The query child"): it opens the directories of the tables the
query reads, its private temp directory and the CA bundle, and DuckDB refuses anything else
at planning, a `..` path, a symlink out of a table directory (DuckDB compares real paths) and
a glob above a table directory included. What remains, reachable only if the guard is
passed: reading inside the allowed table directories, which includes the files of a table
nested in another's location and a raw claim whose listing a later external read would pick
up; the temp directory; reading the CA bundle. DuckDB would also let a statement write where it
may read; the child runs no statement that writes (`COPY … TO`, `INSERT`, `UPDATE`, `DELETE`,
`MERGE`), at the top level or nested inside a `SELECT`, nor a function that runs SQL given as a
value (see "The child runs one SELECT" and "And that SELECT writes nothing" under "The query
child").

**An `EXTERNAL` table's `location_pattern` claims an exclusive `raw/<dir>` directory,
never an arbitrary glob.** `lakana_core.catalog.external_location.parse_external_location`
confines every `location_pattern` to `raw/<dir…>/<basename-glob>`: the directory (the
table's **claim**) is exclusive to one table per environment, and the glob matches only
direct children of the claim, never a subdirectory, whatever `Storage.list` itself
returns — reading a raw file by any other means (a direct `fnmatch` against a stored
pattern, for instance) reopens the bypass this confinement exists to close.

Exclusivity is enforced at three points, not just one, because a row created before this
scheme existed — where `tables.external_claim` is `NULL` — must not go unchecked:
- **Create time:** `check_claim_free` runs under an environment row lock (taken only around
  the check and the insert, after the pattern has already been listed and read, so a slow
  storage listing never serialises unrelated creates behind it), and treats a `NULL`-claim
  row as claiming nothing only if its own stored pattern *also* fails to parse — a `NULL`
  claim whose pattern *would* parse is still compared for overlap, fail closed.
- **The migration backfill:** revision `0011` sets `external_claim` on every pre-existing
  `EXTERNAL` row whose stored `location_pattern` already happens to parse under the new rule
  (a self-contained, duplicated parser, since a migration must not import application code);
  a row whose pattern doesn't parse is left `NULL` and stays fail-closed until re-created.
- **Query time:** the worker re-parses the stored pattern, refuses to read unless
  `external_claim` is set and equals the freshly parsed claim, and re-runs `check_claim_free`
  itself (`exclude_table_id` set to the row's own id) as defence in depth against a claim that
  reached the row by some path other than `create_external_table` — a direct mutation, or a
  future bug. Any of these failing fails the query outright; no result is written.

`claims_overlap`'s component comparison uses Unicode canonical caseless matching
(NFD, casefold, NFD again — not a single NFC-then-casefold pass, which disagrees with the
canonical algorithm on some characters), so `raw/Customers`
and `raw/customers` — or an NFD- and an NFC-encoded `raw/café` — are still caught as the
same directory on a case- or normalization-insensitive local filesystem. **Known gap, partly
closed:** on local (`file://`) storage, a symlink placed under a table's `raw/` claim is
still followed by the listing/read that infers its schema at creation. The query process no
longer follows it: allow-list mode refuses a symlink whose target lies outside the
directories the query may open (unless it points into another table the same query reads).
Placing one needs write access to the *host* filesystem underneath the storage root.

### The query child (M5c3a)

Every query (a read, a write stream, a describe, and the start-up check) runs in a child process,
`python -I -B -m lakana_worker.query_child`, started by `lakana_worker.query_process`. The worker's
own process opens no DuckDB connection and imports no `duckdb`.

- **What it is given** is its request, written to its standard input: `mode`, `sql`,
  `parameters`, `read_prefixes`, `ca_bundle`, `spill`, `memory_limit`, `threads`,
  `temp_directory`, `max_temp_directory_size`, `batch_rows`, `extension_directory` and
  `parent_pid`: the rewritten SQL and the values it reads as variables (the presigned URLs among
  them), the locations it may open, the path of its CA bundle, whether it may spill, the limits
  DuckDB applies, and two process facts. No
  database URL, no storage credential, no token, no secret.
  `test_the_document_has_exactly_the_fields_the_child_is_given` pins the list.
  The request is at most 64 MiB (`MAX_REQUEST_BYTES`; one presigned URL per data file, about
  0.5 kB each). The executor measures it before it starts a process and refuses a larger one with
  a message that names the number of files; the child keeps its own check.
- **Its environment** is built from scratch: `PATH`, a private `HOME` and `TMPDIR` inside its
  private `lakana-spill-*` directory, and the worker's `TZ` when it has one (DuckDB takes the
  session zone from it). Nothing else of the worker's environment reaches it
  (`test_the_child_sees_nothing_of_the_workers_environment` reads a running child's
  `/proc/<pid>/environ`). `-B` keeps it from writing bytecode.
- **No descriptor** but the three pipes (standard input, output, error) is inherited, and
  descriptor 1 is pointed at standard error before anything runs, so DuckDB's progress bar or an
  extension's print cannot get into the result stream.
- **The connection is opened with `threads` and `async_threads`** (`connection_config`):
  DuckDB 2.0 starts a pool of I/O threads per connection, one per core by default (80 on a
  20-core machine, 84 OS threads per query process against 23 on 1.5.5); the child asks for
  `ASYNC_THREADS_PER_THREAD` (4) per processor thread. `async_threads` exists only as a
  connection option: `SET async_threads` is refused once external access is off.
- **`SET extension_directories`** (DuckDB 2.0's list-valued name; a string fails the cast) is
  the first statement. A child with an empty `HOME` would look for `$HOME/.duckdb/extensions`,
  find nothing and try to download `httpfs`; it is told the worker's `~/.duckdb/extensions`
  instead (the image fills it at build time, local mode on the first query), so it loads the
  extension without network access. Then `allow_community_extensions = false` and
  `allow_extension_repositories = 'forbidden'`: the default `undecided` may still change to
  `allowed` at run time, `forbidden` may not and refuses `CREATE EXTENSION REPOSITORY`. All
  three read back in `test_the_connection_holds_no_credential_and_loads_nothing_by_itself`, and
  in a real child in `test_a_child_reads_back_the_settings_duckdb_2_names_differently`.
- **The child dies with the worker:** `PR_SET_PDEATHSIG` (Linux), and a `parent_pid` check at
  start that refuses to run for another parent.
- **Allow-list mode.** The connection runs with `enable_external_access = false`,
  `allowed_paths = [the CA bundle]` and `allowed_directories = [read_prefixes]`, replacing
  `disabled_filesystems = 'LocalFileSystem'` (under which DuckDB 2.0 cannot load its CA bundle,
  so every HTTPS read failed). The prefixes are **per query and per table**: the location of
  each table the query reads (an Iceberg table's id-suffixed location, an external table's
  `raw/<dir>` claim) in the form its files' URLs take, made by
  `Storage.read_url_prefix` (local: the absolute directory plus `/`; remote: the signer's URL
  of `<location>/<sentinel>` without query, fragment and sentinel, so path-style, virtual-host
  and SAS URLs come out right without parsing endpoints; any other shape raises). An Iceberg
  location that does not start with the environment's root is refused, so a foreign commit
  through the REST catalog cannot widen the list to another host. A direct `execute_query`
  call without prefixes can open nothing. `query_child.configure`'s order is load-bearing and
  documented in the module docstring: `ca_cert_file`, `temp_directory`, `allowed_paths` and
  `allowed_directories` must be set before `enable_external_access = false` (DuckDB refuses to
  change them afterwards); the limits after it; **`lock_configuration = true` last**, after
  which no `SET`, `RESET` or `PRAGMA` of any setting succeeds while `SET VARIABLE` still does
  (probed on 1.5.5 and 2.0.0). The lock holds **settings only**: behind the guard (which
  refuses `SET` itself) it stops `SET`/`RESET`/`PRAGMA`/`SET GLOBAL` of `http_proxy` (a proxy
  would receive the query's presigned URLs; it defaults from `HTTP_PROXY`, which the child's
  environment does not have), `extra_http_headers`, `ca_cert_file`, `lambda_syntax`,
  `error_on_division_by_zero` and `allow_extension_repositories`. It does **not** stop a secret: after the lock
  `CREATE SECRET (TYPE http, HTTP_PROXY …)` and `CREATE SECRET (TYPE http, EXTRA_HTTP_HEADERS …)`
  still succeed, and a proxy secret then received `HEAD http://<store>/…?X-Amz-Signature=…`
  over plain HTTP (the Compose default) or a `CONNECT` over HTTPS, on 1.5.5 and 2.0.0 (review
  of #279).
- **The child runs one SELECT, checked by DuckDB's own parser.** Before anything is
  configured, `serve` splits the request's SQL with `connection.extract_statements` and runs it
  only if that is exactly one statement of type `SELECT` (every mode sends one: a read, a write
  stream, and a describe, which wraps it in `SELECT * FROM (…) LIMIT 0`; the start-up check runs
  no SQL). Anything else is an `internal` failure ("the query process was given something other
  than one SELECT statement. This is a defect of Lakana"), nothing run: a second statement,
  `CREATE SECRET`, `SET`, `SET VARIABLE`, `PRAGMA` that sets something, `ATTACH`, `INSTALL`,
  `LOAD`, `COPY`, `CALL`, `EXPLAIN`, a `CREATE`. Pinned by
  `test_a_proxy_secret_in_the_statement_is_refused_before_anything_runs` (red before the check:
  the stand-in proxy received the presigned URL) and
  `test_anything_but_one_select_is_refused_by_the_child`. Together with the lock this closes the
  proxy and header routes for SQL past the guard.
- **And that SELECT writes nothing, checked by DuckDB's parse tree.** On DuckDB 2,
  `extract_statements` reports a write inside a CTE as one `SELECT`, at any depth (a subquery, a
  `UNION` branch, `EXISTS`, a lambda): `WITH x AS (COPY … TO …) SELECT 1`, and `INSERT`, `UPDATE`
  and `DELETE … RETURNING` in a CTE (`MERGE` and `COPY … FROM` in a CTE are parser errors). Without
  this check it ran on 2.0.0: in remote mode a nested `COPY` wrote into the query's private temp directory,
  which `max_temp_directory_size` does not bound (it limits spilling only), so a query could fill
  the worker's temp file system until its time limit; in local mode it wrote into a table
  directory. So after the statement check, still before anything is configured, `serve` asks
  DuckDB for the tree (`SELECT json_serialize_sql(?)`, the SQL bound as a value) and runs the
  statement only if `query_child.reads_only` accepts it: one statement, no `"error"`, and every
  query node anywhere in the tree of a type in `READING_QUERY_NODES` (`SELECT_NODE`,
  `SET_OPERATION_NODE`, `RECURSIVE_CTE_NODE`, `CTE_NODE`). An allow-list, so `COPY_QUERY_NODE`,
  `INSERT_QUERY_NODE`, `UPDATE_QUERY_NODE`, `DELETE_QUERY_NODE` and any node type a later DuckDB
  adds are refused; an object with the fields of a query node (`modifiers`, `cte_map`) counts as
  one whatever its type is called. The tree shows SQL given as a value only as text, so the same
  walk refuses any call (in `FROM` or as an expression, any schema or catalog, quoted, upper-case
  or as a method) of a function in `SQL_RUNNING_FUNCTIONS`: `json_execute_serialized_sql`,
  `query`, `query_table`, `json_serialize_plan`, and `postgres_*`/`mysql_*` query functions of
  extensions the child never loads. Without it, `SELECT * FROM
  json_execute_serialized_sql(json_serialize_sql('WITH x AS (COPY … TO …) SELECT 1'))` passed the
  node check and wrote the file in both modes on 2.0.0 (review of #286). The list was derived
  from 2.0.0's `duckdb_functions()` like the guard's blacklist; its docstring records how and why
  `json_serialize_sql`, `json_deserialize_sql`, `aggregate` and the `pragma_*` functions are left
  out. Anything else, and a serialisation that raises or reports an
  error, is an `internal` failure (`WRITES_IN_SELECT`: "the query process was given a SELECT that
  may write … This is a defect of Lakana"), nothing run. The guard refuses all of these first
  (sqlglot cannot parse `COPY` in a CTE; `_reject_statements_inside` refuses the other writes;
  the table-function allow-list and the blacklist refuse the SQL-running functions). Pinned by
  `test_a_write_nested_in_a_select_is_refused_before_the_connection_is_configured`,
  `test_a_copy_nested_in_a_select_writes_nothing_into_the_temp_directory` and
  `test_a_copy_nested_in_a_select_writes_nothing_into_a_table_directory` (red on 2.0.0 before the
  check, and the serialised form before the function check: the file was written), and the
  guard-accepted shapes in
  `test_one_select_in_any_spelling_still_runs` (`WITH`, `WITH RECURSIVE`, `MATERIALIZED`,
  FROM-first, `VALUES`, set operations, `PIVOT … IN`, `UNPIVOT`, lambdas, JSON arrows, window and
  `FILTER`, subqueries) still run. What still parses as one `SELECT`: on 1.5.5 a
  reading `PRAGMA` (`PRAGMA version`, `PRAGMA table_info`, read-only `pragma_*` calls, refused by
  the guard; on 2.0.0 its type is `PRAGMA` and the child refuses it), and every function a
  `SELECT` can call, which the guard and allow-list mode govern. A `PIVOT` without an `IN` list of its `ON`
  column (DuckDB then creates a type first: `[CREATE, SELECT]` on 1.5.5, `MULTI` on 2.0.0) is
  refused by the guard (`query_rewrite._reject_pivot_without_values`, both syntaxes, `UNPIVOT`
  unaffected) with `PIVOT_VALUES_MESSAGE`, so the user gets the next step rather than the child's
  defect message (review round 2 of #279).
- **What DuckDB enforces** (probed on 1.5.5 and 2.0.0, and pinned by
  `test_query_allow_list.py` against TLS servers in the test process): another host, scheme
  or port, `169.254.169.254`, user-information tricks and a sibling directory (`t10` against
  `t1/`) are refused at planning with a `Permission Error`, which the child reports as
  `invalid` with a fixed text that names no path ("the query tried to open a file or an
  address that is not one of the tables it reads"); locally also `..`, a symlink out of an
  allowed directory and a glob above one. `INSTALL` and `duckdb_extensions()` are refused (the
  extension directory is not allowed); `LOAD` of a built-in extension works. httpfs refuses
  writes to http(s).
- **The child checks the file lists itself, before anything runs.** DuckDB compares URL
  strings and lets a URL's `..` through to the server. So every string of every list-valued
  variable must start with one of the prefixes, and the rest (up to `?` for a URL) must hold no
  `.`/`..` segment, also once percent-decoded and split at `/` and `\`, and no `%2e`; each prefix
  must end in `/`, a remote one be `http(s)://host[:port]/` plus a non-empty path without user
  information, query or fragment, a local one be absolute, `normpath`-canonical and not `/`;
  the CA bundle must be absolute. A violation is an `internal` failure ("a file of the query
  lies outside the locations it was given. This is a defect of Lakana"), not retried, and
  nothing is configured or run (`file_outside_prefixes`, with a property test).
- **The CA bundle** (`lakana_worker.ca_bundle`) is one PEM file, resolved once at start-up and
  logged by path, never by content: `AWS_CA_BUNDLE`, then `SSL_CERT_FILE`, then the first
  readable file of DuckDB 2.0's own system list, then `certifi.where()`. A variable naming a
  missing, unreadable or non-PEM file stops the worker at start-up with a message that names
  it. The bundle is the connection's `ca_cert_file` and its only `allowed_paths` entry, so SQL
  past the guard could read it with `read_text`; that is public data, and the start-up check
  keeps it so: a bundle holding any PEM private key (a `BEGIN … PRIVATE KEY` header: PKCS#8,
  encrypted, RSA, EC, DSA, OpenSSH), from a variable or the system store, stops the worker with a
  message naming the file and the variable, never the key. DuckDB's refusal to read it
  (`Cannot read a CA certificate bundle`) is classified `internal` with a message naming the
  bundle, both variables and the network-settings page; it should not occur once configured.
  **Both 1.5.5 and 2.0.0 verify the store's certificate already** (probed with the fixture:
  `enable_server_cert_verification = false` notwithstanding, a certificate of another
  authority fails with `IO Error: SSL peer certificate or SSH remote key was not OK error for
  HTTP HEAD to '<presigned URL>'`, the same text on both); setting `ca_cert_file` changes which
  authorities are trusted, not whether. `configure` still sets
  `enable_server_cert_verification = true` (after `ca_cert_file`, before the lock), so the
  behaviour does not rest on a default that reads as off. A failed verification (#277) is
  `internal`, not `transient`: a retry meets the same certificate, so the task fails on its
  first attempt. The message (`STORE_CERTIFICATE_UNVERIFIED`) names the bundle, both variables
  and the network-settings section, and the store by `host:port` only: DuckDB's text quotes the
  presigned URL, so the child takes the netloc of the quoted URL and says it only when it is the
  netloc of one of the request's own prefixes (no user information), otherwise "the object
  store". Only an `IO Error` with that text counts (an error of another type quoting it, which
  SQL can produce, keeps its own class; and the class is checked beside the text, pinned by
`test_a_certificate_text_on_an_error_that_is_not_an_io_error_is_not_taken_for_one`). Lakana words the message itself, so the executor adds
  no "run it again" advice and `pipeline._without_the_value` keeps it under a policy. Pinned by
  `test_a_store_certificate_of_a_foreign_authority_fails_once_and_is_not_retried` (and its
  right-authority twin) and the `failure` unit tests. DuckDB's own HTTP retries
  (`http_retries`) still repeat the handshake inside the one attempt.
- **What allow-list mode still leaves open** (reachable only past the guard; the guard is the
  first layer, credential scoping the structural one):
  - **The whole store host:port, not just the prefix**, for SQL that spells a literal URL: a
    `..` or `%2e%2e` segment passes DuckDB's string check (the HTTP client normalises `..` and
    asks for the other path). Without a signature the store answers 403, so what remains is what
    it serves without authentication on that port: anonymous-readable buckets or objects, and
    unauthenticated endpoints on the S3 port. AWS virtual-host style confines this to the
    bucket's host. Pinned by `test_known_residual_a_literal_dotdot_reaches_the_same_server`.
  - **Unsigned requests to the allowed prefixes**: objects of a table nested in another's
    location; the same 403 limit.
  - **Redirects are followed.** httpfs follows a 3xx from an allowed URL to another host
    without consulting the allow-list (1.5.5 and 2.0.0;
    `test_known_residual_a_redirect_from_the_store_is_followed_to_another_host`). The reach is
    what the store itself redirects to; a query cannot choose the target. When the host
    redirected to presents a certificate that fails, DuckDB quotes the original URL, so the
    certificate message names the trusted store's host:port, not the failing host: misleading
    for the operator, but nothing leaks.
  - **Method and payload:** GET and HEAD only; httpfs refuses writes to http(s). No credential
    is sent (none is held); `http_proxy`/`extra_http_headers` are locked as settings, and a
    secret that sets them cannot be created, because the child runs nothing but one `SELECT`.
  - **Local `file://` mode:** reading inside the allowed table directories, the temp
    directory, and reading the CA bundle (see "Known gap" above). A write (`COPY … TO`, also
    nested in a `SELECT` or run through a function that runs SQL given as a value) is refused by
    the child's statement checks before DuckDB is configured.
  - **DNS:** the store's name is resolved per request; the operator's DNS decides.
  - Unchanged: the SeaweedFS gateway's gRPC port 18333 (not HTTP), and Python tasks (trusted
    code, not the query process).
- **A result counts only with exit status 0, the pipe read to its real end and the Arrow
  end-of-stream marker as the last eight bytes.** pyarrow reads a stream cut at a message boundary,
  or after a `SIGKILL`, as a clean end with fewer rows, so the worker checks all three itself.
  A read stores its result, and a write commits its snapshot, only after the supervisor's batch
  iterator has ended normally: batches arrive before that proof, and a partial result is never
  stored or committed.
- **A signal the worker did not send** (the kernel's out-of-memory killer, an operator) is a
  retryable `internal` failure; any other incomplete result is a non-retryable `internal` one with
  a generic message.
- **A process boundary, not a sandbox.** The child runs as the worker's user and sees the worker's
  files and network. What keeps a query from a credential is that the child holds none, as above;
  what it adds is that the worker can stop it for good.

**The structural control is that DuckDB never receives a credential broader than the
query's authorized scope.** The executor resolves table references through the catalog
into the concrete file list a principal may read and hands DuckDB presigned URLs for
exactly those files, on a connection, in a process of its own, that holds no object-store credential of its own —
the ambient `AWS_*` credentials DuckDB auto-populates are cleared, persistent secrets are
off, and the connection opens only the locations of the tables the query reads, in allow-list
mode (`lakana_worker.query_child`'s module docstring says why each of those is separately
load-bearing). A crafted query then has no
unauthorized bytes to reach — not because a check caught it, but because the process never
holds a credential that reaches them. AST blacklisting (`query_rewrite.check_blacklist`)
still runs as defence in depth; it is explicitly not meant to be the primary control —
**catalog resolution and credential scoping are what make an unauthorized read
structurally unreachable (cloud storage), regardless of which DuckDB function a query
calls; the blacklist only turns the common, obvious bypasses into a clear error instead
of a confusing one.** In local `file://` mode there is no cloud credential to scope, so
that structural guarantee does not hold — see "Known gap" above: catalog resolution,
allow-list mode (the directories of the query's own tables) and the AST blacklist are the
controls there, and the threat model's "Bypassing policy by reading files directly" row says
so too.

`_BLACKLISTED_FUNCTIONS` covers every file-reading, SQL-evaluating, or
secret-revealing DuckDB table function found by inspecting DuckDB 1.5.5's function list,
swept again on 2.0.0 (a diff of `duckdb_functions()`: 225 names new, the 18 new table
functions refused by the allow-list below, the scalars read one by one)
— `query`/`query_table`/
`json_execute_serialized_sql` each evaluate a string argument as SQL/a table reference
(`json_execute_serialized_sql` takes `json_serialize_sql`'s own output, so a query naming
only that one function can still run arbitrary SQL from a string), the scalar
`json_serialize_plan` plans the SQL text it is given and DuckDB folds `getvariable(...)` into
the plan, so it would show the session variables that carry a table's file URLs, the `parquet_*_metadata`
family and `sniff_csv` read a named file's structure or contents directly, `read_duckdb`
opens another DuckDB database file, and `which_secret` discloses which configured
credential DuckDB would use for a path. **Introspection** is a fourth class:
`current_query()` returns the *rewritten* statement as result data — the row-filter and
column-mask text spliced into the CTEs and the bound-parameter names, which a write run
could persist into a table others read — so mask and filter text would no longer be
hidden from the caller. It is blocked together with `current_setting`, `duckdb_settings`,
`duckdb_secrets`, `duckdb_extensions`, `duckdb_databases`, `duckdb_memory`,
`duckdb_temporary_files`, `duckdb_external_file_cache`, `duckdb_logs`,
`duckdb_logs_parsed`, `duckdb_log_contexts`, `enable_logging`,
`duckdb_prepared_statements`, `duckdb_variables`, `getvariable`, `duckdb_views`, `duckdb_tables`,
`duckdb_columns`, `duckdb_constraints`, `duckdb_dependencies`, `duckdb_indexes`,
`duckdb_schemas`, `duckdb_sequences`, the `pragma_*` table functions (`pragma_version`,
`pragma_platform`, `pragma_user_agent`, `pragma_database_size`, `pragma_metadata_info`,
`pragma_storage_info`, `pragma_table_info`, `pragma_show`), `sql_auto_complete`,
`duckdb_profiling_settings` and the catalog macros `pg_get_viewdef`,
`pg_get_constraintdef`, `pg_get_expr`, `get_block_size`; the
error names only the function, never policy text or a path. A new introspection function
in a DuckDB upgrade needs adding here. **`stats()` is blocked too**: DuckDB reads Parquet
statistics at bind time, below the rewriter's CTE `WHERE`, so `stats(col)` returned the
min/max (and on strings a prefix, on 2.0.0 the whole value) of rows a row filter hides.
**State-changing and name-dispatching functions** came with 2.0.0: `setval` changes a
sequence (and `nextval`/`currval`, older, are refused with it: sequence state is not data a
governed read needs), `index_key` reads a catalog index by name, and `to_aggregate_state` picks an
aggregate by a name given as data (nothing harmful found behind it, but dispatch by a name in
a string is the class that made `json_execute_serialized_sql` a bypass); all five are listed.
The four sequence and dispatch functions are refused with a message of their own
(`_NO_QUERY_USE_REASONS`): it names what the call does and says to remove it, since "query the
catalog instead" is no next step for them.
**DuckDB's internal namespace is refused as a prefix:** any function whose name starts with
`__` (`__cast`, `__between`, `__internal_barrier`, `__internal_tablefilter_*`, and the
`__internal_compress_*`/`__internal_decompress_*` 1.5.5 already had) is refused by
`check_blacklist` without a list entry, so the next release's additions are refused too; the
error points at the SQL the function stands for (`CAST`, `BETWEEN`). Like every entry, the
name is compared in lower case after sqlglot has removed quotes and any catalog or schema
qualifier, so `"SETVAL"(…)`, `main.setval(…)` and the method form `'s'.setval(…)` are refused
alike. This is a reference list confirmed against DuckDB 2.0.0, not a completeness guarantee — DuckDB adds table functions across
releases, and a future one in any of these categories (file-reading, SQL-evaluating,
secret-revealing, introspecting, statistics-reading or state-changing) needs adding here as
it's found.

**Table functions in `FROM` are allow-listed, not just blacklisted.** `check_blacklist` refuses
any table function except the generators in `_ALLOWED_TABLE_FUNCTIONS` (`range`,
`generate_series`, `unnest`) in any FROM-item position (`FROM fn()`, `LATERAL fn()`, `TABLE(fn())`),
so a function DuckDB adds later is refused by default.
`extract_table_refs` never reports a function call as a table.

**Lambdas are re-spelled, not passed through.** DuckDB 2.0 refuses the arrow lambda
(`x -> x + 1`), so `query_rewrite._render` marks every `exp.Lambda` for sqlglot's keyword form
and the rewrite (and view expansion) hands DuckDB `LAMBDA x : x + 1`. Only the node's spelling
changes: its parameters and body are subtrees the guard checked, rendered by sqlglot like any
other expression. A lambda's body extends as far right as DuckDB can read it, so a lambda
anywhere but in the arguments of a function that takes one (delimited by `,` or `)`) is wrapped
in parentheses: sqlglot turns `mod(x -> x + 10, 4)` into the operator `% 4`, which a bare
`LAMBDA x : x + 10 % 4` would take into the body, a grouping the guard never checked
(`test_a_lambda_that_is_an_operand_keeps_its_grouping`; DuckDB refuses a lambda as an operand
either way, wrapped it refuses the checked tree; review of #284).
DuckDB parses the keyword form as sqlglot does where the arrow form diverged (`x -> x -> '$.a'`,
a lambda whose body is a JSON extract, failed to bind on 1.5.5). Probed on 1.5.5 and 2.0.0 for
casts, `::`, slices, struct literals, nested and two-parameter lambdas, `AND`/`OR`, `CASE`,
`BETWEEN`, `IN`, quoted parameters (`test_a_lambda_runs_as_the_guard_parsed_it`); a refused
function inside a lambda is refused in all three input spellings (`_FUNCTION_CALL_FORMS`).
Not every arrow is a lambda: sqlglot parses `j -> 'a'` in *any* function's arguments as one,
while DuckDB reads it as a JSON extract except in a function with a `LAMBDA` parameter
(`_LAMBDA_FUNCTIONS`: the union of `duckdb_functions()` on 1.5.5 and 2.0.0) or `COLUMNS`. So
`_parse_sql`, before any check, turns such a one-parameter arrow lambda into the JSON extract
(`_json_arrows_outside_lambda_functions`, chained arrows associated to the left as DuckDB does);
its right side is the same subtree, so the guard checks it either way, and the extract renders
as an arrow again. A lambda-taking function a later DuckDB adds and the list misses only fails
to bind (its lambda reaches DuckDB as JSON); it never runs anything the guard did not see
(review of #284).

**User SQL cannot reach the values the platform sets, and may not contain a placeholder
(`?`, `$1`, `$name`).** The rewriter reads each table's resolved file list — on S3, presigned
URLs of the raw files — through `getvariable('v_<n>_files')`; the query child sets each value
before the statement (`set_variables`: ``SET VARIABLE "v_0_files" = ?::VARCHAR[]``, the value
bound) and runs the statement with no parameters (`connection.sql(sql).to_arrow_reader(n)`,
the call that streams and that keeps an Arrow export in DuckDB 2; `sql()` with parameters holds
the whole result before the first batch). An incremental write's watermark is set the same way,
as `v_watermark` (`catalog.watermark.wrap_incremental`). The names are predictable on purpose:
`getvariable` and `duckdb_variables` are blacklisted, `json_serialize_plan` is blacklisted (it
plans the SQL text it is given and folds `getvariable(...)` into the plan), a `SET`/`RESET
VARIABLE` is refused as a statement that is not a read, and every placeholder is refused, so no
user SQL reads or sets a variable; the child writes a name into `SET VARIABLE` only when it is
an identifier. `query_rewrite._reject_placeholders` refuses any `exp.Placeholder`/
`exp.Parameter` node, in `check_blacklist` and again inside every parse
`rewrite_query`/`extract_table_refs` make, including a view's stored SQL. That refusal is
load-bearing: DuckDB 2.0 resolves a `$name` no parameter is bound to as the session variable of
that name, so `SELECT $v_0_files` returns the URL list on a prepared statement (`connection.sql()`
still refuses it on 2.0.0, but nothing promises that), and the placeholder rule is what keeps
the variables unreachable — never relax it
(`test_a_named_placeholder_would_otherwise_read_a_variable`). A placeholder once bound to the
rewriter's own URL list and returned the URLs as data. `executor.execute_query`/`stream_query`/`describe_query` refuse a positional
sequence outright (`TypeError`): only a name-to-value mapping is accepted.

**A foreign commit cannot add a data file without field ids.** `commit_table` calls
`field_ids.check_added_files_have_field_ids` once, with every `AddSnapshotUpdate` of the commit,
after each manifest list is known to lie under the table's location and before anything is
applied. It opens only manifests the parent snapshot (or an earlier snapshot of the same commit)
does not have and that lie under the table's location, whatever content the manifest list claims
for them, as a read walks every manifest. It takes their `ADDED` data entries, so inherited
files, including legacy ones, are never read. It reads each footer once, eight at a time, only
for a file under the table's location (a file elsewhere is never opened, and a read refuses the
table for it). The trailer is read first and a footer above 16 MiB is refused. The check is
bounded: at most 2,000 added files per commit, and 60 seconds for all reads together
(`MAX_CHECKED_FILES`, `CHECK_DEADLINE_SECONDS`, `MAX_FOOTER_BYTES`); a commit over either bound
is refused whole (the deadline also covers the manifest reads, and a commit past the cap stops being
read at that manifest). Reads already running when the deadline passes are abandoned and end at the
storage client's own timeout (the check sets none of its own). A
manifest or manifest list (the parent's included: the limit does not distinguish what the client
sent from what it did not) is read through a stream that stops at `MAX_MANIFEST_BYTES` + 1 and
handed to pyiceberg as bytes; no separate size request exists to swap the file after (#171). A snapshot's parent is looked up only among the table's snapshots
before the commit and the earlier snapshots of the same commit; a parent that is the snapshot
itself, comes later or is unknown counts as none, so the snapshot is checked in full. A file whose
footer does not parse is refused as unreadable (422), never as storage failure. A non-Parquet entry
is reported first; otherwise the first offending file in manifest order is named with the
table. A reader's error text never reaches the caller (the log has the exception's class and
the table). A storage failure answers `StorageUnavailableError`
(503, retry), not a client fault. `append` and `write_batches` pass `own_data_files=True`:
pyiceberg wrote those files with field ids. Limits by design: a client can mark an entry
`EXISTING` in a new manifest and skip the check (the file then reads as NULL, which a policy
does not leak), field ids that match no column read as NULL, and a commit-time check cannot see
an object overwritten in storage afterwards, so it guards correctness and is no boundary
against a writer who holds storage credentials. Files committed before the check existed are
not checked; a file without field ids reads as NULL in every column. The check runs inside the
request while its database session is open; releasing it before the reads is not done.

**A query the guard cannot read is refused, never allowed.** sqlglot's parser sometimes fails
on malformed text with an error that is not a `ParseError`: a `ValueError` (a placeholder where
it expects a name) or an `AttributeError` (a node it left empty). `_parse_sql` turns these two
into a `ParseError`, so every `except SQL_SYNTAX_ERRORS` handler gives the usual syntax message
and a stored view that trips one is named, not the reader's query. Beyond them, `_fail_closed` wraps the
three entry points (`check_blacklist`, `extract_table_refs`, `rewrite_query`): any other
unexpected exception becomes a `ValidationError` ("We could not parse your SQL..."), logged at
WARNING with the exception's type and the entry point, never the SQL. No path returns normally
after an exception, so a fault cannot become an allow. `LakanaError` (so `ValidationError`),
`SQL_SYNTAX_ERRORS` and `MemoryError` pass through unchanged: the first two are the expected
refusals that callers (the pipeline's `except SQL_SYNTAX_ERRORS`) already map to a precise user
message, and a `MemoryError` is the resource limit's signal, which must not be disguised as a
syntax problem.

**No submitted query runs as a recursive query.** `query_rewrite._reject_recursive_ctes`
(in `check_blacklist` and in every `_parse_checked`) refuses a `WITH RECURSIVE` clause anywhere,
because sqlglot's scope binds a recursive CTE's name inside its base term where DuckDB reads a
real relation, and it refuses `CONNECT BY` (`exp.Connect`, plus `exp.Prior` and
`exp.ConnectByRoot`) anywhere too: sqlglot parses a hierarchical query to a plain `SELECT` and
writes the `WITH RECURSIVE` only when it renders the tree, so a keyword check on the parsed tree
alone would let a recursive query reach DuckDB. The parsed tree of a `CONNECT BY` holds no
recursive CTE, so the base-term disagreement does not arise there (its table is classified and
rewritten like any other); it is refused so the invariant holds for the SQL DuckDB is given.
Any new sqlglot syntax that renders to a construct the guard refuses when written directly
needs the same treatment.

**A submitted query holds no statement but a read, at any depth.** `check_blacklist` requires
the statement as a whole to be one of `_TOP_LEVEL_READS`: an `exp.Query` (a `SELECT`, a
`WITH ... SELECT`, a `FROM`-first query, a set operation) or a bare `exp.Values` list
(`VALUES (1, 'a'), (2, 'b')`, which DuckDB runs as a read). Every other check walks the whole
tree, so the rows of a `VALUES` list are checked as a `SELECT` list is, with the same messages:
table functions, blacklisted functions (`read_csv`, `getvariable`, `query`, `current_query`),
placeholders, statements inside, `WITH RECURSIVE` and `CONNECT BY`. sqlglot builds no scope for a top-level
`exp.Values`, so `_parse_checked` makes it `SELECT * FROM (VALUES ...) AS __lakana_values`
(its `ORDER BY`, `LIMIT` and `OFFSET` move to the `SELECT`; any other argument — an alias,
`WHERE`, `JOIN`, `SORT BY`, `FOR UPDATE`, which sqlglot attaches and DuckDB refuses there — is
refused by the allow-list `_VALUES_ARGS`) before the scope-based steps run:
the ambiguity gate, `WITH` binding resolution and the rewrite's CTEs then see the subqueries in
its rows. `_reject_statements_inside` then walks the
whole tree: a node of a statement class (`exp.DML`, which covers `INSERT`, `UPDATE`, `DELETE`,
`MERGE` and `COPY`, and `CREATE`, `DROP`, `ALTER`, `ATTACH`, `SET`, `PRAGMA`, `USE`, `INSTALL`,
a transaction, `DESCRIBE`, `SHOW`, `SUMMARIZE`, `COMMENT` and any `exp.Command`) is refused,
and so is a CTE whose body is not a query or a `PIVOT` (sqlglot reads `WITH m AS (CHECKPOINT)`
as a column named `CHECKPOINT`). DuckDB 2 executes DML inside a CTE and `extract_statements`
reports it as a `SELECT` (the query child refuses it as well, by its parse tree: "And that SELECT
writes nothing"); DuckDB 1.5 rejects it with a parser error. `_parse_checked` runs the
same walk, so `rewrite_query`, `extract_table_refs` and `expand_views` refuse it on their own.
`test_query_rewrite_guard_properties.py` nests every statement in random read shapes.

**A DuckDB error must never echo a mask or filter's own text back to a caller.** A row
filter's or column mask's restricted-subset expression is spliced directly into the
generated CTE, and DuckDB's own binder/parser errors can quote the whole rewritten
statement back verbatim under a `LINE n:` marker and a caret-marker line pointing at the
failing token. `lakana_worker.query_child.strip_duckdb_context` truncates a DuckDB exception's
text at the first such line before the child writes it as its error document, so a query that fails
inside a masked column's rewritten expression — not the mask expression itself, which is
validated at create time — never leaks that expression through `error_message`.

## What is explicitly not defended

From the threat model:

- **Hostile code in a trusted repository.** Python tasks are trusted code from a reviewed
  git repository — the trust boundary is PR review, not the runtime. Lakana is not a sandbox
  for hostile code; treating it as one would trade real usability for theatre.
  `LocalProcessRunner` runs a task as the worker's user: one instance is one trust domain, and
  everyone who may deploy Python code is trusted with everything the worker can read, and on a
  local-runner instance a holder of `MANAGE_CONNECTION` can probe what the worker host reaches
  through a connection test (see "Python tasks and bound task tokens"). `DockerRunner`
  isolates projects from each other and from the worker, not hostile code from the host:
  containers share the kernel (see "Task isolation: the launcher").
- **Inference from a model, and code in a pickled artifact (M5c2b).** An artifact is all or
  nothing: no row filter or column mask applies to a file, so a model reveals what it was trained on
  to everyone who may read it, and loading a pickle runs what the file contains. See "The artifact
  store (M5c2b)" and the threat model's "Artifacts (M5c2b)".
- **Statistical inference from aggregates.** Row filters and column masks stop direct
  reads; they do not stop someone reconstructing a masked value by cross-referencing enough
  aggregate queries.
- **A compromised platform admin.**
- **What the administrator rule does not cover (M5c2c).** `MANAGE_POLICY` lets its holder remove
  a row filter or column mask that applies to itself; `MANAGE_PRINCIPAL` lets its holder
  deactivate every administrator but the last, and nothing reactivates a principal;
  `MANAGE_ENVIRONMENT` with `DEPLOY` and `RUN` binds any active service account as a run
  principal, which gives data access as that account in one environment; grants, memberships
  and tokens created before the rule stay. `MANAGE_POLICY` and the run-principal paths gain data
  access; none gains access to the three actions. `MANAGE_PACKAGE_INDEX` and `MANAGE_REPOSITORY`,
  each grantable on its own, can name a secret only if it is already bound to that index or
  repository, and cannot change the URL of an entry that has a bound secret without
  `MANAGE_SECRET` in the secret's environment. They never bind a secret. A refusal is recorded
  as `AUTHORIZATION_DENIED` with its reason. The residual is below, under "Package index
  credentials (M5c2c)". Under `LocalProcessRunner` a holder who also has `DEPLOY` and `RUN` still
  reads what the worker reads (one trust domain): binding stops an index manager from redirecting a
  secret, not code that runs in the worker. Under `DockerRunner` a task container holds no
  credential. See "The three actions that need the `ADMIN` role".
- **Infrastructure compromise.**
- **An external Iceberg REST client holding its own storage credentials.** Lakana's Iceberg
  REST endpoints vend no storage credentials of their own —
  `lakana_server.api.iceberg.router._to_table_response` always sets `config={}` and
  `storage_credentials=[]` — so a client that already holds a credential to the underlying
  storage by some other means reads raw files directly, with no row filters, column masks,
  or per-query grant check: Lakana's controls apply only to queries run through its own
  worker. Governed credential vending is deferred (issue #5).

What the design defends against: **credential theft is real as of M3a** — PATs and task
tokens are SHA-256-hashed, never stored in plaintext, compared via
`hmac.compare_digest`, and checked for expiry, revocation, and principal deactivation on
every request (`lakana_core.identity`). **Reading tables one is not granted is real as of
M3b** — grants on hierarchical securables, role templates, and task-token scope
narrowing (`lakana_core.grants`) now back every authorization decision. **Reading masked
columns or filtered rows is real as of M3c** — row filters and column masks
(`lakana_core.policy`) are resolved per `(principal, table)` and applied inside the
rewriter's own per-table CTE. **Envelope-encrypted secrets are real as of M3d** —
credentials can now be encrypted at rest and referenced by name instead of stored as
plaintext `storage_options`, though nothing requires an environment to adopt one; see
below. **Audit evasion is real as of M3e** — the audit log now exists and is
access-controlled; see below. Privilege escalation through side channels remains future
work.

## Row filters and column masks (M3c — shipped)

- **Row filters and column masks are written** in a restricted expression subset
  (comparisons, boolean logic, literals, column references, and exactly two context
  functions — `current_user()` and `has_group('name')`; column masks additionally allow
  `substr`/`substring`, `||`, and `md5`), validated by sqlglot at create time
  (`lakana_core.policy.expressions.validate_restricted_expression`), so an invalid policy is
  rejected before it is ever stored. `user_attribute()` is deliberately deferred (issue
  #8) — it has no backing store yet.
- **They are applied inside the base-table CTE `rewrite_query` already builds**, not by a
  second identifier-resolution pass. The original design wording asked for a rewrite "after
  full identifier resolution — CTEs, subqueries, aliases, and `SELECT *` expansion all
  resolved first." What actually satisfies that requirement is structural: M2 already
  replaces every *base* table reference with a synthetic `__lakana_<dotted>_<sha256[:12]>` CTE
  reading only the catalog-resolved files (M3 added collision-resistance enforcement:
  `rewrite_query` refuses an alias claimed by two different dotted names, fail-loud, rather
  than silently letting the second claim win — `_cte_alias` itself is a pure derivation, not
  where the guard lives), and nothing above that CTE ever touches the raw file
  again. So masking and filtering happen exactly once, in that CTE's body — an explicit
  column list built from the table's resolved catalog schema, a masked column's projection
  swapped for its mask expression, and a `WHERE` ANDing every applicable filter. Every
  join, nested CTE, alias, and `SELECT *` above it inherits the masked, filtered rows
  automatically, because it reads the CTE's output rather than the file. The leak spec
  §6.4 warns about is closed by there being nowhere above the CTE to leak from, not by a
  resolution pass. Views (M5a) are resolved to their definitions before this rewrite:
  `lakana_core.query_rewrite.expand_views` replaces every view reference with a subquery of
  its stored SQL before `extract_table_refs` or the rewriter run, so the rewriter only ever
  sees base tables and applies their policies. A reader needs READ on the view and on every
  underlying table — views use invoker's rights, never definer's.
- **Composition fails closed.** Row filters AND together, so more filters only ever narrow
  further. Two or more applicable masks on one column collapse to `NULL` rather than
  guessing which of two independently-authored expressions should win. Policies apply to
  every role including `admin` — there is no bypass *of the rewriter itself* — and layer on
  top of `READ_TABLE` authorization, never replacing it.
- **A policy binds to column names, so a rename or a replaced column fails the read closed.** The
  table is read by field id, and a rename through the Iceberg REST catalog can move a name onto
  another column (`ssn` to `ssn_old`, `email` to `ssn`): a mask on `ssn` would cover the email
  column and `ssn_old` would show the real values. A column dropped and added again, also by a
  type change through `evolve_schema` (a non-promotion gets a new field id under the same name),
  is replaced the same way. Each row filter and column mask records the id of the table's current
  schema when it is created (`schema_id`; null for a raw table, a view and an artifact, for a
  filter that reads no column, and for a policy of a release before the column existed). For a
  read of an Iceberg table, `_resolve_sources` collects, from the reader's applied policies, every
  column a mask covers or its expression reads and every column a filter reads, and calls
  `policy.renames.renamed_policy_column` with the table's metadata. Two schemas are compared at a time, never
  the ones between: the policy's schema and the CURRENT one (a policy with no recorded id, made by
  an earlier release, is judged against every schema the table has had, since it may have been
  written at any point of the history, and a name touched since any of them counts),
  because schema ids are chosen by clients and a set-back makes the current schema an older one: a
  name whose owning field id differs, or a field id that changed its name with the old or the new
  name among the policy's, refuses the read before the query runs (`PolicyColumnRenamedError`,
  cause `invalid_query`, text `POLICY_COLUMN_RENAMED`, which names the first such column and no
  data). A recorded id the metadata does not have makes every column of the policy suspect. It
  applies to a read at a checked snapshot too, before the comparison above. A refused read shows
  nothing and never runs without the policy. The repair order is rename, create the policy for the
  new name, revoke the old one: the old policy is touched until it is revoked, so reads stay
  refused, and revoking it first would leave a window without any policy (two masks on one column
  collapse to `NULL`, filters are ANDed). Binding policies to field ids is not done yet. Pins:
  `test_pipeline_schema_evolution.py` (the `rename` and set-back tests), `test_policy_renames.py`.
- **A write that removes or replaces a column says so; only an administrator learns of a policy
  (#376).** `CatalogService.write_batches` compares the current schema with the planned one
  (`policy.schema_change.changed_columns`: a column whose name is gone, or whose name now owns
  another field id; widening keeps the id) and reads the table's active row filters and column
  masks by `table_id` (`broken_policies`; a mask counts its own column and its expression's, a
  filter its expression's, compared case-folded; an expression that no longer parses counts only a
  mask's own column, so the write never fails for it). Stored as kind, policy id, column and change,
  never the expression or the subject: in the `TABLE_WRITE` audit detail (`broken_policies`, a
  shown field; the audit log needs `READ_AUDIT_LOG`) and, for a write in a run, in
  `task_runs.schema_change_json` (`{"tables", "policies"}`) through the guard (`note_result` ->
  `record_pending` -> `_note_schema_change`), in the write's own transaction after the run context
  held again. A commit through `commit_table` that is not this service's own write
  (`own_data_files`), a maintenance commit or the set-back (an Iceberg REST client's) is checked
  the same way (`_commit_schema_change`): it records `TABLE_SCHEMA_CHANGED` (target, principal,
  `broken_policies`) in the swap's transaction and, under a task's run context, the same task-run
  note. A set-back that makes the schema before current again sets the task run's
  `schema_change_json` to NULL in its `settle` transaction, so no note, warning or notification line
  outlives the change. The task row is written for **every** write that removes or replaces a column, with
  or without a policy, so its presence is no existence signal. Nobody without `MANAGE_POLICY` may
  learn that a policy exists (`table get` shows none; listings need `MANAGE_POLICY`), the asset's
  author included: the run view (`schema_change_for_reader`) gives every reader the same
  `COLUMNS_CHANGED_NOTE` for such a task (it asks the reader to tell an administrator: no channel
  pushes it to one) and `policy_warnings` (id, kind, column, revoke command)
  only to a reader who `permits` `MANAGE_POLICY` on the instance; the REST, SDK and CLI output of a
  non-administrator is identical for a table with policies and one without. A job's notification
  goes to a webhook the project chooses, not to administrators, so it is neutral too: the event
  `columns_changed` (or a line in `failed`/`recovered`) for every run that removed or replaced a
  column, naming only the table. Deploy does not warn: output columns and types are known only at
  run time. The write proceeds and reads stay fail-closed as above. Pins:
  `test_policy_schema_change.py`, `test_runs_service.py`
  (`test_a_schema_change_is_one_neutral_line_unless_the_reader_manages_policies`), the server's
  `test_runs.py` (`test_a_schema_change_shows_policy_details_only_to_an_administrator`, which
  compares a viewer's and a READ_ENVIRONMENT reader's bodies with and without a policy) and
  `test_notifications_payload.py`.
- **Grants, row filters, and column masks bind to ids, never names.** A rename or a
  drop-and-recreate under the same name cannot evade either. An `EXTERNAL` table goes
  through this same rewriter and its own row filters/column masks apply to it, confined to
  an exclusive `raw/` directory claim (`lakana_core.catalog.external_location`) — see "Query
  isolation: credential scoping, not blacklisting" above for the mechanism.
- **A managed table's location is id-suffixed, assigned only by Lakana, and immutable.**
  `ResolvedStorage.table_location` suffixes every new managed table's location with its own
  id (`<name>-<table id hex>`); `create_table` refuses a caller-supplied `location` outright
  rather than honouring or confining one; `commit_table` refuses any `SetLocationUpdate`;
  and `write.metadata.path`/`write.data.path`/`write.object-storage.path` are refused
  outright as table properties, the same as `location=`. Before running a query, the worker
  (`lakana_worker.pipeline._resolve_sources`) refuses outright to read any `ICEBERG` table
  whose own location's final path segment does not end with `-<table id hex>` — a table
  created before this scheme existed fails this check unconditionally and must be
  re-created, there is no in-place migration — and refuses any data-file, manifest, or
  manifest-list path that contains a glob metacharacter (`*`, `?`, `[`, `]`) or is not
  already `posixpath.normpath`-canonical, before checking that every one of those paths
  lies within the table's own location. See `docs/security/threat-model.md`, "Path and
  location confinement", for the full mechanism, including the residual "most specific
  owner" fallback and its known gap against orphaned or `EXTERNAL`-claimed files.
- Creating or revoking a policy needs `Action.MANAGE_POLICY`; the API is
  `/api/v1/row-filters` and `/api/v1/column-masks`.
- **A column mask is checked against the table's columns at creation, always.**
  `ColumnMaskService(session, authz, catalog)` takes the catalog in its constructor and `create`
  has no parameter to skip it, so no caller can store a mask on a column the table lacks (it would
  fail every later query of that principal). The check runs after authorization and after the view
  and artifact refusals, and reading the columns needs `READ_TABLE` on the table. A caller with
  `MANAGE_POLICY` but not `READ_TABLE` is told so, the same for every column; the order means such a
  caller is still told a name is a view or an artifact, as before. The user page is
  `docs/site/how-to/mask-a-column.md`; `test_column_masks_howto.py` keeps its messages true.
- **A row filter is checked the same way, and its columns are bare.**
  `RowFilterService(session, authz, catalog)` reads the table's columns for every column the filter
  reads (a filter that reads none does not ask, so `has_group('x')` still works on a table with no
  schema yet), compares them without regard to letter case, as the database does, and names the
  alphabetically first one that is missing. `validate_restricted_expression(kind="filter")` refuses
  a qualified column (`t.region`, `read_parquet.region`): the relation a policy runs against has no
  name to qualify by, and there is no fixed alias for the reader. Both apply at creation only;
  stored filters are never re-judged, so one that names a missing column still fails every read of
  its person closed. The shared reading and wording are in `policy/columns.py`. A column mask
  expression is refused the same way when it qualifies a column. A dotted name whose first part is
  a column of the table is worded as a struct field (the columns are read only after the
  expression turned out to hold a dotted name; unreadable columns leave the qualifier wording;
  nothing here lets an expression through). The "no schema recorded yet" message teaches the order
  table with columns, then policies, then data or grants: a table created with columns has its
  schema before any data (the dangerous order is data first).
- **No stored value in an error on a policy-bound read.** A row filter or column mask
  expression runs on every stored value, hidden ones included, and the query engine's text for a
  failure quotes the value. When a query reads at least one table with a filter or mask applied
  to the reader, a failure of the query engine, of any class (Parser, Binder, Catalog, Conversion, Out of
  Range, ...), is replaced, in full, by the fixed message of `lakana_core.policy.read_errors.policy_read_failure` (it names the
  table when exactly one has a policy). The parent does it, in `_without_the_value` of
  `lakana_worker/pipeline.py`, from the error document's text, so the query child knows nothing of
  policies; it covers ad-hoc queries, asset runs, checks and views alike, and the stored
  `error_message` and a check's `error` hold the fixed text. An internal, transient (storage, network) or out-of-memory engine failure gets the fixed text and keeps its class (the worker's log keeps the details); the out-of-memory one adds the memory advice, and the memory watchdog's own stop (`MemoryStopQuery`, told apart by class, never by text) keeps its wording. The query child's wording for a file that stores a column in another type is replaced too, because a reader's own SQL can write it, and so is the view diagnosis (`_diagnose_views` is skipped on a policy-bound read). Cancellation, timeout, a stale lease and a process ended from outside keep Lakana's own wording. A
  query that reads no policy-bound table keeps the engine's text. A new error path on a policy-bound
  read goes through the same function.
- **A read at the snapshot of a check shows only the columns the table has now.**
  The query that shows the rows of a check reads a snapshot whose schema can hold a column the
  table no longer has in that form, and the check above means no mask can exist on it. Such
  columns are shown to nobody; see "The query that shows the rows of a check" under "Checks
  and the rollback".

## Secrets (M3d — shipped)

- **Secrets use envelope encryption.** `KeyProvider` (`lakana_core.secrets.keys`) is the
  interface that wraps and unwraps a per-secret data key under a deployment's master key;
  `EnvKeyProvider` is the one implementation, backed by a single base64-encoded
  `LAKANA_SECRETS__MASTER_KEY` read once into `Settings` at startup, never from `os.environ`
  directly. Each secret's key-value bag is encrypted (AES-256-GCM) under its own freshly
  generated data key, which is itself wrapped under the master key — a fresh data key and
  nonce every time, so encrypting the same values twice never produces the same ciphertext.
- **Stored format 2 binds both layers to the row, and a row names its key.**
  `secrets.format_version` (1 or 2) and `secrets.key_id` are columns (migration `0019`).
  Format 2 is the only format written; the values' AES-GCM encryption and the wrap of the data
  key (`KeyProvider.wrap(data_key, associated_data)`) both pass the output of
  `lakana_core.secrets.crypto.associated_data` as associated data: five length-prefixed fields
  (4-byte big-endian length, then the bytes) — the context `lakana.secret.<values|wrap>.v<n>`,
  the secret's id, the environment's id, the secret's name, the `key_id`. The helper is the one
  place the bytes are built; length prefixes make two different rows' bytes never equal, and the
  purpose in the context keeps the two layers from standing in for each other. **Never add a
  mutable column to it** (a rename would then need the master key; there is no rename today —
  if one is added it must decrypt and re-encrypt). `key_id` is
  `KeyProvider.key_id`: for `EnvKeyProvider` `env:` plus 8 bytes of
  `HMAC-SHA256(master key, "lakana.key-id.v1")` in hex, one-way. For someone who can read the
  database it is no offline-guess oracle beyond the one every stored `wrapped_data_key` already
  is; someone who may only list secrets never saw a wrapped key, so it relies on a full-entropy
  master key (`lakana bundle init` makes one).
- **A format-1 row (written before `0019`) is refused unless `LAKANA_SECRETS__ACCEPT_FORMAT_1`
  is on, and is written as format 2 whenever the secret is created or rotated.** The setting
  (`SecretsConfig.accept_format_1`, boolean, default false, only `true`/`false`/`1`/`0`) is process
  configuration, outside the database: `format_version` is a column of the row, so a check on it
  alone cannot tell an old secret from an old-format triple pasted onto a format-2 row by someone
  with database write access. The server and the worker build their `EnvKeyProvider` with the
  setting (`accept_format_1=`); all five decrypt sites pass `key_provider.accepts_format_1` to
  `decrypt_secret_row`, and `decrypt_secret_values` raises `LegacyFormatRefusedError` for format 1
  before anything is unwrapped or decrypted (the crypto module never reads the environment). The
  migration cannot re-encrypt (it has no master key); it labels old rows format 1 with the bare key
  id `env`. A version above the highest known is refused (`SecretFormatError`, "written by a newer
  Lakana, upgrade"), never guessed. **Downgrade attack:** setting a format-2 row's `format_version`
  to 1 reads nothing by default, and with the setting on the unbound read fails authentication on
  both layers. **With the setting on, the binding is weakened:** any old-format ciphertext ever
  written under this master key (a live row, a backup, an earlier copy) is accepted on any row, and
  the row's `key_id` is ignored; the server and the worker each log one WARNING at start-up saying
  so, and one WARNING names each format-1 row that is refused. Rotating a secret to format 2 does
  not retire its old format-1 triple; only turning the setting off does. **Accepted residual:**
  nothing counts rotations, so an older row of the *same* secret restored by someone with database
  write access still decrypts (a counter in the same row would be replayed with it).
- **Every decrypt goes through
  `decrypt_secret_row(secret, key_provider, environment_name=..., accept_format_1=...)`**
  (`StorageResolver`, `lakana_worker.deploy.resolve_credential`); it takes the binding from the
  row, so a caller cannot pass the wrong one. A failing row (altered, moved, malformed, wrong master
  key) raises `LakanaError` naming the secret, saying it could not be decrypted, to put back the key
  if `LAKANA_SECRETS__MASTER_KEY` was changed, and otherwise to restore it or set it again — never
  which bound field differed, never key material, whichever exception the cryptography raised
  (`InvalidTag`, `UnwrapError`, `ValueError` for a field of the wrong size). It logs one WARNING
  with the secret's id, `format_version`, `key_id` and `configured_key_id`: for a format-2 row,
  differing ids mean the wrong master key; equal ids mean the row was altered or moved. A missing
  master key keeps its own message. The migration's downgrade deletes format-2 secrets (the
  previous release cannot read them).
- **`SecretService` enforces write-only through a crypto/service split, not a check.**
  `lakana_core.secrets.crypto` holds the only encrypt/decrypt functions and takes no session
  or principal; `SecretService` (`create`/`list_for_environment`/`rotate`/`revoke`, each
  behind `Action.MANAGE_SECRET`) calls only `encrypt_secret_values` — decryption is
  structurally unreachable from any principal-facing method, not merely unexposed by one.
  `decrypt_secret_row` is called in exactly five places (inventory:
  `lakana-worker/tests/test_secret_use_sites.py`): `StorageResolver` (storage
  credentials), `lakana_worker.deploy.resolve_credential` (a repository's git credential,
  M5b), which reads only the deploy's own environment's secret scope and hands the plaintext
  straight to `lakana_worker.git`, and `lakana_worker.index_credentials.resolve_index_access`
  (the credential of a registered package index: at deploy, to check that the secret exists
  and holds a password, and when a project's environment is built; the plaintext lives in the
  returned `IndexAccess` objects, which hide it in `repr`, and, during a build, in the build's
  environment mapping, in the process environment of the launcher and of uv, and in the output
  redactor's list of values), `lakana_worker.notify` (the fourth: a job's webhook, below), and
  `lakana_core.connections.fetch.connection_secret` (the fifth: a connection's secret, for a running
  task's fetch and the worker's redaction; see "Connections and the run-time credential fetch (M6)").
  The fourth reads the secret named by a delivery row in the row's own environment, **only for
  one attempt**: the URL and the signing key live in local variables of `_prepare` and the
  request object (whose `repr` hides them), and go out of scope when the attempt is recorded.
  The send runs with no database transaction open.
- **`StorageResolver.for_environment` is the one decrypt-and-merge retrofit.** When an
  environment's `storage_secret_ref` is set, it decrypts the referenced secret and merges
  the plaintext values into `config.options`, winning over any same-named plaintext option
  already there — a secret must never be silently shadowed by a stale plaintext value left
  over from before it was configured. A dangling `storage_secret_ref` (revoked or missing)
  fails closed with `NotFoundError` rather than falling back to plaintext-only options.
- **Config and storage reprs redact secret values.** Because a decrypted secret can
  end up in `StorageConfig.options`, its `repr`/`str` mask every option value
  unconditionally (`__repr_args__`), never just ones matching a secret-looking key name;
  `scheme` and `root` stay visible. `DatabaseConfig.url` reprs with its password hidden via
  `sqlalchemy.engine.make_url(...).render_as_string(hide_password=True)`, falling back to
  full redaction if the URL doesn't parse. `SecretsConfig.master_key` reprs as `***` when
  set, `None` when unset. Both `lakana_core.config.Settings` and `lakana_server.settings.Settings`
  inherit this through their nested fields with no override of their own — pydantic's default
  repr calls each nested model's own `__repr__`. `hide_input_in_errors=True` on
  `DatabaseConfig`, `SecretsConfig`, and both `Settings` models keeps a `ValidationError` from
  a malformed `url`/`master_key` from echoing the bad value back in its message.
  `ResolvedStorage`'s repr needs no override of its own: it is a plain dataclass, so it
  inherits `StorageConfig`'s redaction through its `config` field, and neither
  `Storage`/`FsspecStorage` nor pyiceberg's `FileIO` override `__repr__`, so both print only
  a default, identity-only object repr. `.options`, `.url`, `.master_key`, and
  `model_dump()` are unaffected — only the string representations are redacted.
- **`CatalogService.update_environment_storage` closes a confused-deputy gap.**
  Attaching a `storage_secret_ref` to an environment requires `Action.MANAGE_SECRET` on that
  environment *in addition to* the instance-scoped `Action.MANAGE_ENVIRONMENT` the rest of
  the method authorizes against — otherwise a principal who can manage environment storage
  in general but not this environment's secrets could point it at a secret they have no
  authority to read and have the platform decrypt it for them on every query.
- **`create_environment` and `update_environment_storage` require instance-scoped
  `MANAGE_ENVIRONMENT`, never environment-scoped.** An environment-scoped grant — even one
  for a name that does not exist yet — must never be enough to create an environment or
  repoint an existing one's storage root. `delete_environment` still accepts
  environment-scoped `MANAGE_ENVIRONMENT`, since deleting an environment you already
  administer is not the same privilege as creating or re-rooting one. `restore_environment`
  authorizes the same right, but the deletion revoked the environment's own grants, so in
  practice only an instance-level holder (an administrator) restores one (#213).
- **`delete_environment` refuses while a repository is deployed** (`ConflictError`, 409; M5c1):
  a run start locks the deployment pointer, then the environment row; the delete's cascade locked
  them the other way round and the two deadlocked on Postgres (40P01). The delete now locks the
  environment row first (re-read with `populate_existing`, so protection is checked on the locked
row; a row a concurrent delete removed is a 404) and only reads the pointers, so no wait cycle
exists. The 409 names the deployed repositories; undeploy each first.
- **`Environment.is_protected` is enforced.** Both `delete_environment` and
  `update_environment_storage` raise `ValidationError` (422) when `env.is_protected` is
  set, before making any change. Since M5c1 the flag also locks writes down (see
  "Protected-environment write lockdown"). The only way to change the flag once an environment
  exists is `CatalogService.set_environment_protection`, exposed as `PATCH
  /api/v1/environments/{name}` with body `{"is_protected": bool}` and gated by the same
  instance-scoped `MANAGE_ENVIRONMENT`.
- **A deleted environment is a row with `deleted_at` (#213).** See "Deleting an environment"
  above: its tables are tombstones, it is hidden from every lookup by name, and its name stays
  taken until the sweep removes the row.
- **All four environment operations are audited.** `create_environment`,
  `delete_environment`, `update_environment_storage`, and `set_environment_protection` each
  record an `AuditEvent` (`ENVIRONMENT_CREATED`/`ENVIRONMENT_DELETED`/
  `ENVIRONMENT_STORAGE_UPDATED`/`ENVIRONMENT_PROTECTION_CHANGED`) in the same transaction as
  the change itself — including an `options` rewrite made while a `storage_secret_ref` is
  already attached. `ENVIRONMENT_CREATED`'s `detail` is `{"storage_root", "is_protected"}`;
  `ENVIRONMENT_STORAGE_UPDATED`'s is `{"options_keys", "secret_ref_set"}` — never option
  values or the secret reference's own value.

## Package index credentials (M5c2c)

A project's lock may name the URL of a registered package index (`lakana index add`, which
needs `MANAGE_PACKAGE_INDEX` on the instance: the `ADMIN` role has it, and it can be granted as a
single action). The index may have a
`credential_secret`: the name of a secret with `username` and `password` in each environment
that uses it. User reference: `docs/site/reference/package-indexes.md`; how-to:
`docs/site/how-to/private-package-index.md`.

- **A secret has one use, and only `MANAGE_SECRET` binds it.** A secret is bound to a named
  package index, a named repository, a connection of its own environment (migration 0049), or its
  own environment (storage and webhooks); the
  use is `secrets.use_kind` and `secrets.use_target_id`, and the CHECK `ck_secrets_use_consistent`
  (migration 0035, widened by 0049) keeps the pair unset together, environment-only, or kind plus
  id. The one exception for connections (S1): `MANAGE_CONNECTION` may create a new secret born
  bound to its connection (`<name>-connection`), set that secret's values again, or keep the
  secret already bound; naming any other secret, or a change `moves_secret` reports (address,
  weaker TLS, CA certificate, `auth`, `api_key_in`) without new values, also needs
  `MANAGE_SECRET` (`lakana_core.connections.service`). A connection keeps one bound secret: taking
  a secret (in `ConnectionService`, or `SecretService.create`/`bind` with a connection use, which
  also set `Connection.secret_id`) unbinds every other one bound to it, and clearing its secret or deleting it unbinds
  them (unbound, so only `MANAGE_SECRET` binds them again). `lock_entry` refuses a connection of
  another environment than the secret's. A bind that moves a secret away from a connection empties
  that connection's `secret_id` (both connections locked by id before the secret, so two moves in
  opposite directions do not deadlock; a deadlock or serialization failure that still happens is a
  409 to retry, `db.refuse_concurrent_change`), and a `secret_id` whose secret is not bound to that
  connection counts as no secret. A secret is refused for a connection that takes none. The five places that decrypt a secret
  for an index, a repository, storage, a webhook or a connection check the use first and refuse another one,
  with the command that moves it (a connection's: `connections.fetch.connection_secret`, which
  answers "no secret" instead, see "Connections and the run-time credential fetch (M6)"). `MANAGE_PACKAGE_INDEX` and `MANAGE_REPOSITORY` are not
  `MANAGE_SECRET`: their holder can name only a secret already bound to that same entry, and
  cannot change the URL of an entry with a bound secret, whatever credential it names at that
  moment (`require_secret_management_for_url_change`). Counting references to decide whether a
  secret may be bound cannot be made safe: a manager can lower the count and then redirect the
  secret. So a manager never binds. A holder of `MANAGE_SECRET` in the environment binds an
  unbound secret in one step by naming it on `index add|update` or `repo add|update`, or with
  `lakana secret bind`, or by creating the secret with its use; a secret the environment uses
  for storage or webhooks is refused when anyone names it from an index or repository, with the
  command that moves it on purpose. Every refusal is recorded as `AUTHORIZATION_DENIED`, with
  a reason (`secret_not_bound_to_entry`, `secret_bound_elsewhere`,
  `secret_used_by_environment`, `url_moves_bound_secret`) in its detail; the denial is written
  after the transaction is rolled back, so no half of a bind survives it. Migration `0034`
  binds a secret that exactly one entry names and leaves the rest unbound. Residuals: an unbound
  secret keeps serving its environment's storage and webhooks; a webhook in a `lakana.yaml`
  that was never deployed is invisible to the migration (and never runs). `MANAGE_REPOSITORY`
  follows the same rules for a repository's `token`. A bind records the target's URL in the
  `SECRET_BOUND` detail and prints it, because binding approves the URL the entry has then;
  the environment's storage secret cannot be bound to an index or a repository
  (`SecretService.bind`). Where the storage secret goes is guarded the same way (#134): while an
  environment has a `storage_secret_ref`, `update_environment_storage` changes `storage_options`
  (any key; none is judged safe) only with `MANAGE_SECRET` in that environment besides the
  instance-scoped `MANAGE_ENVIRONMENT`; the refusal is `AUTHORIZATION_DENIED` with reason
  `storage_options_move_secret`, written after the rollback. The environment row is locked for
  the check, so an attach that runs at the same time is seen. Options equal to the stored ones
  need no extra right (nothing moves). Attaching a secret records `approved_options` (`s3.endpoint` without userinfo, and `s3.region`; no
  other option, an explicit small list; a proxy or non-S3 endpoint key is not recorded) in the
  `ENVIRONMENT_STORAGE_UPDATED` detail, because it approves the options then. The Compose `setup`
  attaches `seaweedfs-s3` in one `PATCH` together with the bundle's own options, so the secret
  never lands on options someone else set (delete, recreate with another endpoint, restart ends
  on the bundle's endpoint); a `dev` at the bundle's root has its options reset that way. The storage root and scheme cannot change after creation,
  and creation sets no secret; changing `secret_ref` already needs `MANAGE_SECRET`. Without `DEPLOY` a holder of
  `MANAGE_PACKAGE_INDEX` can only stop projects (`index update --url`, `index remove`). Trust
  domain: under `LocalProcessRunner` a holder who also has `DEPLOY` and `RUN` still reads what the
  worker reads (one trust domain): binding stops an index manager from redirecting a secret, not
  code that runs in the worker, and becomes a boundary against such code only with the container
  runner.

- **The invariant, which the tests pin:** a credential is offered only to the host (and port) of
  the registered index's URL, whatever the project's files say; it is never in the project's
  directory, never in the environment or the files of the task's own process, and never in a log
  line or an error.
- **The mechanism.** The worker defines the index itself, in `UV_INDEX`, with the credential
  inside the URL (`index_credentials.py`); uv binds a URL credential to scheme, host and port.
  Two alternatives were probed against the real uv and rejected. A `.netrc` in the build's `HOME`
  matches the host name only, so another port of the same host receives the credential when it
  challenges. `UV_INDEX_<NAME>_USERNAME` and `_PASSWORD` for the project's index name are sent
  whenever `pyproject.toml` points that name at another host, unasked. The probe's ten hostile
  layouts never reached the other host with the worker-defined `UV_INDEX` form
  (`test_index_credentials.py` pins it, and `test_venvs_private_index.py` pins the argv and the
  environment).
- **Who can read the credential:** the worker while a build runs, and any code that runs inside
  the build. A build with a credential runs `uv sync --no-build`, so no package's build script
  runs and only uv itself holds it. A package without a wheel fails the build with a message.
- **The lock check keeps an unknown host out; the build holds the invariant without it.** The
  lock check (`lockfile_issues`, `source_issues`) refuses an index that is not registered, at
  deploy and again before every execution. It names indexes, not download hosts: a wheel's URL
  in the lock may point at another host, and no credential is sent there. If the check were
  bypassed, the worker still offers a credential only to a registered index's own scheme, host
  and port.
- **The worker ignores a project's `[tool.uv]` settings** when it builds (`uv sync --frozen
  --no-config`): they shape the lock on the author's machine only. It still reads the lock and
  the index definitions in `[[tool.uv.index]]`.
- **What a registered URL may be** (`normalize_index_url(url, credential=…)`, the one rule for a
  URL and whether a credential is bound): printable ASCII, no backslash, a
  plain DNS name or a canonical IP literal as host, path segments of letters, digits and
  `- . _ ~ + @`, no percent sign, space or dot segment, `http` only for loopback. Six hosts of
  the public index are refused in every case; `test.pypi.org` is accepted without a credential
  and refused with one, so none of the seven ever receives a credential. A form outside this (a
  percent sign or a space in the path, an underscore in the host) is not supported: it cannot be
  registered, and a lock that names it is refused. `PackageIndexService.create` and `update`
  call the rule with the URL and the credential the row will have, and `resolve_index_access`
  calls it again for a row with a credential before it reads the secret: a stored row that
  fails (a row written straight into the database) raises `IndexCredentialError` with its own
  message, at deploy and before a build, and no build runs without the credential.
- **`resolve_index_access` is the third decrypt site** (see "Secrets"). It runs when an
  environment is built and at deploy, where only the existence and the usable `password` are
  checked and the plaintext is dropped. A cached environment reads no secret.
- **`explicit = true` is required** for a registered index declared in `pyproject.toml`, so a
  public package of the same name cannot replace one of the index's packages. It is checked at
  deploy and before every execution.
- **The name rule is for a deploy only.** A `[[tool.uv.index]]` table with a registered index's
  name and another URL is refused at deploy (`lockfile_issues(deploying=True)`), so that an
  author is not misled. It is not applied before an execution (`source_issues`, and
  `lockfile_issues(deploying=False)`), and never to a table whose URL is a local folder: nothing
  at build time depends on a name (the worker's entries are `lakana-registered-N`), and a
  deployed project must not stop because an administrator later registered a name it uses.
- **Residuals:** see "Private package indexes (M5c2c): residuals" in
  `docs/security/threat-model.md`. In short: under `LocalProcessRunner` a task can read what the
  worker reads: a concurrent build's credential in `/proc/<pid>/environ` of the uv process, and
  the private wheels in the uv cache and in cached environments (one trust domain; under
  `DockerRunner` a task container sees no build and no cache); under `LocalProcessRunner` the key
  of a built environment has no environment in it, so an environment built with one environment's
  credential serves the same lock in another (the deploy check still requires the secret there;
  under `DockerRunner` the key holds `environment_owner`, the repository and the environment); an index that serves its files from another host that
  asks for the credential is not supported (the build fails and is retried, with no advice in the
  message); the worker's shared uv cache
  serves a private wheel to another project whose lock names its URL and hash (when the index
  marks its files as fresh), without asking the index; the index's files must be on its own host
  and port; an author who locks a public package by mistake still ships it; an administrator who
  registers an index trusts its packages with whatever a task may do.

## Connections and the run-time credential fetch (M6)

`GET /api/v1/tasks/self/connections/{name}` (`lakana_server.api.platform.task_connections`,
decision in `lakana_core.connections.fetch.fetch_for_task`) is the only route that returns a
secret's values, and the only way a secret reaches a Python asset (#23). User reference:
`docs/site/reference/connections.md`, "How a task gets its credential".

- **Check order, and why.** (1) the caller is a bound task token (`TaskScope.bound`), else 403
  `only a running task can fetch a connection` (`not_bound`: a PAT, a REST-minted task token, a CI
  token); (2) `name in bound.connections`, else 403 `` connection `NAME` is not declared by asset
  `ASSET` `` (`not_declared`); (3) the token's environment has a connection of that name, else 403
  `` connection `NAME` is not in environment `ENV` `` (`not_in_environment`). Declaration is
  judged before existence, so an undeclared name answers the same whether or not a connection of
  that name exists, in this environment or another: no existence oracle. A malformed name is just
  an undeclared one (the path parameter caps it at 63 characters, a 422 before the service). There
  is no 404 on this route.
- **An ended or re-claimed attempt is a 401, not a 403** (ruling 2026-10-24). Authentication
  resolves `bound` from the database on every request (`resolve_task_token_scope`), and a token
  whose task run is not `RUNNING` with its claim, or whose run ended or is being cancelled, does
  not authenticate at all. So it never reaches `fetch_for_task` and leaves no
  `CONNECTION_CREDENTIAL_REFUSED` row (the failed authentication is audited as any other).
  `BoundTask.connections == ()` (an asset that declares none, or a manifest value that cannot be
  read, `connections_readable=False`) refuses every name as `not_declared`.
- **Audit.** `CONNECTION_CREDENTIAL_FETCHED` (`connection`, `environment`, `repository`, `asset`,
  `run_id`, `task_run_id`, `attempt` = the claim counter; `reason` when no secret was sent) is
  added in the same transaction and committed before the route returns the body.
  `CONNECTION_CREDENTIAL_REFUSED` (the fields known, plus `reason`) is committed through the
  process's `DenialAuditLimiter` (action `FETCH_CONNECTION:<reason>`, so the first refusal of each
  reason is recorded rather than folded into an earlier kind), so a task asking in a loop is
  refused every time but recorded at the limiter's rate; a suppressed one is logged at INFO with
  `reason`, `connection`, `asset` and `task_run_id`. No detail holds a value; `connection`,
  `asset` and `attempt` are `SHOWN_FIELDS`.
- **The secret only through `require_use`.** `connection_secret` loads `Connection.secret_id`,
  treats a missing or revoked secret as none, calls `require_use(session, secret,
  SecretUse.connection(row), environment_name=...)` and only then `decrypt_secret_row`.
  `SecretUseRefused` (a `secret_id` whose secret is now bound elsewhere), and `require_use`'s
  `ValueError` (a stray `secret_id` into another environment), become `secret: null`
  with `reason: secret_not_bound` and a warning naming the connection, never a 422 that would name
  the secret and a bind command to the task. A decryption failure becomes `secret: null` with
  `reason: secret_unreadable` (`decrypt_secret_row` logs why, without a value). The S1 binding
  rules above decide which secret that can be; their tests are in `test_connection_service.py`,
  and `test_connection_fetch.py` pins that a moved or revoked secret is never sent.
- **The answer.** `TaskConnectionOut` carries the settings with a `ca_certificate` in full (the
  helper needs it) and `endpoints` from `connections.endpoints.endpoints`, the list the worker's
  egress grants and the binding's audit `url` use too. `Cache-Control: no-store`,
  `Pragma: no-cache`. The access log is uvicorn's request line and status; no body is logged
  anywhere (`test_task_connections.py` checks `caplog` and `capfd` for the value).
- **Redaction on both sides.** Both sides list the same forms of a value (the worker also hides
  the 4+-character forms of a value shorter than 4 characters; the harness's `register_secret`
  ignores such a value entirely):
  `lakana_core.secrets.forms.secret_forms` (worker) and its SDK copy
  `lakana.context.secret_forms` (harness; the SDK cannot import core), compared by
  `lakana-worker/tests/test_secret_forms_parity.py`: raw, `repr`, JSON with and without
  `ensure_ascii`, `quote` (default and `safe=""`), `quote_plus`, the `repr` of the UTF-8
  bytes (each `repr` under both quote choices, since a dict's repr of a JSON body quotes with
  `'` and escapes an apostrophe), and each JSON form again inside a `repr`, as bytes and
  JSON-encoded twice, each at least 4 characters. The harness's `_mask` masks every occurrence
  of every form as spans and joins overlapping or touching ones (`abab` in `ababab` → `***`).
  A value that cannot be written as UTF-8 (a lone surrogate) has no forms:
  `secrets.forms.require_text` refuses it at every create and rotate (and in
  `check_secret_values`), 422 naming the key only, and the worker fails a task whose stored row
  holds one, permanent, naming `lakana connection update`. Not covered: shorter values, other transformations
  (base64, slices), and derived values (an OAuth2 access token) unless a helper registers them:
  `lakana.ingest._http` registers the basic-auth header value, each OAuth2 access token when it
  arrives, and `base64("user:token")` of the egress proxy URL that httpx sends as
  `Proxy-Authorization`. It binds the credential to the base URL's origin twice: `ConnectionAuth`
  refuses another origin once per `send`, before an OAuth2 token is fetched, and `OwnOrigin`, the
  client's transport, refuses every request httpx sends there, a followed redirect included
  (httpx strips only `Authorization` on such a hop; an API key header would go along).
  The harness (`lakana.ingest._connections.fetch`) registers every value with
  `lakana.context.register_secret`; `lakana.runner._mask` replaces
  them (longest first) with the token in the failure record's `message` and `error_type`, in the
  trace on stderr and in `_clean`. `main` clears the state and the fetched connections only after
  the record and the trace are written. The worker decrypts the declared connections' secrets
  before minting the token (`python_tasks._connection_secrets`, the same `connection_secret`) and
  adds them to `secrets_to_hide` of both runners (`PythonTask.hidden`, never sent to the task, not
  in `repr`), so `OutputRedactor` masks them in the captured log, split across writes too, and in
  the record's message. A worker that cannot decrypt an active secret of a declared connection
  (no master key, or another key than the server's: `secret_unreadable`) fails the attempt
  before minting the token, permanent, `NO_RUNTIME`, naming the connection and
  `LAKANA_SECRETS__MASTER_KEY`: the server would otherwise hand the task a value the captured log
  could not hide. A secret not bound to the connection is none on both sides and only logged.
  **Window, accepted:** the worker decrypts when the attempt starts, before the task fetches; a
  secret added, bound or rotated in between (or the secret of a declared connection created
  after the check, or, on a worker without the key, a secret bound mid-attempt) is hidden by the
  harness but may appear in the worker-captured log. The worker's decryption is not audited: no value leaves
  the worker, and only the task's own fetch hands one out.
- **Not retried.** `ConnectionNotDeclared`, `ConnectionRefused`, `UsageError`,
  `FileShapeError`, `SourceRefused` and `CapExceeded` (`lakana.ingest._errors`) carry
  `retryable = False`, which `_retryable` honours for `IngestError`s of the `function` phase
  only (a user exception's own `retryable` attribute is ignored); `ConnectionChanged` is `True`.
- **The files helper's stores and HTTPS client** (`lakana.ingest._stores`, `_http`) take every
  value from the connection, nothing from the task's environment: the obstore stores get the
  endpoint, credential and `client_options` explicitly (`virtual_hosted_style_request=False`),
  the task's proxy as `proxy_url` and the trust bundle as `root_certificate`. obstore's
  constructors read every `AWS_*`, `GOOGLE_*` and `AZURE_*` variable (measured: an environment
  `GOOGLE_BEARER_TOKEN` or `AZURE_STORAGE_TOKEN` replaced the connection's key, and
  `AWS_SESSION_TOKEN` joined every S3 request), and a GCS credential provider can only hand a
  bearer token. So `_stores._without_cloud_environment` builds each store with those variables
  and `IDENTITY_ENDPOINT`, `IDENTITY_HEADER`, `MSI_ENDPOINT`, `IMDS_ENDPOINT` and
  `SERVICE_ACCOUNT` out of `os.environ` (under a lock, restored in a `finally` that also
  covers an interrupt) and puts them back after; a test per kind sets them and asserts the
  request carries only the connection's credential. With the connection's explicit key no IMDS
  or metadata fallback runs. **Residual, accepted:** only what `os.environ` shows is removed (a
  variable set with `os.putenv` or by a native extension reaches obstore), another Python or
  native thread reading these variables during a build sees them missing (glibc's `setenv` and
  `getenv` are not safe against each other), and a value another thread writes then is
  replaced by the earlier one. Only the task's own code can do any of this, and it is trusted
  (one instance, one trust domain). obstore's refusal of a value (a bad RSA key, a non-base64
  account key) is a `UsageError` that names the connection and never echoes the value; a
  bundle with no certificate that parses is refused before obstore sees it, naming
  `LAKANA_RUN__CA_BUNDLE`. The error class comes from obstore's structure only: its exception
  type, or its dump of the cause with every string literal (key, reply body) blanked first
  (`files._structure`). obstore's client
  falls back to `HTTPS_PROXY` without a proxy, so a task without the egress proxy gets an unused
  proxy (`127.0.0.1:9`) with the store's own host in `proxy_excludes`: the store connects
  directly and any other host fails closed. The httpx client has `trust_env=False` (no
  `HTTPS_PROXY`, `SSL_CERT_FILE`, `.netrc`), `follow_redirects=False` (a redirect is
  `SourceRefused`), and the path is percent-encoded per segment below `base_url`. A GCS key is
  passed on with `private_key`, `private_key_id` and `client_email` only (`gcs_base_url` and
  `disable_oauth` would redirect or unsign requests). Derived values are registered as secrets
  (Basic's base64 `user:password`, a SAS token without its `?`). Messages from obstore and
  httpx are scrubbed before they reach an exception (`files._scrubbed`): every registered secret
  form becomes `***`, URLs lose user, password and query, S3's `StringToSign`,
  `CanonicalRequest` and `SignatureProvided`, Azure's `AuthenticationErrorDetail`, signature
  query parameters and an `Authorization` header are cut; control characters in keys and URLs
  are escaped (`safe_name`). A certificate refusal is decided by type, never by the word: for
  obstore, rustls's `error: InvalidCertificate(` in its dump of the cause; for httpx, an
  `ssl.SSLCertVerificationError` in the exception chain. A 401/403 from obstore's text is the
  dump's `inner: Status { status: 401|403`, never the summary line or a body.
- **The REST helper** (`lakana.ingest.rest`, over `_http.send`) uses the same client, so the
  credential stays on the base URL's origin (`ConnectionAuth` and `OwnOrigin`). `path` is
  relative only (an absolute URL, `//host`, `?`, `#`, a backslash, a control character, an
  empty, `.` or `..` segment, also percent-encoded, or a stray `%` is a `UsageError` before anything is sent) and
  encoded per segment below the base path. A next link is resolved against the page's URL and
  must pass `same_origin` with the base URL (no userinfo), else `SourceRefused` before it is
  requested; a cursor, link or records hash an earlier page gave stops the read (`UsageError`),
  and `max_pages`, `max_bytes` (counted as bytes arrive, chunked bodies included) and `max_rows`
  bound it. Every `http` client asks for `Accept-Encoding: gzip, deflate`, and `_http.read_capped`
  (the REST pages, `files.read` over `http`, the OAuth2 token answer) counts the raw bytes against
  the cap and decodes them itself with `zlib.decompressobj().decompress(data, 64 KiB)`, counting
  each step against the cap too. After the end of a stream a gzip body starts a fresh decoder (a
  further member), and anything else is `SourceRefused` (not retried, as a body not of its
  encoding is): zlib would otherwise pile the bytes into `unused_data` uncounted and copy them
  quadratically (Batch A review I-1: a 27-byte member and a 32 MiB trailer passed a 1 MiB cap). A
  body that ends inside its stream is an `httpx.DecodingError` (retried), never a short body read
  as whole; `deflate` falls back to raw deflate at its first bytes, as httpx did. Any other
  `Content-Encoding` (zstd, br, a chain) is `SourceRefused` unread. httpx decoded a whole
  network chunk before the cap saw it: a 32 KiB zstd body became 1 GiB at a 1 MiB cap (M6 review
  M1, #356). Retries are classified by type and status only (408/429/500/502/503/504, httpx's
  timeout, network, remote-protocol and decoding errors, the egress proxy's 502/503/504; never a
  certificate refusal), within `retries` attempts and `retry_budget_seconds`, honouring
  `Retry-After`; a non-2xx body is never read. An OAuth2 401 calls
  `ClientCredentials.invalidate()` and sends once more with a new token; a second 401 is
  `SourceRefused` naming `lakana connection test`. A page is decoded as strict UTF-8 first (a BOM dropped; a NUL byte or invalid UTF-8 is a `FileShapeError`), so `json.loads` never guesses UTF-16/32 past the byte-level nesting check; polars reads UTF-8 only, and the OAuth2 token answer is decoded the same way. A CSV, JSON or NDJSON file is checked by `_formats.check_text` before any parser: data that starts with a compression magic number (gzip `1f 8b`, zlib `78` with `01`/`5e`/`9c`/`da`, zstd `28 b5 2f fd`, bzip2 `BZh`, xz `fd 37 7a 58 5a 00`) is a `FileShapeError` (polars inflates gzip, zlib and zstd by their magic, past the byte caps and the nesting check, which count compressed bytes: a 437-byte gzip `.json` crashed the task, M6 review I-1), and so is anything that is not strict UTF-8. A page or a JSON/NDJSON file nested more than `_formats.MAX_NESTING` (100) arrays and objects deep is a `FileShapeError` before it is parsed (`nested_deeper`, strings dropped, a running sum in pyarrow): pyarrow and polars recurse on the C stack and segfault the task at about 10,000 levels. A number no column holds (more than 20 digits, a float overflow, `NaN`/`Infinity`) becomes a stand-in through `json.loads`' hooks and a `FileShapeError` naming the field, never a retried "not JSON" or an `inf`. Field names and hosts in messages are cut to 100 characters. Messages show URLs through `shown_url`
  (escaped first, then userinfo, fragment and query values dropped). Each OAuth2 token, also a
  refreshed one, is registered as a secret when it arrives.
- **The SQL helper** (`lakana.ingest.sql`) only reads. Values reach a query only as bound
  parameters of `:name` placeholders (`_params.translate`, a tokenizer that skips literals,
  comments and `::`), never formatted into the asset's SQL. Every read is read-only at the
  source: PostgreSQL with `default_transaction_read_only=on`, SQLite on a `file:` URI with
  `mode=ro` (`ATTACH` and `DETACH` refused: SQLite would create the named file; the file is
  opened by the walker with `O_NOFOLLOW` per component and held open while SQLite opens it by
  path; the regular files the process opened meanwhile, read from `/dev/fd` and compared by
  device and inode, must include that file and nothing else that is still open (another
  thread's passing read is closed again by then, SQLite's file is not). A mismatch is walked and
  opened again, three times at most, then `SourceError`. The window from the walk to the second `/dev/fd` listing is held by one lock per process (`sql._OPEN_WINDOW`), never the query, and a retry waits 50 ms, then 100 ms, so another thread's burst of file activity (a scan of `/proc`) is not met three times, so reads of other files on other threads are not mistaken for a swap (PR #361 review): a swap for a link between the check and
  SQLite's open is refused (M6 review M8, #359). SQLite cannot be handed the walker's descriptor: given
  `file:/proc/self/fd/N` it reads the link and opens the name it holds, and a file unlinked and
  replaced by a link named `<name> (deleted)` was read, measured with SQLite through
  adbc-driver-sqlite 1.13), MariaDB and MySQL
  with `SET SESSION TRANSACTION READ ONLY` and `START TRANSACTION READ ONLY`. MariaDB commits
  before DDL, outside that transaction, so it gets only a statement whose first keyword is
  `SELECT`, `WITH`, `VALUES` or `TABLE`, never `INTO OUTFILE`/`DUMPFILE`; its tokenizer follows the
  session's `sql_mode` (an unknown mode, or a mode and server status that disagree about
  backslashes, sends nothing) and its values are plain scalars. Under the Docker runner libpq
  dials the relay (`hostaddr`) while `host` stays the connection's, so `verify-full` checks the
  real name; PyMySQL gets a socket to the relay and a TLS context for the real host. A query
  error is classified by SQLSTATE (MariaDB by error number). **Known limit, accepted (#357):** a
  refused PostgreSQL login is recognised by libpq's English words only, because libpq keeps no
  SQLSTATE for it (ADBC's error has none at connect, measured with 1.13); a server with a
  non-English `lc_messages` makes it a retried `SourceError` (the connection test: `failed`)
  whose message ends with `_check.PG_LOGIN_HINT`, never `credentials`. A login of Lakana's own to
  read the code was rejected as too much protocol code for the gain. libpq never
  reads the files of the user it runs as (`_check.conninfo`, also for the connection test): a
  connection carries no client certificate, so `sslcertmode=disable` never presents the worker
  user's `~/.postgresql/postgresql.crt` and key to a declared host, and `sslrootcert` (unless
  `verify-full` names the trust bundle), `sslcrl` and `passfile` point at `/dev/null/nowhere`, a
  path that cannot exist, so `require` never becomes a chain check against
  `~/.postgresql/root.crt` and `~/.pgpass` is never consulted. A service file
  (`~/.pg_service.conf`, `$PGSYSCONFDIR/pg_service.conf`) is read by libpq when `PGSERVICE` is
  set, and the bundled libpq has no `servicefile` keyword (measured: "invalid connection
  option"), so `sql.read` connects inside `_stores.without_variables` with `PGSERVICE`,
  `PGSERVICEFILE` and `PGSYSCONFDIR` out of `os.environ` (the lock and residuals of the cloud
  variables apply); the local runner's task environment never holds them anyway, and the
  connection test's subprocess gets only `LAKANA_CA_BUNDLE`. This mattered under the local
  runner, where the worker is a developer's or an operator's own account (M6 review M9, #360;
  Batch A review M-3).
  **Residuals, accepted:** PyMySQL interpolates values client-side with its own escaping (no
  server-side prepared statement), so the wire text holds them, though the asset's SQL never
  does; and read-only is a property of the transaction, so a source user with write rights could
  still call a function with side effects inside a `SELECT`. The docs tell teams to give a
  connection a read-only database user.
- **A `tcp` connection's TLS is the team's driver's.** The relay forwards bytes it never reads; a
  client checks the certificate for the real host only if it takes that name apart from the
  address it dials (the SQL Server recipe passes `sock=` to the relay and the real host as
  `dsn`). **Residual, accepted:** with python-tds 1.17.1 and pyOpenSSL 26.2 or later the name is
  checked against the certificate's CN only (a mismatch fails closed before any login; a name in
  the SAN alone cannot work); `docs/site/how-to/read-from-a-sql-server-database.md` says so and
  recommends a read-only login.
- **Residual, accepted.** Deployed code sees the credentials of the connections its asset
  declares. Under the docker runner it can send them to the endpoints of those connections only
  (task egress); under the local runner, to any host the worker reaches. Reviewing an asset's
  `connections` is reviewing what it may read and where it may send. The fetched values live in
  the task process's memory, which the task's own code can read anyway.
- **`lakana load` writes bronze only, checked before the body** (`POST
  /api/v1/environments/{env}/namespaces/{path}/tables/{name}/load`,
  `CatalogService.check_can_load`). In this
  order: the target's first namespace segment is `bronze` (`catalog.loads.require_bronze`, a 422
  with the same text for the CLI, the SDK and a raw request), the caller's write right
  (`check_can_append`, or `check_can_replace` with `CREATE_TABLE` for a first load), the
  environment's write guard (a protected environment needs break glass with
  `MANAGE_ENVIRONMENT`, also for a dry run, so nothing is uploaded before the 403), then the
  namespace and the snapshot pin; rights before existence, as every catalog precheck. The body
  then takes the governed Arrow path of `table append` (byte cap, type allowlist, full
  validation), and the write is audited by the core write it calls (`TABLE_APPENDED` or
  `TABLE_WRITE`; there is no `via: load` detail). The CLI bounds what it reads before it uploads:
  the file's size, then Parquet and Arrow batches counted as they decode (dictionaries decoded
  first), compressed Arrow IPC refused. The generic append and
  replace routes, which assets use, keep accepting any layer.

## Audit log (M3e — shipped)

- **The audit log is real.** `lakana_core.audit`: `AuditEvent` is the append-only row model,
  `AuditService.record` writes one per event, and `AuditService.query` gates reads behind a
  new instance-scoped `Action.READ_AUDIT_LOG` — the log is itself sensitive (a `WHERE`
  clause can contain a customer's name), so it sits behind its own privilege rather than
  being globally readable, exactly as planned.
- **Content in other people's audit events (query text and column values) needs `READ_QUERY_TEXT`,
  which no role carries (#88, #239).**
  `ROLE_ACTIONS[ADMIN]` is `frozenset(Action) - {READ_QUERY_TEXT}`; the right is granted as a single
  action, on the instance only (`GrantService.grant` refuses another kind with the command to use; the
  log it applies to is instance-wide, and an environment-scoped right would have to be checked per
  row against `securable_environment`, which events without one cannot answer). It is not a
  task-token action. `AuditService.query` is the **one read path** of the log and does the hiding,
  in core: every row it returns is a `ShownAuditEvent` copy (deep-copied `detail`, never the ORM row,
  so nothing a caller does is flushed back), and for a row whose `principal_id` is not the reader's
  (a `NULL` principal included) each field of `lakana_core.audit.content.hidden_fields` is replaced
  as its `Hiding` says, unless `permits(p, READ_QUERY_TEXT, instance)`, asked once per page and only
  when a row has something to hide. `Hiding.FINGERPRINT` (query text only) gives `fingerprint`
  (`{"sha256": hex of UTF-8, "length": characters}`; a non-string value by the fingerprint of its
  sorted JSON; `None` and a stored fingerprint, 64 hex digits and an int, pass unchanged).
  `Hiding.WITHHELD` gives `WITHHELD` (`{"hidden": true}`): no value, no digest, no length, whatever
  was stored (only `None` stays `None`). It is for a value from a small set, which an unsalted digest
  plus a length would give back: a watermark (#239) is the highest value of an incremental key,
  which is an int, long, decimal, date or timestamp (`catalog/watermark.py`), stored typed
  (`date:2026-10-20`, `long:7340219`); the re-review recovered such values from their digests in
  seconds by trying candidates. The reader's own events are always full. **Classification:**
  `CONTENT_FIELDS` (per event type, each field with its `Hiding`: `QUERY_COMPLETED.sql_text`
  fingerprinted, and `TABLE_WRITE.watermark` withheld, a value of the table's key column that a
  reader of the log need not hold `READ_TABLE` for, #239) and `SHOWN_FIELDS` (field names that are
  never content in any event: names, ids, counts, times, fixed reasons) are the one list. A field in
  neither is withheld (fail closed: a new field never leaks, not even as a digest), and
  `AuditService.record` warns `UnclassifiedAuditFieldWarning`, which `pyproject.toml` makes an error
  for the whole test suite, so the 100 % coverage of every `record` call turns an unclassified field
  into a failing test. Decided as shown: a break-glass `reason` (written for the reviewer of the log),
  `problem` of `CODE_INTEGRITY_FAILED` (fixed sentences), table and column names, and `schema_diff`
  (column names and types, no defaults or values). No other event carries a column value: a
  check's `CHECK_FAILED` has the count of failing rows, never the rows; `TABLE_ROLLED_BACK`,
  `TABLE_KEPT` and maintenance reports hold snapshot ids and check names; row filters and column
  masks are audited by id and subject, not their expression. Descriptions are never in an event
  (only their lengths). The fingerprint is unsalted on purpose (the same query is recognisable
  across events): a reader who guesses the exact text can confirm it; short or templated queries are
  guessable. Residual: a legacy field written by an older release and no longer in either list is
  hidden from readers without the right (over-hidden, never leaked). No other route returns another
  principal's SQL: the query status is submitter-only, the query list never selects the text, and
  the worker's and server's other audit reads (`TABLE_ROLLED_BACK` lookups, the download dedup) are
  internal and return nothing to a caller.
- **What is stored: `LAKANA_AUDIT__QUERY_TEXT=full|hash|off` (default `full`).**
  `lakana_core.audit.content.stored_query_text`, used by `runs.store.query_completed_detail` (the
  one builder of `QUERY_COMPLETED`, for the worker, the sweep and a cancel), stores the text, its
  fingerprint or `None`. The mode is process-wide, installed once at start by the server's lifespan
  and the worker's `main` (`install_query_text`, validated by `AuditConfig`; an unknown value stops
  the start, naming the setting), so both must be set alike. Existing rows are never rewritten; the
  `query_runs.sql_text` column keeps the text for the submitter's status read either way.
- **Reads of the audit log are audited (#88).** `AuditService.query` writes `ACTIVITY_VIEWED`
  (`view: audit_log`, instance scope) through `record_view` before it builds the answer, with
  `filters` (`event_type`, `environment`, `since`, `until`, `limit`), the counts of the page actually
  shown (it reads `limit + 1` rows to know the next cursor and counts `limit`), and the targeting key
  `{"principal_id": id}` for a filter on someone else. Same dedup, exemptions (a filter on the
  reader's own id; a page of only the reader's rows), fail-closed 503 and no `ADMIN` exemption as the
  other views. A `PermissionDenied` comes before anything is read or recorded.
- **Denial rows are bounded per principal.** `DenialAuditLimiter`
  (`lakana_core.audit.denial_limiter`) allows `LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`
  (30) rows per principal per minute, a token bucket modelled on the server's `AuthAuditLimiter`;
 one limiter per process is passed to every `AuditingAuthorization` (required argument:
  a server's `app.state.denial_audit_limiter`, a worker's `main`), because one per request would
  never limit. A refused denial is still a `PermissionDenied`; it is not recorded in the table, and the
  next row recorded for that principal carries the number not recorded as `detail.suppressed` (an
  operator reads it with `lakana audit list --event-type AUTHORIZATION_DENIED`). **Each denial that
  is not recorded is logged**: one INFO line from `AuditingAuthorization.authorize`, in the server's
  and the worker's log (`lakana_core.audit.authorization`), `AUTHORIZATION_DENIED not recorded in
  the audit table, because principal <id> is over LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE:
  action=<ACTION> object=<KIND env.namespace.table>`. The names are escaped and cut at 200
  characters (`shown`); no SQL and no token is in it. The line is one per suppressed denial, so the
  log, unlike the table, grows with a flood; an operator filters it on the logger name. Above INFO
  (`LAKANA_LOG__LEVEL=WARNING` or `ERROR`) the line is not printed: the denials over the rate
  then leave only the summary row in the table, and the documentation of the setting says so. The
  line's level is not raised for that. When the
  server stops cleanly (lifespan exit) or the worker stops, the counts still pending are written as
  one summary row per principal (`flush_suppressed`, `detail.suppressed`, no object), for at most 100
  principals and about 3 s, so that a slow database cannot outlast the container's stop timeout; one WARNING
  says how many principals' counts were left out. Three properties hold: the
  **first denial of each (principal, action) is always recorded**, so a principal that used up its
  rate on one action still leaves a row for every other one; each principal has its own bucket, so
  nothing one principal does uses up another's rows; and the tables are bounded
  (10,000 principals, 50,000 principal-action pairs), evicting the oldest, which can only record more.
  Residuals: once a principal is over its rate, **which object it probed is in the process log, not
  in the audit table** (the first denial of each action is the exception): a principal can pad with
  many harmless denials and then make the real probe, which leaves no row, only a log line; the
  padding itself is visible as a burst of 30 rows and a `suppressed` count. The pending count of a
  process that is killed rather than stopped (a crash, `SIGKILL`, the memory watchdog's exit) is
  never written; the limiter is per process, so N processes record up to N times the rate; a
  restart forgets it. A flood by
  an attacker holding thousands of principals can evict another principal's `suppressed` count,
  not its rows.
- **Every existing `authorize()` call site is audited without having been individually
  touched.** `AuditingAuthorization` wraps whatever `AuthorizationService` a caller already
  holds and records `AUTHORIZATION_DENIED` for the `PermissionDenied` it lets through, up to a
  rate per principal (below); authentication success/failure is recorded at the other seam,
  `get_principal`.
  `GrantService`, `SecretService`, `CatalogService`, `PatService`, `TaskTokenService`,
  `GroupService`, and `PrincipalService` additionally call `AuditService.record` explicitly
  at their own mutation points — grants, secrets, environment lifecycle/storage/protection
  changes, group creation/deletion/membership, PAT issue/revoke, task-token mint/revoke, and
  principal creation/deactivation all record an event, and repeating a revoke or
  deactivation is a no-op rather than a second event for grants, secrets, PATs, task tokens,
  row filters, and column masks alike — and the worker's `run_one` records `QUERY_COMPLETED`
  after a query finishes executing.
- **`GroupService.delete_group` refuses while it still has active references (M3f security
  re-run).** Hard-deleting a group used to let `ON DELETE CASCADE` FKs on
  `grants.group_id`/`row_filters.group_id`/`column_masks.group_id` remove every grant, row
  filter, and column mask that targeted it, with no audit event and only `MANAGE_GROUP`
  required — a member who also held a direct grant would then read a table unfiltered and
  unmasked. `delete_group` now raises `ConflictError`, naming only the counts, while any
  active grant, row filter, or column mask still references the group; a revoked reference
  does not block it. Once permitted, it records `GROUP_MEMBER_REMOVED` for each cascaded
  membership and a new `GROUP_DELETED` event, and `create_group` records `GROUP_CREATED` —
  all new `AuditEventType` members, added to the existing `String(64)`/non-native-enum
  column with no migration needed (see `0011_m3f_security.py`'s own widening).
- **Same-transaction atomicity is not uniform across every event type.** Grant changes,
  secret operations, environment lifecycle/storage/protection changes, and query
  completions are written in the same transaction as the action they record, riding along
  with that action's own commit. Authentication events and authorization denials commit
  their own row immediately instead (`AuditService.record(..., commit=True)`), because the
  action they would otherwise ride alongside never happened — there is nothing else about to
  commit in the same transaction.
- The read path is `GET /api/v1/audit-events`, gated by `Action.READ_AUDIT_LOG`. It returns
  `{items, next_cursor}`, newest first, at most `limit` (1–500, default 100) per page. The
  cursor is an opaque keyset position on `(occurred_at, id)`: it grants nothing, since every
  page re-runs authorization and the caller's filters, and a malformed one is a 422.
- `GET /api/v1/whoami` requires authentication but performs **no authorization check**:
  it only describes the caller back to itself (name, type, and task-token scope), which
  reveals nothing a 200-versus-401 probe of any other route doesn't. `lakana login` uses it
  to confirm a credential before saving it.
- `GET /api/v1/info` likewise requires authentication, performs no authorization check and
  returns only the platform version (`importlib.metadata`, never a string in the code) and the
  operator's three artifact limits (`artifact_limits`: `max_files`, `max_file_bytes`, `max_bytes`;
  numbers that an upload's refusal names anyway, sent so the CLI can scan a tree against them
  before it sends a byte). The version is deliberately not on `/healthz`. `lakana --version` asks it once (no retry) under one 3 s deadline for the whole lookup and never fails
  because of the answer.

**All five M3 sub-projects have shipped** — M3a (identity and
credentials), M3b (authorization and grants), M3c (row filters and column masks), M3d
(secrets), and M3e (the audit log, above). **M3 itself is not closed**: the security-review
exit criterion is being re-run after the M3f fixes above — see
`docs/security/threat-model.md`, "M3 exit criteria".

## Authorization: `authorize()` vs `permits()`

- **Use `authorize()` for actions, `permits()` for visibility.** `authorize(principal, action, scope)` raises `PermissionDenied` when a principal lacks the grant; it is the control for mutating operations — creating, updating, or deleting anything. `permits(principal, action, scope)` returns a boolean and is for visibility filtering — deciding what to return from a list. `permits()` does not audit, so never use it to gate an action.
- **Explaining access goes through the decision itself (#147).** `GrantAuthorization.explain`
  returns a `Decision` (allowed, the covering grants, the refusal's text); `authorize` raises from
  it and `permits` is built on `authorize`, so there is one decision. `GET
  /api/v1/principals/{name}/permissions` (`GrantService.explain`,
  `lakana_core.grants.explain.explain_permissions`) asks it action by action and adds only what
  the call sites add beyond the authorizer: for `MANAGE_GRANT`, `MANAGE_GROUP` and `MANAGE_TOKEN`
  the `holds_instance_admin` check of `authorize_admin_only` (through `needs_admin_role`, so the
  module is no call site). A new rule that a call site adds beyond the authorizer must be added
  there too, or the explanation says "allowed" where the route refuses;
  `tests/test_access_explain.py` compares every answer with the routes' own checks over a matrix
  of direct, group and role grants at every level. Who may ask: a principal about itself with a
  personal token (a task token never: it goes through `authorize_admin_only` and is refused),
  anyone else only through `authorize_admin_only(MANAGE_GRANT)`, which runs before the principal
  is looked up, so an unknown name is the same audited `403`. An administrator gets `404` for an
  unknown principal or object; a principal asking about itself is answered for a missing object
  as for one no grant names, so the route is no oracle for what exists.
- **Any new `AuthorizationService` implementation must implement both.** This includes test doubles and mocks. `permits` is defined as "`authorize` would not raise" — if `authorize()` raises on a call, `permits()` must return False for the same inputs.

## Query reads and submitter scope

- **Query reads are scoped to the submitter.** `QueryService.get_query_run` returns 404 for a run submitted by another principal, indistinguishable from a run that does not exist. A run whose submitter was deleted (principal_id NULL) is readable by nobody. Admins audit query completions through `QUERY_COMPLETED` events, not by downloading result bytes from the API.
- **A task-token caller is further scoped to its own token.** `QueryRun.task_token_id` records which token, if any, submitted a run (`None` for a PAT). A task-token-authenticated caller's `get_query_run` additionally requires `QueryRun.task_token_id` to match its own token's id, so it cannot read a run its principal submitted through a PAT or a different task token. A PAT caller sees every run its principal submitted regardless of which token, since a PAT is not itself scoped to one.
- **Cancel follows the read rule, plus one role.** `QueryService.cancel_query` lets the run's submitter cancel under exactly the rule of `get_query_run` (including a task token's own-runs scope), and a holder of `MANAGE_ENVIRONMENT` on the environment cancel anyone's run, with a personal token only: a task token never takes the manager's path, whatever its principal holds. `permits` only chooses the path; `authorize(MANAGE_ENVIRONMENT)` is the gate and what is audited (`QUERY_CANCELLED`, for each accepted request, not for a run that had ended). Everyone else gets the answer of the status read, a 404 (a 403 only where a read is a 403), so the route reveals nothing about a run the caller cannot read. That refusal is audited, for a run that is somebody else's and for one that does not exist alike (`AUTHORIZATION_DENIED`, `reason: not_submitter`, through the denial limiter of `AuditingAuthorization` like the other refusals that go beyond the wrapped service); the row differs in nothing the caller can observe and is readable only with `READ_AUDIT_LOG`. A missing environment records nothing. The route (`POST /api/v1/environments/{env}/queries/{id}/cancel`) adds no check of its own.
- **A wait reason adds no read and reveals only counts.** `waiting_for` (`lakana_core.runs.waiting`) travels inside responses that existing reads already authorize: a query's status (and the cancel's answer) under the submitter rule above, and a task of a run under `READ_ENVIRONMENT` as before. A caller who may not read the run gets the same 404 and no field. `QueryService.waiting_for` takes a run that the caller has already been allowed to read and issues no statement for any status but `QUEUED`. What it reveals, on purpose: `workers` (how many alive workers take this run's kind of work) and `ahead` (how many queued queries and deploys of **every** environment that those workers take first are older than this query) count the whole instance, because that is the order the workers dequeue in, and anyone who may submit a query may learn how long the shared queue is. They are bare numbers, computed with `COUNT`; no id, name, SQL text, host, principal or environment of anyone else's work is read into them, and a test pins that the body holds only numbers and this run's own data. A task's `upstream` holds names of assets of the same run, which the caller can already read in that run. The counts are estimates: a worker that died counts as alive (and busy) for up to one lease, a draining worker counts as free, and so does a worker whose own claim gate is closed (no heartbeat for a lease, not yet counted lost). A task's `upstream` list is capped at 50 names with `upstream_count` beside it, and the sentence escapes the names it quotes (`identifiers.shown`).
- **A failure code adds no read and reveals only a category.** `failure` (`code`, `cause`,
  `next_step`) on a task, on an attempt of the task log, and `failed_tasks` on a run travel inside
  the responses that `READ_ENVIRONMENT` (or `RUN`) already authorizes, and a caller who may not
  read the run gets the same 404 and no field. The cause and the next step are fixed sentences from
  `lakana_core.runs.failures.FAILURES`, written without any value of the run, so they leak nothing
  the free text does not; the code names a category of what the stored `error_message` already
  says (a refused credential is `secret_refused`, never its name; the index and secret names stay
  in the free text, which is redacted as before). `failed_tasks` is a `COUNT` over the run's own
  tasks. The code is chosen by the worker and the scheduler from the exception class or the call
  site, never from the text of a message.
- **The query list follows the cancel's rule and shows less than the status.** `QueryService.list_queries` (`GET /api/v1/environments/{env}/queries`) chooses its path as `cancel_query` does: a holder of `MANAGE_ENVIRONMENT` on that environment who authenticated with a personal token (`permits` chooses the path, `authorize(MANAGE_ENVIRONMENT)` is the gate) sees every row of that environment; everyone else is authorized with `SUBMIT_QUERY`, exactly as the status read, so a caller with no right on the environment gets that read's 403 (or 404 for an instance administrator and a missing environment), and sees only the rows their principal submitted (a task token: only its own token's). The rows of others are absent, not redacted, and no count or "N more" line includes them, so the list is no oracle for whether a query exists; the only figure drawn from other people's work is the wait reason's `ahead`, the instance-wide count above. A row holds `id`, `submitter` (the principal's name, `null` for a deleted one), `kind`, `target`, `status`, the three times, `error_cause` and `waiting_for`; **never** the SQL text, bound values, the error message, lineage, the result location or the worker (the SQL column is not even selected, and a test asserts the key set of the row and of the response). A manager therefore sees another user's query in the list and still cannot read it: the status route stays submitter-only. `target` (the dotted name of the table a write or maintenance run points at, as submitted) is shown to a manager without a `READ_TABLE` check, because the row is the environment administrator's overview; a table name is not table data. Reasons are computed for `QUEUED` rows only, from one read of the workers and one of the queued times (the oldest 10,001; beyond that the sentence says "more than 10000"), so the cost does not grow with the number of rows; the ended read (`--all`) is served by the index on `query_runs (environment_id, finished_at)`. A manager's page that shows another principal's query, or that filters on another `submitter` (even when it finds nothing), writes `ACTIVITY_VIEWED` (`view: queries`, below); a page of the manager's own rows, a filter on the manager, every non-manager and every task token write nothing; a refusal is recorded exactly as the status read's is. The route adds no check of its own.
- **A privileged view of other people's activity is audited, once per window, and fails closed (#88).** `lakana_core.audit.views.record_view` writes `ACTIVITY_VIEWED` for the manager path of `list_queries_page` (`view: queries`, environment scope) and for `table_health` when `readers` is shown (`view: table_readers`, table scope); `view: audit_log` (instance scope) is written by `AuditService.query` for reads of the audit log (see "Audit log"). Nothing is written when the answer holds no other principal's activity and the targeting key is `None`; a deleted submitter (`principal_id` NULL) counts as someone else. The instance `ADMIN` is not exempt. **Dedup** is a lookup in `audit_events` for a row of the same principal, view, scope (the four `securable_*` columns) and targeting key within `LAKANA_VIEW_AUDIT__WINDOW_MINUTES` (default 60, 1 to 1,440), served by `ix_audit_events_event_type_principal_id_occurred_at` (migration `0046`); the cursor, the status filter, `include_ended`, `limit` and `days` are not part of the key, so paging and polling write nothing more; another environment, table or `submitter` is another row. Two concurrent first reads may both write a row (no lock: the window saves rows, it never loses one). **Detail:** `view`, `filters` (those of the first read in the window), `rows_shown`, `others_shown`, `distinct_others`, `targeting_key` (`{"submitter": name}`, `{"principal_id": id}` for the audit log, or `null`); never the names of the people seen, row ids or SQL (pinned by `test_view_audit.py`). **Fail-closed:** the lookup and the row run on a session of their own (`sessions_like`, the service session's bind, never its transaction) that commits before the answer is built; any exception there becomes `AuditUnavailableError` (503, `Retry-After: 5`, the message names the cause by its exception class only and says the read was refused), and the activity is never returned. `LAKANA_VIEW_AUDIT__ENABLED=false` turns it off. The rows are readable with `READ_AUDIT_LOG` only.
- **Host names and process ids appear only in the worker list.** `GET /api/v1/workers` (`RunService.list_workers`) is authorized like `GET /api/v1/scheduler`: `MANAGE_ENVIRONMENT` at instance scope, nothing else gets a row. It shows each worker's id (which contains its host name and process id), host, version, times, `alive` or `lost` by the same comparison every sweep and wait reason uses, and the ids of the runs it carries; never SQL, a name or an environment of the work. No query list, wait reason or status carries a worker's id, host or pid.
- **The cap per principal (M5c3c) is fairness, not a security boundary.** `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` (`auto` or a whole number of 1 or more) keeps one principal from taking every interactive worker: a worker passes by the queued ad-hoc runs of a principal that already has the cap `RUNNING`, and they start when one of its own ends. Nothing is refused or stopped, and it protects no data: a principal who can submit queries can submit them at any rate, and the query isolation does not rest on it. What is counted: the principal's `RUNNING` runs that are interactive (`lakana_core.runs.pools.is_interactive`: ad-hoc reads and writes, view creation, a query with an unbound task token), over every environment; never a task's write, a Python task's input query or maintenance (a pipeline never waits behind the cap, which is also why a Python task cannot deadlock on it); a run whose principal was deleted (`principal_id` NULL) is never counted and never capped. **The condition lives in the statement that acts.** The poller's list of capped principals (`pools.running_by_principal`) is only a hint that spares a dequeue; the claim's start update carries "this principal has fewer than the cap `RUNNING` interactive runs" itself (`pipeline._start`, `pools.under_cap`) beside the unchanged `_claimable_by`, and its row count decides. A claim that the cap refuses matches no row: nothing is executed, written, audited or logged, the transaction ends and the run stays `QUEUED`. **Exact under concurrency.** Under READ COMMITTED two start updates for two runs of one principal share no row, so each would count the other's uncommitted start as absent. The claim therefore takes a Postgres advisory transaction lock with two integer keys (`pools.CAP_LOCK_KEY` and 32 bits of the principal's id, both cast to `integer`, the form of `DeployService._lock_ownership` made unambiguous) right before the update and in the same transaction; the second claim waits for the first's commit and its statement then counts it. Two principals whose ids share 32 bits only wait for each other's claim, never wrongly refuse one. The principal's row is not used as the lock: deleting a principal sets `query_runs.principal_id` to NULL, and a row lock there could meet that delete in a cycle. The lock order is the run's row (taken with `SKIP LOCKED`, never waited for), then the advisory lock, then nothing; the sweep, the enders, the cancel and the write fence never take it. On SQLite the count and the update are one statement of the one writer. **Exactness relies on PostgreSQL's default isolation level, READ COMMITTED**: the code sets none, and an instance configured with `default_transaction_isolation = 'repeatable read'` would not be exact. **The lock's wait is bounded:** `SET LOCAL lock_timeout` (`pools.CAP_LOCK_TIMEOUT_SECONDS`, 5 s, the value of the catalog's locking read) is issued right before it and ends with the claim's transaction; a timeout is a refused claim (rollback, the run stays `QUEUED`, nothing audited, one DEBUG line), so a claimer frozen between lock and commit cannot block every worker for that principal; and the polling loop then passes that principal's ad-hoc runs by for 30 s (`poller.PassedBy`, `PASS_BY_SECONDS`, in the loop's memory, never in the module; pipeline work is never passed by), so the same oldest run is not retried at once and other principals' queries are not held behind it. A running query whose worker died still counts until the sweep ends it (about a lease); in local mode (one worker) a crashed worker's leftover `RUNNING` query therefore holds its user at the limit until then (about a minute). **What a capped principal learns:** nothing about anyone else. It sees its own query waiting; the hint, the lock and the count are internal to the worker, and no answer to a caller carries another principal's count.
- **What the two wait reasons of M5c3c reveal.** `no_worker_of_kind` and `user_limit` add no read, no action and no route: they travel inside the responses above under the same authorization, and a caller who may not read the run gets the same 404 (a test compares the answer for another principal's capped query with that for an id that does not exist). `no_worker_of_kind` carries `workers` (0) and `other_workers`: counts of the whole instance, as `workers` is, and no worker id, host or `takes` value of any one worker. `user_limit` carries `running`, **the reader's own** number of `RUNNING` ad-hoc queries (every environment, also for a task token of that principal: the token acts as the principal), and `limit`, the instance's limit now (the same `pools.cap_for` the workers use; the status read never computes it a second way). A manager who lists another principal's queued row reads `limit` and the sentence `Its submitter has reached the limit of N running queries per user; it can start when fewer than N of theirs are running.`: they learn that the principal has reached the limit across the instance, never how many queries that principal runs, and the sentence is the same at the limit and above it, so it does not even tell which of the two holds. Who is the reader is decided in the service from the caller's id and the row's `principal_id` (`own = row.principal_id == caller`), never from a request parameter; `QueryService.waiting_for` reads as the run's submitter, and no path hands it a `QUEUED` run of another principal: the status read is submitter-only, and the cancel of a queued run (a manager's included) answers with the run `CANCELLED`, which has no reason (a test pins the manager's cancel). `user_limit` is said only while a worker that takes interactive work is free, so a one- or two-worker installation shows the busy sentence as before. The counts read for these reasons (`pools.running_count`, `pools.running_by_principal`, `alive_workers`/`with_work`) are `COUNT` and `DISTINCT` over columns that hold no text of anyone's work. A pipeline task and a deploy are never capped, and a deploy gets no reason in this element. Since #92 a queued deploy has `waiting_for` on the single read (`DeployService.get`; the history computes none), from the same counts: no worker or run content is read, the same READ_ENVIRONMENT/DEPLOY authorization as the status applies, and it can say only `no_worker`, `no_worker_of_kind`, `workers_busy` or `starting`.

## Governed table creation and append (M4a)

- **The append path runs in the API process and executes no SQL.**
  `POST /api/v1/environments/{env}/namespaces/{path}/tables/{name}/data`
  (`lakana_server.api.platform.tables.append_table_data`) decodes an Arrow IPC
  stream and calls `CatalogService.append` directly, and
  `PUT …/data` (`replace_table_data`, M5a) calls `CatalogService.replace`
  the same way — neither ever enqueues a
  query or reaches a worker, so "query execution runs only in workers"
  still holds; this route's write is a catalog/storage operation, not a SQL
  one.
- **Both Arrow write routes share `_governed_arrow_write`** (M5a), so every
  control below — the pre-check, the concurrency cap, the byte/array caps,
  the compression and type refusals and full validation — applies to a
  replace exactly as to an append. A repeatable `source` query parameter
  declares the tables the rows came from; each must exist and be READ-able
  by the caller before anything is written, and each gets a lineage edge. At most
  `MAX_SOURCE_VERSIONS` (64) `source` values and none longer than a legal dotted name
  (`check_sources`, called by both routes before the body is read and again by
  `resolve_lineage_sources`); a name a message must quote is cut by `identifiers.shown`
  (200 ASCII characters), the one helper the artifact messages use too.
- **Authorization runs before the body is read at all, not just before the
  write.** `CatalogService.check_can_append`
  (authorize `Action.WRITE_TABLE`, resolve the table, refuse a
  non-`ICEBERG`/external table) runs first, via `run_in_threadpool`, before
  `request.stream()` is ever touched — an unauthorized or nonexistent-table
  request is refused without spending any time or memory on a body that
  was never going to be written. `CatalogService.append` re-authorizes and
  re-resolves the table itself immediately before the actual write, since
  grants or table type could change between the two calls; `check_can_append`
  is an optimization, not a substitute for that.
  The replace route runs `check_can_replace` instead: `WRITE_TABLE` first in every
  case, so a caller without it learns nothing about whether the table exists, and
  `CREATE_TABLE` only when `expected_snapshot_id=none` names a missing table the upload
  would create (M5c2a).
  In a protected environment a `WRITE_TABLE` holder can get 409 for a numeric
  `expected_snapshot_id` on a missing table before the lockdown guard runs; that reveals
  nothing a `WRITE_TABLE` holder could not already see, and nothing to anyone without it.
- **The pre-check's database connection is released before the body is
  read, and the route caps both concurrency and read time.** `check_can_append` runs a `SELECT`, and holding its pooled
  connection open for the rest of the client-paced `request.stream()` read
  was reproduced exhausting the default connection pool with 16 stalled
  uploads, freezing an unrelated request for the pool-checkout timeout.
  `append_table_data` injects the same request-scoped `Session` `catalog`
  uses (FastAPI caches a `Depends` result per request, so it is the same
  instance) and calls `session.rollback()` immediately after the
  pre-check, via `run_in_threadpool` — `append` re-authorizes and
  re-resolves the table itself regardless, so releasing the connection
  here costs nothing. Two further, independent bounds close the rest of
  the resource-exhaustion surface: `app.state.append_concurrency_limiter`
  (`lakana_server.concurrency.AppendConcurrencyLimiter`,
  `Settings.append.max_concurrent`, `LAKANA_APPEND__MAX_CONCURRENT`) refuses
  a request outright (503, `Retry-After: 1`) rather than queuing it once
  that many appends are already in flight in this process, and the whole
  body read is wrapped in `asyncio.timeout(Settings.append.
  read_timeout_seconds)` (`LAKANA_APPEND__READ_TIMEOUT_SECONDS`), refusing
  (408) a client that stops sending mid-upload rather than holding its
  slot and connection indefinitely.
- **The wire body is bounded by a byte cap, but a byte cap on wire size
  alone does not bound memory.** The route
  reads `request.stream()` incrementally into a running total and raises
  `lakana_server.errors.PayloadTooLargeError` (413) the moment that total
  exceeds `Settings.append.max_bytes` (`lakana_server.settings.AppendConfig`,
  `Field(gt=0)`, default 268 435 456), read from `LAKANA_APPEND__MAX_BYTES`
  — this bounds a request with no `Content-Length` header exactly the same
  as one with it. But Arrow IPC body compression breaks the assumption
  that wire size and decoded size are comparable: a 2,288-byte `zstd`
  stream was reproduced decoding to 64 MB, a ~28,000:1 ratio, because
  `pyarrow.ipc.RecordBatchStreamReader` allocates each buffer at its
  *declared uncompressed* length before decompressing into it — a small
  request can force a large allocation regardless of how tight the wire
  cap is set. `lakana_server.api.platform.tables._decode_arrow_stream`
  scans every IPC message's metadata for a declared compression codec
  (`_message_declares_compression`, a hand-rolled FlatBuffers field read —
  there is no public pyarrow API for this) *before* decoding any batch,
  and refuses outright (422, "compressed Arrow IPC is not supported; send
  uncompressed") rather than trying to decode-then-check, since the
  oversized allocation would already have happened by then. Only once
  every message is confirmed uncompressed does the decoded-size cap below
  mean anything: with compression ruled out, a batch's `.nbytes` is bounded
  by the same wire-byte cap already enforced above (uncompressed wire size
  and decoded size are the same order of magnitude), and the running total
  is checked again there purely as an independent, cheap second bound —
  not because decoded size could otherwise run away unbounded.
- **The byte cap, the compression refusal, the schema's own column count,
  and cumulative batches × columns are what bound memory together — not
  the byte cap alone.** `_decode_arrow_stream`
  refuses a schema declaring more than 1000 columns outright (422),
  matching `TableCreate`'s own bound, before any batch is decoded; and
  once every message is confirmed uncompressed, each decoded batch's
  `num_columns` counts toward `Settings.append.max_arrays`
  (`LAKANA_APPEND__MAX_ARRAYS`, `Field(gt=0)`, default 100 000), refused
  (413, suggesting fewer/larger batches) as soon as the cumulative total
  is exceeded, without decoding the rest of the stream. This exists
  because each decoded batch costs roughly 1–3 KB of object overhead in
  decode and again in pyiceberg, independent of its byte size: 1.7M
  one-row batches inside a 256 MiB body — safely under the byte cap — was
  reproduced costing +1.46 GB RSS and 7s to decode, then +3.3 GB and 32s
  in the pyiceberg append. The decoded table is then `combine_chunks()`d
  before being handed to `CatalogService.append`, for the same reason on
  the receiving side. **Residual, by design:** per-array overhead up to
  whatever `max_arrays` is configured to allow — the cap bounds it, it
  does not eliminate it.
- **A field's Arrow type is checked against a fixed allowlist before any
  batch is decoded.** Not every type a
  compression bomb-free, cap-respecting stream can declare is safe to
  validate or hold in memory: `string_view`/`binary_view` arrays can pack
  many short views that each reference the *same* large shared data
  buffer, so `Table.validate(full=True)`'s UTF-8 check ends up
  re-validating that buffer's content once per view — a request safely
  inside the byte cap was reproduced describing views whose combined
  *logical* content reached several gibibytes, all backed by a
  single-digit-megabyte wire body. `_decode_arrow_stream` checks every
  field in `reader.schema` — available immediately once the stream is
  opened, before any batch is read — against `_is_allowed_arrow_type`: an
  explicit allowlist (`int32`, `int64`, `float32`, `float64`, `bool`,
  `string`, `large_string`, `binary`, `large_binary`, `decimal128(p,s)`,
  `date32`, `time64("us")`, `timestamp("us")` with or without a timezone,
  `uuid` — the last added after the allowlist was found
  refusing a caller's legitimate `uuid` column, which every other layer of
  the append path already accepted)
  mirroring the Arrow types `lakana_core.catalog.append`'s own schema check
  can ever accept — `lakana-server` cannot import pyiceberg
  (`server-goes-through-core`), so this list is a hand-maintained, no-import
  mirror rather than something derived from core directly, and a comment
  at its definition says so. A field outside the allowlist (`dictionary`,
  `list`, `struct`, `string_view`, ...) is refused (422, naming the field)
  before a single batch is decoded or validated — types `to_iceberg_schema`
  could never produce from a caller's JSON column list anyway, so this
  costs a legitimate caller nothing.
- **A batch is fully validated before anything else reads it — including
  its own `.nbytes`.** Calling `.nbytes` on
  a batch whose internal lengths are corrupted but not yet validated was
  reproduced *segfaulting the process outright* — not raising a Python
  exception, a crash no `except` clause anywhere can catch. The
  reproduction used a dictionary-encoded column whose dictionary batch
  declared an internal length far larger than its actual buffer
  (`pyarrow.lib.ArrowInvalid: Buffer #1 too small ... length 2231369730`)
  — a call to `batch.validate(full=True)` on that exact batch raises this
  error cleanly, but the very next line reading `batch.nbytes` on the same
  unvalidated batch segfaults. `_decode_arrow_stream`'s per-batch loop
  therefore calls `batch.validate(full=True)` *before* `batch.nbytes` or
  anything else touches the batch, every time, with no exception —
  defense in depth alongside the type allowlist above, which already
  refuses the dictionary type this specific reproduction used, but the
  ordering itself is the actual fix for the general class of bug (an
  unvalidated batch's derived properties are not safe to read, regardless
  of which type triggered a given corruption).
- **Malformed-but-decodable data is caught by full validation, not left
  for a later crash.** After decoding,
  `_decode_arrow_stream` calls `data.validate(full=True)` once more on the
  assembled table (each batch was already validated individually, per the
  point above). Corrupted offsets in a string/binary array (non-monotonic,
  or an out-of-bounds final offset) can decode "successfully" under
  pyarrow's lighter default validation and only fail unpredictably the
  first time something downstream touches the bad bytes — reproduced as a
  bare `SystemError` from `.cast()` on such an array. The decode step's
  exception handling catches `pyarrow.lib.ArrowException` (covering
  `ArrowInvalid`, `ArrowNotImplementedError`, `ArrowMemoryError`,
  `ArrowTypeError`), `OSError`, and bare `ValueError` — the last needed
  because `Table.validate(full=True)` itself was observed raising a bare
  `UnicodeDecodeError` (a `ValueError` subclass, not a pyarrow exception)
  on a corrupted string array — so every decode failure this function can
  produce surfaces as its own `ValidationError` (422), never an unhandled
  500.
- **Decoding runs off the event loop.**
  `_decode_arrow_stream` is synchronous and can be CPU-bound —
  `Table.validate(full=True)` scales with a batch's logical content, not
  its wire size. `append_table_data` calls it via `run_in_threadpool`
  (alongside the two `CatalogService` calls, already threadpooled since
  the first hardening), never inline on the `async def` route, so a slow
  decode/validate never blocks the event loop other requests share.
- **Arrow field and schema metadata is stripped before that validation
  runs, so a stale or mismatched `PARQUET:field_id` can never redirect a
  write.** pyiceberg's writer honours a
  `PARQUET:field_id` metadata entry on an Arrow field when present, writing
  that column by its declared id rather than by name — even though the
  schema check right after (below) validates column identity purely by
  name. Caller-supplied metadata with a stale or mismatched `field_id` was
  reproduced committing swapped columns (`a=99, b=1`) despite passing that
  by-name check, and a field id pointing at a column of a different
  Iceberg type raising an unhandled exception (500) from deep inside
  pyiceberg's own schema-compatibility check. `lakana_core.catalog.append.
  strip_field_metadata` rebuilds the schema with every field's metadata
  removed (`Table.from_arrays`, not `Table.cast` — casting to a
  non-nullable target whose actual column contains a null raises its own
  bare `ValueError`, exactly the "null in a required column" input the
  schema check below exists to catch cleanly) and the table's own schema
  metadata cleared, leaving pyiceberg nothing to key off but the same
  column order/name the schema check already uses.
- **Schema validation happens before any file is written.**
  `CatalogService.append` checks the decoded Arrow schema column-by-column
  against the table's current Iceberg schema before any data file is
  written — an extra, missing, mismatched-type, or wrongly-nullable column
  is refused by name.
- **Writing outside the table's own location is closed by the same
  controls as every other write.** Data files are written under the
  table's id-suffixed, Lakana-assigned location and committed through the
  existing `commit_table` compare-and-swap; a file written for a commit
  that then loses the race is deleted best-effort. See "Row filters and
  column masks" above and `docs/security/threat-model.md`'s "Reading
  tables one is not granted" row for the location-confinement mechanism
  this reuses.
- **Every successful append is audited.** `AuditEventType.TABLE_APPENDED`
  is recorded in the same transaction as the commit, with detail
  `{rows, files, snapshot_id}` — never the appended values themselves.
- **Table creation from a JSON schema goes through the same `create_table`
  every other path uses.** `POST /api/v1/environments/{env}/namespaces/{path}/tables`
  converts the caller's `[{"name", "type", "nullable"}]` list to an Iceberg
  `Schema` (`lakana_core.catalog.json_schema.to_iceberg_schema`) and calls
  `CatalogService.create_table` — the medallion-namespace rule and the
  refusal of a caller-supplied `location` both still apply, unchanged.
  `TableCreate`'s own schema bounds a column name
  to 255 characters, a type name to 64, and the column list to 1000
  entries, and sets `model_config = {"extra": "forbid"}` so a caller-sent
  `"location"` (or any other unrecognised field) is refused with a 422
  rather than silently ignored. `ColumnIn` sets
  the same `extra="forbid"` at the per-column level.

## The SDK's own credentials and surface (M4a)

- **The SDK speaks only to the platform API.** `lakana` never imports `lakana-core` or
  `lakana-server` (`import-linter`'s `sdk-is-independent` contract) and holds no storage or
  database credential of its own — every operation, including `tables.append`, is an HTTP
  call to a route in `lakana._routes.ROUTES`, authenticated with the caller's own PAT or task
  token. It has no privileged path around the REST API, the same as the CLI or any other
  caller (see "The REST API is the only way in" above).
- **Tokens are never logged or shown in a `repr`.** `lakana._tokens.mask_token` masks
  everything but a token's key-id prefix (e.g. `lakana_pat_<key>_***`); `Client.__repr__` and
  `ResolvedConfig.__repr__` both use it, and an HTTP-mapped error's message comes only from
  the server's own response body, never from the request that carried the token.
- **Credentials at rest: the profile file must be `0600` or stricter.**
  `lakana._config._read_profile_file` opens `$XDG_CONFIG_HOME/lakana/config.toml` (or
  `~/.config/lakana/config.toml`) once, with `O_NOFOLLOW`, and checks its permission mode on
  that same file descriptor before reading — no window between the check and the read for
  the file to be swapped for a symlink (TOCTOU). A mode with any group/other bit set is
  refused with `ConfigError` naming the `chmod` to run; a symlinked file is refused outright
  via the `O_NOFOLLOW`-triggered `ELOOP`. Once parsed, `lakana._config.validate_profile_document`
  (shared by the SDK's reader and the CLI's writer) refuses a file of the wrong shape — a
  non-table `profiles`, or a non-string `url`/`token`/`default_environment`/`default_profile`
  — with `ConfigError` naming the key, instead of crashing. **Known limitation:** this guards the file itself,
  not its parent directory — a symlinked *parent* directory is not detected, matching the
  documented gap in `docs/security/threat-model.md`'s "SDK credentials at rest" row. An OS
  keyring integration is a deliberate non-goal for now: a single, permission-checked file is
  the whole credential-storage model.
- **The CLI's profile writes** (`lakana.cli._profiles`, the only writer): atomic — a fresh
  temp file opened `O_CREAT|O_EXCL|O_NOFOLLOW` with mode 0600, fsynced, then `os.replace`d
  over the target, in a 0700 directory, which is then fsynced itself on POSIX so the rename
  survives a crash. An existing file the SDK would refuse (symlink,
  group/other permissions) is refused, never re-permissioned or rewritten; the parent
  directory gets the equivalent check on write — a symlinked `~/.config/lakana`, or one made
  readable/writable by group or other, is refused rather than written into (POSIX only;
  skipped on Windows, where neither check applies). TOML values are quoted with JSON's
  string escaping, plus one addition JSON doesn't require: DEL (`0x7F`), which TOML basic
  strings forbid unescaped, is escaped too.
  **Accepted TOCTOU window:** the parent directory is checked with `lstat` and the temp
  file is opened afterwards by path, so the directory could in principle be swapped for a
  symlink in between. Winning that race needs write access to the parent of
  `~/.config/lakana` (normally `~/.config`, i.e. the user's own home), which already means
  the user account is compromised; closing it (`openat` relative to a directory fd) isn't
  worth the portability cost.
- **No token on the command line.** The CLI has no `--token` option (it would reach shell
  history and `ps`); tokens come from a hidden prompt, `--token-stdin`, `LAKANA_TOKEN`, or
  the profile. A newly issued or minted token is printed once; every other output path —
  errors, `profile list`, `--verbose` tracebacks — carries at most the masked key id.
- **`--url` can't carry a profile's token to another origin.** When the token comes from the
  profile file (neither `LAKANA_URL` nor `LAKANA_TOKEN` is set), `lakana.cli._state.
  default_client_factory` accepts `--url` only if its origin — scheme, host, and effective
  port — equals the origin of the profile's own `url`; only the path may differ. Anything
  else is refused with `ConfigError` (exit 3) before a client is built, so a typo or a pasted
  `--url https://attacker…` never sends the stored token anywhere. When `LAKANA_URL`/
  `LAKANA_TOKEN` supply the credentials, `--url` may override the URL freely: the user handed
  over that token for this invocation.
- **The append path itself (`tables.append` on the SDK side) is documented in "Governed
  table creation and append (M4a)" above** — the SDK serializes the caller's
  `pyarrow.Table`/`RecordBatch` to an uncompressed Arrow IPC stream (`combine_chunks()`d,
  written as batches capped at 65,536 rows each, so a normal upload
  stays far below the server's `max_arrays` cap without the caller thinking about batching)
  and retries once on a `409 Conflict` (not when the caller passed `expected_snapshot_id`: the conflict is then the answer); every actual control (byte cap, compression check,
  type allowlist, batch validation, the array/column caps) lives server-side and is
  described there, not duplicated here. Three SDK-side data-shape adjustments, all in
  `lakana._arrow`: only a UTC-alias timezone (`UTC`, `+00:00`,
  `Etc/UTC`, `Z`, mirroring pyiceberg's own `UTC_ALIASES`) maps to `timestamptz` — any other
  timezone is refused locally, naming the column, rather than silently reinterpreted, since
  Iceberg's `timestamptz` always means UTC; and a `timestamp[ns]` column (pandas' default)
  is safely cast to `timestamp[us]`, refusing (naming the column) rather than truncating a
  column that actually holds sub-microsecond precision. `tables.append` also sends its own
  300s timeout, longer than `Client`'s general default — **a timeout on
  append is ambiguous, not a clean failure**: the server may have already committed by the
  time the client gives up waiting, so a caller that blindly retries a timed-out append can
  duplicate rows, unlike the `Conflict` retry above, which is safe precisely because a
  conflict response proves nothing committed from that request.

## Governed writes, views, lineage and maintenance (M5a)

- **Every write is authorized twice, at submission and in the worker.** `QueryService.submit_write`
  authorizes `SUBMIT_QUERY`, then `CREATE_TABLE` for `create` and a new view, `WRITE_TABLE` for
  `replace`, `append` and an existing view, and — for an incremental append — `READ_TABLE` on the
  target too, since the watermark is a value from the target's rows. Each action is checked before
  the target's existence is revealed. The worker re-authorizes through `CatalogService`
  (`write_batches`, `read_watermark`, `create_or_replace_view`), so a grant revoked while a run is
  queued fails the run unwritten. Reading every source still needs `READ_TABLE`, as for any query.
- **An incremental append is refused while a row filter or column mask on its target applies to
  the caller**: the watermark comes from Iceberg manifest statistics over every row, including rows
  the caller can't see.
- **A merge (`CatalogService.merge`, `POST …/tables/{name}/merge`) needs `READ_TABLE` as well as
  `WRITE_TABLE` (except for a bound task token on its own asset, see "A bound task token on its
  own asset is a non-reader"), and is refused while a row filter or column mask on its target applies to the
  caller** (`check_can_merge`, run as the route's precheck and again by `merge`). A merge reads
  the target's rows that the batch's keys match and compares them: `rows_updated` against
  `rows_inserted`, the "key repeats in N rows of the table" refusal and a no-op that commits
  nothing would each tell the caller about rows or values it may not see. A key of several
  columns is capped at `COMPOSITE_KEY_MAX_ROWS` (5,000) rows per batch, checked before anything
  is read: pyiceberg matches such a key with one filter term per row and its time grows with the
  square of the batch, so an uncapped batch would hold a server thread and an append slot for
  minutes.
- **A zero-row batch never writes a file.** An empty result creates or replaces the table without
  an empty Parquet file, and an append of nothing leaves no orphan.
- **Masked data is persisted masked.** A write's SELECT goes through the same rewriter as a query,
  so the target receives exactly the rows and values the caller may see. This is the intended,
  safe outcome, not a leak: a masked value is never written unmasked. (A `NULL` mask yields an
  integer-typed column in DuckDB, which can make a `replace` into an existing string column fail
  its schema check.)
- **Writes are atomic.** A write's data files go under the target's own id-suffixed location and
  are published by one commit through `commit_table`, so M3f's snapshot guards apply. `replace` is
  one `OVERWRITE` snapshot; `create` publishes the table row with its first snapshot in one step.
  On any failure every data file and manifest the write created is deleted; the only residue is a
  metadata JSON written before a lost compare-and-swap, as for any commit.
- **A write whose rows depend on its target commits only against the snapshot it read.** An
  incremental append is pinned to the target snapshot its watermark was read from, and a write
  whose query reads its own target — directly or through a view — to the snapshot resolution
  listed the target's files from (`SnapshotPin`, `write_batches(pin=…)`). If the target changed in
  between — another run, an Arrow append, a drop and re-create — the run fails with a
  `ConflictError`, "<table> changed during the write; retry.", commits nothing and deletes every
  file it wrote, instead of appending rows a second time or overwriting rows it never read. A
  write that loses the commit compare-and-swap fails with the same message. A plain append and a
  `replace` that doesn't read its target stay unpinned: their rows don't depend on the target, so
  landing after a concurrent append is a valid serial order, and concurrent plain appends still
  both land. A `create`'s target can't exist yet, so it has nothing to pin.
- **Views use invoker's rights and can't widen access.** `query_rewrite.expand_views` replaces
  every view reference with its stored SQL before resolution, so resolution and the policy rewrite
  see base tables only. A reader needs `READ_TABLE` on the view and on every table beneath it; a
  denied view is reported with the same opaque message as a denied table. A view's stored SQL is
  re-checked against the AST guard at expansion, and a view reference carrying a construct the
  expander can't carry over faithfully is refused rather than silently altered (only joins,
  pivots and samples carry over; a `LATERAL VIEW` is refused). A view's stored SQL may hold no
  placeholder either, as for submitted SQL. A query expands at
  most 100 view references. `table get` shows a view's SQL and references only to a caller who can
  `READ_TABLE` every referenced table; otherwise it returns `hidden_references`, a count. A stale
  view's message names a renamed table only if the reader may read it; otherwise it is word for
  word the message for a dropped one ("was dropped or replaced"), so the two can't be told apart.
  Views are never exposed over
  Iceberg REST, including `DELETE`. A row filter or column mask can't be attached to a view
  (`ValidationError`): expansion replaces the view before policies resolve, so it would silently
  never apply. Attach it to the view's base tables, whose policies always apply.
- **Lineage is pruned by grants.** `LineageService` shows only nodes the caller may `READ_TABLE`
  (a dropped node is checked at the **namespace** its edges recorded, never as a table — see below), counts the rest in
  `hidden_nodes`, and never walks through a hidden node, so it can't reveal a hidden table's name
  or that two readable tables connect through one. A graph has at most 500 nodes, after which the
  response sets `truncated` — only when a node the caller may read is left out, so a hidden node
  past the cap is counted in `hidden_nodes` and never sets it.
- **A dropped table's lineage node is identified by its table id and shown only at namespace level
  or above.** A lineage node carries the table's id; the table row is gone once dropped, so
  the node can no longer be checked as that table. It is never checked under its recorded *name*
  as a table: a table recreated under that name has a different id, and a table-level `READ_TABLE`
  grant on it must not reveal the old node, its edges, snapshot ids or run ids. A dropped node is
  visible only to a principal holding `READ_TABLE` on the namespace of the **newest** name recorded
  for its id on any edge (`LineageService._newest_recorded_name`; a namespace grant there or above,
  which would have covered the table) and to administrators. One name per id, not the name on the
  edge the walk meets first, so the verdict and the name shown are the same from every root (#53);
  a legacy name with more dots (`bronze.x.y`) is split at the last one (namespace `bronze.x`).
  Never add a code path that authorizes a dropped node through a `Securable.table_scope`.
  **A principal acting with a task token never sees a dropped node** (any token with a task
  scope, bound or not): a token's asset patterns can name a namespace without saying which
  dropped table they meant, so the node is counted in `hidden_nodes` like any hidden one
  (`test_lineage_graph_task_token.py`, with the real `GrantAuthorization`).
  Residuals: a grant on the exact path of a namespace is revoked when the namespace is dropped
  (`drop_namespace`, in the same transaction), but a grant on an *ancestor* namespace or the
  environment covers a recreated namespace as it always did, and so shows the lineage of tables
  dropped from the old one; likewise an administrator who newly grants read on a recreated
  namespace also exposes it. A rename does not write an edge, so a table renamed and then dropped
  with no write after the rename is judged by its name before the rename. The pins are
  `test_lineage_visibility_integration.py` and `test_lineage_graph.py` (the moved-then-dropped
  tests).
- **Maintenance deletion is confined to the table's own location.** Orphan candidates are only
  files under `<location>/data/` and `<location>/metadata/` that the table owns under the "most
  specific owner" rule (a nested table's files are never its own), that no remaining snapshot,
  manifest list, manifest or metadata file references, that aren't `*.metadata.json`, and whose
  modification time is known and older than `older_than_hours` (default 72, minimum 1, maximum
  87600 — ten years — on every surface, settable per call; the cutoff is computed before the
  expiry commits, so a bad value can never leave an unaudited expiry). On `file://`, symlinks are
  never followed. Orphan removal is skipped while a write
  run into the table is running. Expiry commits through `commit_table` under `MAINTAIN_TABLE`,
  which only the built-in admin role carries and which is not a task-token action.
- **Maintenance fails closed.** Expiry commits only against the exact metadata it selected from;
  a concurrent change (a new snapshot, a branch or tag) aborts the run and deletes nothing; the
  report sets the machine-readable `conflict: true` (note "table changed during maintenance,
  retry"), and the CLI exits 1 on `conflict`, never on the note's wording. The metadata pointer is
  re-read immediately before orphan deletion; if it moved, orphan removal is skipped with a note
  (the expiry stands; not a conflict). If any referenced path is not
  within the table location in canonical form (another writer's `s3a://` or `file:/` spelling,
  say), orphan removal is skipped with a note rather than guessed at.
- **A cutoff shorter than the longest external write is unsafe.** The running-write guard only
  sees Lakana's own write runs. A writer that reaches the table another way (Iceberg REST, an
  external engine) has its files unreferenced until it commits, and maintenance deletes
  anything older than the cutoff. Keep `older_than_hours` above the longest such write.
- **Residual, by design:** a dropped table's leftover files inside a namespace nested under another
  table's `data/` directory are indistinguishable from that table's orphans (the M3f "most specific
  owner" residual); maintenance would delete them.

## Webhook egress (M5d-c)

`lakana_worker.egress` is the only code that sends a webhook. Since #15 its address checks, pinning,
TLS and proxy set-up, deadline, log silencing and failure reasons live in `lakana_core.net.egress`,
which the CI OIDC fetcher shares ("CI OIDC verification" below); the webhook reasons, the
`LAKANA_WEBHOOK__*` allow-list and the test hook stay in the worker, and the worker's tests run
unchanged against it. A webhook URL is hostile input:
anyone with the deploy right can name a secret of the environment as one if it is unbound or bound
to the environment. `test_egress.py`
pins each line.

- **Check, then pin.** `check_destination` requires `https` and port 443, resolves the name
  once, checks **every** address and returns the one to connect to. `post` connects to that
  address (`https://<ip>:443/...`) with `Host` set to the name and `extensions["sni_hostname"]`
  set to the name, so the certificate is verified against the **name**, never the address, and
  DNS cannot answer differently between check and connect. A test fails the lookup after the
  check to prove no second resolution happens.
- **Refused:** everything the standard library does not call global, plus explicit lists:
  private, loopback, link-local (`169.254.0.0/16`, `fe80::/10`), `100.64.0.0/10`, `0.0.0.0/8`,
  documentation and benchmarking ranges, multicast, `240.0.0.0/4`, `fc00::/7`, `fec0::/10`
  (deprecated site-local, which `ipaddress` calls global on 3.11 and 3.14; #243), the unspecified
  address, and any IPv6 form that embeds such an IPv4 (mapped, compatible, 6to4, Teredo,
  NAT64: the well-known `64:ff9b::/96` and the local-use `64:ff9b:1::/48` of RFC 8215, both
  judged by their last 32 bits). All of 6to4 is refused. A shorthand IPv4 spelling (`127.1`, `2130706433`, a last label
  of digits) is refused before any lookup. Userinfo in the URL is refused.
- **No ambient configuration.** `httpx` runs with `trust_env=False` and `follow_redirects=False`;
  `HTTPS_PROXY`, `SSL_CERT_FILE` and the like change nothing. `LAKANA_WEBHOOK__PROXY` is the
  only proxy; with it the destination name is still checked but cannot be pinned (the proxy
  resolves it), so the proxy is trusted. `LAKANA_WEBHOOK__CA_BUNDLE` replaces trust and never
  disables verification. `LAKANA_WEBHOOK__ALLOW_HOSTS` exempts named hosts from the scheme and
  port rules and from the private-address check, nothing else: a listed host is still refused
  (`NEVER_REACHABLE`) when any of its addresses `is_never_reachable` (loopback, link-local,
  unspecified, multicast, reserved, `fd00:ec2::254`, `100.100.100.200`, or an IPv6 form that
  carries such an IPv4), the rule of `LAKANA_CI_OIDC__ALLOW_HOSTS` (#243). One exception: the
  entry `localhost`, for the URL host `localhost` only, may reach loopback addresses (never the
  rest of the set), so that a receiver on the worker's own machine still works (the get-told
  documentation example); any other listed name whose DNS points at loopback is refused. The
  scheme and port exemption stays: an internal chat server on `http` or another port is the
  setting's purpose, and with the address rule the exemption reaches no address the rule
  refuses. Pins: `test_an_allowed_host_never_reaches_loopback_link_local_or_metadata`,
  `test_a_listed_localhost_may_reach_loopback_and_nothing_else_never_reachable`.
  **Residual:** a NAT64 prefix length other than /96 inside the local-use `64:ff9b:1::/48`
  (RFC 6052 then places the IPv4 elsewhere) is not decoded, so a host on
  `LAKANA_WEBHOOK__ALLOW_HOSTS` or `LAKANA_CI_OIDC__ALLOW_HOSTS` that resolves to such an address
  carrying a metadata or loopback IPv4 is reached as a private address; an unlisted host is
  refused there, because the whole `/48` is not global. A NAT64 prefix an operator chose in their
  own global IPv6 space cannot be known at all: an address in it looks public, to listed and
  unlisted hosts alike, and only the NAT64 gateway's own rules keep it from internal IPv4
  destinations.
- **No URL in any text.** Every exception is reduced to a fixed reason
  (`lakana_core.net.egress.classify`); `str(exc)`
  of an `httpx` error embeds the URL and is never read. Refusals are raised outside `except`
  blocks, so no exception is chained to them. `Destination.__repr__` hides everything. One
  deadline covers the whole attempt (`asyncio.wait_for`), so a server that dribbles bytes cannot
  hold a worker.
- **The test hook.** `_TEST_ALLOW_LOOPBACK` admits loopback addresses (and a port other than
  443 on them). Only the `allow_loopback` fixture in the worker's `conftest.py` sets it, and
  `test_nothing_in_the_source_tree_sets_the_test_hook` greps every package's `src` to keep it so.
- **Lookups and addresses.** The name (and an operator proxy's name) is resolved on a module-level
  thread pool with `RESOLVE_TIMEOUT` (5 s), as an absolute name (`host.`), so no search domain is
  tried; a timeout gives a fixed, retryable reason. One attempt can take up to about 20 s in all
  (the webhook's lookup and the proxy's, each up to 5 s, then the 10 s request), inside the 60 s
  lease. Every address is checked, then each is tried
  in the system's order until one connects, within the attempt's one deadline. A certificate
  failure or a broken answer ends the attempt. Port 0 is refused.
- **httpx and httpcore never log during a send.** `httpx` logs the full URL (the secret) at INFO
  and `httpcore` traces host names at DEBUG, so a filter on every logger of both (children such
  as `httpcore.connection` included, re-attached at the start of each `post`) drops their records
  while a `ContextVar` flags a send in progress. Outside a send their logging is untouched.
  `test_the_http_libraries_never_log_the_url` turns every level on and checks success, a 404 and
  a TLS failure.
- **The URL is never logged, stored or quoted (`lakana_worker.notify`).** Not in a log line, a
  `notification_deliveries` row (`last_result` is one of the fixed reasons), an audit detail
  (`NOTIFICATION_FAILED` carries run, job, event and reason, no actor), an exception text or an
  answer. A secret that is missing, revoked, undecryptable or without a `url` that starts with
  `http://` or `https://` fails the delivery with a reason that names the secret, never its
  value. An unexpected exception from the send is logged by its type only. Anyone with the
  deploy right can name a secret that is unbound or bound to the environment as a webhook. That
  reveals no value, but
  the stored reason does tell them whether that secret holds an `http(s)` `url`, whether that
  host is private or refused, and the status code it answers; the worker also POSTs run metadata
  (names, no data) to that URL. The model accepts this. `test_notify.py` seeds a URL and a signing
  key and searches the logs, the row and the audit details for every failure kind.
- **Delivery is at least once and never touches the run.** One row at a time is
  claimed, in its own transaction with a fresh database time, with `FOR UPDATE SKIP LOCKED` and a
  60 s lease that therefore starts with its send; the claim counts the attempt, so a worker
  killed mid-send still uses one of the five. A secret the worker cannot decrypt (its key, its
  version) is retried so that another worker can send it; a missing, revoked, old-format or
  URL-less secret fails at once. A worker with no usable secrets key starts no delivery loop. The result is recorded in a new transaction that matches
  the row only while its owner and exact lease expiry are still this worker's; a lost lease
  records nothing, and the next attempt carries the same delivery id. Nothing in delivery writes
  to a run or its tasks. `LAKANA_WORKER__NOTIFICATIONS=false` keeps a worker from delivering
  (for a worker without outbound access); a drain stops the loop from claiming.
- **Allow-list caution.** An allowed name skips every address check and may use http and any
  port, so a name whose DNS record (or CNAME) points at `169.254.169.254` reaches the metadata
  service. List only names whose DNS you control; never `*.domain` where others can create
  records (dynamic DNS, tenant domains). An entry may not end in a number, so no IP address is
  allowed and `*.0.0.1` is refused.
- **The proxy step.** With `LAKANA_WEBHOOK__PROXY` the proxy resolves the name again, so an
  attacker's DNS can answer the worker with a public address and the proxy with an internal one.
  Deny internal destinations in the proxy's ACL. For squid:
  `acl to_internal dst 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 169.254.0.0/16 100.64.0.0/10 ::1 fc00::/7 fe80::/10`
  then `http_access deny to_internal` before any allow rule. The worker still needs its own DNS
  to resolve the name, even with a proxy. An `https` proxy's certificate is verified with
  `LAKANA_WEBHOOK__CA_BUNDLE`.
- **Residuals:** ISATAP addresses (`2001:db8::5efe:10.0.0.1` forms) and an operator-specific NAT64
  prefix (RFC 6052) cannot be recognised and are accepted; they reach an internal IPv4 only
  through a translator or router the operator runs. A lookup that times out holds its thread
  until the system resolver gives up (16 threads).

## CI OIDC verification (#15)

`lakana_core.ci_oidc` verifies the OIDC ID token of a CI job (GitHub Actions, GitLab CI). It is the
first part of a second authentication path; the exchange endpoint, the trust rules and the single
use of each `jti` are the next section. `test_ci_oidc_verification.py`, `test_ci_oidc_jwks.py`,
`test_ci_oidc_config.py` and `test_net_https_json.py` pin each line, with locally generated RSA keys
and a fake fetcher; no test reaches the network.

- **The order of the checks** (`IdTokenVerifier.verify_id_token`; each refusal is an
  `IdTokenRefused` with a stable `RefusalCode` and a fixed message that never quotes the token or a
  claim):
  1. at most 16 KiB (`token_too_large`), then three base64url segments whose header and payload are
     JSON objects with no member twice (`malformed`): parsers disagree on which duplicate wins, so
     the claims checked must be exactly the ones signed;
  2. the header's `alg` is in `LAKANA_CI_OIDC__ALGORITHMS` (`algorithm_not_allowed`);
  3. `jku`, `x5u`, `x5c`, `jwk` and `crit` in the header are refused (`header_not_allowed`): a key
     the token names for itself proves nothing;
  4. a `kid` (`missing_key_id`);
  5. the **unverified** `iss` is exactly one of `LAKANA_CI_OIDC__ISSUERS` (`issuer_not_allowed`), no
     normalisation, so `https://gitlab.com/` is not `https://gitlab.com`;
  6. the key by `kid` from that issuer's cache (`unknown_key`, `keys_unavailable`); it must be RSA,
     not bound by its JWK's `alg` to another algorithm (`key_mismatch`) and at least 2048 bits
     (`weak_key`);
  7. the signature, by PyJWT's `PyJWS.decode_complete` with that one algorithm and that one key
     object (`bad_signature`); the signature binds the `iss` read in step 5;
  8. `aud` is, or is a list that contains, `LAKANA_CI_OIDC__AUDIENCE` (`audience_mismatch`);
  9. `exp`, `iat` (required) and `nbf` (optional) are integers (`claim_missing`), with 60 s of skew:
     expired (`expired`), `nbf` or `iat` in the future (`not_yet_valid`), `iat` older than
     `LAKANA_CI_OIDC__MAX_TOKEN_AGE_SECONDS`, default 900 (`too_old`), `exp - iat` over a day
     (`lifetime_too_long`);
  10. `sub` and `jti` are non-empty texts (`claim_missing`).
- **Issuer before network.** Steps 1 to 5 use the token alone. Only a token whose `iss` is a
  configured issuer reaches the key cache, so an attacker's token can make the server fetch
  nothing but a configured issuer's own discovery document and keys; the cache itself refuses an
  issuer it was not built with. A random `kid` refreshes an issuer's keys at most once per 60 s,
  whatever the reason, so unknown key ids cannot turn the server into a request generator.
- **Algorithm confusion.** `none` and every `HS*` are refused when the settings load, and only the
  RSA algorithms (`RS256`/`384`/`512`, `PS256`/`384`/`512`) are supported. The verifier also refuses
  any algorithm outside that set before PyJWT sees it, so even a verifier handed `HS256` by
  mistake never checks an HMAC keyed with the issuer's public key (a test does exactly that).
  PyJWT is given the key object, never a PEM text, and one algorithm, never the header's choice
  among several.
- **Keys** (`JwksCache`): the discovery document comes from `<issuer>/.well-known/openid-configuration`
  on the issuer's own host and must name the issuer exactly; its `jwks_uri` must be `https` on the
  issuer's host or on a host of `LAKANA_CI_OIDC__ALLOW_HOSTS` (exact names, no wildcard, no IP),
  without userinfo, and the fetcher checks it again. Keys are fresh for 1 h; when a refresh fails
  the last good keys stay usable for 24 h past their freshness and then every lookup fails closed
  (`keys_unavailable`). Only public RSA signing keys of at most 8192 bits are kept (`use` absent or
  `sig`, no private member); two keys with one `kid` make the whole set unusable. Lookups of fresh
  keys take no lock; one refresh per issuer runs at a time, and the threads that wait for it use
  its result. A failed refresh logs the issuer (an operator setting) and the fixed reason.
- **The fetcher** (`lakana_core.net.https_json`): `https`, port 443 and an allowed host name only
  (never an IP literal), one lookup under 5 s, refused if **any** address is internal (the same
  `is_internal` as the webhooks), except for a host on `LAKANA_CI_OIDC__ALLOW_HOSTS` (below), then
  a connection to the checked address with the name as TLS
  server name; `trust_env=False`, no redirect (a 3xx is a refusal), only a 200, `Accept-Encoding:
  identity` and a refusal of any other `Content-Encoding` (no decompression bomb), at most 64 KiB
  counted while reading, 10 s for the whole request. Deep JSON nesting ends as `NOT_JSON`, never as
  an exception. `LAKANA_CI_OIDC__PROXY` and `LAKANA_CI_OIDC__CA_BUNDLE` work as the webhook ones do:
  through a proxy the name cannot be pinned, so the proxy must refuse internal destinations.
- **The settings refuse to start a wrong configuration**, naming the variable and the fix: an issuer
  that is not `github`, `gitlab` or an `https` URL with a host name and no port, credentials, query
  or trailing slash; issuers without `LAKANA_CI_OIDC__AUDIENCE` (there is no public-URL setting to
  derive it from); `none`, `HS*` or an unsupported algorithm; an allowed host that is a pattern or
  an address. Empty issuers turn the feature off (`disabled`).
- **Seams for the exchange.** `VerifiedIdToken` carries all claims, the issuer, `sub`, `jti` and the
  expiry; the caller must accept each `jti` once (until its expiry) and match its trust rules
  against the claims. `RefusalCode` is what the rate-limited `OIDC_EXCHANGE_REFUSED` audit row
  records.
- **An issuer on a private address** (a self-hosted GitLab on an internal network) needs two
  settings: its URL in `LAKANA_CI_OIDC__ISSUERS` and its host name in
  `LAKANA_CI_OIDC__ALLOW_HOSTS`. Only a host **exactly** on that list may resolve to a private
  address (10/8, 172.16/12, 192.168/16, 100.64/10, `fc00::/7`), for the discovery document and
  the keys alike; the issuers' own hosts are not exempt unless listed, and every other host must
  be public. Even a listed host is refused when any of its addresses is loopback, link-local
  (`169.254.0.0/16`, `fe80::/10`, so 169.254.169.254), unspecified, multicast or reserved, a
  known metadata address inside a private range (`fd00:ec2::254`, `100.100.100.200`), or an
  IPv6 form that carries such an IPv4 address (`is_never_reachable`). Since #243
  `LAKANA_WEBHOOK__ALLOW_HOSTS` applies the same rule (with a loopback exception for the entry
  `localhost`), and it still exempts the scheme and the port. Pinning, `https` on 443, no redirects, the 64 KiB cap and the deadlines
  still apply. As with the webhooks, list only names whose DNS you control: whoever controls
  a listed name's DNS can point it at any private address.
- **Residuals:** the `typ` header is not checked; `aud` and the issuer's own key set keep other
  tokens of the same issuer out.

## CI OIDC exchange and CI tokens (#15)

User documentation: `docs/site/reference/ci-trust-rules.md`. Threat model: "CI OIDC exchange
(#15)". This section is what a reviewer of code in this area has to know.

- **Trust rules** (`lakana_core.ci_oidc.rules`, table `ci_trust_rules`): issuer (one of
  `LAKANA_CI_OIDC__ISSUERS`, presets resolved), an active `SERVICE_ACCOUNT`, environment and
  repository (all by id; issuer, environment and repository never change), kinds (`deploy`,
  `dry_run`; never rollback or undeploy), exact-match claim conditions. Required: the numeric id
  claim (`id_claim`: `repository_id` for `GITHUB_ISSUER`, `project_id` for every other issuer,
  which the settings only accept as GitLab), digits only, `ref` or `environment`, and one claim of
  `trigger_claims` (decided 2026-10-20): `event_name` or `environment` for GitHub (a
  `pull_request_target` run has the base branch as `ref`), `ref_protected`, `pipeline_source` or
  `environment` for GitLab (a `ref` pin alone also matches the branch while it is unprotected and
  pipelines of every source: schedules, triggers, web, API; a fork's merge request pipeline
  carries the fork's `project_id`, which the id pin already refuses). `update` and `enable` check the stored conditions again;
  `rule_problem` makes a stored rule that fails them match no exchange and appear in `explain` as
  not allowed. Reserved
  claims (`iss`, `aud`, `exp`, `iat`, `nbf`, `jti`), more than 16 conditions, a non-text value,
  an empty one, one over 255 characters or with a control character are refused.
  `CiTrustRuleService` has eight call sites of `authorize_admin_only(..., MANAGE_TOKEN)` (a rule
  lets CI act as an account, which is issuing it a token), each in `test_admin_only_call_sites`'s
  `_GUARDED`. Every change records `CI_TRUST_RULE_*` in its transaction and raises `revision`
  (enable and disable too, a repeat changes nothing); a token is valid only under the revision it
  was issued with.
- **Matching** is `condition_mismatches`, the one function of the exchange and of `explain`: the
  claim must be present, a `str`, and equal. No case folding, no strip, no coercion (`123` never
  matches `"123"`, `["x"]` never matches `"x"`). Only enabled rules of the verified issuer for the
  requested environment (live) and repository count; two matches refuse as ambiguous.
- **The exchange** (`CiTokenService.exchange`, `POST /api/v1/auth/oidc/token`; the route reads
  at most 16 KiB itself, `streams_its_own_body`, and runs the synchronous verifier in the
  threadpool with a session of its own): verify; record `jti_digest(issuer, jti)` in
  `ci_oidc_jtis` and **commit before matching**, so a refused exchange still uses the ID token
  up and one ID token probes one environment and repository at most (a concurrent duplicate fails
  on the primary key); rows whose ID token expired more than the 60 s skew ago are swept in that
  transaction. Then rules, an active service account, and for a rule with `deploy` a `sha` claim
  of 40 lower-case hex digits (`valid_sha`). The `CiToken` (`lakana_ci_<key id>_<secret>`,
  SHA-256 of the secret, like a PAT) stores rule, revision, principal, environment, repository,
  kinds, `sha`, `jti_sha256`, TTL `LAKANA_CI_OIDC__TOKEN_TTL_SECONDS` (3600, 60 to 21600), and
  `OIDC_TOKEN_EXCHANGED` is recorded as the service account in the same transaction.
- **What a refused caller learns** (decided): a verifier refusal answers 401 with its
  `RefusalCode` message, a replay 401 with its own; everything after verification (no rule,
  ambiguous, unknown environment or repository, inactive principal, missing commit) answers one
  403 text that echoes the requested names only. The server records `OIDC_EXCHANGE_REFUSED`
  through `app.state.oidc_exchange_audit_limiter` (an `AuthAuditLimiter` of its own,
  `LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE`): `code`, the issuer only when it is a configured one
  (`_claimed_issuer` reads the unverified `iss` of a refused token and keeps it only then), and the
  requested names, `jti_sha256` and rule ids only after verification. Never the JWT or the token.
  The 422 for a bad body names fields, never values (`hide_input_in_errors`).
- **Authentication** (`get_principal`, prefix `lakana_ci`): `CiTokenService.verify` → hash
  lookup, not expired, then `resolve_ci_scope` (fresh reads, `populate_existing`): rule exists,
  enabled, same revision, same principal, environment and repository; principal an active
  `SERVICE_ACCOUNT`; environment live; repository exists. The result is
  `PrincipalRef.ci_scope` (`CiScope`: token, rule, environment and repository with ids, kinds,
  `sha`, `used`). Sign-ins are bounded like PATs (`("ci", key_id)`).
- **The authorizer** (`GrantAuthorization._ci_scope_refusal`, before any grant): a CI-scoped
  principal may only `DEPLOY` on an `ENVIRONMENT` securable that is its environment. Not
  `READ_ENVIRONMENT` (narrower than the design's "DEPLOY and READ_ENVIRONMENT": nothing the CI
  flow needs reads it, since `DeployService.get` takes the `DEPLOY` branch), no table, namespace or
  instance action. The grants still decide `DEPLOY`. `GrantService.explain` treats a CI token like
  a task token: never "about oneself".
- **Every `DEPLOY` check** decides the rest (`lakana_core.ci_oidc.scope`; refusals are audited
  as `AUTHORIZATION_DENIED` with `reason: ci_token` through the denial limiter):
  - `DeployService.submit`: `check_ci_request` (its environment and repository by id, a kind in
    `kinds`, never a rollback, a deploy only with a `sha`, and only of it: no ref means the
    `sha`, another ref is refused), `deploy_runs.ci_token_id` set, and `use_ci_token` in the same
    transaction (one conditional `UPDATE` on unused, unexpired, same rule, rule enabled at the
    same revision). A second submission, of either kind, is refused.
  - `DeployService.get`: only a run whose `ci_token_id` is the token's; any other is the same 404
    as a missing one.
  - `DeployService.authorize_deploy` (the worker, `run_deploy`): `ci_scope_for_run` resolves the
    run's token again (missing, another principal, or invalid: refused; an expiry since queuing is
    not), `DEPLOY` is authorized with the scope, `check_ci_request` again, and a deploy's
    `requested_ref` must be the `sha`. After the fetch, `DeployService.check_ci_commit` refuses
    another commit. The table checks of the deploy use the account without the scope, as any
    deploy.
  - `DeployService.undeploy`, `JobService.pause`, `resume`, `notify_test` and `delivery`:
    refused (`refuse_ci_token`, before their own check).
  - The jobs a CI deploy starts after it (`on_deploy: true`, `jobs/on_deploy.py`) are authorized
    as the account **with** the token's scope. Only the rule's explicit opt-in
    `start_on_deploy_jobs` allows them (`GrantAuthorization._ci_deploy_job_start`: `RUN`, its own
    environment, and only while the worker marks the scope `starting_deploy_jobs`); otherwise
    `RUN` is refused and the deploy names each job (`lacks_run`). The token is refused every other
    run either way (threat model, "CI OIDC exchange", residual 7). Pins: `test_ci_deploy_jobs.py`,
    `test_deploy_ci_token.py`.
  - `test_ci_token_enforcement.py::test_every_deploy_check_is_covered` lists every function in
    `packages/*/src` that names `DEPLOY` and fails on a new one; add it there with a test.
- **No foreign key on `deploy_runs.ci_token_id`** (like `QueryRun.task_token_id`): a missing
  token row must not make the run look like one no CI token queued; the worker refuses it.
- **Tests:** `test_ci_trust_rules.py`, `test_ci_oidc_exchange.py`, `test_ci_token_enforcement.py`
  (core), `test_deploy_ci_token.py` (worker: the signed commit, another fetched commit, a rule
  disabled after queuing, a tampered ref), `api/platform/test_ci_oidc.py` and
  `test_ci_trust_rules.py` (server), `sdk/test_sdk_ci_trust.py`. Locally signed keys and a fake
  fetcher; nothing reaches the network.
- **Review checklist:** a new `DEPLOY` check decides what a CI token may do there; a new action
  stays refused for a CI scope unless it is `DEPLOY`; nothing logs or records a JWT or a
  `lakana_ci_` token; a refusal after verification keeps the one text.

## Git fetch hardening (M5b)

`lakana_worker.git.fetch_commit` is the only code that runs git. It lives in the worker so git,
network egress and temp disk never touch the API server. Every change to it keeps this
checklist true; `packages/lakana-worker/tests/test_git.py` pins each line.

- **Deployers never choose a URL.** Only `MANAGE_REPOSITORY` registers one; a deploy names a
  repository. URL, ref and local path are re-validated in the worker
  (`lakana_core.projects.git_input`, against the *current* `LAKANA_REPO_ROOTS`) before any argv is
  built, and the resolved local path is what git receives.
- **No option injection.** Refs follow `check-ref-format` and may not start with `-`; every
  user-supplied value follows `--end-of-options`; the URL is passed as `remote.origin.url`
  through `GIT_CONFIG_KEY_n`, never as an argument; hosts and paths may not start with `-`.
  Remote URLs may not contain `?` or `#`: `urlsplit` ends the host there and git does not, so
  `ssh://good.example?@evil.example/r` would validate one host and connect to another.
- **Minimal environment.** `PATH`, a private `HOME`, `LC_ALL=C`, `GIT_*` and, only when the operator sets
  `LAKANA_GIT__NO_PROXY`, the one `no_proxy` variable — no
  `LAKANA_*`, database, storage or KMS variable reaches git, ssh or the askpass helper.
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_TERMINAL_PROMPT=0`,
  `GIT_CEILING_DIRECTORIES` set to the parent of the work directory, so repository discovery
  never climbs out of the private temp root.
- **One protocol.** `protocol.allow=never` plus the repository's own (`https`, `ssh`, or `file`
  for `kind=local` only). `credential.helper` empty, `core.hooksPath=/dev/null`,
  `core.fsmonitor=false`, no submodules, `http.followRedirects=false`. No LFS: the commit is
  exported with `git archive` from a fresh bare repository, so no checkout or smudge runs.
- **Local repositories are fetched, never used in place**, from a path resolved under
  `LAKANA_REPO_ROOTS`. For `kind=local`, `git-upload-pack` runs *in the source repository* and
  reads its repo-level config: command-scope settings do not reach it, so the source's config
  is **not** ignored. What protects the worker is that upload-pack runs no working-tree,
  fsmonitor or hook code, and git refuses repo-level `uploadpack.packObjectsHook` by design.
  `test_local_repo_config_never_runs` and `test_local_upload_pack_hooks_in_the_source_config_never_run`
  pin `core.fsmonitor`, `core.hooksPath`, `uploadpack.packObjectsHook` and
  `core.alternateRefsCommand`, and a partial-clone promisor remote with an `ext::` URL.
  A repository owned by another uid (the Compose `./repos` mount) trips git's "dubious
  ownership" check inside upload-pack, so `fetch_commit` writes a private global config
  (`GIT_CONFIG_GLOBAL`, 0600, per fetch) whose `safe.directory` entries name exactly the resolved
  source and its `.git` — never `*`, never the fresh fetch target, never for https/ssh.
  A resolved path whose last component is `*` is refused at registration and again in the worker
  (`resolve_local_path`), because git reads `directory = "/repos/*"` as a prefix glob.
  `test_local_fetch_trusts_only_the_resolved_source_through_a_private_global_config` pins the file;
  the Compose smoke deploy pins the behaviour.
  `LAKANA_REPO_ROOTS` confines the *path*, not the *data*: a gitfile (`gitdir: /outside/...`),
  `objects/info/alternates` or `commondir` inside a root can point outside it, so roots must hold
  only trusted content. Minimum git: **2.45.1 or newer**, or one of the security lines 2.39.4, 2.40.2,
  2.41.1, 2.42.2, 2.43.4, 2.44.1 (they honour `GIT_NO_LAZY_FETCH`, which the worker also sets, so
  a promisor remote in the source cannot make upload-pack fetch).
- **Credentials.** In memory, and in 0600 files (askpass 0700) under a 0700 directory inside
  the fetch's own directory in the work directory, removed in `finally`. A username (which may
  contain `@ / % #`) reaches git only through the askpass helper, never inside the URL. SSH
  uses `-F /dev/null`, `StrictHostKeyChecking=yes` against the repository's `ssh_known_hosts`
  only, `BatchMode`, no agent, `IdentitiesOnly=yes` (and `IdentityFile=none` without a deploy key:
  ssh resolves `~` from the passwd entry, not `$HOME`, so the worker user's own `~/.ssh` keys
  would otherwise be offered). Never in argv, logs, audit details or `error_message`.
- **Errors.** stderr is redacted (the token, its basic-auth encoding, the key, any URL
  userinfo) and truncated; 401, 403, 404 and "not found" all become
  "repository not found or credential rejected".
- **Defence in depth.** `transfer.fsckObjects=true` rejects malformed objects at fetch. Every
  git command (and `git archive`) runs in its own session and a timeout kills the whole process
  group, so `git-remote-https` and `ssh` die with it. A `git archive` that reaches its deadline
  fails even if git already exited 0 (output may still sit unread in the pipe), and git's exit
  status is checked; those two are what guarantee a cut-off archive is not extracted. An archive
  without tar's end-of-archive marker (two zero blocks) is also refused, which catches most cuts
  but not all: one that falls just after a zero-filled last member still ends in zero blocks.
- **Limits.** 120 s per git command, 600 s for the job, 200 MiB of archive and 200 MiB of summed
  extracted file sizes, 100 000 members. Tar members are extracted through
  `tarfile.data_filter`; no member may lie below an earlier symlink member (a purely lexical rule on names compared
  case- and Unicode-normalisation-folded, as on APFS/HFS+; names with HFS+-ignorable code points
  are refused; filesystem resolution can be defeated past `PATH_MAX`), and duplicate names are refused,
  compared the same folded way. **Limitation:** a repository with paths that differ only in case or
  Unicode normalisation (e.g. Linux's `xt_CONNMARK.h` and `xt_connmark.h`) cannot be deployed,
  even on a Linux worker; the error says the names collide when compared case- and
  normalisation-insensitively. Symlinks are kept as links for discovery to
  refuse by name; device nodes, FIFOs and hard links are refused
  (discovery opens files before it checks their type, so nothing but files, directories and
  symlinks may reach the checkout).
- **Only branches, tags and the server's HEAD deploy (M5c1).** `validate_ref` refuses a fully
  qualified ref outside `refs/heads/` and `refs/tags/` (`refs/pull/*`, `refs/changes/*`,
  `refs/merge-requests/*`, `refs/remotes/*`, ...). Short names are not blocklisted (a branch may be
  called `environments/prod`); the worker is authoritative. It lists
  `ls-remote --refs --heads --tags` for the ref and matches `refs/tags/<name>` then
  `refs/heads/<name>` (or the qualified name) **exactly**, never trusting git's own name
  resolution, which would also try `refs/<name>`, `refs/remotes/<name>`, `refs/refs/heads/<name>`.
  After the fetch, `FETCH_HEAD` must equal the listed object id, otherwise the deploy fails
  ("changed while it was being fetched"), so a ref deleted or replaced between listing and
  fetch cannot fall back to a decoy. `HEAD` is resolved the same way: `ls-remote origin HEAD`
  must list an exact `HEAD` line (its patterns match by suffix, so `refs/remotes/HEAD` is listed
  too and ignored), `FETCH_HEAD` must equal that object id, and an unborn HEAD is refused ("the
  repository's HEAD does not point to a commit; deploy a branch, tag or commit") rather than
  letting git's DWIM fetch `refs/HEAD` or `refs/remotes/HEAD`. It is whatever the server's HEAD
  points to: a branch on every mainstream host, though a server administrator could point it
  elsewhere.
- **SHA deploys need a reachable commit (M5c1).** A full SHA is first checked in a throwaway
  probe repository: `ls-remote --refs --heads --tags` (refused above `DeployLimits.fetch_refs`;
  `refs/pull/*` and `refs/changes/*` do not count, so a busy repository on a code host still
  deploys), then every branch and tag fetched with `--filter=tree:0` (commits and tags only), the
  SHA peeled to a commit and `for-each-ref --contains` required to name a ref (after a best-effort
  `commit-graph write --reachable`, which keeps the check fast at thousands of refs; its failure
  is ignored and its file is not counted against `fetch_bytes`). A tree or blob SHA
  is refused ("does not name a commit" when the probe has the object, otherwise "not reachable").
  Protocol v2 serves any object by id, so without this a commit force-pushed away (or never
  pushed to a branch or tag) would still deploy. The probe is a partial clone, so every probe
  command runs with `git_environment` (`GIT_NO_LAZY_FETCH=1`): looking up an unknown SHA must not
  download it on demand.
  `test_looking_up_an_unreachable_sha_in_the_probe_never_lazily_fetches_it` pins that. A server
  without filter support sends everything, bounded by the fetch limit below.
- **Fetches are bounded (M5c1).** Every `git fetch` and `ls-remote` runs through
  `_run(max_bytes=DeployLimits.fetch_bytes)`: while git runs, the temporary repository's size is
  polled every 0.5 s and the process group is killed past the limit ("repository fetch exceeded
  1 GiB"; default 1 GiB, 10 000 branches and tags); the size is checked again after git exits.
  Polling means the size may overshoot by up to one interval's throughput. Every git command
  run through `_run` (all but `git archive`, which streams to a file under its own
  `checkout_bytes` limit) has its stdout read as it arrives and cut off (process group killed) at
  16 MiB or `max_bytes`, whichever is smaller ("git ls-remote output exceeded 16 MiB"), and
  at most 64 KiB of stderr is kept, so a hostile server cannot grow the worker's memory with
  `ls-remote` output.
- **Proxies and internal CAs are explicit operator settings.** `LAKANA_GIT__HTTPS_PROXY`,
  `LAKANA_GIT__NO_PROXY` and `LAKANA_GIT__CA_BUNDLE` (`lakana_core.config.GitConfig`, validated by
  shape in the shared settings and again by `fetch_commit`: `http(s)://` URL with a host, userinfo
  only as percent-encoded `user:password` — a username alone is refused, because git would ask the
  askpass helper for the proxy password; the CA an absolute path, whose existence and readability
  the worker alone checks at startup, since the server and `migrate` load the same settings
  without its mount) reach git only as `http.proxy`, the single `no_proxy` variable (git has no
  `http.noProxy`; libcurl reads the variable), `http.sslCAInfo` and, for an `https://` proxy,
  `http.proxySSLCAInfo`, for `https` repositories only. The askpass helper answers only git's
  prompts for the repository's own origin and exits 1 for any other (the proxy's), so the
  repository token cannot reach a proxy. The worker's own `HTTPS_PROXY`,
  `ALL_PROXY`, `NO_PROXY` and CA variables are still never inherited. The CA bundle replaces
  trust, it does not disable verification. Proxy credentials are added to the redaction secrets
  (`proxy_secrets`: password, `user:password`, percent-decoded and base64 forms), validation
  errors never echo the value, and the setting is a `SecretStr` (`repr`, `str` and `model_dump`
  hide it) that `DeploySettings` leaves out of its `repr`. **Residual:** the proxy URL is in the
  git process's command-scope config (its environment), like the repository URL. SSH ignores all
  three settings; `ProxyCommand` is unsupported.

## Local mode (M4c)

- **Binds to `127.0.0.1` only.** `lakana server start --local` always passes `LAKANA_HOST=127.0.0.1`
  to `lakana-server`; there is no flag to bind it anywhere else, and `--port` only chooses which
  loopback port.
- **The data directory must be private to the user, not just present.**
  `lakana.cli.local._datadir.prepare_data_dir` refuses to use it if the leaf is a symlink,
  isn't owned by the current user, or is accessible to group or other (mode `& 0o077`) —
  the same rule the SDK's profile file already applies. The *parent* directory is refused
  only if it is other-writable without the sticky bit set; a merely group-writable parent is
  accepted, since that's the normal shape of a user-private-group umask (e.g. Ubuntu's
  default `0002`), not a sign of a shared, hostile directory. **Residual risk:** the
  ownership/mode checks on the leaf directory assume a user-owned parent path; a
  group-writable parent shared with other users on the same account's group is accepted, and
  a TOCTOU window exists between checking the parent's mode and creating the leaf inside it,
  same shape as the profile-directory gap in "SDK credentials at rest" above. Winning it
  needs write access to the parent directory, which already implies significant access to
  this user's account.
- **`master.key` is generated once, mode `0600`, and read back safely.**
  `load_or_create_master_key`/`_read_master_key` open it with `O_NOFOLLOW` and check
  ownership and permission mode via `os.fstat` on that same descriptor before reading —
  no window between the check and the read (TOCTOU) — and validate the contents decode as
  exactly 32 bytes of base64. Creation is atomic: a fresh `O_CREAT | O_EXCL | O_NOFOLLOW`
  temp file at mode `0600` is written, fsynced, and `os.link`ed into place, so two processes
  racing to create it never overwrite each other — the loser reads back the winner's key.
- **Local repositories (M5b).** `lakana server start` sets `LAKANA_REPO_ROOTS` to the resolved home
  directory unless the caller's environment sets it, so any repository under `$HOME` can be
  registered as `kind=local`. Nothing outside it can. As everywhere, the roots are trusted
  content only (gitfiles/alternates can point outside; see the M5b section above).
  The refusal names the setting and the next step (move the project under the home directory;
  the operator edits `LAKANA_REPO_ROOTS`) but never the configured roots or a resolved path:
  a caller without the operator's rights must not learn the server's layout.
  The roots check runs **before** the is-a-directory check, so every path outside the roots gets
  the identical refusal whether or not it exists; the other refusals ("is not a directory", a last
  component of `*`) are reachable only for paths already inside a root. Keep that order: swapping
  it lets a caller probe the server's file system for arbitrary paths.
- **Unsafe paths are refused with exit 2; filesystem errors exit 3, naming the path.** A
  data directory or `master.key` that is a symlink, owned by another user, or accessible by
  group/others is a refusal (exit 2), with the fix in the message (`chmod 700`/`chmod 600`).
  A directory that can't be created or written, a pidfile that turns out to be a directory,
  a transient `EIO`/`EACCES` on the key file — all surface as `CliError` exit 3 naming the
  exact path. Only a `master.key` with corrupt contents or the wrong owner advises deleting
  the data directory: nothing else says "your secrets are unrecoverable", since a transient
  I/O error, or a mode fixable with `chmod`, doesn't mean the key itself is bad.
- **The bootstrap credential is passed once, through the child's environment, and never
  logged.** On first run only, the CLI mints a key id (`secrets.token_hex(8)`) and secret
  (`secrets.token_urlsafe(32)`) and sets `LAKANA_BOOTSTRAP_PAT_KEY_ID`/`_SECRET` in the
  environment passed to `lakana-server` (and, since both children share one constructed
  environment, to `lakana-worker` too, though the worker never reads either variable).
  Environment variables are readable only by processes running as the same user (via
  `/proc/<pid>/environ` on Linux, or the platform equivalent), which is the control this
  relies on. **Accepted, not incidental:** vending the credential to the worker's
  environment as well as the server's, though the worker has no use for it, is accepted
  because in local mode the same local user already administers both processes — there is
  no separate worker operator to keep it from. When the server honours a caller-supplied
  bootstrap credential, it logs only the key id at `DEBUG`, never the token — the caller
  (the CLI) already holds the full value. The server never logs a token at all (#208, below).
  **Later runs never repeat this.** `_child_env` builds the child environment from
  `os.environ` plus explicit overrides; on any run after the first (`key_id`/`secret` both
  `None`), it explicitly pops `LAKANA_BOOTSTRAP_PAT_KEY_ID`/`_SECRET` from that environment, so
  a value inherited from the parent shell can never silently re-bootstrap or leak into a
  later, unrelated run.
- **A first run whose `migrate` fails after creating `lakana.db` is classified as a later run
  on the next `start`, not a repeat of the first.** `first_run = not root.db_path.exists()`
  looks only at whether the SQLite file exists, and `migrate` creates it (empty tables
  aside) before applying any migration, before it can fail. The next `start` therefore
  passes no bootstrap credential — but the database has no admin either, since migration
  never completed — so the server's own lockout recovery (`bootstrap_if_needed`, above) acts
  exactly as it would for any instance found with no active admin: it mints a fresh
  credential and writes it to `<data dir>/bootstrap-token` (`_child_env` sets
  `LAKANA_BOOTSTRAP_TOKEN_FILE` there, inside the 0700 data directory; an inherited value
  wins), never to the log. The CLI itself never sees or prints it, and its own message on this
  path still only tells the user to delete the data directory and start over — a correct
  instruction (the database is otherwise unmigrated and unusable).
- **The `local` profile is saved before `dev` is created.** If `environments.create` then
  fails or is interrupted (e.g. Ctrl-C), the admin token is already on disk. The next run's
  `_later_run_profile_check` notices `dev` is missing and creates it itself, over the REST
  API, printing one line when it does. The check is best-effort: a rejected token or a
  server error prints a warning (never the token) and the instance keeps running. Run by hand, the recovery command is `lakana env create
  dev --storage-root file://<data-dir>/lake/dev` (not `lakana environment create`, which isn't
  a command) — either way the credential is never lost with no way back short of deleting
  the data directory.
- **A later run only ever updates its own previous loopback URL, never someone else's
  profile.** If `--port` changes between runs, `_later_run_profile_check` rewrites the
  `local` profile's URL only when its *current* URL already matches this machine's own
  loopback shape (`http://127.0.0.1:<port>`) — the existing token is kept, nothing else
  changes. A `local` profile pointing anywhere else (a remote server, another data
  directory's instance) is left untouched, with a warning to stderr, rather than silently
  repointed at this run while keeping a now-mismatched token. A **first** run, unlike a
  later one, does overwrite an existing `local` profile unconditionally — it has to save its
  freshly minted credential somewhere named `local` — but if that profile's URL wasn't
  already this machine's own loopback address, a warning names the URL being replaced first,
  so starting a first run against a second `--data-dir` (or after a remote profile was set up
  under the same name) doesn't silently discard another instance's token with no trace.
- **Shutdown never signals a PID read back from a pidfile.** `Supervisor.stop()` sends
  `SIGTERM`/`CTRL_BREAK_EVENT` only to the child handles it holds in memory from `Popen`
  itself, covering `migrate` as well as `server`/`worker` — a Ctrl-C during migration no
  longer leaves that child running, untracked, in its own session; pidfiles are only hints, for
  `lakana server status` and for refusing a `start` over a lock-less older-lakana instance;
  the data-directory lock decides otherwise (a PID is a hint, not proof — the OS may
  have reused it since a previous instance crashed) — nothing in local mode ever calls `os.kill` on a PID recovered from a
  file.
- **Closing the terminal doesn't orphan the children.** `SIGTERM` and, on POSIX, `SIGHUP`
  both get `_raise_keyboard_interrupt` installed for a run's duration (restored on exit),
  taking the same shutdown path as Ctrl-C. `SIGHUP` is left alone if it's already `SIG_IGN`
  — installing over that would defeat `nohup`. `SIGINT` gets `signal.default_int_handler`
  installed explicitly too, so a backgrounded `start` (which inherits `SIGINT` as `SIG_IGN`
  from a non-interactive shell) still stops on `kill -INT`. All three are set to `SIG_IGN`
  for the duration of the final `stop()` call itself, so a second interrupt can't abort
  cleanup partway through and leave an untracked child behind.
- **`GET /readyz` is unauthenticated and reveals check names only.** It answers 200
  `{"status": "ready"}` or 503 `{"status": "not_ready", "failed": [...]}` with `database`,
  `storage` and `worker`; never a host, an environment, an error or a version (the reasons go to
  the server log). It cannot be used to load the database or a storage: one answer is reused for
  `LAKANA_READINESS__CACHE_SECONDS` (at least 1 s), requests during a check wait for that check
  (an `asyncio.Lock`, no thread each), each check runs in one daemon thread with a time limit,
  and a check still running from an earlier request is reported failed instead of started
  again, so a hanging database or storage holds one thread each. No rate limit: past the cache a
  request costs what `/healthz` costs, and a limit would fail a monitor's own probes. The
  storage probe asks about the root (no object read or written), at most the first 5
  environments by name (`lakana_server.readiness`, `lakana_core.readiness`).
- **`GET /healthz` is unauthenticated and reveals only liveness.** It lives outside
  `/api/v1` (not part of the SDK route contract) and returns `{"status": "ok"}` with no
  other information — the same shape the supervisor's own readiness probe and `lakana server
  status` both rely on. Both probes send `trust_env=False`: they always target `127.0.0.1`,
  and `httpx`'s default of honouring `HTTP_PROXY`/`HTTPS_PROXY` would otherwise route a
  loopback probe through a configured proxy, turning a dead or unreachable one into a
  spurious 30 s readiness timeout or `status` failure against a perfectly good local server.
- **An exclusive lock on the data directory decides whether an instance runs.**
  `start` takes a non-blocking exclusive lock on `<data-dir>/lock` (`lakana.cli._filelock`:
  `fcntl.flock` on a `0600`, `O_NOFOLLOW` file; `msvcrt.locking` on Windows, best effort)
  *before* `migrate` and holds it for the supervisor's whole lifetime, releasing it last,
  after the children are stopped and the pidfiles removed. If it is taken, `start` exits 2
  with `Lakana is already running from <data-dir>`, whatever `/healthz` would say, so a slow
  live server is never judged stale and the window during `migrate` is covered. The OS drops
  the lock when the process dies, so a crashed instance never blocks the next `start`. An
  instance started by an older lakana holds no lock, so with the lock free `start` still
  refuses if the recorded server PID is alive *and* answers `/healthz` on the recorded port
  (the same check `status` ORs in); any other leftover pidfiles are stale and are removed.
  Children never inherit the lock descriptor. Pidfiles remain for `lakana server status` and
  that upgrade-time check. `status` reports running if the lock is held (a non-blocking try-acquire, released at once; nothing is created if the
  lock file doesn't exist) **or** a recorded server PID is alive and answers `/healthz` (the
  second branch keeps an instance started before the lock existed visible). Advisory only:
  it stops cooperating `lakana` processes, not a process that ignores the lock; a `status`
  probe holds the lock for microseconds, so a `start` racing exactly that moment can be
  refused spuriously and should be retried.

## Team mode (M4d)

`lakana bundle init` generates a Docker Compose bundle (`docs/site/how-to/deploy-with-compose.md`) for a small
team sharing one instance. Its security posture differs from local mode's in the ways that
matter for an operator deciding where to run it:

- **Postgres and SeaweedFS publish no host ports.** `compose.yaml` gives them no `ports:`
  mapping at all — only the `server` service does, on `${LAKANA_BIND}:${LAKANA_PORT}`. The
  catalog and object storage are reachable only from other containers on the Compose
  network, never directly from the host or the network beyond it.
- **`.env` (mode `0600`) is the root of trust for the whole deployment**, and stays valid
  until explicitly revoked or rotated: the envelope-encryption master key
  (`LAKANA_SECRETS__MASTER_KEY`), the Postgres password, the SeaweedFS S3 keys, and the
  bootstrap admin credential (`LAKANA_BOOTSTRAP_PAT_KEY_ID`/`_SECRET`) all live there in
  plaintext. Anyone who can read it can decrypt every stored secret, reach the catalog
  database directly, and authenticate as the instance's original admin. See "Revoking the
  bootstrap credential" in `docs/site/how-to/deploy-with-compose.md`.
- **The image runs non-root**, uid 10001 (`Dockerfile`), for the server, every worker,
  `setup` and `egress` alike. The one exception is `launcher`, the only service that mounts the
  Docker socket (root on the host): `user: "0:0"` only to open the socket, whose group varies by
  host, with `network_mode: none`, `cap_drop: [ALL]`, a read-only filesystem, and the base
  interpreter (`python -I -S`, standard library only) for its command and its healthcheck, so no
  `.pth` file of a dependency runs next to the socket. The workers reach it through the
  `launcher-socket` volume and never mount the Docker socket; `test_deploy_init.py` pins both.
- **The server binds to `127.0.0.1` by default.** `lakana bundle init`'s `--bind` defaults to
  loopback; exposing it on the network (a specific address, or the `0.0.0.0`/`::` wildcard)
  triggers a plain-HTTP warning, since Lakana itself does not terminate TLS.
- **TLS comes from the operator's own reverse proxy**, put in front of the server and paired
  with `--public-url` — Lakana is not designed to be exposed directly to the network without
  one.
- **`setup` goes through the REST API only.** The one-shot `setup` service creates `dev`,
  its storage secret, and the binding between them using only the SDK against the running
  server's public API (the bootstrap admin token and the SeaweedFS S3 keys, both from
  `.env`) — it holds no direct database or storage credential of its own, consistent with
  "The REST API is the only way in" above.
- **The image is public.** `ghcr.io/lakana-labs/lakana` needs no login to pull. Its package visibility
  is a setting of the GitHub package, not of the repository: check it when the image name changes.
- **Worker shutdown (closed by M5c3b).** `SIGTERM` stops claiming; a running task run is
  released after `LAKANA_RUN__DRAIN_GRACE_SECONDS` (the bundle waits 45 s before `SIGKILL`), and a
  task whose worker dies is reclaimed with its write run failed. The ad-hoc queries and deploys of
  a worker that dies (`SIGKILL`, power loss) are ended by the scheduler's sweep after about a
  lease, `FAILED` with the cause `worker_lost`; a write that had already committed ends
  `SUCCEEDED`. Nothing stays `RUNNING` for ever. A venv build in progress is stopped once the
  drain grace has run out (#291).
- **SeaweedFS's internal HTTP APIs are unreachable from the worker (#14), by network isolation.**
  The worker runs user SQL with DuckDB's `httpfs` (since allow-list mode, a query process opens
  only the URL prefixes of the tables it reads, but follows a redirect and lets a literal `..`
  reach the same host; see "Allow-list mode"), and SeaweedFS's
  master (9333), volume (8080) and filer HTTP APIs need no credentials, so anything the worker
  can reach would be a way around credential scoping. Chosen: `weed server` (master, volume,
  filer; `-disableHttp` too) runs as `seaweedfs-core` on `seaweed-internal` (`internal: true`),
  which only SeaweedFS's services share. `seaweedfs` is a separate `weed s3` container, the one
  service on both that network and `default`, listening on 8333 plus gRPC 18333 (`-port.iceberg=0
  -port.lance=0` turn its other HTTP ports off). `server`, every worker and `setup` are on
  `default`, so S3 with the keys, and the residual 18333, are all they can reach. One container cannot be
  split by port, which is why the gateway is its own container and not `weed server -s3`.
  JWT signing was not chosen: network isolation needs no key distribution, and a signing key
  would have to stay out of the worker anyway. Asserted by
  `test_seaweedfs_internal_apis_are_unreachable_from_the_worker` (compose structure) and by a
  check run inside the worker in `scripts/compose_smoke_test.sh` (CI). **Not protected:** the gateway's gRPC port 18333 (it includes IAM) cannot be turned off and has no authentication. It is reachable from the default network, so from code in the worker: user SQL cannot reach it, because DuckDB's HTTP filesystem cannot speak gRPC, but a Python task under `LocalProcessRunner`, which runs as trusted code in the worker, could remove the storage identity and make storage unavailable until a restart. Under `DockerRunner` a task's network holds only the server (and, for a task with connections, the egress proxy and the task's relay) and a build's only the egress proxy; the proxy and the relay refuse the networks they are attached to, so neither reaches SeaweedFS's port 18333. The isolation relies on Docker's own network isolation (the default); a daemon with `iptables: false`, Podman or rootless setups may not provide it. The S3
  keys, held by the worker as before, give full bucket access (per-environment scoping is the
  presigned-URL path above); anyone with Docker access to the host reaches everything. Any change
  that joins a worker or server to `seaweed-internal`, or publishes a port of `seaweedfs-core`,
  reopens #14 and fails that test.

## Mutation testing

- **Mutation testing runs weekly.** `.github/workflows/mutation.yml` runs on schedule (Monday 03:00 UTC) and on `workflow_dispatch`. A survivor in the mutation targets (`scripts/check_mutants.py`) must be addressed by either: (1) writing a test that kills it, or (2) recording it as a confirmed equivalent mutation and updating the exit-run totals in `docs/security/mutation-testing.md`. `scripts/check_mutants.py` is the gate; any survivor left unresolved fails CI.

## Process

Every PR runs bandit, gitleaks, pip-audit, and the license check (`security.yml`).
gitleaks uses its default rules plus the allowlist in `.gitleaks.toml`, which may hold only
exact literals — test-fixture token values, or a specific code line the generic-api-key rule
misreads (e.g. `token=token, …` in `lakana login`) — never a pattern broad enough to hide a real
credential. Prefer fixture tokens that don't trip the rules at all; allowlist one only when the
test needs its shape (e.g. the SDK's masking tests, which need a secret with underscores and
dashes). gitleaks scans every commit in a PR, so a false positive in any earlier commit needs an
allowlist entry even after the line has changed. Scope such an entry to the commits that carry the
line (an `[[allowlists]]` table with `condition = "AND"`, `commits` and `regexes`): a global line
entry for a fake key header also hides a real key written right after it, because the
private-key rule's match runs on to the next key's header. The `secrets` job runs the gitleaks command line
(MIT) from one pinned release whose SHA-256 is checked, not the gitleaks GitHub Action, which needs
a paid licence key for a repository owned by an organisation; raise the version and the checksum
together from the release's checksums file. Any
feature touching auth, policy, secrets, or the query path should also get a `/security-review`
pass before merge. Semgrep runs weekly (`security-deep.yml`); CodeQL (`codeql.yml`) runs on every
pull request, every push to `main` and weekly; both upload SARIF to code scanning. OpenSSF Scorecard (`scorecard.yml`) runs on every push to `main`,
weekly and on a branch-protection change, and publishes its score for the README badge; read the
results at <https://scorecard.dev/viewer/?uri=github.com/lakana-labs/lakana>.
