# Threat model

This is the living threat model for Lakana. It is updated at every milestone as controls
land — see "Current posture" below for what actually exists today, which is less
than the rest of this document might suggest in isolation. Component boundaries and
constraints are in `docs/agent/architecture.md`; agent-facing orientation lives in
`docs/agent/security.md`.

**Stating what this document does not defend against is the point of it.** A threat model
that lists only its controls invites the reader to assume everything else is covered.
The "Explicitly not defended" section below is not a disclaimer tacked on at the end — it
is as load-bearing as the controls table, and for the same reason a `WHERE` clause is
load-bearing: what it excludes is exactly what defines what it includes.

## Assets

What an attacker, or an over-broad grant, could reach:

- **Table data in object storage** — the Parquet/Iceberg data files under a managed zone.
- **Catalog metadata** — table schemas, partitioning, snapshot history, namespace
  structure; reveals shape and existence of data even without reading it.
- **Credentials and secrets** — session cookies, PATs, task tokens, and the secrets stored
  for connections and tasks.
- **The audit log** — itself sensitive, since query text and `WHERE` clauses can contain
  customer data. Reading it needs `READ_AUDIT_LOG`; another principal's query text and
  column values (an incremental write's watermark) need `READ_QUERY_TEXT` too, which no role
  carries (#88, #239), and every read of other people's events
  is itself recorded (`ACTIVITY_VIEWED`).
- **The code in linked git repositories** — the Python and SQL that tasks execute, and the
  history of who reviewed and approved it.
- **Artifacts (M5c2b)** — stored model files and other files that Python assets produce, and
  the training data that a model can reveal.

## Trust boundaries

These are architectural boundaries the design commits to, not runtime checkpoints running
today — at M0 there is no REST API, no worker process, and no object storage integration
to draw a line around yet (see "Current posture"). They are stated here because
every control in the next section is described relative to one of them.

- **The REST API is the only entry point.** No component — not the CLI, not a future UI,
  not user code — has a privileged side channel around it (see
  `docs/agent/architecture.md`).
- **The worker process boundary**, where user SQL and user Python actually execute. Code
  crossing into a worker is trusted; what it can reach from there is bounded by the
  credentials it holds, not by inspecting what it does.
- **The task container boundary** (M7b, `DockerRunner`): a Python task or environment build runs
  in a container of its own, without the worker's credentials, on a network shared with the
  server only (a build: with the egress proxy only; a task whose asset declares connections: also
  with the egress proxy and its own relay, which reach exactly those connections' endpoints, M6).
  It isolates projects, not hostile code: the kernel is shared.
- **The object storage boundary**, where credential scoping applies. This is where the
  query engine's authority is actually cut down to size — see "Bypassing policy by reading
  files directly" below.
- **The git repository boundary.** **PR review is the trust boundary for executed code,**
  not the worker runtime. Code merges to a linked repo only after review; once merged, Lakana
  runs it as trusted.

## Defended, with the control and the milestone that delivers it

A row whose Control cell says **real and shipped** is a status report; every other row is
still design, scheduled for the milestone in the right-hand column, and
should be read as a roadmap — see "Current posture" below for what has actually shipped.

| Threat | Control | Milestone |
|---|---|---|
| Reading tables one is not granted | Grants on hierarchical securables (environment → namespace → table, plus jobs, repos, secret scopes, connections, experiments), five role templates, and task-token scope narrowing — **real and shipped** (`lakana_core.grants`), enforced at every call site in `lakana-server` and `lakana-worker`. Grants, row filters, and column masks are bound to `environment_id`/`table_id`, not names, so a rename or a drop-and-recreate under the same name cannot evade one (a rename or replacement of a *column* is caught separately: policies bind to column names while Iceberg tables are read by field id, so a read is refused, and nothing is shown, when a column a policy uses was renamed or replaced since the policy was created); dropping a table, namespace, or environment soft-revokes every grant, row filter, and column mask attached to it, in the same transaction as the drop (`revoke_attached`). An `EXTERNAL` table's `location_pattern` must be shaped `raw/<dir…>/<basename-glob>`: the directory (the table's **claim**) is exclusive to one table per environment, compared component-wise using Unicode canonical caseless matching (NFD, casefold, NFD again) so case or Unicode-normalization variants of a directory name still collide, checked at create time under an environment row lock and re-checked at query time against the stored row; a pre-existing row was backfilled by migration `0011` where its stored pattern parses and left fail-closed (`NULL`, still compared for overlap) where it does not. Every new managed table's location is suffixed with its own id (`<name>-<table id hex>`), assigned by Lakana and never by a caller — a caller-supplied `location` on create is refused outright, `commit_table` refuses any `SetLocationUpdate`, and `write.data.path`/`write.object-storage.path`/`write.metadata.path` are refused outright as location-valued table properties. Before reading a single file, the worker refuses any `ICEBERG` table whose own location's final path component does not end with `-<table id hex>` (a legacy pre-M3f table fails closed and must be re-created — the "most specific owner" fallback below cannot see every case), refuses any manifest-list, manifest, or data-file URI that contains a glob metacharacter or is not already `posixpath.normpath`-canonical, and — for a table that does pass the id-suffix check — falls back to a "most specific owner" rule so a namespace nested inside another table's directory cannot claim that table's files as its own. The worker carries a task token's declared environment and asset scope into the `PrincipalRef` it authorizes every referenced table against, re-checking the token itself (missing, revoked, expired, or its principal deactivated) before running a queued query, so scope narrowing applies to the tables a query actually touches, not only to `SUBMIT_QUERY` at submission. **Residual gap:** the id-suffix and "most specific owner" checks only compare against other currently-existing `ICEBERG` rows, so a dropped table's orphaned data files, or an `EXTERNAL` table's `raw/` claim, sitting inside a namespace coincidentally named `<table>-<hex>` nested inside another table's own directory remain readable through that outer table; on local (`file://`) storage a symlink placed under an `EXTERNAL` table's `raw/` claim is followed by both create-time schema inference and the worker's own read | M3b/M3f |
| Reading masked columns or filtered rows | Row filters and column masks in a sqlglot-validated restricted expression subset, applied inside the synthetic per-table CTE the query rewriter already substitutes for each base-table reference — so every join, nested CTE, alias, and `SELECT *` above it reads masked, filtered rows rather than the file — **real and shipped** (`lakana_core.policy`, `lakana_core.query_rewrite`). Views (M5a) are expanded into their definitions before the rewriter runs, so their base tables' row filters and column masks apply and a view never widens access; reading one needs READ on the view and on every table beneath it. Iceberg tables are read by field id, and a commit through the REST catalog that adds a data file without field ids is refused (bounded to 2,000 files and 60 seconds), so a renamed or re-added column cannot make a file read under another column's name or leave a column unmasked; a column rename that could move a name onto another column refuses the read of every policy-bound reader until the policy is created again (`lakana_core.policy.renames`). Policies resolve per `(principal, table_id)`, not table *name*, closing the rename/recreate evasion the row above also describes. An `EXTERNAL` table goes through the same rewriter and its own row filters and column masks apply to it; its `location_pattern` is confined to an exclusive `raw/` claim (see the row above) rather than matching whatever the storage backend lists, so files outside that claim are never read under the external table's policies. A managed table's location is id-suffixed, assigned only by Lakana, and immutable after creation, and the worker refuses a query outright if any data file, manifest, or manifest-list path its current snapshot names does not lie within that location — closing the path where a foreign snapshot or a foreign data file committed under a table's own manifest could be read under its policies rather than the owning table's. A mask or filter's own restricted-subset expression is spliced directly into the generated CTE, and DuckDB's own binder/parser errors can echo the whole rewritten statement back verbatim under a `LINE n:` marker and a caret-marker line — **real and shipped**: `lakana_worker.executor` strips everything from the first such line before wrapping a DuckDB exception in `ValidationError`, so a query that fails inside a masked column's rewritten expression never leaks that expression through `error_message` | M3c/M3f |
| Bypassing policy by reading files directly | **Credential scoping** — DuckDB never holds a credential broader than the query's authorized scope, because the executor resolves table references through the catalog into the concrete file list a principal may read and hands DuckDB presigned URLs for exactly those, on a connection with every ambient `AWS_*` credential cleared, persistent secrets off, and DuckDB in allow-list mode: it opens only the locations of the tables the query reads (per query and per table, `Storage.read_url_prefix`), its private temp directory and the CA bundle, with its settings locked (`lock_configuration`, which does not cover secrets) and only one `SELECT` run per query process, checked by DuckDB's parser (so no `CREATE SECRET` can set a proxy or headers) — **real and shipped** (`lakana_worker.executor`, `lakana_worker.query_child`). AST blacklisting (`read_parquet`, `ATTACH`, `INSTALL`, `COPY … TO`) is defence in depth only, never the primary control. The resolved URLs are set as DuckDB variables the statement reads with `getvariable`, never written into its text, so they must stay unreachable from user SQL: a submitted query containing any placeholder (`?`, `$1`, `$name`) is refused (`query_rewrite._reject_placeholders`, in `check_blacklist` and inside `rewrite_query`'s own parse), a statement that is not a read is refused at any depth, CTEs, subqueries and `LATERAL` joins included (`query_rewrite._reject_statements_inside`), as are `getvariable` and `duckdb_variables`, and nothing is bound to the statement itself — **real and shipped**; before this, a placeholder bound to the rewriter's own list and returned the presigned URLs as a result value, bypassing row filters and column masks. The same guard blocks DuckDB's introspection functions: `current_query()` returned the *rewritten* statement as a result value — row-filter predicates, column-mask expressions and parameter names — and a write run could persist it; `current_setting`, `duckdb_settings`, `duckdb_secrets`, `duckdb_extensions`, `duckdb_logs`, the `pragma_*` functions and the `duckdb_views`/`duckdb_tables`/`duckdb_columns` catalog dumps are refused with it; so is `stats()`, which reads Parquet statistics at bind time below the row filter and so reported the min/max (a prefix, for strings) of hidden rows, in a submitted query and in a view's stored SQL at expansion. An `EXTERNAL` table's `location_pattern` is confined to an exclusive `raw/` directory claim (see "Reading tables one is not granted" above) rather than matching whatever the storage backend lists, so a crafted pattern can no longer read any file the environment's credential reaches. **Known gaps:** past the guard, a literal URL with a `..` segment reaches the same store host outside the prefix, and httpfs follows a redirect from the store to another host (both answered by what the store serves without a signature, or redirects to; `docs/agent/security.md`, "Allow-list mode"); in local `file://` mode there is no credential to scope, and a query can read and write inside the directories of the tables it reads; a symlink under a claimed `raw/` directory is still followed by schema inference at creation (the query process refuses one that points outside its tables) | M2/M3f |
| Credential theft | Hashed PATs (SHA-256, never stored in plaintext) and task tokens, compared via `hmac.compare_digest` — **real and shipped**, including expiry, revocation, and deactivated-principal checks on every verification. Envelope-encrypted, write-only secrets are **real and shipped** (`lakana_core.secrets`) — credentials can be encrypted at rest and referenced by name rather than stored as plaintext `storage_options`, though nothing yet requires an environment to use one. Tokens the worker mints for Python tasks (M5c2a) are bound to one task attempt, invalid the moment that attempt ends or is reclaimed, and revoked by the worker on every exit path. A task token's declared asset scope is enforced in the worker's query-execution path, not just at submission, and a task token may only ever perform the actions in `TASK_TOKEN_ACTIONS` (`READ_ENVIRONMENT`, `READ_NAMESPACE`, `READ_TABLE`, `SUBMIT_QUERY`, `CREATE_TABLE`, `WRITE_TABLE`, `DROP_TABLE`) — the three actions that work only with the `ADMIN` role (`MANAGE_GRANT`, `MANAGE_TOKEN`, `MANAGE_GROUP`), `MINT_TASK_TOKEN`, and every other namespace/environment mutation is refused outright regardless of the token's own principal's grants. `MINT_TASK_TOKEN` over REST is self-only for a principal (M5c2a): minting for another principal needs the `ADMIN` role at instance scope, and a refusal is audited (`AUTHORIZATION_DENIED`, `reason: not_self`); the other three need the `ADMIN` role for every use (see the row "A non-administrator who holds `MANAGE_GRANT`…" below). A token the worker mints for one Python task attempt is **bound** (M5c2a) and narrower still: no `DROP_TABLE`; `READ_TABLE` only on the asset's declared inputs (and the tables behind an input that is a view); `CREATE_TABLE` and `WRITE_TABLE` only on the asset's own table or artifact (M5c2b), whether or not the environment is protected (a token belongs to one environment); no table listing. It keeps `READ_ENVIRONMENT`, so it can read the deploys and runs of its environment. Its inputs are read from the project version manifest on every request, never from the token's `asset_scope` strings. Tables are compared as tuples of parts, and a manifest name that does not split into a valid namespace and table matches nothing. A namespace-kind securable must be covered by a declared asset-scope pattern to be navigated: `ns.*` covers `ns` and everything nested under it, a table-shaped pattern `ns.table` also covers its containing namespace `ns`, and a bare `ns` pattern is accepted and covers `ns` the same way. This coverage is not specific to `READ_NAMESPACE`: it applies to every `TASK_TOKEN_ACTIONS` member checked against a namespace-kind securable, including `READ_TABLE` — `CatalogService.list_tables` authorizes `READ_TABLE` at namespace scope, so either a bare `ns` or a table-shaped `ns.table` pattern covers that call, and the Iceberg REST route that lists tables returns every table *name* in `ns`; the platform's table listing (`GET .../tables`) then keeps only what the caller may read (tables, views, external tables and artifacts), so a task token sees there only the names its scope covers (it used to fail with a 403 that named the first object outside the scope); loading a table's actual data or schema is a separate, later table-kind check against the same patterns, satisfied only for a table the pattern names exactly (or a `ns.*` wildcard). Separately, `list_namespaces` authorizes `READ_NAMESPACE` at *environment* scope, which scope narrowing never restricts by pattern once the environment itself matches, so a token can list every namespace *name* in its declared environment regardless of asset scope. Neither listing lets a token load anything outside its declared scope. `TaskTokenService.mint` rejects a malformed or unusably-broad asset-scope pattern and caps `ttl_seconds`, operator-configurable via `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` — `TaskTokenService` is the single source of that cap; nothing at the HTTP schema layer imposes a separate, lower one. Deleting an environment revokes every unrevoked task token scoped to it by name in the same transaction as the drop (`revoke_task_tokens_for_environment`), so a token minted for a deleted environment cannot stay usable against one later re-created under the same name; a queued run submitted with a PAT whose principal has since been deactivated fails closed before any parsing too, the same way an invalidated task token's queued run already did. **Known gap:** the dotted-namespace-and-table matchers are ambiguous for a component containing a literal `.`. New names can no longer contain one, so only a namespace or table created before that rule can trigger it; those stay usable but are deprecated. The CLI (M4b, `lakana.cli`) keeps tokens out of shell history and process listings: there is no `--token` argument anywhere; `login` reads a hidden prompt or, for CI, `--token-stdin`, and every other command resolves a credential from `LAKANA_TOKEN` or the profile, never a flag. `secret create`/`rotate` take values the same way — `--values-stdin` (a JSON object on stdin) is the documented default, and the one flag-based alternative, `--value key=value`, is documented as history-visible and never echoes a malformed item's value back in its error, only its position. The profile file itself (`lakana.cli._profiles`, its only writer) is written atomically — a fresh `O_CREAT \| O_EXCL \| O_NOFOLLOW` temp file at mode 0600, fsynced, then `os.replace`d over the target, inside a 0700 `~/.config/lakana` — and a target the SDK's own reader would refuse (a symlink, or group/other permissions) is refused rather than rewritten or re-permissioned; the same refusal applies to a symlinked or group/other-accessible parent directory, and the directory is fsynced after the rename. `--url` can't carry a profile's token elsewhere: with profile credentials it must match the profile URL's origin (scheme, host, effective port) or the CLI exits 3 before building a client. `env create`/`set-storage` `--option` values, which can hold storage credentials, are likewise never echoed when malformed | M3a/M3d/M3f/M4b/M5c2a |
| Privilege escalation via a side channel | One API, one authorization path — no component has a privileged way around it; user code holds only a task token, never a broader credential | M3/M5 |
| Escalating through environment creation or re-rooting | `create_environment` and `update_environment_storage` authorize instance-scoped `MANAGE_ENVIRONMENT`, not environment-scoped, so a grant for a name that does not exist yet cannot be used to create an environment or repoint an existing one's storage root — **real and shipped** (`lakana_core.catalog.service`). `delete_environment` still accepts environment-scoped `MANAGE_ENVIRONMENT`. `Environment.is_protected`, when set, makes `delete_environment` and `update_environment_storage` raise outright; the only way to change it is `set_environment_protection` (`PATCH /api/v1/environments/{name}`, body `{"is_protected": bool}`), itself gated by instance-scoped `MANAGE_ENVIRONMENT`. All four operations record a matching audit event in the same transaction as the change | M3f |
| A revoked bootstrap credential silently restoring itself | `bootstrap_if_needed` acts only when `admin_exists` finds no active instance admin at all — **real and shipped** (`lakana_core.bootstrap`). Deliberately revoking the bootstrap principal's grant and PATs (the documented way to retire it once real admins exist) is therefore permanent across restarts, rather than being silently re-granted on the next start. When it does act, the whole recovery — reactivating the principal, revoking its stale PATs, re-granting instance ADMIN, minting one fresh PAT, and recording `BOOTSTRAP_ADMIN_RESTORED` — is one transaction, so a failure partway through never leaves a committed grant with no credential. A configured recovery `key_id` that already belongs to some PAT row (of any principal, revoked or not) is not reused and no other credential is invented: start-up fails with a message naming the cause. A randomly minted token is written to a 0600 file (`LAKANA_BOOTSTRAP_TOKEN_FILE`, created exclusively, never overwritten) and never logged — a log reader cannot learn an administrator's token (#208) | M3f |
| Audit evasion | Append-only, access-controlled audit log — **real and shipped** (`lakana_core.audit`). Grant changes, secret operations, environment lifecycle/storage/protection changes, and query completions are written in the same transaction as the action they record; authentication events and authorization denials commit their own row immediately, since the action they would otherwise ride alongside never happened. Every identity or policy mutation is covered: group creation/deletion/membership, PAT issue/revoke, task-token mint/revoke, principal creation/deactivation, grants, row filters, column masks, environment lifecycle/storage/protection changes, and bootstrap recovery. `ENVIRONMENT_CREATED`'s detail is `{"storage_root", "is_protected"}`; `ENVIRONMENT_STORAGE_UPDATED`'s is `{"options_keys", "secret_ref_set"}` — never option values or a secret's own value. Repeating a revoke or deactivation is a no-op, not a second event, for grants, secrets, PATs, task tokens, row filters, and column masks alike. `GroupService.delete_group` (previously the one mutation here with no refusal and no audit trail) now refuses with `ConflictError` while any active grant, row filter, or column mask still references the group — naming only the counts, never the rows — instead of hard-deleting it and letting `ON DELETE CASCADE` FKs remove those rows with no event at all; a permitted delete records `GROUP_MEMBER_REMOVED` for each cascaded membership and a new `GROUP_DELETED` event, and `create_group` records `GROUP_CREATED`, both in the same transaction as the change | M3e/M3f |
| Reading another principal's query results | Query-run visibility is per submitter (**real and shipped**). Another principal's run is a 404, indistinguishable from a missing one; admins audit query completions through `QUERY_COMPLETED` events rather than downloading result bytes. A task-token-authenticated caller is further scoped to runs submitted with that same token, so one token cannot read a run its principal submitted through a PAT or a different token. Cancelling follows the same rule, plus the manager's: a holder of `MANAGE_ENVIRONMENT` (never through a task token) may cancel anyone's run in that environment, each accepted cancel is audited as `QUERY_CANCELLED`, and a cancel reveals nothing about a run the caller could not read (the same 404); a refused cancel is audited as `AUTHORIZATION_DENIED` (`not_submitter`) alike for another principal's run and for a missing one | M3/M3f |
| Discovering environments one is not granted | Environment discovery is grant-filtered through a non-auditing `permits()` call (**real and shipped**). `CatalogService.list_environments` returns only environments where authorization would succeed; no grants yields an empty list, not a 403 | M3 (shipped) |
| Resource exhaustion via unbounded queries | Query execution memory and result size are bounded by `QueryLimitsConfig` (**real and shipped**). The executor enforces per-query `memory_limit`, `max_result_rows`, and `max_result_bytes`; queries exceeding these limits fail with a ValidationError naming the limit and advising aggregation or `LIMIT`, never truncate. A write run (M5a) streams its result to storage instead and is bounded by `max_write_rows` (`LAKANA_QUERY_LIMITS__MAX_WRITE_ROWS`, default 100 million) and the same `memory_limit`; exceeding it fails the run and deletes every file it wrote. Per-query wall-clock time, CPU threads and spill size are bounded too (**real and shipped**): the worker asks the query to stop after `LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS` (default 900; for a write run it covers the whole streaming phase), waits `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` (default 10) and then kills its process; the run fails with a time-limit message; `LAKANA_QUERY_LIMITS__THREADS` (default 4) caps the cores one query takes; each run spills into its own temp directory, removed afterwards, capped by `LAKANA_QUERY_LIMITS__MAX_TEMP_DIRECTORY_SIZE` (default 10GB). The time limit is hard since M5c3a: every query runs in a child process the worker kills. DuckDB's `memory_limit` stays best effort inside that process. On Linux the worker's own memory limit (`LAKANA_WORKER__MEMORY_LIMIT`) first ends the largest consumer, a query or a running Python task, at once (a query fails with the cause `out_of_memory`; a task's attempt fails as transient and is retried), and stops the whole worker only if it is still over the limit after that or nothing endable was running; on macOS the whole worker stops. Spilling works only in local `file://` mode (unchanged): elsewhere the query process gets `max_temp_directory_size = '0KB'`, so a query over `memory_limit` fails rather than spilling. The child holds no credential; it is a process boundary, not a sandbox: it runs as the worker's user | M3 (shipped) |
| Query rewrite alias collision or hijacking | CTE aliases are collision-resistant, with a fail-loud guard (**real and shipped**). Every synthetic CTE alias incorporates a SHA-256 hash of its dotted table name, and `rewrite_query` raises ValidationError if the same alias is ever claimed by two different dotted names (`_cte_alias` itself is a pure derivation; the guard lives in `rewrite_query`) | M3 (shipped) |
| Flooding the audit log with failed-authentication attempts | `AuthAuditLimiter` (`lakana_server.auth_rate_limit`) bounds `AUTHENTICATION_FAILED` writes to `LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE` per TCP peer address (`request.client.host`) — **real and shipped**. A client past its rate still gets `401`, but no row is written for that attempt; the count of attempts suppressed since the last recorded one is attached to the next row that is recorded (`detail.suppressed`), so the flood stays visible in aggregate. `key_id` is truncated to 64 characters before being recorded. **Known gaps:** see "Explicitly not defended" below for proxy-aware limiting and address rotation | M3f |
| SDK credentials at rest | The SDK (`lakana`) resolves a URL/token from explicit arguments, `LAKANA_URL`/`LAKANA_TOKEN`, or a profile file (`$XDG_CONFIG_HOME/lakana/config.toml`, default `~/.config/lakana/config.toml`) — **real and shipped** (M4a, `lakana._config`). The profile file is opened once with `O_NOFOLLOW` and its permission mode checked on that same file descriptor before it is read, closing the check-then-read TOCTOU window: a mode with any group/other bit set is refused (`ConfigError` naming the `chmod` to run), and the file itself being a symlink is refused via the `O_NOFOLLOW`-triggered `ELOOP`. **Known gap:** this guards the file itself, not a symlinked *parent* directory — placing the profile file's directory itself behind a symlink is not detected. An OS keyring integration is a deliberate non-goal; a single, permission-checked plaintext file is the whole credential-storage model for now. A token is never written to a log line, an exception message, or a `repr()` — `lakana._tokens.mask_token` masks everything but the key-id prefix, used by both `Client.__repr__` and `ResolvedConfig.__repr__` | M4a |
| Oversized or malformed input to the governed append path | `POST /api/v1/environments/{env}/namespaces/{path}/tables/{name}/data` (`lakana_server.api.platform.tables.append_table_data`) runs entirely in the API process and executes no SQL — it decodes an Arrow IPC stream and calls `CatalogService.append`, never a worker — **real and shipped** (M4a, hardened since). `CatalogService.check_can_append` (authorize `Action.WRITE_TABLE`, resolve the table, require `ICEBERG`) runs first, before `request.stream()` is ever read — refusing an unauthorized or nonexistent-table request costs nothing beyond a lookup, rather than the whole body having already been read and decoded first, as it did before round 1's fix. The wire body is then read incrementally against a running total, rejected the instant it crosses `Settings.append.max_bytes` (`LAKANA_APPEND__MAX_BYTES`, default 256 MiB) whether or not the client declared `Content-Length`. A wire-byte cap alone does not bound decoded memory, though: Arrow IPC body compression let a 2,288-byte `zstd` stream decode to 64 MB (~28,000:1) in testing, since `pyarrow` allocates each buffer at its declared *uncompressed* length before decompressing. `_decode_arrow_stream` (run via `run_in_threadpool`, never inline on the route's event loop, since decoding and full validation are CPU-bound and their cost scales with logical content, not wire size) closes this by scanning every message's FlatBuffers metadata for a declared compression codec before decoding any batch — refusing outright (422) rather than decode-then-check, since the bomb has already gone off by then. Once the stream's schema is available (before any batch is read), it is refused outright if it declares more than 1000 columns — the same bound `TableCreate` enforces at table-creation time — and otherwise every field's Arrow type is checked against a fixed allowlist (`int32`/`int64`/`float32`/`float64`/`bool`/`string`/`large_string`/`binary`/`large_binary`/`decimal128(p,s)`/`date32`/`time64("us")`/`timestamp("us")` with or without tz/`uuid`, mirroring what `lakana_core.catalog.append`'s own schema check can ever accept — `uuid` was once missing, refusing a legitimate uuid column every other layer already accepted) — refusing a type like `dictionary`, `list`, or `string_view` outright, since none of these can ever come from `to_iceberg_schema` anyway, and `string_view` specifically was shown able to make full validation touch several gibibytes of logical content from a single-digit-megabyte, cap-respecting wire body (many short views referencing one shared buffer). With compression and disallowed types both ruled out, batches are decoded one at a time (not `read_all()`'s single bulk decode); **each batch is fully validated (`batch.validate(full=True)`) before anything else reads it, including its own `.nbytes`** — calling `.nbytes` on an unvalidated, corrupted batch was reproduced *segfaulting the process outright*, not raising a Python exception (a crash reproduced with a dictionary-encoded column whose declared internal length vastly exceeded its actual buffer); only once a batch passes validation does its `nbytes` count toward a second, independent cap on cumulative decoded size (in practice bounded by the same order of magnitude as the wire-byte cap already enforced above, now that compression is ruled out — this second check is defense in depth, not a response to an otherwise-unbounded decoded size), and its `num_columns` counts toward a separate cumulative cap, `Settings.append.max_arrays` (`LAKANA_APPEND__MAX_ARRAYS`): each decoded batch costs roughly 1–3 KB of object overhead in decode and again in pyiceberg, independent of its byte size, so many tiny batches inside an otherwise byte-cap-respecting body were reproduced costing +1.46 GB RSS and 7s to decode (then +3.3 GB and 32s in the pyiceberg append) from a 256 MiB body holding 1.7M one-row batches — a cost the byte cap alone never bounds. Both cumulative checks stop decoding the rest of the stream as soon as either is exceeded. The residual this does not close, by design: per-array overhead up to whatever `max_arrays` is configured to allow. The assembled table is then fully validated once more: corrupted-but-structurally-plausible offsets can decode "successfully" under lighter default validation and only crash later (a bare `SystemError` from `.cast()`, reproduced) if not caught here first, and the decode step's own exception handling was widened to catch a bare `ValueError` alongside `pyarrow.lib.ArrowException`/`OSError` after `Table.validate(full=True)` was itself observed raising an uncaught `UnicodeDecodeError` on a corrupted string array. Once decoded and validated, the Arrow schema is checked column-by-column against the table's Iceberg schema, so a name, type, or nullability mismatch is refused by name before any write. Writing outside the table's own location is closed the same way every other write is: `core`'s `create_table`/`commit_table` assign and enforce the table's id-suffixed location, refuse a caller-supplied `location`, and the append's own data files are written under that location and committed through the same compare-and-swap `commit_table` path other writers use. A file written for a commit that then loses the compare-and-swap is deleted, best-effort. Every successful append records `AuditEventType.TABLE_APPENDED` in the same transaction as the commit, with detail `{rows, files, snapshot_id}` — never the appended values themselves. `TableCreate` and `ColumnIn` both set `extra="forbid"`, `TableCreate` bounds a column name to 255 characters, a type name to 64, and the column list to 1000 entries, and a caller-sent `location` (or any other unrecognised field, at either level) is refused (422) | M4a |
| Writing into a table one may not write, or reading a source one may not read, through a write run | A write run (M5a) is authorized at submission and again in the worker: `SUBMIT_QUERY`; `CREATE_TABLE` for `create` and a new view; `WRITE_TABLE` for `replace`, `append` and an existing view; `READ_TABLE` on the target too for an incremental append — **real and shipped** (`lakana_core.catalog.query_service`, `lakana_core.catalog.service`). Sources are resolved and rewritten exactly as for a query, so their grants, row filters and column masks apply and masked values are persisted masked. The write is one commit through `commit_table` (M3f's guards), under the target's own location; a failure deletes every file it wrote. An incremental append is refused while a policy on its target applies to the caller, and a zero-row batch never writes a file. A write whose rows depend on its target — an incremental append, or a query reading the target directly or through a view — commits only against the target snapshot it read, so a concurrent writer can be neither duplicated nor overwritten unread: a change in between fails the run with `ConflictError` ("… changed during the write; retry") and deletes its files. Arrow writes declaring `sources` must be able to read each one | M5a |
| Widening access through a view | Views use invoker's rights: `expand_views` replaces each view reference with its stored SQL before resolution, so the reader needs `READ_TABLE` on the view and every table beneath it, and those tables' row filters and masks apply — **real and shipped**. A view's SQL is re-checked against the AST guard at expansion, cycles, nesting beyond 10, more than 100 expanded references, unsupported constructs (including `LATERAL VIEW`) and placeholders in stored SQL are refused, and a stale view names a renamed table only to a reader who may read it — to anyone else it reads word for word like a dropped one. `table get` hides a view's SQL and references (returning `hidden_references`) from a caller who can't read every referenced table. Views are not exposed over Iceberg REST, including `DELETE`. A row filter or column mask can't be created on a view — it would never apply, since views expand before policies resolve — so no control silently does nothing. Lineage shows only nodes the caller can read, never walks through a hidden one, and stops at 500 nodes, setting `truncated` only when a readable node is left out. A dropped table's node is never authorized through a table that carries its name (a recreated table is a new table): only a reader of the namespace of the newest name recorded for it (the same from every starting table), or an administrator, sees it, and a table-level grant on the new table reveals none of its names, snapshot ids or run ids. A task token never sees a dropped node. **Residual:** a grant on an ancestor namespace or the environment covers a recreated namespace as before, so it shows the lineage of tables dropped from the old one (a grant on the exact namespace path is revoked with the namespace) | M5a |
| Deleting another table's data through maintenance | Maintenance needs `MAINTAIN_TABLE` (admin only; not a task-token action). It deletes only unreferenced files older than `older_than_hours` (default 72, minimum 1, maximum 87600) under the table's own `data/` and `metadata/` directories that the table owns under the "most specific owner" rule, never a metadata JSON file, skips orphan removal while a write into the table is running, never follows symlinks on `file://`, commits expiry only against the metadata it selected from (a concurrent change aborts the run with `conflict: true`), re-reads the metadata pointer immediately before deleting and skips orphan removal if it moved, and skips orphan removal if a referenced path lies outside the table location — **real and shipped** (`lakana_core.catalog.maintenance`). **Residual:** a cutoff shorter than the longest external write can delete that write's uncommitted files; a dropped table's files left inside a namespace nested under another table's `data/` directory are treated as that table's orphans | M5a |
| Server-side request forgery or internal probing through a repository URL | Only `MANAGE_REPOSITORY` (admin, instance scope) registers or changes a URL; deployers name a repository and never a URL (M5b, `lakana_core.projects.repositories`). Schemes are limited to `https`, `ssh` and scp-style; an `https` URL may carry no userinfo and an `ssh` URL no password (`lakana_core.projects.git_input.validate_repo_url`); hosts and paths may not start with `-`, and a remote URL may contain no `?` or `#` (where `urlsplit` and git disagree on the host); refs may not start with `-`. Private addresses are allowed — self-hosted GitLab is usually internal — so the trust boundary is the admin grant. A `local` repository must resolve, symlinks included, under `LAKANA_REPO_ROOTS` (empty by default: refused), checked at registration and again by the worker before each fetch. Audit events name the bound secret, never its value. | M5b |
| Server-side request forgery through a webhook URL (cloud metadata, loopback, internal hosts) | A webhook URL is a secret of the environment and hostile input: the worker's client (`lakana_worker.egress`, checks shared with the CI OIDC fetcher in `lakana_core.net.egress`) requires `https` on 443, resolves the name once, refuses it when **any** address is not plainly public (private, loopback, link-local, CGNAT, documentation, multicast, reserved, `fc00::/7`, deprecated site-local `fec0::/10`, and IPv6 forms that embed such an IPv4), connects to the checked address with the name as TLS server name, follows no redirect and reads no proxy or CA from the environment — **real and shipped**. A host on `LAKANA_WEBHOOK__ALLOW_HOSTS` may use `http`, any port and a private address, but never a loopback, link-local, unspecified, multicast, reserved or known metadata address (`is_never_reachable`, #243); only the entry `localhost` itself may reach loopback. **Residual:** an allowed name whose DNS someone else controls can still be pointed at any private address, and an operator proxy resolves the name itself and is trusted to refuse internal destinations | M5d-c, #243 |
| Credential leakage through a deploy | A repository's credential is a secret *name* resolved in the deploy's own environment scope by the worker (`lakana_worker.deploy.resolve_credential`); the value exists only in memory and in 0600 files (askpass 0700) under a 0700 directory removed in `finally`, never in argv, logs, audit details or `error_message`. git's stderr is redacted (token, its basic-auth form, key, URL userinfo); 401/403/404 collapse to one message. git runs with a minimal environment — no `LAKANA_*`, database, storage or KMS variable. Corporate proxies and internal CAs are explicit operator settings (`LAKANA_GIT__HTTPS_PROXY`, `NO_PROXY`, `CA_BUNDLE`), validated by shape (a username-only proxy URL is refused, and the askpass helper answers only the repository's own origin, so the repository token never reaches a proxy), passed to `https` fetches only as `http.proxy`, `no_proxy`, `http.sslCAInfo` and `http.proxySSLCAInfo` (the worker's own proxy variables are still not inherited), with proxy credentials redacted like the token and the URL held as a `SecretStr`; a CA bundle replaces trust and never disables verification, and the operator who sets a proxy trusts it to see which repository host is contacted (TLS is still end to end). | M5b |
| Malicious repository content | Local repositories are fetched into a fresh bare repository, never used in place; the commit is exported with `git archive`, so no checkout, hook or smudge filter runs. `git-upload-pack` does read the *source's* own config (command-scope settings never reach it), but runs no working-tree, fsmonitor or hook code and git refuses a repo-level `uploadpack.packObjectsHook`; canaries for `core.fsmonitor`, `core.hooksPath`, `uploadpack.packObjectsHook` and `core.alternateRefsCommand` never run. The source is trusted by path only: a private global git config marks exactly the resolved source (and its `.git`) as `safe.directory`, because the Compose `./repos` mount is owned by another uid; `LAKANA_REPO_ROOTS` therefore holds *trusted content* (a gitfile, `alternates` or `commondir` inside a root can point outside it). Symlinks under `assets/` or as `lakana.yaml` are refused by name, never followed; 1 MiB per SQL file, 2000 assets, 200 MiB checkout, 120 s per git command, 600 s per deploy; `RecursionError` in sqlglot is a validation error. YAML is loaded with a `SafeLoader` that refuses anchors and aliases (no alias bombs) within 64 KiB per document. | M5b |
| Discovering tables through a deploy | An external input the deployer cannot read reports "not found", exactly like a missing one; manifests withhold an asset's SQL, references and lineage unless the reader can read every referenced table (`hidden_references`), and drop unreadable external inputs. | M5b |
| Deploying to a protected environment from a laptop | `DEPLOY` is a per-environment grant; "only CI deploys to prod" means only the CI service principal holds it there — and since the `ADMIN` role holds every action but `READ_QUERY_TEXT`, it also means no human holds `ADMIN` on the instance or on `prod` (the bootstrap admin holds it on the instance). It is re-checked when the worker claims the run. **Scope:** per environment, not per repository — a principal with `DEPLOY` on `prod` can deploy any registered repository there. CI authenticates with a stored PAT, or (#15) exchanges its OIDC ID token for a CI token that the CI trust rule narrows to one repository, one kind and one signed commit; see "CI OIDC exchange (#15)" below. | M5b, #15 |
| Malicious or compromised task code | A Python task is trusted code from a reviewed repository. `LocalProcessRunner` is **not a sandbox**: the task runs as the worker's operating-system user, can read what the worker can read and can write the shared venv cache; the scrubbed environment and the private attempt directory are hygiene. **One instance is one trust domain**: everyone who may deploy Python code is trusted with everything the worker can read, so run one instance per client. On a local-runner instance `MANAGE_CONNECTION` (held by `DATA_ENGINEER`, who has no `DEPLOY`) also buys a probe from the worker's host: a connection test dials any address the connection names and reports `ok`, `refused`, `timeout` or the TLS outcome, and a `directory` or `sqlite-file` test shows which paths exist under a configured ingestion root and how many entries a directory has. No payload is sent beyond a connect, `SSLRequest` or a TLS ClientHello. Under the Docker runner a test runs under a task's own network rules (next PR). What limits a task is its token: bound to the attempt, `READ_TABLE` on declared inputs only, `CREATE_TABLE`/`WRITE_TABLE` on its own table or artifact only, no drop (and never the deletion of an artifact version), no listing, no administrative action; in a protected environment the lockdown holds it to its own asset. Deploy never executes repository code (`ast` discovery, TOML parsing). With `DockerRunner` (`LAKANA_RUN__RUNNER=docker`, the Compose bundle's default) each task runs in its own container: no worker credential, no other project's environment, a network shared with the server only (with connections: their endpoints too, through the egress proxy and a per-task relay, and nothing else; see "Task egress (M6): residuals"), runtime-enforced limits, nothing left after it ends (M7b); a protected environment refuses Python tasks on a worker without it. **Residual:** under `LocalProcessRunner` (local mode, or `LAKANA_RUN__RUNNER=local`) a task can poison the venv cache for other tasks that share its lock, and read the worker's credentials; under `DockerRunner` containers share the kernel (see "Task isolation (M7b): residuals") | M5c2a, M7b |
| Supply chain of a task's dependencies | Dependencies are user code and run with the task's token. The venv is built from the committed `uv.lock` (`uv sync --frozen`: hashes pinned by the lock) by a subprocess with a from-scratch environment, a time and size limit and no ambient proxy or credentials; local path and workspace dependencies are refused; its output is redacted. A package built from source runs its build backend and sees the index and proxy credentials of the build: under `LocalProcessRunner` as the worker's user with the worker's network access, under `DockerRunner` in a build container (uid 10002) whose only way out is the egress proxy, to the hosts its token names. Registered private indexes and their credentials: see the two M5c2c rows below | M5c2a |
| A project that sends a package index credential to another host | The worker defines the registered index itself in `UV_INDEX`, with the credential inside the URL, so uv binds it to that index's scheme, host and port: another host, or another port of the same host, never receives it, whatever the project's files say. The build runs `uv sync --frozen --no-config` with a fresh, empty `HOME`. **Read at build time:** the lock, the index definitions in the project's `[[tool.uv.index]]` tables, and, under `LocalProcessRunner`, the operator's proxy and CA settings (`LAKANA_RUN__HTTPS_PROXY`, refused at start under `DockerRunner`; `LAKANA_RUN__CA_BUNDLE`, whose certificates builds of both runners trust through `SSL_CERT_FILE`). **Not read:** the project's `[tool.uv]` settings, any `uv.toml`, a `.netrc`. Proved by unit tests against the real uv (`packages/lakana-worker/tests/test_index_credentials.py`: another host, a redirect, a reused index name, the proxy setting) and, for deploy and run, by `test_python_assets_integration.py`, whose `test_a_project_that_names_another_host_gives_that_host_no_credential` drives a build of a project that names another host and finds no `Authorization` header there. **Real and shipped.** | M5c2c |
| Dependency confusion for a registered index | `explicit = true` is required at deploy for a registered index declared in `pyproject.toml`, and a lock may name only the public index, `wheelhouse`, the operator's index and registered indexes. **Real and shipped.** | M5c2c |
| A non-administrator who holds `MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN` grants themselves access, joins an administrators' group, or issues a token for an administrator | The three actions work only for a holder of the `ADMIN` role on the instance (`lakana_core.grants.admin.authorize_admin_only`, which asks the authorization service for the action and then requires `holds_instance_admin`). A refusal is `AUTHORIZATION_DENIED` with `reason: not_instance_admin`, through the denial limiter. `GrantService.grant` refuses a single-action grant of the three at any scope; one that existed before stays, has no effect, is named in a WARNING at server start and marked in `lakana grant list`. A grant below instance scope never satisfied these checks. A task token never reaches the three (`TASK_TOKEN_ACTIONS`, pinned for an administrator's task token too), `MINT_TASK_TOKEN` stays self-only for a principal unless it holds `ADMIN`, and a principal's own tokens stay administrator-only until the sign-in design (#32). A test (`test_admin_only_call_sites.py`) enumerates the call sites, so a new one must go through the helper. **Real and shipped.** What stays outside the rule is under "Explicitly not defended" | M5c2c |
| Racing deploys or tampering between deploy and run | The environment pointer moves by compare-and-swap; the loser fails with a retryable conflict and writes no project version (pinned on Postgres). A project version is immutable and records `code_sha256` of the uploaded tarball, whose object name contains that digest, so a concurrent same-SHA deploy cannot overwrite it; the worker verifies it before a run's first task (M5c1) and again when it unpacks the code for a Python task (M5c2a). | M5b/M5c1/M5c2a |
| A human with `RUN` makes prod write as themselves | Protected runs write only as the deployment's bound service account; binding needs `MANAGE_ENVIRONMENT`; audited `RUN_AS_BOUND`/`RUN_AS_CLEARED`. **Real and shipped.** | M5c1 |
| Undeploy racing a run start leaves a run of an undeployed repository | Pointer delete + active-run check in one transaction vs a conditional pointer lock at start. **Real and shipped.** | M5c1 |
| A scheduler reclaim or cancel landing between the lockdown guard's check and the commit lets a superseded attempt write (into a protected environment, or over the attempt that replaced it anywhere) | The guard verifies the run context again right before the commit with the task run and run rows locked `FOR SHARE`; the reclaim either committed first (write refused, audited) or waits for the commit. **Real and shipped**, pinned on Postgres. | M5c1 |
| Deleting an environment racing a run start deadlocks (pointer→environment vs environment→pointer lock order) | `delete_environment` locks the environment row first and refuses (409) while any repository is deployed. **Real and shipped**, pinned on Postgres. | M5c1 |
| A human with write grants on prod writes outside the deployed code (SQL, SDK, Iceberg REST client) | Protected environments refuse every catalog mutation outside a running task run or an audited break-glass; enforced in core, not by grant hygiene. **Real and shipped.** | M5c1 |
| A request forges a run context | The context is set only by the worker from `query_runs.task_run_id`, or at authentication from a bound task token's database row (the claim form, M5c2a), never from a request, and re-verified against a running task run of the same environment and asset (the claim form: its current claim, and the own asset in every environment). **Real and shipped.** | M5c1 |
| A tampered code tarball in storage is executed | `code_sha256` verified before the first task of every run on every worker; a mismatch, a missing object or an escaping path fails the run, audited `CODE_INTEGRITY_FAILED`. **Real and shipped.** | M5c1 |
| Deploying a commit that was force-pushed away or never reached a branch (protocol v2 serves any object id) | Full-SHA deploys verify reachability from a branch or tag in a tree-less, blob-less probe fetch that cannot lazily fetch (`GIT_NO_LAZY_FETCH`); ref deploys resolve by exact match to a branch, a tag or the server's `HEAD` (never `refs/pull/*`, `refs/changes/*` and the like; the fetched object must equal the listed one), so code that never reached a branch or tag cannot be deployed, except where a server administrator points `HEAD` elsewhere. **Real and shipped.** | M5c1 |
| A hostile or huge repository fills the worker's disk during a fetch | Fetch size polled and bounded (1 GiB default); for full-SHA deploys the branch and tag count is bounded (10,000, pull and review refs not counted) before fetching. **Real and shipped.** Stdout of every git command but `git archive` (streamed to a file under the checkout limit) is read as it arrives and cut off at 16 MiB, so `ls-remote` output cannot grow worker memory. | M5c1 |
| A stalled or killed worker finishes a task the scheduler already gave to another | Renewals, outcomes and the scheduler's reclaim are conditional on the lease owner and a monotonic claim counter (`task_runs.claim_count`; the attempt number repeats after an uncounted retry); expired leases are reclaimed and counted as `lost`. In **every** environment, protected or not, a write carrying a run context is verified against the database at the guard and again right before its commit (`FOR SHARE` on the task run and run): a reclaimed, superseded or cancelled attempt commits nothing (in an unprotected environment refused as "this task attempt was superseded", audited `PROTECTED_WRITE_DENIED` with `protected: false`; a live attempt that writes past its own asset is told so instead and audited with `reason: other_asset`, or `reason: own_asset_operation` for an operation it may not do to its own asset). The final `QueryRun` status is written only while it is still `RUNNING`, so a reclaim's `FAILED` stamp is never overwritten. **Real and shipped**, pinned on Postgres. A SQL task whose write commits before the reclaim takes its locks (the reclaim waits for it), or whose worker dies between its commit and recording the attempt, is found by the reclaim through the evidence the commit left on the write run: the task ends `succeeded` and is not run again, unless its asset has data quality checks: such a task is taken again and only checks, because a table that was written and not checked must not count as done (M5d-a). **Residual, by design: at-least-once for a Python task**, whose own writes through the API leave no such evidence: a committed write whose worker is lost is run again, and `replace` and incremental watermarks make the rerun idempotent. | M5c1 |
| Failing data reaching readers, or being published by a side door, around a data quality check | A check is counted as an ordinary read of the run's principal (its row filters and column masks apply), and its result holds a count and a fixed error text, never a row of the table. A failed blocking check sets the table back to the state before the run through one operation (`CatalogService.roll_back`) that only the worker calls, under the claim form of the run context, and no route offers; it changes the table only if the table's current snapshot is still the one the run wrote, so another writer's commit is never discarded. A table whose first build failed is registered but not published, and every read of it is refused. No commit, whoever sends it and through whichever route, may remove a table's current snapshot or publish a not-published table by pointing at an existing snapshot. A written task that has not been checked is never counted as done by the sweep, the memory stop or a reclaim. **Real and shipped.** **Residual, by design:** readers see the failing data between the write and the set-back, for as long as the checks take (#98); a Python asset's set-back covers only its own writes, and a Python asset without checks, which records no baseline, keeps what it wrote when its worker is lost (a Python asset with checks is set back by the next claim, which a worker that is lost, that stops itself for memory or that sees a cancel after its write all lead to, and which the write guard lets pass a cancel request for that one rollback commit); a worker lost in every check phase is claimed again until `lakana runs cancel`. See `docs/agent/security.md`, "Checks and the rollback (M5d-a)" | M5d-a |
| Exposing a local-mode instance beyond this machine, or its on-disk secrets to another local user | `lakana server start --local` binds only to `127.0.0.1` — there is no flag to bind elsewhere — **real and shipped** (M4c, `lakana.cli.server`). The data directory is refused unless its leaf is not a symlink, is owned by the current user, and is accessible to neither group nor other (mode `& 0o077`); the parent is refused only if it is other-writable without the sticky bit, so an ordinary group-writable parent (a user-private-group umask) is accepted. `master.key` (the secrets master key passed as `LAKANA_SECRETS__MASTER_KEY`) is generated once at mode `0600`, written atomically (`O_CREAT \| O_EXCL \| O_NOFOLLOW` temp file, fsynced, `os.link`ed into place so a losing racer reads the winner's key rather than overwriting it), and read back through the same `O_NOFOLLOW`-open-then-`fstat`-check pattern the SDK's profile file uses, closing the check-then-read TOCTOU window. The one-time bootstrap credential (a freshly minted key id and secret) is passed to both children only through their shared process environment, readable only by processes running as this same user; the server logs just the key id when honouring a caller-supplied credential, never the secret, and every run after the first strips any inherited `LAKANA_BOOTSTRAP_PAT_KEY_ID`/`_SECRET` from the child environment before launching, so a stray value left over in the parent shell can never silently re-bootstrap a later run. `GET /healthz` is unauthenticated by design but reveals only process liveness (`{"status": "ok"}`), the same fact a 200-versus-connection-refused probe already would. Shutdown signals only the supervisor's own in-memory child handles, never a PID read back from a pidfile — a pidfile is only a hint (for `lakana server status`, and to refuse over a lock-less older-lakana instance whose PID is alive and answers `/healthz`); otherwise whether an instance runs is decided by an exclusive lock on `<data-dir>/lock`, held from before `migrate` for the supervisor's lifetime and released by the OS if it dies. **Residual risk, accepted:** the worker process receives the same first-run bootstrap credential as the server, though it never uses it, because in local mode one local user already administers both processes; there is no separate worker operator to keep it from. **Residual risk, unclosed:** the data-directory ownership/mode checks assume a user-owned parent path — a parent directory shared (group-writable) with other accounts on this user's group is accepted, and a TOCTOU window exists between checking that parent's mode and creating the leaf inside it, the same shape as the profile-directory gap described in "SDK credentials at rest" above; winning either already needs meaningful access to this user's account. | M4c |
| Compromise of the launcher | The `launcher` service is the only holder of the Docker socket: no network (`network_mode: none`), no capabilities, a read-only filesystem, the base interpreter (`python -I -S`); a worker authenticates with a token in a volume only the two share; one narrow request schema of five operations, every Docker field (image from an allow-list, user, capabilities, mounts, network) fixed in its code; standard library only (import-linter `launcher-stands-alone`); any change gets a separate security review. **Residual:** a code-execution bug in it is root on the host | M7b |
| Exposing a team (Compose) instance beyond its operator, or its secrets to anyone who can't already reach the host | `lakana bundle init` generates a Docker Compose bundle (`docs/site/how-to/deploy-with-compose.md`) — **real and shipped** (M4d, `lakana.cli.deploy`). `compose.yaml` publishes no host port for Postgres or SeaweedFS: only `server` has a `ports:` mapping (`${LAKANA_BIND}:${LAKANA_PORT}:8000`), so the catalog and object storage are reachable only from other containers on the Compose network. `--bind` defaults to `127.0.0.1`; a wildcard or a specific non-loopback address triggers a plain-HTTP warning on stderr, since the image terminates no TLS itself — an operator exposing it puts a reverse proxy in front and passes `--public-url`. `.env` (mode `0600`) is the deployment's whole root of trust — the envelope-encryption master key, the Postgres password, the SeaweedFS S3 keys, and the bootstrap admin credential all live there in plaintext, valid until explicitly rotated or revoked (see "Revoking the bootstrap credential" in `docs/site/how-to/deploy-with-compose.md`); a bundle already present at the target directory is refused (a dangling symlink counts as present) rather than risk regenerating the master key over one already in use, and a failed `init` run removes whatever it already created so a retry starts clean. The bundle mounts `./repos` read-only into the server and worker and sets `LAKANA_REPO_ROOTS` to it (M5b); nothing else on the host is reachable as a local repository. The image runs as uid 10001, never root, for the server, every worker, and the one-shot `setup` job. `setup` creates `dev`, its storage secret, and the binding between them using only the SDK against the running server's own REST API — no direct database or storage credential of its own — and retries while the server is still settling; it leaves a `dev` that already exists with a different storage root or a secret bound to a different name completely untouched, on the assumption that an operator, not this bootstrap, now owns it. The image stays private on GHCR until the public beta, so pulling it needs a `read:packages`-scoped login in the interim. **Worker shutdown (closed by M5c3b):** on `SIGTERM` the worker stops claiming and releases a running task run after `LAKANA_RUN__DRAIN_GRACE_SECONDS` (the bundle's `stop_grace_period` is 45 s); a task whose worker dies is reclaimed with its write run failed, and the ad-hoc queries and deploys it held are ended `FAILED` with the cause `worker_lost` after about a lease (a write that had already committed ends `SUCCEEDED`), so nothing stays `RUNNING` for ever | M4d |

## Protected-environment lockdown: residuals

An instance-level `MANAGE_ENVIRONMENT` holder can unprotect an environment, write, and
re-protect it. That is audited as `ENVIRONMENT_PROTECTION_CHANGED`, not `BREAK_GLASS_WRITE`.
`delete_environment` is not lockdown-gated either. Both are governance operations by design;
the audit log, not the guard, is the control.

## Artifacts (M5c2b)

An artifact is a file or a directory that a Python asset stores in numbered versions and another
asset reads. It is a row of the table registry with the type `ARTIFACT`, so grants, ownership,
orphans and lineage work as for tables, and its bytes are in the environment's storage under
`artifacts/<artifact id>/`. Agent-facing detail and the test of each control:
`docs/agent/security.md`, "The artifact store (M5c2b)". User-facing statement:
`docs/site/explanation/artifacts.md`.

**Assets.** The model files (the code they may carry, and the intellectual property in them), and the
training data that a model reveals. A model is derived from rows, and Lakana cannot see what it
contains.

**Entry points.** The three requests that add a version (begin, one request per file, commit) and
the request that withdraws an upload and the one that lists the caller's own open uploads; the download of a file; the requests that delete a version and
drop an artifact; the output directory of a running asset, where user code writes before the
harness uploads it; the copy of an artifact that a consumer's task or a person's download writes to
disk; and the sweep, which deletes bytes unattended.

### What is defended

| Threat | Control | Pinned by |
|---|---|---|
| A path that leaves its directory, or passes through a link: a `..` or a backslash in a manifest sent by a hand-made client, a link or device in the output directory, a link placed where a consumer's copy is written | One set of path rules, kept twice (core and SDK, parity-tested): relative, `/`-separated, no empty, `.` or `..` part, no control, format or surrogate character, no `:`, no name Windows reserves, at most 1024 bytes, 255 per part and 32 parts. The server applies them to every manifest; the SDK applies them to the local tree before a byte is sent and to the server's manifest before a byte is written. A storage path is built only from an artifact id and a validated hash, never from a manifest path. The scan walks by descriptor and refuses links, devices, hard links from outside and changes while reading; the consumer's copy is written into a staging directory without following a link and moved into place only when every file matches. A returned path is checked against the output directory by descriptor | `test_a_path_that_leaves_its_directory_is_refused`, `test_scan_refuses_a_symbolic_link_and_names_it`, `test_a_link_inside_the_tree_is_never_followed`, `test_a_returned_path_outside_the_output_directory_fails_permanently` |
| A manifest that collides after folding: `A/x` and `a/y`, or two Unicode forms of one name, which become one file on a case-insensitive file system | Every file and every directory prefix must be unique after `NFD(casefold(NFD(path)))`; a path may not be both a file and a directory. The consumer's copy is then the same tree on every file system | `test_paths_that_collide_after_folding_are_refused` |
| Deleting bytes that a live version still needs: two versions share a file, `keep_versions` prunes while an upload is open, the sweep runs between a file's upload and its commit | One lock per artifact. Rows change first and bytes go second, under the lock again and only while the file still has no row. A file counts as needed while a live version, an unexpired upload or a manifest object the catalog does not know names it. A damaged manifest stops every deletion for that artifact. The sweep removes a file that has no record only when a deleted version's manifest names it or it is older than seven days, and never while a manifest object the catalog does not know names it | `test_deleting_a_version_keeps_a_file_another_version_shares`, `test_an_open_upload_protects_its_files_from_deletion`, `test_the_sweep_never_removes_a_file_a_live_version_or_open_upload_needs`, `test_a_restored_database_does_not_cost_the_versions_it_forgot` |
| A stale or forged attempt commits a version | The guard runs at begin and at commit, and the commit re-reads the attempt `FOR SHARE` inside the locked transaction, so a reclaim or a cancel is seen. The worker counts an attempt as a success only if the database holds a version row with exactly this task run and claim, whether or not it was deleted since (an overlapping run's `keep_versions` can prune it between its commit and the check); the harness's result file is never believed | `test_a_superseded_attempt_cannot_commit_a_version`, `test_a_stale_attempt_cannot_commit_over_rest`, `test_a_version_of_another_attempt_does_not_make_this_attempt_succeed` |
| A task token that reaches beyond its declaration: reads an artifact it did not declare, adds a version to another asset, deletes, or writes a table when its asset produces an artifact | A bound token reads only the artifacts of its declared inputs and writes only its own asset. The guard holds a table task to tables and an artifact task to its artifact, from the manifest. No task token, bound or not, can delete a version or drop an artifact, but not every form has more than one layer. `ArtifactService._refuse_run` refuses every task token and every run context, and is the only layer that covers all of them. A bound token is also refused by its role (`BOUND_TASK_TOKEN_ACTIONS` has no `DROP_TABLE`) and, in every environment, by the guard. An unbound task token (minted over REST; `TASK_TOKEN_ACTIONS` contains `DROP_TABLE`, as for tables) and the worker's in-process query form are refused by `_refuse_run` alone in an unprotected environment, because the guard does not stop either there; in a protected environment the guard refuses them as well. `keep_versions` is read from the deployed manifest only | `test_a_bound_token_reads_only_declared_artifacts`, `test_a_task_token_of_any_form_never_deletes`, `test_runs_lockdown.py` (the output-kind refusals) |
| Learning that an artifact, a version or a file exists without the right to read it | Every operation authorizes on the name before it looks anything up, so a caller without the right gets one 403 with one text; the artifact listing and the table listing omit what the caller may not read instead of failing with a refusal that names it (the table listing asks the same read check per entry: tables, views, external tables and artifacts), and listing an artifact's versions reads no manifest; the worker asks the run principal's right before it looks at the catalog, and fails an unreadable artifact input with one text that does not depend on whether it exists | `test_an_unreadable_artifact_and_a_missing_one_are_refused_alike`, `test_a_caller_without_read_gets_the_same_answer_for_missing_and_existing`, `test_an_input_the_run_principal_may_not_read_answers_as_one_that_does_not_exist` |
| A file or a tree that exhausts memory, disk or connections | A manifest over `LAKANA_ARTIFACTS__MAX_FILE_BYTES`, `MAX_BYTES` or `MAX_FILES` is refused before a byte is sent and names the setting. A body longer than declared is cut off as it arrives. No component holds a whole file (pieces of at most 4 MiB on the server, 1 MiB in the SDK). At most 16 open uploads per artifact, 16 MiB for a manifest request and two manifest requests decoded at once per process (a third gets 503 before its body is read), the append limiter's slots and read timeout for files, 32 concurrent downloads and a minimum download speed of 64 KiB/s per process, a bounded sweep. A download that does not fit the disk is refused before its first byte | `test_an_exceeded_limit_names_the_setting_and_the_size_found`, `test_a_body_longer_than_declared_is_cut_off_and_nothing_is_stored`, `test_download_refuses_when_the_disk_is_too_small`, `test_the_seventeenth_open_upload_is_refused` |
| A recorded kind that is wrong: a name another repository claimed, a consumer deployed before its producer ran | The worker checks each input's kind against the catalog at run time in both directions and fails the attempt permanently with a text that names the next step (redeploy); a table input is never fetched as an artifact or the reverse | `test_an_input_recorded_as_an_artifact_that_is_a_table_fails_permanently` |
| The sweep deleting what a restore or a failover made the catalog forget | A file with no record in the catalog is removed only after seven days, or at once when the manifest of a deleted version names it, and never while a manifest object the catalog does not know names it. A directory without a catalog row goes only with the `.dropped` marker that a drop writes first. A manifest object the catalog does not know is never removed by the sweep; it is kept with the files it names and reported in the log ("After a restore" in `deploy-with-compose.md`). An unreadable manifest leaves the artifact whole | `test_a_rowless_prefix_without_a_marker_is_left_alone_counted_and_reported_once`, `test_unknown_manifest_objects_that_cannot_be_read_leave_the_artifact_whole` |
| An artifact asset whose token writes a table of its own name | Refused by the guard: a task that produces an artifact cannot write a table (`Task '<asset>' produces an artifact; it cannot write a table. …`), at the check and again under lock | `test_runs_lockdown.py` (`_ARTIFACT_REFUSAL`) |

### Residuals, stated as such

What an attacker with which rights can and cannot do, where a control does not apply. Each is
accepted for M5c2b.

- **What the answer to a delete tells, and the listing of one's own uploads.** The
  answer to deleting a version says how many files and bytes it freed and kept, and why. Two
  things in it are new and accepted. A caller with `DROP_TABLE` and without `READ_TABLE` learns that
  some other live version shares files with the deleted one (that another version exists, and with
  a file size, whether that file survives): the deleter already learned the version's size, and no
  version number is shown without `READ_TABLE`. Any deleter learns that someone has an open upload
  naming files of this version: the per-artifact cap of 16 uploads already tells a writer about
  other people's uploads, and the answer gives a count and bytes, never the upload, its principal
  or a path. The listing route returns only the caller's own open uploads (same principal, and for a
  task the same run and claim, filtered in the query), omits an artifact the caller may no longer
  write, reads no manifest and answers a bad cursor with a 422 that echoes nothing.

- **A reader of a model may infer its training data.** Anyone with `READ_TABLE` on an artifact, or on
  its namespace, can learn from the model about rows that they may not query. Row filters and column
  masks cannot be attached to a file, and a grant on a namespace covers its tables and its artifacts,
  so a model trained on a filtered or masked table is readable by everyone with read on its
  namespace. Lakana cannot lower this; the control is to grant read on a model as carefully as on its
  training table and to keep models in a namespace of their own.
- **A pickled artifact executes code in the task that loads it.** Anyone who can store a version
  decides what a consumer loads: a deployer of the producing repository, and a holder of
  `WRITE_TABLE` on the name (the role `DATA_SCIENTIST` has it; `lakana artifact upload` adds a
  version outside the pipeline). A consumer that is not part of a run of the producer reads that
  version as the current one. The code runs with the consumer's task token (its declared inputs and
  its own output) and, because `LocalProcessRunner` is not a sandbox, as the worker's operating-system
  user, which can read the worker's credentials. In a protected environment only a run, or a
  `MANAGE_ENVIRONMENT` holder with a break-glass reason, can store a version. Formats that carry no
  code (JSON, safetensors, ONNX) avoid it. With `DockerRunner` each task runs in a container of its
  own (M7b), so the code reaches the consumer's token and container, not the worker's credentials.
- **`BoundTask` has no input kinds.** A bound token can download an artifact at any declared input
  name, even one that the manifest declares as a table. The token may read that name anyway; nothing is
  read that was not declared.
- **One bit about kinds.** A deployer who names an object that another repository deploys
  but that the deployer may not read learns its kind, because the kind comes from the other
  repository's deploy. The existence of the name and the owning repository were already visible to
  that deployer through the ownership check. Conversely a repository with `DEPLOY` that claims a name
  nobody produces yet, or an existing table that no repository produces, sets the kind that other
  repositories record for it when their deployer cannot read it. The run-time re-check turns that
  into a clear permanent failure of the consumer until it is redeployed; it exposes no data.
  Squatting an unclaimed name existed before artifacts.
- **A grant revoked after the worker prepared a task**. The worker resolves the version
  of each artifact input as the run principal, before the task starts. If that principal's read
  right is revoked after this and before the harness downloads the version, the server answers 403
  and the task fails with `reading input 'x' (name, version N) failed: ...`. That text names a
  version number that the principal could read when it was resolved. It names no other version, no
  file and no content.
- **An operator can lose files by removing a manifest object by hand.** The sweep never removes a
  manifest object of a version the catalog does not know, and keeps its files. If an operator
  removes such an object, the next sweep may remove the files, and nothing detects a mistake.
  "After a restore" in `deploy-with-compose.md` says to remove one only when its digest equals the
  `manifest_sha256` of a version the catalog lists as not deleted, or when the operator is certain it
  belongs to a commit that failed.
- **A writer learns which files its own artifact already holds.** `missing` in the answer to a begin
  request, and the text of a size mismatch, tell a principal with `WRITE_TABLE` whether the artifact
  stores a file of a given hash. It reveals nothing about other artifacts (blobs are not shared
  between artifacts) and it needs write on this one.
- **`receive_blob` calls no guard**. A superseded attempt whose token is still accepted
  at that moment can store a file until its upload expires. The file can never become a version (the
  commit is guarded); the sweep removes it.
- **A download that fails halfway is still audited**, and `ARTIFACT_DOWNLOADED` is one row per
  principal and version within the upload lifetime, not one per file and not a count of bytes sent. A
  bound token writes none. The log therefore shows who fetched a version, not how often within the
  hour.
- **A holder of `WRITE_TABLE` can block uploads to the artifact and can fill storage.** The cap of 16
  open uploads is per artifact, not per principal, so a person who opens 16 uploads and leaves them
  keeps a task's `begin` refused until they expire (one hour by default, then again). There is no
  quota per artifact or environment: versions of up to `MAX_BYTES` each accumulate until a deletion
  or `keep_versions`.
- **A holder of `READ_TABLE` can occupy the download slots.** One reader of a large artifact can hold
  all 32 download slots of a server process for as long as its downloads run above the minimum speed;
  others get 503. No per-principal share of the download slots exists; #12 is about append slots and a minimum upload rate, not these.
- **A holder of the write right can hold table appends back.** A file upload (a blob `PUT`) holds one
  slot of the append limiter (default 4, shared with every table append, per server process) until
  it ends, and it ends only at its upload's expiry or when it sends no byte for the read timeout
  (300 s), so a client that sends one byte per read timeout keeps the slot for the upload's
  lifetime (one hour by default), and can open another upload to keep it. Downloads have a minimum
  rate (64 KiB/s); uploads have none. A table append then gets 503 and the SDK asks again. #12
  (per-principal append slots, a minimum upload rate) is the follow-up.
- **A slow writer can hold a manifest slot.** A request that opens an upload holds one of the two
  manifest decode slots of a process while its body arrives, for at most the read timeout; a
  principal holds at most one at a time, so one caller cannot lock the others out. A
  request that finds no slot, or its principal's own slot taken, gets 503 and the SDK asks again.
  Two tasks of one run share the run's principal and may retry for a second.
- **The sweep does not finish an artifact with more than about 200,000 distinct files.** Each run
  starts an artifact's pass over its file records from the beginning with a fixed share of the
  run's time, so such an artifact is never swept past its early records, however often the sweep
  runs. The summary line says so when a run stops at its time limit. Nothing is deleted wrongly;
  leftovers stay.
- **The worker's log is readable by whoever reads its standard error** (`docker compose
  logs worker`). Since the worker prints INFO lines, that includes the artifact sweep's lines, which
  name artifacts of every environment (and the number of versions and files), and DuckDB's own text
  for a refused spill file. It holds no SQL, no parameter value and no token.
- **The server's log is readable by whoever reads its standard error** (`docker compose logs server`).
  The server prints INFO lines of its own code since the process-log trace of a suppressed denial
  was made visible: the denial line names the principal, the action and the object, with the
  names escaped and cut at 200 characters, and holds no SQL, no body and no token. Uvicorn's access
  lines are there as before. `LAKANA_LOG__LEVEL` sets the level of both logs; above `INFO` the
  denial line is not printed, so the denials over the audit rate leave only the summary row in the
  audit table. At `DEBUG` libraries stay at `WARNING`, and Lakana has no `debug` call today; one added later must
  not log a token, a signature, a secret, a body or SQL text.
- **Whoever may deploy an asset can delete its history.** `keep_versions` prunes inside the commit of
  a run without `DROP_TABLE`, and nothing brings a version back. A run that stored a version and then
  died leaves it live, with the previous one already pruned when `keep_versions=1`. Consumers outside the run (another run, a person's download) read that failed attempt's version as the current one.
- **Interrupted multipart uploads on an object store** leave parts that a listing does not show
  . The control is the bucket's rule `AbortIncompleteMultipartUpload`, which the operator
  sets (see `deploy-with-compose.md`, "Storage for artifacts").
- **A delete request that timed out at the client can still reach the object store** after the
  artifact's lock is released, and so can a storage delete in flight when the server ends its
  database session. In that rare case a live version can lose a file; a download then fails with
  `A file of version <n> of <name> is missing from storage, and the catalog still lists it as stored,
  …`. Deleting that version and every other version that has the file repairs it (then run the
  asset again). After a restore to a moment before a delete, re-running the asset does not repair it
  by itself either: the catalog still says the file is stored.
- **No disk quota on the attempt directory** beyond what any task already can write. The trusted code
  of a deployer can fill the worker's disk with its output. A consumer's download checks the free
  space before it writes a byte.
- **The harness's working directory and `inputs` check are by name** (#67): a thread of the
  asset's own code can redirect the upload by calling `os.chdir`, or swap `inputs` between the check
  and the download. It adds nothing to what that code can already do with the task's rights.
- **An environment delete leaves the artifact bytes in storage**, as it leaves table data, and a drop
  that crashes leaves empty directories. Nothing deletes them but the operator. A backup holds a
  deleted version's bytes as long as the backup lives.
- **Two deployments on one storage root** would each treat the other's `artifacts/` as their own.
  Nothing detects it; the documentation says never to do it.

## Private package indexes (M5c2c): residuals

What the credential of a registered package index does not protect against. Each is accepted.

1. **Under `LocalProcessRunner` a task runs as the worker's user** and can read what the worker
   can read, including the process environment of a concurrent build that holds a credential.
   Through `/proc/<pid>/environ` of the uv process it can read that credential, and it can read
   the private wheels in the download cache and in cached environments. This is the existing
   residual of "one instance is one trust domain". Under `DockerRunner` a task container holds no
   credential and cannot see a build's processes or the download cache; a build's own
   dependencies still can see the credentials the build holds, except that a registered index's
   credential makes the build wheels only, so no package's build code runs next to it (the
   password in `LAKANA_RUN__INDEX_URL` does not).
2. **The worker's shared download cache serves a private wheel** to another project whose lock
   names its URL and hash, without asking the index again, when the index marks its files as
   fresh. Lakana checks the lock and that the secret exists; it does not ask the index who may
   read a package.
3. **The index's files must be on the index's own host and port.** A lock names indexes, not
   download hosts: a file served from another host is fetched without the credential. An index
   that serves its files from another host and asks for the credential there is not supported:
   the build fails (uv exits with status 1) and is retried, with no advice in the message.
4. **An author who locks a public package by mistake still ships it,** as with any dependency.
   `explicit = true` prevents that for the packages the author names.
5. **An administrator who registers an index trusts its packages** with whatever a task may do.
6. **Under `LocalProcessRunner` the key of a built environment has no environment in it.** An environment built with one
   environment's credential serves the same lock and `pyproject.toml` in another environment. The
   deploy check still requires the index's secret to exist in the environment it deploys to, and
   a cached environment reads no secret. Under `DockerRunner` the key holds the repository and the
   environment (`environment_owner`), so an environment is never shared between environments.

7. **A holder of `MANAGE_PACKAGE_INDEX` alone cannot redirect a secret to another host.** A secret
   is bound to one use (a package index, a repository, or its environment) and only
   `MANAGE_SECRET` binds it. The holder names only a secret already bound to that index and
   changes the URL of an index with a bound secret only with `MANAGE_SECRET` in its environment;
   each refusal to name a secret, bind one or move a URL is audited as `AUTHORIZATION_DENIED`. The
   residual is an unbound secret, which
   keeps serving its environment's storage and webhooks and is refused to an index until an
   administrator binds it. Changing `storage_options` (the endpoint and every other key) of an
   environment that has a storage secret also needs `MANAGE_SECRET` in it, besides
   `MANAGE_ENVIRONMENT`; the refusal is audited as `AUTHORIZATION_DENIED`
   (`storage_options_move_secret`).
   Under `LocalProcessRunner` a holder who also has `DEPLOY` and `RUN`
   still reads what the worker reads (item 1 and the trust domain): binding stops the
   redirection by an index manager, not code that runs in the worker. See "What the
   administrator rule does not cover (M5c2c)" under "Explicitly not defended".

What a registered index's URL may look like is stated once, in
[Package indexes](../site/reference/package-indexes.md): `https` (`http` only for loopback), no
user name, query or fragment, a plain host, path segments of letters, digits and `- . _ ~ + @`,
and none of six hosts of the public index; TestPyPI (`test.pypi.org`) only without a credential.
A form outside that is not supported and cannot be registered.

## Task isolation (M7b): residuals

With `LAKANA_RUN__RUNNER=docker` each Python task and each environment build runs in a container
that the `launcher` service starts (`lakana_worker.launcher`; the controls are in
`docs/agent/security.md`, "Task isolation: the launcher" and "DockerRunner: the launcher client
and environments"; the operator's view in
[Run Python tasks in containers](../site/how-to/run-python-tasks-in-containers.md)). What it does
not protect against. Each is accepted.

1. **Containers share the host's kernel.** A kernel or runtime escape is out of scope: this
   isolates projects from each other and from the worker, not hostile code from the host.
2. **The launcher holds the Docker socket.** A code-execution bug in it is root on the host (the
   row "Compromise of the launcher" above).
3. **A compromised worker** holds the launcher's token. It can start allowed task images with its
   own code as uid 10001, mount any environment volume read-only by its key, build into an
   environment that is not complete, and stop another worker's run only if it learns that run's
   id: 128 random bits each worker makes per task and build and never stores or logs. The
   launcher's owner check (#305) does not add a secret, because in the bundle every worker reads
   the others' owner names from the shared `worker-cache` volume; it stops a request that
   repeats a live id from ending the first run. It cannot choose an image, a host path, a
   capability or a network. It also holds everything a worker holds anyway.
4. **Task images are named by tag** and pulled once when absent: a tag repointed in the registry
   before the first pull is what runs. Releases attest the images; the launcher does not verify
   an attestation.
5. **A task's network is the server's too.** A task reaches every port the `server` container
   listens on, and the REST API with its own token, as under `LocalProcessRunner`. A task with
   connections also reaches their endpoints (see "Task egress (M6): residuals").
6. **`no-new-privileges` is off on Docker installed as a snap**, which refuses it; the launcher
   detects that at start (`LAKANA_LAUNCHER__NO_NEW_PRIVILEGES=on` refuses to start instead).
7. **Build-time code of a dependency** (a source distribution, built only when no registered
   credential is in use) sees the build's `LAKANA_RUN__INDEX_URL` password and its egress token,
   and can reach the hosts the token names. It can write the build's private uv cache and its
   own environment, never a task's or another project's.
8. **While the launcher is down,** Python tasks fail as "runner unavailable" and are retried;
   containers it had started keep running until it starts again and removes everything with its
   stack label. Their task tokens are revoked by the worker when its call to the launcher fails.
9. **Disk.** A task's `/work`, the environment volumes and the build caches have no quota
   (Docker volumes have none on most storage drivers); an environment over
   `LAKANA_RUN__VENV_MAX_BYTES` is refused after the build. The launcher keeps 10 environments
   and removes caches idle for 14 days. Memory is bounded per container and in total
   (`LAKANA_LAUNCHER__MAX_TOTAL_MEMORY_MB`), networks by three quarters of the range's /29s.
10. **The address range.** Task and build networks take /29s from `10.207.0.0/16` unless
    `LAKANA_LAUNCHER__NETWORK_RANGE` says otherwise; a range that overlaps a route of the host's
    network shadows that route for the host. The how-to tells the operator to choose a free one.
11. **Egress.** The proxy and the relays refuse loopback, link-local and metadata addresses, the
    networks they are attached to, the launcher's task range, the gateway of every Docker
    network (the Docker host's addresses there, such as `172.17.0.1`, listed again at every
    network open), the Docker host's well-known names (`host.docker.internal` and the like,
    whatever they resolve to) and the ranges of `LAKANA_LAUNCHER__EGRESS_REFUSED` (an
    IPv4-mapped entry is read as its IPv4 form); without the launcher's list of those the proxy
    connects to nothing. A direct connection may still reach other private addresses, because a
    company's index often has one, and the Docker host's other addresses (LAN, VPN, `virbr0`,
    Docker Desktop's `192.168.65.0/24`) are refused only when the operator lists them there (the
    how-to says to, and to bind host services to loopback). With `LAKANA_EGRESS__UPSTREAM_PROXY`
    the local check covers numeric hosts, and for tasks and connection tests the addresses a
    name resolves to here; the company proxy resolves the name again and decides, so a
    split-horizon or rebinding name can pass, and the token still limits the host:port pairs. A
    tunnel whose writes block can outlive its token's expiry by up to the 300-second idle
    timeout. One build holds at most `LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK` (32) of the
    proxy's `LAKANA_EGRESS__MAX_CONNECTIONS` (256), so it takes at most its share; enough builds
    at their cap together (eight with the defaults) still fill the proxy. With
    `LAKANA_RUN__CA_BUNDLE` builds and tasks trust a private authority in addition to Mozilla's
    roots (#292): an authority the operator adds can sign a certificate for any host a token
    names, so the bundle is as trusted as the operator's file. Only its certificate blocks reach
    the read-only `/trust` mount, and a file that also holds a private key stops the worker,
    because every task can read the bundle.
12. **Protection is read once per attempt.** A protected environment refuses a Python task on a
    worker without containers when the attempt is prepared; an attempt already past that point
    when the environment is protected runs to its end without containers.
13. **The local runner and local mode keep one trust domain** (row "Malicious or compromised
    task code"), and it includes everyone who holds `MANAGE_CONNECTION` there: buys a probe from the worker's host: a connection test dials any address the connection names and reports `ok`, `refused`, `timeout` or the TLS outcome, and a `directory` or `sqlite-file` test shows which paths exist under a configured ingestion root and how many entries a directory has. No payload is sent beyond a connect, `SSLRequest` or a TLS ClientHello. Under the Docker runner a test runs under a task's own network rules (next PR).

## Task egress (M6): residuals

Under `DockerRunner` a task whose asset declares connections reaches exactly their endpoints:
HTTPS through the egress proxy with a token of purpose `task` (bound to the task's /29, its
host:port pairs and its lifetime plus a minute), databases and `tcp` through a relay container of
its own (one port per endpoint, forwarded to that endpoint only), directories as read-only mounts
of the operator's ingestion roots. The worker computes the routes from the deployed manifest,
never from request input. A task without connections keeps the M7b posture. The controls are in
`docs/agent/security.md`, "Task egress: connections, the proxy and the relay (M6)" and "The relay
(M6)"; the operator's view in
[Run Python tasks in containers](../site/how-to/run-python-tasks-in-containers.md#connections-what-a-task-reaches).

**Defended:** another task (a token copied into another task's container is refused; each relay
serves its own /29 only, and the proxy and every relay refuse the launcher's whole task range);
the Lakana stack (every network the proxy or a relay is attached to, so the Compose network with
Postgres and SeaweedFS's port 18333); loopback in every spelling, link-local and the metadata
address; the gateway of every Docker network, listed at every network open, and the Docker host's
well-known names; the ranges of `LAKANA_EGRESS_REFUSED`; any host:port the asset does not declare
(403 naming what to declare). A task with routes refuses to start without the `egress` service.
Proved on a real engine by `test_docker_task_egress_integration.py`, the attached-network rule on
its own by a service beside the source on the egress service's network that no refused range
holds.

What it does not protect against. Each is accepted.

1. **The shared kernel**, as for every task container.
2. **The token is in the task's environment** (`LAKANA_EGRESS_PROXY`). Any code of the task can
   use it for the declared pairs while the run lasts; it is useless in another container and
   after the run.
3. **A declared host receives what the task sends.** The task can send the declared
   connections' credentials, and anything it reads, to any endpoint of those connections. The
   asset's `connections` are reviewed with its code (PR review is the trust boundary).
4. **The Docker host's other addresses** (LAN, VPN or Tailscale, `virbr0`, Docker Desktop's
   `192.168.65.0/24`) are reachable when a connection names them, unless the operator lists them
   in `LAKANA_EGRESS_REFUSED`; the docs say to, and to bind host services to loopback. Docker's
   gateways and the host's names are refused without it. A network made after the last listing is
   refused from the next network open on.
5. **`tcp` traffic is encrypted only if the team's driver encrypts it.** The relay never reads
   the bytes; TLS, where the protocol has it, stays end to end.
6. **A company proxy decides after the local check.** With `LAKANA_EGRESS__UPSTREAM_PROXY` the
   proxy and the relays refuse numeric hosts and names that resolve here to a refused address;
   the company proxy resolves the name again (residual 11 of "Task isolation (M7b)").
7. **A task can exhaust its own relay** (64 MiB, 16 connections per route) and its share of the
   proxy (`LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK`), never another task's relay.
8. **An ingestion root is mounted whole.** A task whose `directory` or `sqlite-file` connection
   names a root reads every path under it, other connections' paths included. The how-to tells
   operators to give each group of data that some tasks must not read a root of its own.

## CI OIDC exchange (#15)

A CI job exchanges its provider's OIDC ID token (`POST /api/v1/auth/oidc/token`, no credential)
for a CI token (`lakana_ci_…`). The details and the code are in `docs/agent/security.md`, "CI
OIDC exchange and CI tokens (#15)"; the user's view is
[CI trust rules and CI tokens](../site/reference/ci-trust-rules.md).

### What is defended

- **Forged or borrowed ID tokens.** The verifier (#15, part 1) pins the issuer, the algorithm and
  the audience; anyone can obtain an ID token of GitHub or GitLab for any audience, so the rule's
  conditions decide. Every rule pins the numeric repository or project id, which a rename, a
  transfer or a look-alike name cannot take over, and a ref or environment, and every condition is
  an exact match of a text (no pattern, no case folding, a number never matches a text).
- **Replay.** Each `jti` is accepted once, by a primary key, until the ID token expires; it is
  used up even when the exchange is then refused, so one ID token probes one environment and
  repository at most.
- **Ambiguity.** Two enabled matching rules refuse the exchange; an administrator decides with
  `lakana ci-trust explain`.
- **Enumeration.** Every refusal after verification gives one answer, so a caller does not learn
  whether a rule, an environment or a repository exists; the reason goes to a rate-limited audit
  row (`OIDC_EXCHANGE_REFUSED`, its own per-client bucket), which records text from the request
  only once the ID token verified.
- **A stolen CI token.** It lives at most `LAKANA_CI_OIDC__TOKEN_TTL_SECONDS` (one hour, at most six)
  and may only `DEPLOY` on its environment (the authorizer refuses every other action, whatever the
  service account's grants), and its service account still needs the `DEPLOY` grant. At the
  `DEPLOY` checks it may queue one run (a conditional `UPDATE` in the queuing transaction), of its
  repository, of a kind its rule allows, never a rollback; a deploy only of the signed `sha`, which
  the worker checks again after the fetch. Afterwards it reads only its own run. Undeploy, job
  pause, resume and test, and delivery reads refuse it. A test enumerates every `DEPLOY` check.
- **Revocation.** Disabling, changing or deleting the rule, deactivating the service account or
  deleting the environment ends the token at its next request (each change raises the rule's
  revision); the worker re-checks a queued run's token, rule and account at claim time.

### Residuals, stated as such

1. **The rule is the trust decision.** Whoever can push to the pinned ref (or run a workflow in
   the pinned environment) can deploy. A ref alone does not say how a run was started (a GitHub
   `pull_request_target` run gets an ID token for the base branch; on GitLab a `ref` pin also
   matches the branch while it is unprotected and pipelines of every source, such as schedules,
   triggers, the web and the API; a fork's merge request pipeline carries the fork's
   `project_id`, which the id pin refuses), so every rule must also pin
   the trigger: `event_name` or `environment` on GitHub, `ref_protected`, `pipeline_source` or
   `environment` on GitLab (decided 2026-10-20). What remains is the choice of those values: a
   rule that pins `event_name=pull_request_target` accepts such runs.
2. **A dry run is not pinned to the commit** (design decision Q3): a CI token for a rule that
   allows `dry_run` can dry-run any branch or tag of its repository and read the diff.
3. **The Lakana repository's URL is not compared with the ID token.** The rule binds the
   provider's repository id to a registered Lakana repository; a holder of `MANAGE_REPOSITORY` who
   changes that repository's URL changes what a CI token's commit is fetched from (the commit is
   still the signed `sha`).
4. **A deploy queued before the token expired still runs**; the worker refuses it only when the
   rule or the account changed. A downgrade below this release runs a queued CI deploy as an
   ordinary deploy of its service account.
5. **Restoring a deleted environment** brings back its rules, and an unexpired token issued
   before the deletion works again.
6. **The audit row's per-client bucket trusts `request.client.host`**, as `AUTHENTICATION_FAILED`
   does (see "Explicitly not defended"); behind a proxy all clients share one bucket.

## Explicitly not defended

Listing these is not a formality — each is excluded for a specific reason, and pretending
otherwise would misrepresent what the platform does.

- **Hostile code in a trusted repository.** Lakana executes Python from linked git
  repositories as trusted code. The trust boundary is PR review, not the runtime — Lakana is
  not a sandbox for hostile code. Building runtime containment against code that already
  passed review would buy security theatre at real usability cost, for a threat the review
  process already exists to catch.
  Python tasks run as the worker's user (`LocalProcessRunner`), so one instance is one trust
  domain: everyone who may deploy Python code is trusted with everything the worker can read.
  With `DockerRunner` each task runs in a container of its own (M7b); containers share the
  kernel, so that isolates projects, not hostile code.
- **Inference from a model.** An artifact is all or nothing: nothing applies a row filter or a
  column mask to a file, and a model can reveal what it was trained on. See "Artifacts (M5c2b)".
- **Code in a pickled artifact.** Loading a model saved with `pickle` or `joblib` runs what the file
  contains; Lakana stores the bytes and never reads them. See "Artifacts (M5c2b)".
- **Statistical inference from aggregates.** Row filters and column masks stop someone
  from directly reading a value they are not authorized to see. They do not stop someone
  from reconstructing that value by cross-referencing enough aggregate queries — that is a
  different problem (query auditing, differential privacy, aggregation thresholds) that
  this design does not attempt to solve.
- **A compromised platform administrator.** An administrator grants access, manages
  secrets, and reads the audit log by design; the model has no defense against that trust
  being abused, only against everyone else exceeding their grant. Holding the `ADMIN` role does
  not show other people's query text, but an administrator can grant `READ_QUERY_TEXT` to
  themselves: the control is that the grant is audited (`GRANT_CREATED`), not that it is
  impossible.
- **Infrastructure compromise** — the host, the database, or object storage itself. If an
  attacker has root on the host or direct database/storage access, the application-level
  controls above (grants, masks, credential scoping) are moot; they operate above that
  layer, not against it.
  **Stored secrets are defence in depth here, not a boundary**: format 2 of a stored
  secret binds its ciphertext and its wrapped data key to the row (id, environment id, name
  and key id), so copying one secret's encrypted triple onto another row makes decryption fail
  instead of resolving a credential that was never issued for that row. Format 1 (written before
  this existed) binds nothing, and `format_version` is a column of the row, so the same person
  could paste an old-format triple onto any row and set it to 1. Lakana therefore **refuses format
  1 by default**; `LAKANA_SECRETS__ACCEPT_FORMAT_1`, process configuration outside the database,
  is the only way to read it. **While that setting is on, any old-format ciphertext written under
  the same master key is accepted on any row**, whatever the row's `format_version` and
  `key_id` say; the server and the worker log a WARNING at start-up to say so, and the way out is
  to set each secret again and remove the setting. Someone with database write access can still
  delete or replace rows, change which secret an environment references, and, if they also hold
  the master key, read every secret. **Residual, accepted:** nothing counts rotations, so an older
  row of the same secret, restored by someone with database write access, still decrypts (a
  counter stored in the same row would be replayed with it). `lakana secret list` shows each
  secret's `format_version`.
- **A symlink planted inside a local-filesystem storage root that points outside it.**
  `FsspecStorage` (see "path confinement" below) confines the root-relative path string it
  is given, but does not resolve symlinks before handing the resolved path to the
  filesystem, so a symlink placed inside the root that points elsewhere on disk is
  followed. Closing this with a `realpath` containment check would add a stat on every
  call and a TOCTOU window between checking and using the resolved path, and the fix does
  not generalise: object-store backends such as S3 and ABFS have no symlinks and are
  unaffected by this limitation, and they are the production backends this control exists
  for. Planting a symlink inside a storage root already requires filesystem write access
  to that root, at which point the attacker holds more access than this control protects
  against. `ResolvedStorage.confine_to_root` (see "path confinement" below) has the
  identical gap and for the identical reason: it is a lexical check — string comparison
  after `posixpath.normpath` — with no filesystem access of its own, so it does not and
  cannot detect that a path lying inside the root resolves, via a symlink, to somewhere
  that does not.
- **Per-principal query limits.** M3 bounds queries globally. Deferred: per-principal limits
  that prevent one principal from starving others, or enforce different budget tiers.
- **Result sharing between principals.** Shared access to a query result across multiple
  principals. Deferred.
- **Proxy-aware failed-authentication rate limiting.** `AuthAuditLimiter`
  (`lakana_server.auth_rate_limit`) bounds `AUTHENTICATION_FAILED` audit writes per
  client key, taken from `request.client.host` — the TCP peer address ASGI hands the
  application. `X-Forwarded-For` and similar proxy-supplied headers are deliberately not
  trusted, since any client can set them to an arbitrary value. Deployed behind a reverse
  proxy or load balancer, every request's `client.host` is the proxy's own address, so the
  limiter bounds writes per proxy, not per original client — an attacker behind a shared
  proxy is rate-limited together with every other client behind it, and one that controls
  many source addresses in front of the proxy is not distinguished at all. An attacker who
  can rotate the source address itself (for example, a residential or cloud IPv6 allocation
  large enough to present a fresh `/64` per request) gets a fresh bucket each time instead of
  being throttled, since nothing here recognises the addresses as related. Configuring the
  deployment's proxy to forward real client addresses in a way the application can trust
  (and validating that trust), and recognising related addresses as one client for the
  purpose of this limiter, are both deferred.
- **What the administrator rule does not cover (M5c2c).** The three actions
  (`MANAGE_GRANT`, `MANAGE_GROUP`, `MANAGE_TOKEN`) need the `ADMIN` role on the instance; other
  permissions can still do harm of a lower kind, and each of these is accepted:
  - A holder of `MANAGE_POLICY` can remove a row filter or a column mask that applies to itself
    and then reads the rows or values. It gains no other access.
  - A holder of `MANAGE_PRINCIPAL` can deactivate every administrator but the last, and nothing
    reactivates a principal (a restart restores only the bootstrap administrator when none is
    left). It gains no access: a new principal inherits nothing and has no token.
  - A holder of `MANAGE_ENVIRONMENT` who also holds `DEPLOY` and `RUN` can bind any active
    service account as the run principal of an environment, an administrator's included. The
    run's token is confined to that one environment and never reaches the three actions, but
    the code reads and writes that account's tables there. `MANAGE_REPOSITORY` with `DEPLOY` and
    `RUN` can reach the same through a bound repository.
  - A holder of `MANAGE_PACKAGE_INDEX`, which can be granted as a single action, registers an
    index at any URL but cannot send a secret to it: a secret is bound to one use, only
    `MANAGE_SECRET` binds it, and the holder names only a secret already bound to that index.
    Moving the URL of an index with a bound secret needs `MANAGE_SECRET` in that secret's
    environment. `MANAGE_REPOSITORY` follows the same rules for a repository. Every refusal is
    audited as `AUTHORIZATION_DENIED` (every refusal to name a secret, bind one or move a URL).
    Under `LocalProcessRunner` a holder who also has `DEPLOY` and `RUN` still reads what the
    worker reads, so binding does not stop code that runs in the worker. Residual: an unbound
    secret keeps serving its environment's storage and webhooks. Without `DEPLOY`, a holder can stop
    the projects that
    use an index (a changed URL, a removed index) and nothing more.
  - The rule is not retroactive: a grant, a group membership or a token that a holder of one of
    the three actions created before the upgrade stays. The audit log lists them
    (`GRANT_CREATED`, `GROUP_MEMBER_ADDED` and `PAT_ISSUED` events by that principal).
  - Task tokens never reached the three actions, and still cannot (`TASK_TOKEN_ACTIONS`).
- **Checking a table's data files at commit time.** `commit_table`'s `AddSnapshotUpdate`
  guard inspects only the new snapshot's `manifest_list` pointer, a shallow, single-path
  check — it does not open that manifest list or the manifests it leads to, and it never
  runs at all for a row mutated outside `commit_table` (a direct database write, or a row
  that predates this guard). It is defence in depth, not the control this design relies on.
  **The worker's own read-time check is the control:** before running a query,
  `lakana_worker.pipeline._resolve_sources` walks the table's current manifest-list path,
  every manifest path it names, and every data-file path those manifests name, and refuses
  the query outright if any of them lies outside the table's own location — see "Reading
  tables one is not granted" above. A row could still carry a manifest pointing outside its
  location between the moment it is committed and the moment it is next queried; nothing
  scans committed rows proactively for this today.
- **An external Iceberg REST client holding its own storage credentials.** Lakana's Iceberg
  REST endpoints vend no storage credentials of their own —
  `lakana_server.api.iceberg.router._to_table_response` always sets `config={}` and
  `storage_credentials=[]` — so a client that already holds a credential to the underlying
  storage by some other means reads raw files directly, with no row filters, column masks,
  or per-query grant check: every control in the "Defended" table above applies only to
  queries run through Lakana's own worker. Governed credential vending is deferred (issue
  #5).
- **SeaweedFS's internal HTTP APIs inside the Compose team bundle (#14) — closed by network
  isolation.** The worker runs user SQL with `httpfs` and external access enabled, and
  SeaweedFS's master (9333), volume server (8080) and filer HTTP APIs need no credentials.
  `compose.yaml` runs them in `seaweedfs-core` (also `-disableHttp` on the filer), on an
  `internal: true` network only SeaweedFS's containers share; the S3 gateway `seaweedfs` is the
  one service on both that network and the one the server and workers use, and it listens on
  8333 and its gRPC port 18333 (its other HTTP ports are off). A worker's HTTP can reach SeaweedFS only through S3 with the keys. A test asserts the
  network layout and the CI smoke test checks it from inside the worker. **Residual:** the gateway's gRPC port 18333 (it includes IAM) cannot be turned off and has no authentication. It is reachable from the default network, so from code in the worker: user SQL cannot reach it, because DuckDB's HTTP filesystem cannot speak gRPC, but a Python task under `LocalProcessRunner`, which runs as trusted code in the worker, could remove the storage identity and make storage unavailable until a restart. Under `DockerRunner` a task's network holds only the server (and, for a task with connections, the egress proxy and the task's relay) and a build's only the egress proxy; the proxy and the relay refuse the networks they are attached to, so neither reaches SeaweedFS. The isolation relies on Docker's own network isolation (the default). The S3 keys
  in the worker's reach give full bucket access, and a host administrator (Docker access) is
  outside the model.
- **Applying Iceberg row-level deletes.** A table written to by an engine other than Lakana's
  own append path — e.g. a merge-on-read `UPDATE`/`DELETE` from another engine through Lakana's Iceberg REST
  catalog — can commit a snapshot with position- or equality-delete files. Lakana's query
  engine does not apply them (M5). Reading the data files while silently ignoring the
  deletes that apply to them would return rows the writer meant removed, so the worker made
  `list_data_files` refuse outright instead: a table whose current snapshot has a live
  delete file raises `UnsupportedTableError`, surfaced to the submitter as a clear failure
  rather than wrong data.

## Current posture

**M0 shipped no authentication, no authorization, and no data** — all three packages then
existing were `__init__.py`-only skeletons. Every control below has since landed on top of
that empty starting point; nothing in this section should be read as still describing M0.

**Identity and credentials (M3a).** Principals, groups, PATs, and task tokens are real
(`lakana_core.identity`): PATs and task tokens are SHA-256-hashed, verified with
`hmac.compare_digest`, and checked for expiry, revocation, and principal deactivation on
every request.

**Authorization and grants (M3b/M3f).** `GrantAuthorization` (`lakana_core.grants.authorization`)
backs every authorization decision across `lakana-server` and `lakana-worker`, including the
worker's own query-execution path: `run_one` resolves the submitting run's `task_token_id`
into a fresh `TaskScope` before authorizing each referenced table, re-checking the token's
own current validity (missing, revoked, expired, or its principal deactivated) before
running a queued query, so a task token's declared scope narrows table resolution the same
way it narrows submission, rather than being checked only once at submission time and never
again. `QueryService.get_query_run` scopes a task-token caller's reads to runs submitted with
that same token. `AllowAllAuthorization` remains only for bootstrapping the very first grant
(before any grant can exist to authorize it) and in tests that don't exercise authorization.
See "Reading tables one is not granted" and "Credential theft" in the table above for the
current task-token allow-list and namespace-coverage rules.

**Row filters and column masks (M3c/M3f).** Row filters and column masks
(`lakana_core.policy`) are written in a restricted expression subset (comparisons, boolean
logic, literals, column references, and `current_user()`/`has_group('name')`, plus
`substr`/`substring`/`||`/`md5` for masks), validated by sqlglot at create time so an invalid
policy is never stored, and resolved per `(principal, table_id)` — bound to the table's id,
not its name, the same way grants are. Enforcement happens in `lakana_core.query_rewrite`: M2
already replaced every base-table reference with a synthetic
`__lakana_<dotted>_<sha256[:12]>` CTE reading only the catalog-resolved files (`rewrite_query`
raises a fail-loud `ValidationError` if the same alias is ever claimed by two different
dotted names), and M3c builds that CTE's body as an explicit column list — a masked column's
projection swapped for its mask expression, a `WHERE` ANDing every applicable row filter.
Nothing above that CTE ever touches the raw file, so a join, a nested CTE, an alias, or a
`SELECT *` inherits the masked and filtered rows rather than leaking around them; the design
spec's call for a rewrite "after full identifier resolution" is satisfied structurally, by
there being nowhere above the CTE to leak from, not by a second resolution pass. Composition
fails closed: filters AND together, and two or more masks applying to the same column
collapse to `NULL` rather than ranking two independently-authored expressions. Policies apply
to every role including `admin` — no bypass *of the rewriter itself* — and layer on top of
`READ_TABLE` authorization rather than replacing it. Creating or revoking one requires
`Action.MANAGE_POLICY` through `/api/v1/row-filters` and `/api/v1/column-masks`.
`user_attribute()` remains a future sub-project (issue #8). An `EXTERNAL` table goes through
this same rewriter and its own policies apply to it; see "Reading masked columns or filtered
rows" in the table above for how its `location_pattern` is confined so those policies cannot
be bypassed by reading a different table's files under its name.

**Secrets (M3d).** `lakana_core.secrets` is real: a `KeyProvider` interface wraps and unwraps
each secret's own freshly generated AES-256 data key under a deployment's master key, with
`EnvKeyProvider` the one implementation today (`LAKANA_SECRETS__MASTER_KEY`, read once into
`Settings` at startup); each secret's key-value bag is AES-256-GCM-encrypted under that
per-secret data key before it is persisted. `SecretService` creates, lists, rotates, and
revokes secrets behind `Action.MANAGE_SECRET`, and never decrypts one — the crypto/service
split means the only method in the codebase that can turn a stored secret back into
plaintext is `StorageResolver`'s own `_resolve_secret_values`, called exactly where an
environment's storage is resolved for actual use. `StorageResolver.for_environment` merges
those decrypted values into `config.options`, winning over any same-named plaintext option
already there, so a secret is never silently shadowed by a stale plaintext value left over
from before it was configured. Attaching a `storage_secret_ref` to an environment
additionally requires `Action.MANAGE_SECRET` on that environment, not just the
instance-scoped `Action.MANAGE_ENVIRONMENT` the rest of `update_environment_storage`
authorizes against — otherwise a principal who can manage environment storage in general but
not this environment's secrets could point it at a secret it has no authority to read and
have the platform decrypt it on their behalf. Secret-backed storage is possible
per-environment, not mandatory: `PATCH /api/v1/environments/{name}/storage` with plaintext
`options` and no `secret_ref` still works, and nothing forces migrating an existing
plaintext-configured environment to a secret. Each stored secret carries a `format_version`
(1: no associated data; 2: both encryption layers bound to the row's id, environment id, name
and key id, `lakana_core.secrets.crypto.associated_data`) and a `key_id` naming the provider
and key that wrapped its data key; every write is format 2, old rows are refused unless
`LAKANA_SECRETS__ACCEPT_FORMAT_1` is on and become format 2 when the secret is set again, and a
row of an unknown version is refused. See "Infrastructure compromise" under "Explicitly not defended" for what this does and does not
defend. Tokens the worker mints for a Python task
(M5c2a) are revoked at the end of the attempt; REST-minted task tokens still expire only by their TTL
or an explicit revoke. See "Escalating through environment creation or re-rooting" in the table above for
`create_environment`/`update_environment_storage`'s instance-scoped authorization and
`is_protected` enforcement.

**Audit log (M3e/M3f).** `lakana_core.audit` is real: `AuditEvent` is the append-only row
model, `AuditService.record` writes one per event, and `AuditService.query` gates reads
behind instance-scoped `Action.READ_AUDIT_LOG`. Every existing `authorize()` call site is
audited without having been individually touched: `AuditingAuthorization` wraps whatever
`AuthorizationService` a caller already holds and records `AUTHORIZATION_DENIED` for the
`PermissionDenied` it lets through, at most `LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` rows per
principal per minute (the first denial of each action is always recorded, and the next row carries
`detail.suppressed`). Each denial past the rate is still written to the server's or the
worker's log as one INFO line naming the principal, the action and the object, and the counts
still pending when a process stops cleanly are written as one summary row per principal (at most 100 principals and about 3 s; a
WARNING counts the rest). Residual:
once a principal is over its rate, which object it probed is in the log, not in the table, and a
process that is killed loses its pending count; a run of many harmless denials followed by the
real probe is visible as a burst. Authentication success/failure is recorded at the other
seam, `get_principal`. `GrantService`, `SecretService`, and `CatalogService` additionally
call `AuditService.record` explicitly at their own mutation points — see "Audit evasion" in
the table above for the full event-type list and detail shapes. Same-transaction atomicity is
not uniform across every event type: grant changes, secret operations, environment
lifecycle/storage/protection changes, and query completions are written in the same
transaction as the action they record, riding along with that action's own commit;
authentication events and authorization denials commit their own row immediately
(`AuditService.record(..., commit=True)`), because the action they would otherwise ride
alongside never happened. Reading the log back is exposed at `GET /api/v1/audit-events`,
matching the "Assets" section's listing of the audit log itself as sensitive.

**All five M3 sub-projects have shipped** — M3a (identity and credentials), M3b
(authorization and grants), M3c (row filters and column masks), M3d (secrets), and M3e (the
audit log). **M3 itself is not closed**: the security-review exit criterion has not yet been
re-run since the M3f fixes above landed — see "M3 exit criteria" below.

**Governed table creation and append (M4a).** Two platform routes give the SDK a write path
that needs no pyiceberg or storage credential of its own: `POST .../tables` creates a
managed table from a caller-declared JSON column list (converted to an Iceberg `Schema` by
`lakana_core.catalog.json_schema.to_iceberg_schema`, then `CatalogService.create_table` as
usual — same medallion-namespace rule, same refusal of a caller-supplied location), and
`POST .../tables/{name}/data` appends an Arrow IPC stream's rows through
`CatalogService.append`. See "Oversized or malformed input to the governed append path" in
the table above for the append path's controls in full.

Two things were planned, not implemented, ahead of their delivery milestones so that later
work replaces an implementation instead of inserting call sites into code that never
expected them:

- The `Principal` model and `AuthorizationService` interface shipped in **M1** as an
  allow-all implementation behind a real interface, with a single bootstrap admin. **M3a
  replaced the placeholder credential check with real, verified PATs and task tokens; M3b
  replaced the authorization *implementation*, not the call sites** — every core operation
  from M1 onward accepted and threaded a resolved principal, and that check is now real.
- Query isolation via credential scoping (the row above, "Bypassing policy by reading
  files directly") was planned for **M2/M3**. The query executor, the rewriter, and the
  DuckDB integration all shipped in M2 (`lakana_worker.executor`,
  `lakana_core.query_rewrite`), and the policy rewriter on top of them shipped in M3c; the
  executor holds no object-store credential of its own, reading only the presigned URLs
  the catalog resolved for the query.

### Path and location confinement

One further control landed ahead of the milestone that planned it: **path confinement.**
`FsspecStorage` (`packages/lakana-core/src/lakana_core/storage/`) confines every root-relative
path it is given to its environment's configured storage root — rejecting a path that still
resolves outside the root after normalising `.` and interior `..` segments, whether the
escape is written as a leading `../`, an absolute-looking path, or a NUL byte.
`lakana_core.identifiers.validate_path_component` backs this up one layer earlier:
`NamespacePath.from_parts` and `ResolvedStorage.table_location` both reject a namespace or
table component containing a `.` (anywhere, so also `..`), `/`, or the unit separator, so a caller cannot
construct a traversal in the first place.

**The precise claim is narrower than "`FileIO` is confined": `FileIO` accepts any absolute
URI it is given** — `ResolvedStorage.table_location` builds that URI directly and hands it
to `file_io`, never through `Storage`, so `FsspecStorage`'s own confinement guard is not on
that path at all. What is actually true is that **the locations Lakana itself constructs are
confined**, by validating every component before it is joined into a path, not that the
`FileIO` object enforces any confinement of its own. This is what makes the
per-environment blast-radius isolation real today: one environment's storage cannot be
reached through another environment's root, independent of and ahead of the grants and
credential-scoping controls in the "Defended" table above. It is not a row in that table
because it is narrower than what those rows cover and does not substitute for them — it
constrains *where on a backend* a path can land, not *who* is allowed to read or write it.
Its known gaps: a symlink inside a local-filesystem root escaping confinement (recorded
under "Explicitly not defended" above), and the fact that this guarantee holds only for
locations Lakana itself builds through `NamespacePath` and `ResolvedStorage` — code that
constructs a `file_io` location by some other means bypasses it entirely, since the
confinement lives in the construction path, not in `FileIO` itself.

**`CatalogService.create_table` and `CatalogService.commit_table` are exactly that "some
other means", and close it with a second control at those choke points.** The Iceberg REST
`CreateTableRequest` lets a client supply both a `location` (the table's root) and
`properties` — and several of pyiceberg's own table properties are locations in all but
name. A `CommitTableRequest` carries the same two surfaces in a different shape: a
`SetLocationUpdate` rewrites the table's root and a `SetPropertiesUpdate` sets the same
properties. All of them are caller-supplied absolute URIs that would otherwise reach
`file_io` without ever passing through `NamespacePath` or `table_location`'s own validation.

**`create_table` refuses a non-`None` `location=` outright, and `commit_table` refuses any
`SetLocationUpdate` outright** — `ValidationError`, before either reaches any confinement
check — rather than confining either to the storage root: Lakana assigns every managed table's
location itself (`ResolvedStorage.table_location`, id-suffixed as of M3f: every new managed
table's location is `<name>-<table id hex>`, so `rename_table` — which does not move files —
can never hand a new table a previous table's old directory), and a caller-supplied location
is the same kind of privilege escalation `write.metadata.path` and its siblings are, below,
not merely something to constrain. A table's location can therefore never change after
creation.

**`ResolvedStorage.validate_table_properties` refuses three of pyiceberg's own table
properties outright, whatever their value**, because each is, in effect, an extra location
parameter that arrives under a different name and would otherwise bypass the refusal
`location=` gets: `write.metadata.path` would substitute for the derived metadata directory
when computing where to write, redirecting the write `create_table` performs immediately;
`write.data.path` would be persisted verbatim into the metadata file, where the first
component to write data would honour it; `write.object-storage.path` is the Apache Iceberg
table-properties spec's name for the same concept — the installed pyiceberg (0.12) does not
currently read it, so it is refused defensively, purely to close the door before a future
release might open it. The keys come from `pyiceberg.table.TableProperties` where a constant
exists, so a renamed **constant** fails at import rather than silently ceasing to match; a
renamed **value** would not, so `test_the_guarded_property_keys_are_the_ones_pyiceberg_reads`
pins the resolved strings directly. A fourth property, `write.py-location-provider.impl`, is
refused outright for a different reason: it names an arbitrary `module.ClassName` for
pyiceberg to `importlib.import_module` and instantiate, an arbitrary-import primitive from
caller input, not a path — confining the file location it eventually produces would do
nothing, since the import and instantiation already happened by the time there is a path to
confine.

This guard lives on `ResolvedStorage`, not inside `create_table`, deliberately: that is where
the storage root is, and every write path has the same exposure and must be able to make the
same one call rather than remember to repeat the check; `commit_table` makes exactly that
call. Both write paths additionally call `confine_to_root` on the *computed* metadata file
location `next_metadata_location` returns, as defence in depth against a future pyiceberg
release giving the location provider an input the guards above do not yet name — this does
**not** subsume refusing `location=`/`SetLocationUpdate` outright: when `write.metadata.path`
is set, the provider derives the metadata target from *that* property and ignores the table
location entirely, so an out-of-root location paired with an in-root `write.metadata.path`
would pass a computed-location-only check while the persisted table root, from which every
future data write derives its paths, sat outside the environment. `ResolvedStorage.
confine_to_root` has the identical symlink limitation as `FsspecStorage` (see "Explicitly
not defended" above): a lexical check, not a filesystem one.

**The worker's own containment check is the other half of the same guarantee, and the one
that actually decides which files a query reads.** `is_within_location`/`is_owned_by_location`/
`is_id_suffixed_location` (`lakana_core.storage.resolver` — shared with `commit_table`'s own
`AddSnapshotUpdate` guard, so `lakana-core` need not import `lakana-worker` for the identical rule
to protect both) require a URI to be `posixpath.normpath`-canonical and free of glob
metacharacters (`*`, `?`, `[`, `]` — DuckDB expands a glob in a local path itself, so a
canonical-looking "file path" like ``.../managed/*/*/data/*.parquet`` would otherwise match
every table's data across the whole `managed/` zone once opened) before comparing it against
a table's own location on a path-segment boundary, not a bare string prefix. Before listing a
single file, the worker refuses to read any `ICEBERG` table whose own location's final path
segment does not end with `-<table id hex>` — a table created before this location scheme
existed can have a location that is the environment root or an intermediate namespace
directory, and the set of "other" table locations a fallback rule could compare against can
never be complete (an `EXTERNAL` table's `raw/` claim and a dropped table's orphaned data
files are never in it, since only currently-existing `ICEBERG` rows contribute a location),
so such a table is refused outright rather than trusted to a rule that cannot see every case.
For a table that does pass the id-suffix check, a "most specific owner" rule still applies on
top: between two tables that both pass it, a namespace coincidentally named after another
table's own id suffix could otherwise nest one table's directory inside another's, and this
rule catches that case specifically. A table failing the id-suffix check must be re-created;
there is no in-place migration for an existing table's location.

### Unattended deletion of table locations (#90)

The worker's location sweep deletes table data with no person in the loop, so what it may delete
is decided by catalog records, never by the absence of one: a create commits its location's record
before its first file, the commit that publishes the table moves that record to `LIVE` behind a
row-locked fence, and the sweep deletes only a location whose record is `ABANDONED`, or `DROPPED`
by a drop whose undo period (`LAKANA_TABLES__UNDO_DAYS`) has passed. It deletes only inside an id-suffixed directory under `managed/` of the record's
environment root, and before every delete it checks again, under the record's lock, that nothing in
use in any environment is that location or lies inside it. A directory without a record is counted
and never deleted, so a restored or replaced database cannot make the sweep delete what it forgot.
Residuals (a writer that wakes up after its claim was abandoned, a namespace created inside an
abandoned location between the check and a delete, a create in flight at a backup's moment after a
restore) are listed in `docs/agent/security.md`, "Table locations".

An undrop restores a dropped table's grants, row filters and column masks, so it must not restore
more than the drop took: it un-revokes only the ids the drop recorded, and only while each is still
stamped with the drop's own time, and it needs `DROP_TABLE` on the namespace (never the table grants
of a new table that took the name) plus `CREATE_TABLE`. The sweep marks a purge as started under the
record's lock before its first delete, and an undrop under the same lock refuses a marked record, so
a table is never restored with some of its files gone. A purge also waits while a dropped table
lies inside its location, and maintenance treats a dropped table's directory as another table's, so
neither deletes a nested tombstone's files during its undo period; an undrop whose metadata file
is gone anyway is refused before it writes anything.

A retention (#87) lets the sweep drop a table with no person in the loop, so a retention is a
delayed drop and is guarded like one: making a table expire sooner (or at all) needs `DROP_TABLE` or
ownership, while anyone who may write a table may keep it (a later expiry, or `never`), and every
change and refusal is audited. The expiry decides again under the environment's and the table's row
locks and runs the ordinary drop path, so it leaves a tombstone with at least a day of undo period;
a protected environment, a deployed project's asset, a table with a write queued or running, and a
table without its own `LIVE` location record are never dropped. The clock uses the catalog's own
time of the last commit and the recorded reads, never the client-written snapshot timestamp, so a
client cannot backdate a table into expiry.

Deleting an environment (#213) is a drop of every managed table in it, through the same drop path:
each becomes a tombstone with the undo period, and the environment's row stays, hidden from every
lookup by name and with its name taken, until the sweep has purged the last of them. A create or an
undrop racing the deletion takes the environment's row lock before it publishes, so no table
appears in a deleted environment. A restore needs `MANAGE_ENVIRONMENT`, brings back only the tables
and policies that deletion took, and is refused whole once a purge of one of them has started. An
external table's files are never deleted.

Table directories that older releases left without a location record (#229) are never deleted by
the sweep. `lakana env orphaned-locations ENV --delete` removes them on request: it needs
`MANAGE_ENVIRONMENT` on the instance, never an environment's own grant, because the root can be
shared; it deletes only canonical, id-suffixed directories strictly inside `managed/` that no
record in any state and no table of any environment names, holds or surrounds, that are not
another environment's root nor inside one (a root that is this `managed/` included), and none
while this root lies in another environment's `raw/`, `artifacts/` or `_artifacts/`, compared under every reading a storage may give a location (case,
Unicode form, percent decoding, `.`/`..`, doubled slashes, scheme aliases, symbolic links; a
relative or host `file` location keeps everything on the local disk), the same comparison the
sweep's purge makes, and only when their newest file is older than the age asked for (30
days by default). Each is reserved by inserting a record for its location (unique, so a create
that claimed it first wins) and then purged by the sweep's own purge, under that record's lock, with
the same checks before every file. One audit event records each such call.

## M3 exit criteria

The M3 epic defines three exit criteria:

- **Property-based mask and filter tests:** M3c delivered property-based tests that
  `rewrite_query` correctly applies masks and filters under arbitrary principals, policies,
  and table schemas, with no leakage and correct composition. **Met.**
- **Mutation testing:** `docs/security/mutation-testing.md` records the mutation-testing
  campaign, exit-run totals (556 killed / 2 equivalent / 558 total), and the requirement that
  survivors must be either killed by a test or recorded as confirmed equivalences. **Met.**
- **Security review:** `/security-review` over `f6f59b4..m3-close-out` ran and **failed**,
  with 1 Critical, 5 Important, and several Minor findings, tracked and addressed by the M3f security-fixes sub-project above.
  **Re-run after M3f; result recorded in the M3f PR description.** This exit criterion
  stays open, and the epic stays open, until that re-run passes.

## Upgrading to M3f

- **Every pre-M3f Iceberg table must be re-created.** The worker now refuses to read any
  `ICEBERG` table whose location's final path component does not end with `-<table id
  hex>` (see "Path and location confinement" above) — a table created before this scheme
  existed fails this check unconditionally, with no in-place migration. Re-create the
  table (and reload its data) under the new scheme before upgrading dependents.
- **Two pre-M3f `EXTERNAL` tables whose claims overlap both become unqueryable until one
  is dropped.** Migration `0011` backfills `tables.external_claim` for every pre-existing
  `EXTERNAL` row whose `location_pattern` parses, without checking whether two rows'
  claims collide. `check_claim_free` runs again, symmetrically, at every subsequent query
  against either table (`exclude_table_id` set to the row's own id), so if two claims do
  overlap, each query refuses on account of the *other* row's claim — neither table reads
  again until the overlap is resolved by dropping or re-pointing one of them.
- **A restart no longer rescues an instance whose only admin has lost every PAT.** Before
  M3f, `bootstrap_if_needed` unconditionally re-granted instance `ADMIN` and re-minted
  a PAT on every restart whenever the bootstrap principal had none of its own, so a restart
  alone was enough to recover. It now acts only as lockout recovery, and only while
  `admin_exists` finds *no* active instance admin at all — so if the operator can still
  authenticate as *some* admin, deleting or revoking that admin's last PAT no longer triggers
  recovery on the next restart; the admin (and the instance) is simply locked out with an
  admin grant nobody can use. **Recovery:** revoke or deactivate every instance-scoped
  `ADMIN` grant directly in the database (or deactivate the principals holding one), then
  restart. `bootstrap_if_needed` finds no active admin, restores the bootstrap principal to
  `ADMIN`, and writes one freshly minted PAT to the token file (never to the log) — the only
  working credential afterward, since recovery also revokes that principal's own stale PATs.

## Review cadence

This document is updated at every milestone as controls land — a milestone is not
considered closed if this file still describes its controls as future work. A feature
touching authorization, policy, secrets, or the query path should also get a
`/security-review` pass before merge, in addition to the automated scanning below.

Two layers of automated scanning are already running today, independent of the application
code above:

- **Every PR** runs bandit, gitleaks, pip-audit, and the dependency license check
  (`.github/workflows/security.yml`, jobs `static` and `secrets`) — they run on every pull
  request today and report their results against the real, substantially built codebase
  now in `packages/`, not just a placeholder scan waiting for code to exist. The
  branch protection ruleset requires these status checks before a merge: `lint`,
  `test (3.11)`, `test (3.14)`, `conventional`, `static`, `secrets`, `integration` and
  `docs-examples`. The CLA check (`cla.yml`) runs on every pull request and records signatures
  in a separate private repository. It is not a required check, because Dependabot's pull
  requests get no Actions secrets and could never pass it, so the maintainer confirms a
  signature before merging an outside contribution.
- **Weekly**, a deep scan runs semgrep against the full codebase (`.github/workflows/security-deep.yml`),
  uploading its SARIF output to GitHub's code scanning UI and keeping it as a build artifact.
  CodeQL analyses the Python code on every pull request, on every push to `main` and weekly
  (`.github/workflows/codeql.yml`); its findings are in the same UI.
