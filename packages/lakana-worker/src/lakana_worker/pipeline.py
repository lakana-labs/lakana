"""The per-query pipeline: parse, authorize, resolve, rewrite, execute or write, record.

Ties together every piece built in Phase A: `lakana_core.query_rewrite` (parse,
blacklist, rewrite), `lakana_core.lineage` (column lineage from the same
parse), `lakana_core.catalog.datafiles` (Iceberg file listing),
`lakana_core.storage` (presigned URLs), and `lakana_worker.executor` (isolated
execution).

`error_message` is persisted on the query run and read back by whoever
submitted it, so nothing written to it may name a resolved storage location.
A presigned URL carries its own signature and a plain resolved path
describes the worker's own disk, and both appear verbatim in the text of
errors raised while resolving storage and in DuckDB's own "no files found"
message — so the resolution stage reports only its exception's class name
(the idiom `lakana_server.api.iceberg.route._OPAQUE_MESSAGE` already applies
to unmodeled exceptions), with three narrow exceptions: `UnsupportedTableError`
(a table with live Iceberg delete files), `NotPublishedError` (a
table that is not published because its first build failed) and `KeptAfterCheckError`
(a table a task may not read because it was kept after a failed check) are passed
through with their own message verbatim, since that message is built entirely from the dotted
table name the submitter's own query already named (and, for the last two, a check's
name and a run id) and never carries a storage location. The refusals of a read at a check
(`CheckedSnapshotGoneError`, `CheckResultGoneError`, `NoCheckedSnapshotError`) pass
through on the same grounds, and so does the refusal of a read after a rename touched a
policy's column (`PolicyColumnRenamedError`); see `_resolve_sources_opaquely`. Every other
stage's message has the run's own resolved locations redacted out of it before it is
stored. A decrypted secret's plaintext values are treated the same way: added to the same
redaction list as soon as they are resolved, which `run_one` does first —
before view expansion, `rewrite_query`, `execute_query`, the write step or
a maintenance run, since any of those can fail and echo one back. Only
sufficiently long values are added, so a short or empty option value can
never blindly scrub an unrelated error message down to noise.

A run ends `SUCCEEDED`, `FAILED`, or `CANCELLED`: the last only when
`QueryService.cancel_query` set `cancel_requested` and the watch `run_one` starts
(`_watching_for_cancel`) made the query's child stop; the same request on a queued run ends
it in the core, and the worker never starts it.

A read run started with `on_result` hands its result to the caller as a `ReadOutcome`
and stores no result file: the count of a data quality check comes back to the worker that
runs the task (`lakana_worker.checks.count_check`). Authorization, the policy rewrite, the
limits and the completion event are the same as for any read. Only the worker itself passes
the callback; nothing a request can set reaches it.

A run with `check_result_id` shows a person the rows a check found: an ad-hoc read
of its submitter, without a task run and so without a run context, in which the one table the
stored result names is read at the snapshot the check read (`_resolve_sources`). Only
`lakana_core.runs.check_rows.submit_check_rows` sets the field.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, NoReturn

import pyarrow as pa
import pyarrow.ipc
import sqlglot.expressions as exp
from sqlalchemy import ColumnElement, and_, or_, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import (
    Action,
    AuthorizationService,
    PrincipalRef,
    RunContext,
    Securable,
    TaskScope,
    WriteFence,
)
from lakana_core.catalog.datafiles import list_data_files
from lakana_core.catalog.external_location import check_claim_free, parse_external_location
from lakana_core.catalog.field_ids import read_fields, refuse_changed_nested_fields
from lakana_core.catalog.health import record_table_reads
from lakana_core.catalog.maintenance import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_LAST,
    DEFAULT_ORPHAN_AGE_HOURS,
    MaintenanceStopped,
)
from lakana_core.catalog.merge import SQL_ADVICE
from lakana_core.catalog.schema_compat import SchemaDiff, arrow_to_iceberg_schema, view_columns
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.watermark import check_incremental_key, wrap_incremental
from lakana_core.catalog.writes import (
    SnapshotPin,
    WriteResult,
    max_rows_message,
    target_changed_message,
)
from lakana_core.config import (
    DEFAULT_MAINTENANCE_TIMEOUT,
    DEFAULT_MAX_HISTORY_OPEN_VERSIONS,
    QueryLimitsConfig,
)
from lakana_core.db import rowcount
from lakana_core.errors import (
    CheckedSnapshotGoneError,
    CheckResultGoneError,
    ConflictError,
    KeptAfterCheckError,
    LakanaError,
    NoCheckedSnapshotError,
    NotFoundError,
    NotPublishedError,
    PermissionDenied,
    StorageUnavailableError,
    UnsupportedTableError,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef, shown
from lakana_core.identity.task_tokens import resolve_task_token_scope
from lakana_core.lineage import extract_lineage
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import (
    AuditEventType,
    CheckResult,
    Environment,
    Namespace,
    Principal,
    QueryErrorCause,
    QueryRun,
    QueryRunStatus,
    Table,
    TableType,
    WriteMode,
)
from lakana_core.policy.cast_types import arrow_cast_type, iceberg_cast_type
from lakana_core.policy.expressions import columns_of
from lakana_core.policy.read_errors import policy_read_failure
from lakana_core.policy.renames import policy_column_renamed_message, renamed_policy_column
from lakana_core.policy.resolver import PolicyResolver
from lakana_core.projects.history_columns import HistorySpec
from lakana_core.query_rewrite import (
    ExpandedQuery,
    ReadField,
    RewrittenQuery,
    TableSource,
    ViewDefinition,
    check_blacklist,
    expand_views,
    extract_table_refs,
    named_columns,
    rewrite_query,
    unreadable_view_message,
)
from lakana_core.runs import pools
from lakana_core.runs.checks import (
    CHECK_RESULT_REMOVED,
    checked_snapshot_gone_message,
    column_not_shown_message,
    no_column_left_message,
    policy_needs_a_column_message,
)
from lakana_core.runs.lockdown import WriteFenceRefused
from lakana_core.runs.store import query_completed_detail
from lakana_core.secrets.keys import KeyProvider
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS, describe_syntax_error
from lakana_core.storage.base import Storage, StreamWriter
from lakana_core.storage.resolver import (
    ResolvedStorage,
    StorageResolver,
    is_id_suffixed_location,
    is_owned_by_location_given_nested,
    is_within_location,
    nested_locations_within,
    table_root_from_metadata_location,
)
from lakana_core.storage.unavailable import TABLE_STORAGE_UNREACHABLE, is_storage_unavailable
from lakana_worker.activity import ACTIVITY
from lakana_worker.executor import (
    INTERNAL_ADVICE,
    KILLED_FROM_OUTSIDE_MESSAGE,
    MEMORY_LIMIT_ADVICE,
    DuckDBInternalError,
    OutOfMemoryQuery,
    QueryCancelled,
    QueryCancelRequested,
    QueryTimeout,
    ResultTooLarge,
    TransientQueryError,
    WorkerStaleQuery,
    cancel_requests,
    describe_query,
    execute_query,
    stream_query,
)
from lakana_worker.memory import MACHINE_MEMORY_ADVICE, trim_heap
from lakana_worker.query_protocol import STORE_CERTIFICATE_UNVERIFIED

if TYPE_CHECKING:
    from pyiceberg.table.metadata import TableMetadata
    from pyiceberg.types import NestedField

logger = logging.getLogger(__name__)

_CANCEL_POLL_SECONDS = 0.5
"""How often the cancel watch reads the run's `cancel_requested`.

The supervisor asks the watch about once a second, so a request reaches the child within
about a second and a half; the read is one indexed lookup by primary key.
"""

_CANCEL_WATCH_JOIN_SECONDS = 5.0
"""How long ending the watch waits for its thread: a read hung on the database must not hold
the end of a query up; the thread is a daemon and ends with that read."""

_PRESIGN_TTL_SECONDS = 300
"""How long a presigned URL stays valid — matched to a generous query timeout.

M2 has no per-query timeout configuration yet (that is execution-engine
scope beyond this milestone's exit criterion); this constant is the TTL
until one exists.
"""

_REDACTED = "<redacted>"
"""Stands in for a resolved storage location inside a stored error message."""

_MIN_SCRUBBED_OPTION_LENGTH = 8
"""Shortest storage option value added to the redaction scrub-list.

`_redact` does a blind `str.replace` for every entry in its scrub-list, so a
short or empty value (an empty `session_token` is plausible and reachable
through the real API: `SecretCreate.values`/`EnvironmentStorageUpdate.options`
are bare `dict[str, str]` with no non-empty constraint) would match
constantly through an unrelated error message and destroy it. A real access
key or secret key is always longer than this; a value this short is not a
meaningful credential worth scrubbing.
"""

_DELETED_PRINCIPAL_MESSAGE = "The submitting principal no longer exists."
"""Why a run whose principal was deleted is refused rather than run.

`QueryRun.principal_id` is nullable, so a deleted principal leaves a run
whose submitter is unknown. There is no identity to authorize as, and
inventing one would authorize every per-table check in this run against a
principal that never existed — in M1 that is invisible because
authorization is allow-all, and in M3 it would be a real decision made on
fabricated input.
"""

_INACTIVE_PRINCIPAL_MESSAGE = "Principal is no longer active."
"""Why a PAT-submitted run whose principal was deactivated after submission is refused.

Mirrors `_INVALID_TASK_TOKEN_MESSAGE`'s reasoning for the PAT path: without
this check, a principal deactivated between submission and execution would
still have its queued query run under `PrincipalRef(is_active=True)`'s
implicit assumption, reinstating the deactivated principal's access for the
run's own duration. Checked before any parsing, alongside the deleted- and
invalid-task-token checks, and recorded the same way — `QUERY_COMPLETED` and
the `FAILED` status in one transaction.
"""

_INVALID_TASK_TOKEN_MESSAGE = "Task token no longer valid."  # nosec B105 -- not a credential
"""Why a run submitted with a task token that has since gone bad is refused.

`QueryRun.task_token_id` has no foreign key (migration `0011`),
deliberately, so a deleted token still leaves a row `_task_scope_for_run`
can detect as invalid rather than one that silently loses its scope. A
revoked or expired token, or one whose principal has since been
deactivated, is refused the same way: without this check, `run_one` would
otherwise build a `PrincipalRef` with no `task_scope` at all, and its
per-table authorization would fall back to whatever the *principal's* own
grants allow — reintroducing the old hole for a token that is no longer supposed to
work at all, not just narrowing what an active one can read.
"""


class _ResolutionFailed(Exception):
    """A failure while resolving table references, reported without its message.

    Carries an already-safe replacement message; the original exception is
    kept as `__cause__` so a traceback still reaches a log.
    """


def failure_cause(exc: BaseException) -> QueryErrorCause:
    """Classify the exception that failed a run for `QueryRun.error_cause`.

    Parameters
    ----------
    exc
        The exception `run_one` recorded the failure from (for a failed
        resolution, the underlying exception, not the opaque wrapper).

    Returns
    -------
    QueryErrorCause
        What kind of failure it was. Anything not named here, including a
        transient storage error, a DuckDB internal error and a write
        conflict, is `INTERNAL`.
    """
    if isinstance(exc, QueryCancelled):
        return QueryErrorCause.CANCELLED
    if isinstance(exc, QueryTimeout):
        return QueryErrorCause.TIMEOUT
    if isinstance(exc, ResultTooLarge):
        return QueryErrorCause.RESULT_TOO_LARGE
    if isinstance(exc, (OutOfMemoryQuery, MemoryError)):
        return QueryErrorCause.OUT_OF_MEMORY
    if isinstance(exc, (WorkerStaleQuery, WriteFenceRefused)):
        return QueryErrorCause.WORKER_LOST
    if isinstance(exc, SQL_SYNTAX_ERRORS):
        return QueryErrorCause.INVALID_QUERY
    if isinstance(
        exc,
        (
            NotPublishedError,
            KeptAfterCheckError,
            NotFoundError,
            CheckedSnapshotGoneError,
            CheckResultGoneError,
            NoCheckedSnapshotError,
        ),
    ):
        return QueryErrorCause.NOT_FOUND
    if isinstance(exc, PermissionDenied):
        return QueryErrorCause.PERMISSION_DENIED
    if isinstance(exc, (UnsupportedTableError, ValidationError)) and not isinstance(
        exc, (TransientQueryError, DuckDBInternalError)
    ):
        return QueryErrorCause.INVALID_QUERY
    return QueryErrorCause.INTERNAL


def _redact(message: str, locations: Sequence[str]) -> str:
    """Remove every resolved storage location from an error message.

    Parameters
    ----------
    message
        The message about to be persisted.
    locations
        Resolved URLs and path prefixes that must not appear in it. Longest
        first, so a full URL is replaced before the root prefix it contains.

    Returns
    -------
    str
        `message` with each location replaced by `_REDACTED`.
    """
    for location in sorted(locations, key=len, reverse=True):
        message = message.replace(location, _REDACTED)
    return message


_OUTSIDE_PREFIX = KILLED_FROM_OUTSIDE_MESSAGE.split("{signal}", 1)[0]
"""The start of Lakana's own text for a query process ended from outside (no engine text)."""


def _without_the_value(failure: LakanaError, restricted: frozenset[str]) -> LakanaError:
    """Replace the engine's text for a failure, on a read under a row filter or column mask.

    Errors on a read under a row filter or column mask always show one fixed message, whatever
    the class of the error (parser, binder, catalog, conversion, out of range, I/O, memory, ...)
    and whatever the wording the query process gave it: the engine's text can quote the rewritten
    SQL or a stored value, the policy's expressions run on every row, also on the ones it hides,
    and the reader's own SQL can write any wording itself. The class is kept (an internal, a
    transient or an out-of-memory error stays one, so retries and the memory handling are as
    before). What Lakana words itself (a cancel, a timeout, a stale lease, a query process ended
    from outside) is returned as it is.

    Parameters
    ----------
    failure
        The failure about to be recorded.
    restricted
        The dotted base tables of the query on which a row filter or a column mask applied;
        empty for a query that reads none, which keeps the engine's text (the data is the
        reader's own).

    Returns
    -------
    LakanaError
        The fixed message as a `ValidationError` when `failure` is such a failure of a
        query that reads a table under a policy, otherwise `failure` itself.
    """
    if not restricted:
        return failure
    only = next(iter(restricted)) if len(restricted) == 1 else None
    if isinstance(failure, DuckDBInternalError) and not str(failure).startswith(
        STORE_CERTIFICATE_UNVERIFIED
    ):
        # The engine's internal errors can format a value too; the type stays, so the cause and
        # a retry are as before, and the worker's log keeps the details.
        return DuckDBInternalError(
            f"Query failed: the query engine failed on its own. {INTERNAL_ADVICE}"
        )
    kind = type(failure)
    if kind is OutOfMemoryQuery:
        # The database's own out-of-memory error: fixed message plus the advice, no engine text.
        return OutOfMemoryQuery(f"{policy_read_failure(only)} {MEMORY_LIMIT_ADVICE}")
    if kind is TransientQueryError and not str(failure).startswith(_OUTSIDE_PREFIX):
        return TransientQueryError(policy_read_failure(only))
    if kind is not ValidationError or not str(failure).startswith("Query failed: "):
        return failure
    return ValidationError(policy_read_failure(only))


class _RowsNotListed(ValidationError):
    """The rows of a check cannot be listed as the table and the reader's policies are now.

    Raised by `_resolve_sources` for a query bound to a check result, after the read grant
    on the checked table passed, with a text built from the table's dotted name as the query
    names it, the result's run and the environment; `_resolve_sources_opaquely` passes it
    through. A `ValidationError`, so the run's cause is `invalid_query`, as for a check whose
    SQL names a column that is not shown.
    """


class PolicyColumnRenamedError(ValidationError):
    """A read is refused because a rename may have moved a row filter or column mask.

    Raised by `_resolve_sources` for an Iceberg table when a column that a policy of the reader
    uses changed its name, or changed owner, since the policy was created. Nothing is read. The
    text is built from the table's dotted name as the query names it and a column of the
    reader's own policy, so `_resolve_sources_opaquely` passes it through.
    """


def _columns_the_table_still_has(
    snapshot: Sequence[NestedField], current: Sequence[NestedField]
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Split the columns of a checked snapshot into those shown and those withheld.

    A column of the snapshot is shown when the table's current schema has a column of the
    same name, the same type and the same field id. A type is compared as the worker hands
    it to the rewrite (the text of the Iceberg type), so a nested type is compared whole: a
    struct that gained or lost a field is another type. The field id tells two columns that
    were renamed into each other through the Iceberg catalog, which keep names and types
    while each now holds what the other held; Lakana's own writes keep the id of a column
    they do not change. Whether a column is required is not compared.

    Parameters
    ----------
    snapshot
        The fields of the schema of the checked snapshot.
    current
        The fields of the table's current schema.

    Returns
    -------
    tuple
        The columns shown (name to type, in the snapshot's order) and the names of the others.
    """
    now = {field.name: (str(field.field_type), field.field_id) for field in current}
    shown_columns = {
        field.name: str(field.field_type)
        for field in snapshot
        if now.get(field.name) == (str(field.field_type), field.field_id)
    }
    return shown_columns, tuple(f.name for f in snapshot if f.name not in shown_columns)


def _resolve_sources(
    session: Session,
    environment_name: str,
    principal: PrincipalRef,
    dotted_refs: list[str],
    authz: AuthorizationService,
    key_provider: KeyProvider | None = None,
    pins: dict[TableRef, SnapshotPin] | None = None,
    *,
    check_result_id: uuid.UUID | None = None,
    withheld: dict[str, str] | None = None,
    prefixes: list[str] | None = None,
) -> dict[str, TableSource]:
    """Authorize and resolve every table a query references to readable URLs.

    A query bound to a check result (`check_result_id`) shows a person the rows a
    check found. Exactly one of its tables is read differently: the table whose catalog row
    has the id the stored result names is loaded with `CatalogService.load_table_at_check`
    and read at the snapshot the check read, with that snapshot's files, also when the table
    is not published. Of that snapshot's columns only those are read that the table has now,
    under the same name, type and field id, for every reader alike (see the comment at that
    place).
    The table is told by its row's id and never by its name, and every other table goes
    through `load_table` as in any query. Grants, row filters and column masks are resolved
    for the reader as for any read.

    Parameters
    ----------
    session
        Database session.
    environment_name
        The environment the query runs against.
    principal
        The submitting principal — authorized per table, not just once at
        submission.
    dotted_refs
        Dotted table names from `extract_table_refs`.
    authz
        Authorization service to check each table against.
    key_provider
        Decrypts a secret-backed environment's storage credentials, if this
        environment's storage_secret_ref requires one. None otherwise.
    pins
        When given, filled with the snapshot each `ICEBERG` table resolved
        to — its files are what the query reads — so a write reading its
        own target can commit against exactly that snapshot ). For the table read at a check it is
        the checked snapshot.
    check_result_id
        `QueryRun.check_result_id` of the run: the check result whose rows the query shows.
        `None` for every other query.
    withheld
        When given, filled with the columns of the checked snapshot that were left out of
        the read, each with the text that says so to a reader whose query names it. A
        column whose name another table of the query also has is not among them: the text
        is chosen by the bare name the database could not find, which then may mean either
        table.
    prefixes
        When given, filled with the read prefix of every table (`Storage.read_url_prefix`
        of an Iceberg table's location, or of an external table's ``raw/`` claim): the only
        locations the query process may open.

    Returns
    -------
    dict
        Maps each dotted name to its `TableSource`, ready for
        `rewrite_query`. `file_format` is always `"parquet"` for an
        `ICEBERG` table (the only format Iceberg data files use here); for
        an `EXTERNAL` table it is whatever `create_external_table` recorded.
        `columns` carries the table's declared schema, which is what a
        table with no files at all is read as instead: an Iceberg table
        before its first snapshot, or an external pattern that currently
        matches nothing. The declared type names are handed to sqlglot's
        type parser as they are stored — Iceberg's own spelling for a
        managed table, `create_external_table`'s inferred Arrow spelling for
        a raw-zone one — so an exotic Arrow type (`large_string`,
        `timestamp[us]`) on an external table that has lost its files fails
        the query rather than reading as zero rows. Also carries any
        applicable row filters/column masks for `principal`, resolved fresh
        for this call via `lakana_core.policy.PolicyResolver` — never cached
        across requests, since group membership and policy grants can
        change between them.

    Raises
    ------
    PermissionDenied
        If the principal may not read one of the referenced tables.
    NotFoundError
        If a referenced table does not exist.
    ValidationError
        If a referenced name is a view — it must be expanded into base
        tables before resolution ever sees it (defence in depth);
        if an `EXTERNAL` table's row has no `external_config` recorded — an
        invariant `create_external_table` always upholds, so this signals a
        corrupted row rather than a normal runtime outcome; if its stored
        `location_pattern` does not parse as `"raw/<dir...>/<glob>"`:
        reachable only for a row created before this confinement
        rule existed; if its stored `external_claim` does not equal the
        claim its own pattern parses to — `NULL` (a legacy row the
        migration's backfill could not populate) never matches, and a
        stored claim that disagrees with its own pattern signals the row
        was altered outside `create_external_table`; or if its claim
        overlaps another active `EXTERNAL` table's claim in this
        environment (`check_claim_free`, re-checked here as defence in
        depth against a row that bypassed it, e.g. by direct mutation); if
        an `ICEBERG` table's own location is not the id-suffixed leaf form
        `ResolvedStorage.table_location` assigns at creation — a table predating this confinement
        scheme is refused
        outright rather than trusted; if its current snapshot's manifest
        list or any of its manifests does not lie under that table's own
        location, or lists a data file that does not belong to it, or names
        a path containing a glob metacharacter (`*`, `?`, `[`, `]`;
        DuckDB expands a glob in a local path itself) — a foreign snapshot or manifest
        committed under the table's own metadata can otherwise name another table's files, and a
        namespace coincidentally named after another (suffixed) table's own
        directory can nest one table's location inside another's; if an `ICEBERG` table's
        location does not lie under the environment's storage root, or a table's read prefix
        cannot be derived (`Storage.read_url_prefix`).
    NotPublishedError
        If a table's first build did not finish, or failed a blocking check, and no later
        run has published it (raised by `CatalogService.load_table`, after the read grant
        passed); `_resolve_sources_opaquely` passes it through.
    KeptAfterCheckError
        If the reader is a task and a table was kept after a failed check and has not passed
        its checks since (raised by `CatalogService.load_table`, after the read grant
        passed); `_resolve_sources_opaquely` passes it through.
    UnsupportedTableError
        If an `ICEBERG` table's current snapshot has any live position- or
        equality-delete file — written by any engine other than
        Lakana's own append path, e.g. a merge-on-read `UPDATE`/`DELETE`
        from another engine through the Iceberg REST catalog. `list_data_files` raises this with
        a path-free message; re-raised here naming the dotted table, since
        that name is safe to surface (the submitter already used it in
        their own query) and is what makes the failure legible in
        `error_message` once `_resolve_sources_opaquely` passes it through
        unredacted. Applying deletes instead of refusing the table is M5.
    CheckResultGoneError
        If the query is bound to a check result that no longer exists in this environment
        (`CHECK_RESULT_REMOVED`, before any table is looked at), or, from the catalog, that
        is not one of the table as it exists now.
    CheckedSnapshotGoneError
        If the table no longer holds the checked snapshot (from the catalog); if none of
        the tables the query reads is the table the result names (the table was dropped and
        built again, so the name now leads to another table that never had that snapshot);
        or if the name the result stores for its table leads to no table any more (it was
        renamed or dropped).
    PolicyColumnRenamedError
        For an Iceberg table: if a column that a row filter or column mask of the reader uses
        was renamed, or given to another field id, since the policy was created.
    _RowsNotListed
        For the table read at a check: if the table has none of the snapshot's columns any
        more, or if a row filter or an applied column mask of the reader reads a column that
        is not the same column in the snapshot and in the table now.
    NoCheckedSnapshotError
        If the result names no snapshot (from the catalog; the submission refuses such a
        result first).
    """
    catalog = CatalogService(session, StorageResolver(session, key_provider), authz)
    policy_resolver = PolicyResolver(session)
    resolved_storage = StorageResolver(session, key_provider).for_environment(environment_name)
    root_prefix = resolved_storage.config.root.rstrip("/") + "/"
    environment_id = session.execute(
        select(Environment.id).where(Environment.name == environment_name)
    ).scalar_one()
    # Every other ICEBERG table's root location in this environment, for the
    # "most specific owner" check below — which
    # only ever matters between two *suffixed* tables (the
    # `is_id_suffixed_location` gate refuses a legacy table outright before
    # this list is even consulted for it), for instance a namespace
    # coincidentally named after another table's own id suffix. Computed
    # once per call, not per referenced table: it does not depend on which
    # table is being checked, only on the environment. `EXTERNAL` rows have
    # no `metadata_location` and are excluded by the `is_not(None)` filter,
    # not by `table_type`, so a future non-Iceberg table type with the same
    # shape is excluded the same way without this query needing to change.
    other_table_locations = [
        location
        for (metadata_location,) in session.execute(
            select(Table.metadata_location)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .where(
                Namespace.environment_id == environment_id,
                Table.metadata_location.is_not(None),
            )
        ).all()
        if metadata_location is not None
        and (location := table_root_from_metadata_location(metadata_location)) is not None
    ]
    checked: CheckResult | None = None
    if check_result_id is not None:
        # Read once, before any table: a result that went with its run must fail the query.
        # Treating it as "no binding" would read every table as it is now and show rows the
        # check never saw under the heading of the check.
        checked = session.execute(
            select(CheckResult).where(
                CheckResult.id == check_result_id, CheckResult.environment_id == environment_id
            )
        ).scalar_one_or_none()
        if checked is None:
            raise CheckResultGoneError(CHECK_RESULT_REMOVED)
    read_at_check = False
    # The columns left out of the read at the check, with their texts, and the name of the
    # source they were left out of.
    left_out: dict[str, str] = {}
    pinned: str | None = None
    sources: dict[str, TableSource] = {}
    for dotted in dotted_refs:
        declared_columns_only = False
        field_ids: tuple[ReadField, ...] = ()
        mask_types: dict[str, exp.DataType] = {}
        # For the one table read at a check: the result, and the names (lower-cased, as the
        # database compares them) of the columns that are the same column, by field id, in
        # the checked snapshot and in the table now.
        at_check: tuple[CheckResult, frozenset[str]] | None = None
        # The metadata of an Iceberg table, whose schemas show what a rename moved; `None` for a
        # raw table, whose files are read by name and whose policies bind by name.
        iceberg_metadata: TableMetadata | None = None
        *namespace_parts, name = dotted.split(".")
        ref = TableRef(
            environment=environment_name,
            namespace=NamespacePath.from_parts(namespace_parts),
            name=name,
        )
        try:
            row = catalog.load_table_row(principal, ref)
        except NotFoundError as exc:
            # `load_table_row` asks for the read grant on the name before it looks the name
            # up, so only a reader who may read whatever has this name gets here. For the
            # name the check result stores for its table, "not found" means the table was
            # renamed or dropped: the reader typed no name and cannot act on a bare "not
            # found", so they are told what is left. The name only chooses the text; which
            # table is read at the snapshot is decided by the row's id below.
            if checked is not None and dotted == checked.table_name:
                raise CheckedSnapshotGoneError(
                    checked_snapshot_gone_message(dotted, checked.run_id, environment_name)
                ) from exc
            raise
        if row.table_type == TableType.VIEW:
            raise ValidationError(f"{dotted!r} is a view and must be expanded before resolution.")
        if row.table_type == TableType.ARTIFACT:
            raise UnsupportedTableError(
                f"{dotted} is an artifact; SQL cannot read it. Only a Python asset can read an "
                "artifact: declare it in the asset's inputs."
            )
        if row.table_type == TableType.EXTERNAL:
            if row.external_config is None:
                raise ValidationError(f"External table {dotted!r} has no external_config recorded.")
            location_pattern = row.external_config["location_pattern"]
            file_format = row.external_config["format"]
            columns = dict(row.external_config.get("schema", {}))
            mask_types = {
                name: target
                for name, stored in columns.items()
                if (target := arrow_cast_type(stored)) is not None
            }
            # Re-parsed rather than trusted as already-confined: a row
            # created before the id binding may hold a pattern outside
            # `raw/<dir...>/<glob>`, and this must fail the query rather
            # than silently read it. Not caught here — it propagates to
            # `_resolve_sources_opaquely`, which reports it the same way
            # as any other resolution failure.
            loc = parse_external_location(location_pattern)
            if row.external_claim is None or row.external_claim != loc.claim:
                raise ValidationError(
                    f"External table {dotted!r}'s stored claim does not match its own "
                    "pattern; refusing to read it."
                )
            # Defence in depth: `check_claim_free` is the create-time gate,
            # but a row could in principle claim an overlapping directory
            # by some path that bypassed it (a direct mutation, or a future
            # bug). `row.id` is excluded so the row's own claim does not
            # conflict with itself.
            check_claim_free(session, environment_id, loc.claim, exclude_table_id=row.id)
            # `Storage.list` may be recursive; `loc.matches` excludes any
            # path outside `loc.claim` itself, so a nested file under the
            # claim is never read even if the backend's listing is.
            paths = [
                root_prefix + relative
                for relative in resolved_storage.storage.list(loc.claim)
                if loc.matches(relative)
            ]
            readable = loc.claim
        else:
            # By the row's id, never by a name: the result's `table_name` goes stale on a
            # rename, and a table built again under the name is another table.
            loaded_at = (
                catalog.load_table_at_check(principal, ref, check_result_id=checked.id)
                if checked is not None and row.id == checked.table_id
                else None
            )
            if loaded_at is None:
                loaded = catalog.load_table(principal, ref)
                metadata = loaded.metadata
                snapshot_id = metadata.current_snapshot_id
                fields = metadata.schema().fields
            else:
                read_at_check = True
                metadata = loaded_at.metadata
                snapshot_id = loaded_at.snapshot_id
                # The checked snapshot's own schema: a rollback sets the table's schema
                # back, so the current one can lack a column the snapshot's files have.
                fields = loaded_at.columns
            iceberg_metadata = metadata
            table_location = metadata.location
            # Fail closed: a
            # table whose own location is not the id-suffixed leaf
            # `ResolvedStorage.table_location` assigns at creation predates
            # this confinement scheme (or `confine_to_root`, its
            # predecessor) and is refused outright, before any file is
            # listed. This replaces trying to complete the "other table
            # locations" set well enough for the most-specific-owner rule
            # alone to defend a legacy table's over-broad location — that
            # set structurally can never include an `EXTERNAL` table's
            # `raw/` claim or a dropped table's orphaned files, so a legacy
            # table's claim over either would have gone uncaught. The path
            # is never in the message.
            if not is_id_suffixed_location(table_location, row.id):
                raise ValidationError(
                    f"Table {dotted!r} was created by an older Lakana, with a location that "
                    "does not end in its id; re-create it."
                )
            # The query process may open this location and nothing else of the store, so it must
            # lie under the environment's root; a location committed outside it is refused, never
            # turned into a prefix on another host.
            if not table_location.startswith(root_prefix):
                raise ValidationError(
                    f"Table {dotted!r} has a location outside its environment's storage; "
                    "refusing to read it."
                )
            readable = table_location.removeprefix(root_prefix)
            # Precomputed once per table, not once per file: the set of other tables' locations
            # nested inside
            # this one does not depend on which file is being checked.
            nested_locations = nested_locations_within(table_location, other_table_locations)
            # Checked before either the manifest list or any manifest is
            # read: a manifest is Iceberg's own
            # metadata, not the catalog's, and `commit_table`'s own check on
            # `AddSnapshotUpdate.snapshot.manifest_list` is shallow (the
            # pointer only, not what it leads to) and does not run at all
            # for a row that predates it or was altered outside
            # `commit_table`. `functools.partial` rather than a lambda: a
            # lambda capturing `table_location` here would close over the loop
            # variable (bugbear B023), even though it is only ever called
            # before it is reassigned.
            within_table = functools.partial(is_within_location, table_location)
            if snapshot_id is None:
                paths = []
            else:
                try:
                    paths = (
                        list_data_files(
                            metadata, resolved_storage.file_io, is_valid_path=within_table
                        )
                        if loaded_at is None
                        else loaded_at.data_files(
                            resolved_storage.file_io, is_valid_path=within_table
                        )
                    )
                except UnsupportedTableError as exc:
                    # Re-raised with the table name: `list_data_files`'s own
                    # message is path-free by design (it has no table
                    # identity to name), but naming the table here is safe
                    # -- it is the dotted reference the submitter already
                    # used in their own query -- and is what makes the
                    # failure legible in `error_message`
                    # (`_resolve_sources_opaquely`'s narrow pass-through for
                    # this exception, below).
                    raise UnsupportedTableError(
                        f"Table {dotted!r} has row-level deletes (Iceberg delete files), "
                        "which Lakana's query engine doesn't apply yet."
                    ) from exc
            # A namespace coincidentally named after another table's id
            # suffix can still nest one suffixed table's directory inside
            # another's — the most specific owner wins.
            for path in paths:
                if not is_owned_by_location_given_nested(table_location, path, nested_locations):
                    raise ValidationError(f"Table {dotted!r} references data outside its location.")
            file_format = "parquet"
            columns = {column.name: str(column.field_type) for column in fields}
            if checked is not None and loaded_at is not None:
                # A read at a checked snapshot shows only the columns the table has NOW,
                # under the same name, type and field id, to every reader. Row filters and column
                # masks are set on the table as it is now: a mask can only be put on a
                # column the current schema has. A column that only the snapshot has (the
                # failed run added it and the rollback set the schema back; or a later run
                # dropped or retyped it) can be masked for nobody, and a mask that once
                # covered it may have been revoked since because it broke ordinary queries.
                # So nothing is shown column-wise that the reader could not read in the
                # table today. The field id is part of "the same column": two columns
                # renamed into each other keep names and types. The rule does not depend
                # on the reader's policies: it would otherwise change when a stale mask is
                # revoked. The source is marked so that the rewrite reads the files through
                # this list and never with `*`.
                ids_now = {f.name.lower(): f.field_id for f in metadata.schema().fields}
                at_check = (
                    checked,
                    frozenset(
                        column.name.lower()
                        for column in fields
                        if ids_now.get(column.name.lower()) == column.field_id
                    ),
                )
                columns, not_shown = _columns_the_table_still_has(fields, metadata.schema().fields)
                if not columns:
                    raise _RowsNotListed(
                        no_column_left_message(dotted, checked.run_id, environment_name)
                    )
                pinned = dotted
                left_out = {
                    column: column_not_shown_message(
                        column, dotted, checked.run_id, environment_name
                    )
                    for column in not_shown
                }
                declared_columns_only = True
            # The read projects every file onto the schema it uses, by field id, so a column
            # renamed, re-added or widened since a file was written reads as that schema says.
            read = [column for column in fields if column.name in columns]
            field_ids = read_fields(dotted, read)
            mask_types = {
                column.name: target
                for column in read
                if (target := iceberg_cast_type(column.field_type)) is not None
            }
            if snapshot_id is not None:
                refuse_changed_nested_fields(dotted, metadata, snapshot_id, read)
            if pins is not None:
                pins[ref] = SnapshotPin(row.id, snapshot_id)
        urls = [
            resolved_storage.storage.resolve_read_url(
                path.removeprefix(root_prefix), ttl_seconds=_PRESIGN_TTL_SECONDS
            )
            for path in paths
        ]
        if prefixes is not None:
            prefixes.append(resolved_storage.storage.read_url_prefix(readable))
        resolved_policy = policy_resolver.resolve(principal, ref, table_id=row.id)
        column_masks = resolved_policy.column_masks
        if iceberg_metadata is not None:
            # A policy binds to a column name and the files are read by field id, so a rename
            # since the policy was written can leave it on another column, with no error. That
            # holds for a read at a checked snapshot as well. Nothing is read, and a policy is
            # never left out.
            renamed = renamed_policy_column(iceberg_metadata, resolved_policy.policies)
            if renamed is not None:
                raise PolicyColumnRenamedError(policy_column_renamed_message(renamed, dotted))
        if at_check is not None:
            # Policies are written for the table as it is now; this source is an older
            # snapshot of it, reduced to the columns the table still has.
            result_read, same_columns = at_check
            # A mask on a column that is not among the shown ones has nothing to cover: the
            # column was added after the snapshot, or is withheld. It is skipped for this
            # source, where the rewrite would refuse the whole read for it. A mask whose
            # name differs from a shown column only in case is NOT skipped: it is meant for
            # that column, and the rewrite's refusal stands rather than the column in clear.
            shown_names = {column.lower() for column in columns}
            column_masks = {
                column: mask
                for column, mask in column_masks.items()
                if column.lower() in shown_names
            }
            # A row filter and the expression of an applied mask name columns, and mean the
            # table's columns of today. Each column they read must be the same column in
            # the snapshot, by field id: one that was added or dropped since is missing on
            # one side, and one that was dropped and added again, or renamed into another,
            # holds other data under the old name, so the filter would pass the wrong rows
            # and the mask would be built from the wrong values. Then nothing is shown; a
            # policy is never left out. Said here, before the query runs: in the database a
            # missing column would be a "column not found" that suggests columns the reader
            # is not shown, and a column that is merely another one would not fail at all.
            # A column that has the same field id but is not shown (its type changed) is not
            # mapped by the read, so a policy on it is told the same way, not as a column of the
            # reader's own SQL.
            not_the_same = sorted(
                column
                for expression in (*resolved_policy.row_filters, *column_masks.values())
                for column in columns_of(expression)
                if column.lower() not in same_columns or column.lower() not in shown_names
            )
            if not_the_same:
                raise _RowsNotListed(
                    policy_needs_a_column_message(
                        not_the_same[0], dotted, result_read.run_id, environment_name
                    )
                )
        sources[dotted] = TableSource(
            urls=urls,
            file_format=file_format,
            columns=columns,
            row_filters=resolved_policy.row_filters,
            column_masks=column_masks,
            declared_columns_only=declared_columns_only,
            field_ids=field_ids,
            mask_types=mask_types,
            typed_masks=resolved_policy.typed_masks,
        )
    if checked is not None and not read_at_check:
        # Every table the query names was resolved (and its read authorized), and none is
        # the table the result names: that table was dropped, and the name the check's SQL
        # uses leads to another table now, which never had the checked snapshot. Running
        # the query against today's tables would show rows the check never saw.
        raise CheckedSnapshotGoneError(
            checked_snapshot_gone_message(checked.table_name, checked.run_id, environment_name)
        )
    if withheld is not None:
        # The reason for a failed query is found by the bare column name in the database's
        # "column not found". A name that another table of the query has too may be about
        # that table, so it gets no reason of ours and the database's message stands. Names
        # are compared without regard to case, as the database compares them.
        elsewhere = {
            column.lower()
            for dotted, source in sources.items()
            if dotted != pinned
            for column in source.columns
        }
        withheld.update(
            (column, text) for column, text in left_out.items() if column.lower() not in elsewhere
        )
    return sources


# `_resolve_sources_opaquely` wraps `_resolve_sources` and takes the same parameters.
# jscpd:ignore-start
def _resolve_sources_opaquely(
    session: Session,
    environment_name: str,
    principal: PrincipalRef,
    dotted_refs: list[str],
    authz: AuthorizationService,
    key_provider: KeyProvider | None = None,
    pins: dict[TableRef, SnapshotPin] | None = None,
    *,
    check_result_id: uuid.UUID | None = None,
    withheld: dict[str, str] | None = None,
    prefixes: list[str] | None = None,
) -> dict[str, TableSource]:
    # jscpd:ignore-end
    """Call `_resolve_sources`, replacing any failure's message with a safe one.

    Everything this stage touches knows a resolved storage location — a
    presigned URL, a path on the worker's own disk — and says so in its
    error text. None of that may reach `error_message`, and unlike the
    execution stage there is no resolved-location list to redact against
    yet, precisely because resolution is what failed. `UnsupportedTableError`
    is the one narrow exception to the opaque-message rule: its
    message is built entirely by `_resolve_sources` itself, out of the
    dotted table name the submitter's own query already named, so it is
    passed through unmodified instead of being folded into
    `_ResolutionFailed` — otherwise the query would fail with a message that
    gives no hint the query engine, not the query, is at fault. `NotPublishedError`
    passes through for the same reason, and is safe for a second one: the catalog raises
    it only after the read grant on that table passed, and its text is built from the
    table's dotted name (the one in the query, or through a view the one in the stored
    view SQL, which this reader may read the table by), the short name of a failed check
    and a run id, so a principal who may not read the table gets the opaque message and
    learns nothing about publication. `KeptAfterCheckError` passes through on the
    same two grounds: raised after the read grant, and built from the table's dotted name, the
    name of a failed check, a run id and the environment's name. The three refusals of a read
    at a check (`CheckedSnapshotGoneError`, `CheckResultGoneError`,
    `NoCheckedSnapshotError`) pass through as well. From the catalog they are raised after the
    read grant on the table, and they name a run only once the result is known to be that
    table's own. From `_resolve_sources` itself they come only for a query bound to a check
    result, which is queued solely for a caller who was allowed to read that result's run, and
    their texts hold no more than the run, the table's name as that result stores it and the
    environment. `_RowsNotListed` passes through on those grounds too: raised only for the table
    read at a check, after `load_table_at_check` authorized it, with a text of the same three
    names. Every other exception is still made opaque exactly as before.

    Parameters
    ----------
    session
        Database session.
    environment_name
        The environment the query runs against.
    principal
        The submitting principal.
    dotted_refs
        Dotted table names from `extract_table_refs`.
    authz
        Authorization service to check each table against.
    key_provider
        Decrypts a secret-backed environment's storage credentials, if this
        environment's storage_secret_ref requires one. None otherwise.
    pins
        Passed through to `_resolve_sources`.
    check_result_id
        Passed through to `_resolve_sources`.
    withheld
        Passed through to `_resolve_sources`.
    prefixes
        Passed through to `_resolve_sources`.

    Returns
    -------
    dict
        Exactly what `_resolve_sources` returned.

    Raises
    ------
    UnsupportedTableError
        If `_resolve_sources` raised it — passed through verbatim.
    NotPublishedError
        If `_resolve_sources` raised it — passed through verbatim, the second
        exception this function does not make opaque.
    KeptAfterCheckError
        If `_resolve_sources` raised it — passed through verbatim, the third.
    CheckedSnapshotGoneError
        If `_resolve_sources` raised it — passed through verbatim.
    CheckResultGoneError
        If `_resolve_sources` raised it — passed through verbatim.
    NoCheckedSnapshotError
        If `_resolve_sources` raised it — passed through verbatim.
    _RowsNotListed
        If `_resolve_sources` raised it — passed through verbatim.
    PolicyColumnRenamedError
        If `_resolve_sources` raised it — passed through verbatim.
    _ResolutionFailed
        If `_resolve_sources` raised anything else. The original exception
        is the `__cause__`; only its class name survives into the message, except for a
        storage that did not answer (`StorageUnavailableError`, or an `OSError` that
        `is_storage_unavailable` accepts), which gets `TABLE_STORAGE_UNREACHABLE`: the name
        of the environment the query runs against and what to do next.
    """
    try:
        return _resolve_sources(
            session,
            environment_name,
            principal,
            dotted_refs,
            authz,
            key_provider,
            pins,
            check_result_id=check_result_id,
            withheld=withheld,
            prefixes=prefixes,
        )
    except (
        UnsupportedTableError,
        NotPublishedError,
        KeptAfterCheckError,
        CheckedSnapshotGoneError,
        CheckResultGoneError,
        NoCheckedSnapshotError,
        _RowsNotListed,
        PolicyColumnRenamedError,
    ):
        raise
    except Exception as exc:
        if isinstance(exc, StorageUnavailableError) or is_storage_unavailable(exc):
            if not isinstance(exc, StorageUnavailableError):  # the catalog logged that one
                logger.warning(
                    "The storage of environment %s could not be reached while the tables of a "
                    "query were resolved (%s).",
                    shown(environment_name),
                    type(exc).__name__,
                    extra=log_event(LogEvent.STORAGE_UNREACHABLE),
                )
            raise _ResolutionFailed(
                TABLE_STORAGE_UNREACHABLE.format(environment=shown(environment_name))
            ) from exc
        raise _ResolutionFailed(
            f"Resolving the tables this query reads failed ({type(exc).__name__})."
        ) from exc


def _resolve_storage_opaquely(
    session: Session, environment_name: str, key_provider: KeyProvider | None
) -> ResolvedStorage:
    """Resolve an environment's storage, replacing any failure's message with a safe one.

    `run_one` calls this before anything else, so none of the credentials
    it decrypts is on the redaction list yet if it fails; only the
    exception's class name survives, as for any resolution failure.

    Parameters
    ----------
    session
        Database session.
    environment_name
        The environment.
    key_provider
        Decrypts a secret-backed environment's storage credentials.

    Returns
    -------
    ResolvedStorage
        The resolved storage.

    Raises
    ------
    _ResolutionFailed
        If resolution failed for any reason; the original is the `__cause__`.
    """
    try:
        return StorageResolver(session, key_provider).for_environment(environment_name)
    except Exception as exc:
        raise _ResolutionFailed(
            f"Resolving this environment's storage failed ({type(exc).__name__})."
        ) from exc


def _run_context(run: QueryRun, task_scope: TaskScope | None) -> RunContext | None:
    """Build the run context a query run executes under.

    Parameters
    ----------
    run
        The query run.
    task_scope
        Its re-resolved task-token scope, if it was submitted with one.

    Returns
    -------
    RunContext or None
        The claim form for a run submitted with a bound task token (a Python
        task's own query); the query-run form for a run the worker inserted
        for a task itself (a SQL task's write run, and the count of a check,
        which is a read and never reaches the write guard); `None` otherwise.
    """
    if task_scope is not None and task_scope.bound is not None:
        return RunContext(task_run_id=task_scope.bound.task_run_id, claim=task_scope.bound.claim)
    if run.task_run_id is not None and run.task_token_id is None:
        return RunContext(task_run_id=run.task_run_id, query_run_id=run.id)
    return None


def _task_scope_for_run(session: Session, run: QueryRun) -> TaskScope | Literal["invalid"] | None:
    """Resolve the task-token scope, if any, a queued run must be authorized under.

    Carrying `task_scope` into the worker's own `PrincipalRef` is what lets
    `GrantAuthorization` apply a task token's declared asset scope to each
    table this run references — the same narrowing already applied at
    submission time (`QueryService.submit_query`'s own `SUBMIT_QUERY`
    check) must also apply here, since the tables a query touches are only
    known once the worker parses it. The actual validity check —
    missing/revoked/expired token, or an inactive principal — lives in
    `lakana_core.identity.task_tokens.resolve_task_token_scope`, shared with
    `TaskTokenService.verify` so the two never drift apart; this function
    only adapts that shared result to the `None`/`"invalid"`/`TaskScope`
    contract `run_one` expects, and adds the one check that only makes
    sense from this side: `run.task_token_id` has no foreign key (migration
    `0011`), so it additionally requires the token's own
    `principal_id` to still match `run.principal_id`.

    Parameters
    ----------
    session
        Database session.
    run
        The query run about to be started.

    Returns
    -------
    TaskScope, None, or "invalid"
        `None` if `run.task_token_id` is unset — the run was submitted with
        a PAT, so no task scope narrows it. A `TaskScope` if
        `resolve_task_token_scope` finds the token still usable and its
        principal still matches `run.principal_id`. The literal string
        `"invalid"` otherwise — the token row is missing, revoked, expired,
        its principal has been deactivated, or its principal no longer
        matches the run's own.
    """
    if run.task_token_id is None:
        return None
    scope = resolve_task_token_scope(
        session, run.task_token_id, expected_principal_id=run.principal_id
    )
    return "invalid" if scope is None else scope


@dataclass(frozen=True)
class _Prepared:
    """A query authorized, resolved and rewritten, ready to execute or write.

    Parameters
    ----------
    rewritten
        The rewritten SQL and the variables it reads.
    views
        The dotted names of the views expanded into it, innermost first
        ; empty for a query that reads no view.
    pins
        The snapshot each base `ICEBERG` table it reads resolved to, directly
        or through a view — what a write reading its own target commits
        against.
    restricted
        The dotted base tables on which a row filter or a column mask of the reader applied.
    withheld
        For a query bound to a check result: the columns of the checked snapshot that were
        left out of the read and that no other table of the query has under their name, each
        with the text that says so.
    tables
        Each table the query reads, by dotted name, with the lower-cased names of the columns
        the read declares for it: what a failure that names only a column is traced to a
        table by (raw-zone tables only: an Iceberg table is read by field id, and its files
        never lack a column).
    projected
        The tables among them that are read through an explicit column list, because a row
        filter, a column mask or a read at a checked snapshot applies to them.
    named
        The lower-cased names of the columns the reader's SQL names, with its views expanded.
    read_prefixes
        The read prefix of every table the query reads, sorted and without duplicates: the
        locations the query process may open (`execute_query`'s `read_prefixes`).
    """

    rewritten: RewrittenQuery
    views: tuple[str, ...] = ()
    pins: Mapping[TableRef, SnapshotPin] = field(default_factory=dict)
    restricted: frozenset[str] = frozenset()
    withheld: Mapping[str, str] = field(default_factory=dict)
    tables: Mapping[str, frozenset[str]] = field(default_factory=dict)
    projected: frozenset[str] = frozenset()
    named: frozenset[str] = frozenset()
    read_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadOutcome:
    """What a read run hands to the worker instead of storing a result file.

    Parameters
    ----------
    table
        The query's result.
    snapshots
        Dotted base table to the snapshot the query read (`None` for a table without one),
        for every `ICEBERG` table read directly or through a view.
    restricted
        The dotted base tables on which a row filter or a column mask of the reader applied.
    """

    table: pa.Table
    snapshots: Mapping[str, int | None]
    restricted: frozenset[str]


def _view_lookup(
    catalog: CatalogService, principal: PrincipalRef, environment_name: str
) -> Callable[[str], ViewDefinition | None]:
    """Build the lookup `expand_views` uses, authorizing READ on each view it finds.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The reader.
    environment_name
        The environment.

    Returns
    -------
    callable
        `dotted -> ViewDefinition | None`; a name that isn't a dotted table
        name is not a view.
    """

    def lookup(dotted: str) -> ViewDefinition | None:
        try:
            ref = TableRef.from_dotted(environment_name, dotted)
        except ValidationError:
            return None
        return catalog.find_view(principal, ref)

    return lookup


def _expand_views_opaquely(
    catalog: CatalogService,
    principal: PrincipalRef,
    environment_name: str,
    sql: str,
    root: str | None,
) -> ExpandedQuery:
    """Expand a query's views, reporting a denied view like a denied table.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The reader.
    environment_name
        The environment.
    sql
        The query.
    root
        The view being created, if any.

    Returns
    -------
    ExpandedQuery
        The expanded query.

    Raises
    ------
    _ResolutionFailed
        If `principal` may not read a referenced view — the same opaque
        message a denied table gets, so a view's existence is revealed no
        more than a table's.
    ValidationError
        If a view is stale, views form a cycle or nest too deep. Those
        messages carry only names the query or the view definition used.
    """
    try:
        return expand_views(sql, _view_lookup(catalog, principal, environment_name), root=root)
    except PermissionDenied as exc:
        raise _ResolutionFailed(
            "Resolving the tables this query reads failed (PermissionDenied)."
        ) from exc


def _raise_expanded_text_failure(expanded: ExpandedQuery, exc: Exception) -> NoReturn:
    """Raise for a failed re-parse of an expanded statement.

    The statement holds the SQL of every view expanded into it, which the reader may
    not see; their own text was parsed first and was fine.

    Parameters
    ----------
    expanded
        The expanded statement.
    exc
        The syntax error its re-parse raised.

    Raises
    ------
    ValidationError
        Naming the outermost expanded view (one the reader's own query named) and none of
        its text, when the statement contains a view.
    Exception
        `exc` itself when it contains none.
    """
    if not expanded.views:
        raise exc
    raise ValidationError(unreadable_view_message(expanded.views[-1])) from exc


def _resolve_and_rewrite(
    session: Session,
    environment_name: str,
    principal: PrincipalRef,
    sql: str,
    authz: AuthorizationService,
    key_provider: KeyProvider | None,
    locations: list[str],
    *,
    root: str | None = None,
    check_result_id: uuid.UUID | None = None,
) -> _Prepared:
    """Expand views, then authorize and resolve every base table, and rewrite.

    Views are expanded first, so resolution and the policy
    rewrite only ever see base tables and a view can't widen access.

    Parameters
    ----------
    session
        Database session.
    environment_name
        The environment the query runs against.
    principal
        The submitting principal.
    sql
        The already-blacklist-checked query.
    authz
        Authorization for every check.
    key_provider
        Decrypts a secret-backed environment's storage credentials.
    locations
        The run's redaction list; every resolved URL is added as soon as
        it is known.
    root
        The view being created, if this is a `mode: view` run.
    check_result_id
        The check result whose rows the query shows (`QueryRun.check_result_id`); `None` for
        every other query. See `_resolve_sources`.

    Returns
    -------
    _Prepared
        The rewritten query and the views expanded into it.

    Raises
    ------
    _ResolutionFailed
        As `_expand_views_opaquely` and `_resolve_sources_opaquely` raise it.
    UnsupportedTableError
        As `_resolve_sources_opaquely` passes it through.
    CheckedSnapshotGoneError
        As `_resolve_sources_opaquely` passes it through.
    CheckResultGoneError
        As `_resolve_sources_opaquely` passes it through.
    NoCheckedSnapshotError
        As `_resolve_sources_opaquely` passes it through.
    ValidationError
        If expansion or rewriting fails.
    """
    catalog = CatalogService(session, StorageResolver(session, key_provider), authz)
    expanded = _expand_views_opaquely(catalog, principal, environment_name, sql, root)
    try:
        read_refs = extract_table_refs(expanded.sql)
    except SQL_SYNTAX_ERRORS as exc:
        _raise_expanded_text_failure(expanded, exc)
    pins: dict[TableRef, SnapshotPin] = {}
    withheld: dict[str, str] = {}
    prefixes: list[str] = []
    sources = _resolve_sources_opaquely(
        session,
        environment_name,
        principal,
        read_refs,
        authz,
        key_provider,
        pins,
        check_result_id=check_result_id,
        withheld=withheld,
        prefixes=prefixes,
    )
    locations.extend(url for source in sources.values() for url in source.urls)
    locations.extend(prefixes)
    try:
        rewritten = rewrite_query(expanded.sql, sources)
    except SQL_SYNTAX_ERRORS as exc:
        _raise_expanded_text_failure(expanded, exc)
    restricted = frozenset(
        name for name, source in sources.items() if source.row_filters or source.column_masks
    )
    return _Prepared(
        rewritten=rewritten,
        views=expanded.views,
        pins=pins,
        restricted=restricted,
        withheld=withheld,
        tables={
            name: frozenset(column.lower() for column in source.columns)
            for name, source in sources.items()
            if not source.field_ids
        },
        projected=frozenset(
            name
            for name, source in sources.items()
            if not source.field_ids
            and (source.row_filters or source.column_masks or source.declared_columns_only)
        ),
        named=named_columns(expanded.sql),
        read_prefixes=tuple(sorted(set(prefixes))),
    )


_REFERENCED_COLUMN = re.compile(
    r'Binder Error: (?:Referenced column "([^"]+)" not found'
    r'|[A-Za-z ]+ "[^"]*" does not have a column named "([^"]+)")'
)
"""DuckDB's words for a column a query names that its relation does not have.

The first form is a bare column, the second a column named through a table alias (`o.note`;
DuckDB calls the aliased relation a table or a values list). Only the referenced column is read
from it. The first form goes on to suggest other columns ("Candidate bindings"), which are not
the cause.
"""


def _withheld_column_failure(
    exc: LakanaError, withheld: Mapping[str, str]
) -> ValidationError | None:
    """Explain a failed query that named a column withheld from the read at a check.

    The read at a checked snapshot leaves out the columns the table does not have now (see
    `_resolve_sources`). A check whose SQL names such a column then fails in the database
    with "column not found", which says nothing of the reason. The database's wording is
    all there is to tell it by; a failure worded otherwise keeps its own message. The column
    may be written in another letter case or through a table alias.

    Parameters
    ----------
    exc
        What failed the query.
    withheld
        Each column left out of the read that no other table of the query has under its
        name, with the text that says so; empty for every query that is not bound to a check
        result.

    Returns
    -------
    ValidationError or None
        The reason, when the column the database could not find is a withheld one; `None`
        when the query failed for another reason, also when a withheld column is merely
        among the columns the database suggests.
    """
    found = _REFERENCED_COLUMN.search(str(exc))
    if found is None:
        return None
    # The database compares names without regard to case, so a query that spells the column
    # another way ("NOTE") is still about the withheld one.
    named = (found.group(1) or found.group(2)).lower()
    reason = next((text for column, text in withheld.items() if column.lower() == named), None)
    return None if reason is None else ValidationError(reason)


_FILES_DIFFER = re.compile(r'schema mismatch in glob: column "([^"]+)"')
"""DuckDB's words for a read of several files that do not all have one of the columns.

Only the column is read from it. The same error goes on to list the columns of one file
("Candidate names"), which are not the caller's to see.
"""


def _without_candidates(text: str) -> str:
    """Cut the database's list of candidate names or bindings out of an error text.

    The list names columns of the relation or of a file that the caller may not see.

    Parameters
    ----------
    text
        The error text.

    Returns
    -------
    str
        The text up to the line that starts the list.
    """
    for marker in ("\nCandidate bindings:", "\nCandidate names:", "\nCandidate tables:"):
        text = text.split(marker, 1)[0]
    return text


def _schema_mismatch_failure(
    exc: LakanaError,
    tables: Mapping[str, frozenset[str]],
    projected: frozenset[str],
    named: frozenset[str],
) -> ValidationError | None:
    """Explain a read that failed because the files of a raw-zone table lack a column it declares.

    An Iceberg table is read by field id and never meets this; `_resolve_and_rewrite` leaves it
    out of `tables`.

    A column added to a table is not in the files written before. The database reads the files
    of one read together and fails at the first that lacks a column the read names, in either of
    two words: the files of a glob differ, or the column is "not found" in the one file it binds
    against. A read under a row filter or column mask (and the read of a checked snapshot)
    selects every declared column of the table, so after such a change every reader under any
    policy of that table fails, whichever column the policy names. The read fails closed, but
    both texts list the columns of a file, which can include a column the caller may not see;
    the text here names the table and the column and nothing of the files.

    A reader who did not name the column cannot have caused the failure, so the text blames
    the files. The first wording comes from the files themselves, so every table that declares
    the column is named; the second is explained only for a table read through an explicit
    column list (`projected`), whose generated read asks for every declared column.

    A reader who named the column (`named`, from their SQL with views expanded) may have made a
    mistake of their own, or may be reading the column that was added, the common case. Either
    wording, and so either order of the files, then gives the database's line without its list
    of candidates, followed by a hint that names the projected tables that declare the column
    and says their older files lack it.

    Parameters
    ----------
    exc
        What failed the query.
    tables
        Each table the query reads with the lower-cased names of its declared columns.
    projected
        The tables among them that are read through an explicit column list.
    named
        The lower-cased names of every column the reader's SQL, with its views expanded,
        names.

    Returns
    -------
    ValidationError or None
        The reason; `None` when the failure is worded otherwise, or when no table of the query
        can be said to lack the column, so the database's own message stands. A column named
        through a table alias (`o.tier`) is worded by the database otherwise and also stays.
    """
    text = str(exc)
    glob = _FILES_DIFFER.search(text)
    found = glob or _REFERENCED_COLUMN.search(text)
    column_name = None if found is None else found.group(1)
    if column_name is None:
        return None
    column = column_name.lower()
    owners = [
        f"`{name}`"
        for name, columns in tables.items()
        if column in columns and (glob is not None or name in projected)
    ]
    if not owners:
        return None
    declaring = [f"`{name}`" for name in tables if column in tables[name] and name in projected]
    if column in named and declaring:
        # The reader names the column, so their own SQL may be what is wrong: the database's
        # line stands, in either wording and so in either order of the files, and the hint
        # names the tables whose generated read declares the column.
        line = (
            _without_candidates(text)
            if glob is None
            else f'Query failed: Binder Error: Referenced column "{column_name}" not found in '
            "FROM clause!"
        )
        return ValidationError(
            f"{line} {' and '.join(declaring)} declares the column `{column}`; if the query "
            "reads it from there, older data files of the table lack it (it was added after "
            "some or all of them were written). Ask the owner of the table to write it again, "
            "so that every file has the column."
        )
    return ValidationError(
        f"The data files of {' and '.join(owners)} do not all have the column `{column}` (it was "
        "added after some or all of them were written). A read under a row filter or column "
        "mask selects every column of the table, so it fails on them whichever column the "
        "policy names. Ask the owner of the table to write it again, so that every file has "
        "the column."
    )


_STOPPED = (QueryCancelRequested, QueryCancelled, QueryTimeout, OutOfMemoryQuery, WorkerStaleQuery)
"""Failures that say the worker stopped the query: nothing a vanished snapshot explains."""


def _snapshot_gone_after_failure(
    session: Session,
    run: QueryRun,
    environment_name: str,
    principal: PrincipalRef,
    dotted_refs: list[str],
    authz: AuthorizationService,
    key_provider: KeyProvider | None,
    failure: BaseException,
) -> CheckedSnapshotGoneError | None:
    """Tell whether a failed read of a checked snapshot failed because the snapshot is gone.

    Maintenance removes the snapshot of a failed check after seven days. A query that resolved
    the snapshot just before can then fail while it reads, or while it resolves, with whatever
    the storage or the metadata says, and no part of that text says the snapshot is gone. The
    text is not read for it: the catalog is asked again, as the query asked it, after the
    failure. The second look lists the snapshot's manifests once more, which doubles the time a
    failed read takes when the storage itself is down; that is accepted for a read a person
    asked for. Only an answer that the snapshot is gone replaces the failure; any other answer,
    including a refusal or another error, leaves the failure as it was, so a storage fault
    that has nothing to do with the snapshot is never reported as one.

    Parameters
    ----------
    session
        Database session; read only here.
    run
        The failed query run.
    environment_name
        The environment the query ran against.
    principal
        The submitting principal.
    dotted_refs
        The tables the query names; empty when it failed before they were known.
    authz
        Authorization service the query ran under.
    key_provider
        Decrypts a secret-backed environment's storage credentials, if any.
    failure
        What failed the query.

    Returns
    -------
    CheckedSnapshotGoneError or None
        The refusal that says the snapshot is gone; `None` for a query that is not bound to a
        check result, for a failure that is a stop of the worker or already a refusal about
        the check, and when the snapshot can still be read. Also `None` while the worker is
        stopping: the run's outcome is rolled back then, so there is nothing to explain.
    """
    if (
        ACTIVITY.stopping
        or run.check_result_id is None
        or not dotted_refs
        or isinstance(failure, (*_STOPPED, CheckedSnapshotGoneError, CheckResultGoneError))
    ):
        return None
    try:
        _resolve_sources(
            session,
            environment_name,
            principal,
            dotted_refs,
            authz,
            key_provider,
            check_result_id=run.check_result_id,
        )
    except CheckedSnapshotGoneError as gone:
        return gone
    except Exception:  # any other answer leaves the original failure standing
        return None
    return None


class _ArrowSink:
    """Give pyarrow a file-like object that forwards what it writes to a storage stream.

    Parameters
    ----------
    stream
        The open stream.
    """

    def __init__(self, stream: StreamWriter) -> None:
        self._stream = stream
        self.closed = False

    def write(self, data: bytes | memoryview) -> int:
        """Pass a piece on.

        Parameters
        ----------
        data
            The bytes pyarrow wrote.

        Returns
        -------
        int
            How many bytes were taken.
        """
        piece = bytes(data)
        self._stream.write(piece)
        return len(piece)

    def flush(self) -> None:
        """Do nothing: the stream buffers on its own."""


def _store_result(storage: Storage, path: str, result: pa.Table) -> None:
    """Write a query result to `path` as an Arrow IPC file, piece by piece.

    The table is serialised straight into a storage stream, so the bytes of the file never
    exist in memory a second time (they sat outside DuckDB's ``memory_limit``, about once more
    than `max_result_bytes` for a bytes object plus its copy). Nothing is visible at `path` until
    the whole file is written; a failure discards what was sent.

    Parameters
    ----------
    storage
        The environment's storage.
    path
        Root-relative path of the result.
    result
        The rows.
    """
    stream = storage.open_write(path)
    try:
        sink = _ArrowSink(stream)
        with pyarrow.ipc.new_file(pa.PythonFile(sink, mode="w"), result.schema) as writer:
            writer.write_table(result)
        stream.commit()
    except BaseException:
        stream.abort()
        raise


def _create_view(
    session: Session,
    run: QueryRun,
    environment_name: str,
    principal: PrincipalRef,
    prepared: _Prepared,
    dotted_refs: list[str],
    authz: AuthorizationService,
    key_provider: KeyProvider | None,
    limits: QueryLimitsConfig,
    *,
    spill: bool,
) -> None:
    """Plan a `mode: view` run's SELECT and store it as a view, without executing it.

    By the time this runs the SQL has passed the AST guard, every view it
    names has been expanded with `root` set (so no cycle), and every base
    table resolved — which authorized READ on everything referenced.

    Parameters
    ----------
    session
        Database session.
    run
        The running view run.
    environment_name
        Its environment.
    principal
        The creator.
    prepared
        The expanded, rewritten SELECT.
    dotted_refs
        The tables and views the SELECT names directly (its references).
    authz
        Authorization.
    key_provider
        Decrypts a secret-backed environment's storage credentials.
    limits
        Supplies `memory_limit`.
    spill
        Whether the query process may spill (local ``file://`` mode), as for `execute_query`.

    Raises
    ------
    LakanaError
        If the run has no target — `submit_write` always sets one.
    """
    if run.target_name is None:  # pragma: no cover
        # `submit_write` sets it in the same insert; kept for type narrowing.
        raise LakanaError("A view run has no target.")
    catalog = CatalogService(session, StorageResolver(session, key_provider), authz)
    schema = describe_query(
        prepared.rewritten.sql,
        prepared.rewritten.parameters,
        read_prefixes=prepared.read_prefixes,
        spill=spill,
        limits=limits,
    )
    references = catalog.resolve_lineage_sources(principal, environment_name, dotted_refs)
    row = catalog.create_or_replace_view(
        principal,
        TableRef.from_dotted(environment_name, run.target_name),
        sql=run.sql_text,
        references=references,
        columns=view_columns(schema),
        query_run_id=run.id,
    )
    run.target_table_id = row.id
    run.rows_written = 0


VIEW_REFUSED_BY_GUARD = (
    "View {view!r} is refused by Lakana's query checks, which may have become stricter since it "
    "was created. Ask its owner to recreate it; the error then says what to change."
)
"""What the view diagnosis says of a stored view that `check_blacklist` now refuses. It quotes
none of the view's text."""


def _diagnose_views(
    session: Session,
    environment_name: str,
    principal: PrincipalRef,
    views: Sequence[str],
    authz: AuthorizationService,
    key_provider: KeyProvider | None,
    limits: QueryLimitsConfig,
    locations: list[str],
    *,
    spill: bool,
) -> ValidationError | None:
    """Find the first expanded view that no longer plans on its own.

    Called only after a query reading views has already failed, so its cost
    lands on the failure path alone.

    Parameters
    ----------
    session
        Database session.
    environment_name
        The environment.
    principal
        The reader.
    views
        The views expanded, innermost first.
    authz
        Authorization.
    key_provider
        Decrypts a secret-backed environment's storage credentials.
    limits
        Supplies `memory_limit`.
    locations
        The run's redaction list, extended with anything this resolves.
    spill
        Whether the query process may spill (local ``file://`` mode), as for `execute_query`.

    Returns
    -------
    ValidationError or None
        One naming the view and its references, or `None` if every view
        plans and the query failed for another reason, or if the diagnosis
        itself was stopped (a cancel, the drain, a limit).
    """
    catalog = CatalogService(session, StorageResolver(session, key_provider), authz)
    for dotted in dict.fromkeys(views):
        view: ViewDefinition | None = None
        try:
            view = catalog.find_view(principal, TableRef.from_dotted(environment_name, dotted))
            if view is None:
                continue
            # A view was checked when it was created; the guard may refuse more since, and
            # nothing reaches DuckDB without passing it. The message quotes none of the view's
            # text: the reader may lack READ on what it references.
            try:
                check_blacklist(view.sql)
            except (ValidationError, *SQL_SYNTAX_ERRORS):
                return ValidationError(VIEW_REFUSED_BY_GUARD.format(view=dotted))
            planned = _resolve_and_rewrite(
                session, environment_name, principal, view.sql, authz, key_provider, locations
            )
            describe_query(
                planned.rewritten.sql,
                planned.rewritten.parameters,
                read_prefixes=planned.read_prefixes,
                spill=spill,
                limits=limits,
            )
        # The describe was stopped itself (a cancel or the drain arrived meanwhile, or a limit
        # was reached): that says nothing about the view, and the query's own failure stands.
        except (QueryCancelled, QueryTimeout, OutOfMemoryQuery, WorkerStaleQuery):
            return None
        # Deliberately broad: anything that stops a view planning on its own
        # is the diagnosis, and nothing here may escape `run_one`.
        except Exception as exc:
            references = ", ".join(view.references) if view is not None else ""
            return ValidationError(
                f"View {dotted!r} no longer plans against its references ({references}): {exc}"
            )
    return None


_clock: Callable[[], float] = time.monotonic
"""The clock the maintenance deadline reads; tests replace it."""

_MAINTENANCE_TIMEOUT: float = DEFAULT_MAINTENANCE_TIMEOUT
"""Seconds a maintenance run may take; set once by `install_maintenance_timeout`."""


def install_maintenance_timeout(seconds: float) -> None:
    """Set how long one maintenance run may take.

    Parameters
    ----------
    seconds
        `LAKANA_RUN__MAINTENANCE_TIMEOUT`.
    """
    global _MAINTENANCE_TIMEOUT
    _MAINTENANCE_TIMEOUT = seconds


_MAX_HISTORY_OPEN_VERSIONS: int = DEFAULT_MAX_HISTORY_OPEN_VERSIONS
"""Most open versions a history write takes on; set once by `install_history_cap`."""


def install_history_cap(count: int) -> None:
    """Set how many open versions a history write may take on.

    Parameters
    ----------
    count
        `LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS`.
    """
    global _MAX_HISTORY_OPEN_VERSIONS
    _MAX_HISTORY_OPEN_VERSIONS = count


def _run_maintenance(
    session: Session,
    run: QueryRun,
    environment_name: str,
    principal: PrincipalRef,
    authz: AuthorizationService,
    key_provider: KeyProvider | None,
) -> None:
    """Execute a maintenance run and store its report on the run.

    Parameters
    ----------
    session
        Database session.
    run
        The running maintenance run.
    environment_name
        Its environment.
    principal
        Its submitter; `CatalogService.maintain` re-authorizes MAINTAIN_TABLE.
    authz
        Authorization.
    key_provider
        Decrypts a secret-backed environment's storage credentials.

    Raises
    ------
    LakanaError
        If the run has no target — `submit_maintenance` always sets one.
    QueryTimeout
        If the run passed `LAKANA_RUN__MAINTENANCE_TIMEOUT`. It stopped at a safe point, its
        report so far is stored on the run, and running maintenance again continues.
    """
    if run.target_name is None:  # pragma: no cover
        # `submit_maintenance` sets it in the same insert; kept for type narrowing.
        raise LakanaError("A maintenance run has no target.")
    request = dict((run.maintenance or {}).get("request", {}))
    catalog = CatalogService(session, StorageResolver(session, key_provider), authz)
    deadline = _clock() + _MAINTENANCE_TIMEOUT
    try:
        report = catalog.maintain(
            principal,
            TableRef.from_dotted(environment_name, run.target_name),
            keep_days=int(request.get("keep_days", DEFAULT_KEEP_DAYS)),
            keep_last=int(request.get("keep_last", DEFAULT_KEEP_LAST)),
            dry_run=bool(request.get("dry_run")),
            older_than_hours=int(request.get("older_than_hours", DEFAULT_ORPHAN_AGE_HOURS)),
            should_stop=lambda: _clock() >= deadline,
        )
    except MaintenanceStopped as stopped:
        run.maintenance = {
            "request": request,
            "report": {**stopped.report.to_json(), "stopped": True},
        }
        raise QueryTimeout(
            f"Maintenance ran past its time limit ({_MAINTENANCE_TIMEOUT:g} s) and stopped at "
            f"a safe point; the table is consistent. Run `lakana table maintain "
            f"{environment_name} {run.target_name}` again to continue where it stopped, or "
            "ask the operator to raise LAKANA_RUN__MAINTENANCE_TIMEOUT."
        ) from stopped
    run.maintenance = {"request": request, "report": report.to_json()}


def _run_write(
    session: Session,
    run: QueryRun,
    environment_name: str,
    principal: PrincipalRef,
    prepared: _Prepared,
    dotted_refs: list[str],
    authz: AuthorizationService,
    key_provider: KeyProvider | None,
    limits: QueryLimitsConfig,
    *,
    spill: bool,
) -> int:
    """Execute a write run: stream the result into its target and record the outcome.

    `CatalogService` re-authorizes the target itself
    (the worker's check, beside submission's): `write_batches` for the
    write, `read_watermark` for an incremental read of the target,
    `resolve_lineage_sources` for each source's lineage edge.

    Parameters
    ----------
    session
        Database session.
    run
        The running write run.
    environment_name
        Its environment.
    principal
        Its submitter.
    prepared
        The rewritten SELECT.
    dotted_refs
        The tables and views the SELECT names, one lineage edge each.
    authz
        Authorization for every check.
    key_provider
        Decrypts a secret-backed environment's storage credentials.
    limits
        Supplies `max_write_rows` and `memory_limit`.
    spill
        Whether the query process may spill (local ``file://`` mode), as for `execute_query`.

    Returns
    -------
    int
        Rows written.

    Raises
    ------
    LakanaError
        If the run has no target — `submit_write` always sets one.
    ValidationError
        If the incremental key is missing from the result, or anything the
        write service refuses.
    PermissionDenied
        If the principal may not write the target; for the first run of an incremental asset,
        which creates it, the message names the right it lacks and the table.
    ConflictError
        If the target changed after the run read it — between resolving it
        and reading its watermark here, or before `write_batches` commits —
        so nothing is written twice or overwritten unread.
    """
    if run.write_mode is None or run.target_name is None:  # pragma: no cover
        # `submit_write` sets both in the same insert; unreachable, kept so
        # a type checker can narrow them.
        raise LakanaError("A write run has no target.")
    catalog = CatalogService(session, StorageResolver(session, key_provider), authz)
    ref = TableRef.from_dotted(environment_name, run.target_name)
    sources = catalog.resolve_lineage_sources(principal, environment_name, dotted_refs)
    sql = prepared.rewritten.sql
    parameters: dict[str, object] = dict(prepared.rewritten.parameters)
    # A write whose rows depend on its target — it reads the target, or
    # filters on the target's watermark — commits only against the snapshot
    # it read; one that doesn't read it stays unpinned.
    pin = prepared.pins.get(ref)
    watermark = None
    # An incremental asset's first run is a create: there is no table to read a
    # watermark from, and every row of the result is new. A merge asset's later runs read it
    # as an append does.
    if run.incremental_key is not None and run.write_mode in (WriteMode.APPEND, WriteMode.MERGE):
        watermark, watermark_pin = catalog.read_watermark(principal, ref, run.incremental_key)
        if pin is not None and pin != watermark_pin:
            raise ConflictError(target_changed_message(ref.dotted()))
        pin = watermark_pin
        if watermark is not None:
            incremental = wrap_incremental(sql, run.incremental_key)
            sql = incremental.sql
            parameters[incremental.parameter] = watermark.value
    encoded = None if watermark is None else watermark.encode()
    with stream_query(
        sql, parameters, read_prefixes=prepared.read_prefixes, spill=spill, limits=limits
    ) as (schema, batches):
        if run.incremental_key is not None and run.incremental_key not in schema.names:
            raise ValidationError(
                f"Incremental key {run.incremental_key!r} is not a column of the result."
            )
        if run.incremental_key is not None and run.write_mode not in (
            WriteMode.APPEND,
            WriteMode.MERGE,
        ):
            # The next run reads the key from the table this run creates: refuse a key it
            # could not use now, rather than leave a table that fails that run.
            check_incremental_key(
                arrow_to_iceberg_schema(schema), run.incremental_key, table_name=ref.dotted()
            )
        try:
            if run.history is not None:
                result = _history_result(
                    catalog,
                    principal,
                    ref,
                    run,
                    schema,
                    batches,
                    sources=dotted_refs,
                    max_rows=limits.max_write_rows,
                    pin=pin,
                )
            elif run.merge_key is not None:
                result = _merge_result(
                    catalog,
                    principal,
                    ref,
                    run,
                    schema,
                    batches,
                    sources=dotted_refs,
                    max_rows=limits.max_write_rows,
                    pin=pin,
                    watermark=encoded,
                )
            else:
                result = catalog.write_batches(
                    principal,
                    ref,
                    mode=run.write_mode,
                    schema=schema,
                    batches=batches,
                    evolve_schema=bool(run.evolve_schema),
                    max_rows=limits.max_write_rows,
                    sources=sources,
                    query_run_id=run.id,
                    watermark=encoded,
                    pin=pin,
                )
        except PermissionDenied as exc:
            first_run = run.write_mode is WriteMode.CREATE and (
                run.incremental_key is not None
                or run.merge_key is not None
                or run.history is not None
            )
            # The guard refuses with the same error for other reasons (a superseded attempt);
            # advice about a right would be false there.
            if not first_run or Action.CREATE_TABLE.value not in str(exc):
                raise
            kind = (
                "a history"
                if run.history is not None
                else "a merge"
                if run.merge_key is not None
                else "an incremental"
            )
            raise PermissionDenied(
                f"{str(exc).rstrip('.')}. The first run of {kind} asset creates "
                f"{ref.dotted()}, which "
                "needs the CREATE_TABLE right on it. Grant it to the run's principal, or create "
                "the table first."
            ) from exc
    run.target_table_id = result.table_id
    run.rows_written = result.rows
    run.snapshot_id = result.snapshot_id
    run.watermark = encoded
    run.schema_diff = None if result.schema_diff.is_empty() else result.schema_diff.to_json()
    return result.rows


def _collect(batches: Iterable[pa.RecordBatch], max_rows: int) -> list[pa.RecordBatch]:
    """Collect a result into memory, refusing more rows than the write may take.

    Parameters
    ----------
    batches
        The result's rows; consumed once.
    max_rows
        `QueryLimitsConfig.max_write_rows`.

    Returns
    -------
    list of pa.RecordBatch
        Every batch.

    Raises
    ------
    ValidationError
        When the result has more than `max_rows` rows.
    """
    collected: list[pa.RecordBatch] = []
    rows = 0
    for batch in batches:
        rows += batch.num_rows
        if rows > max_rows:
            raise ValidationError(max_rows_message(max_rows))
        collected.append(batch)
    return collected


def _merge_result(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    run: QueryRun,
    schema: pa.Schema,
    batches: Iterable[pa.RecordBatch],
    *,
    sources: Sequence[str],
    max_rows: int,
    pin: SnapshotPin | None,
    watermark: str | None,
) -> WriteResult:
    """Merge a SQL asset's result into its table on the run's key (`CatalogService.merge`).

    The rows are collected into one Arrow table, at most `max_rows` of them, because a merge
    compares the whole batch with the table. A `CREATE` run (the asset's first) merges with
    ``expected_snapshot_id="none"``, which creates the table after the same key checks as every
    later merge; a `MERGE` run commits against `pin` when its rows depend on the table.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The run's principal.
    ref
        The target.
    run
        The write run; `merge_key` is set.
    schema
        The result's schema.
    batches
        The result's rows; consumed once.
    sources
        Dotted names of what the SELECT read, one lineage edge each.
    max_rows
        `QueryLimitsConfig.max_write_rows`.
    pin
        The snapshot the rows were computed from, or `None`.
    watermark
        The encoded watermark the SELECT filtered on, or `None`.

    Returns
    -------
    WriteResult
        The table, the rows inserted and updated together, and the snapshot.

    Raises
    ------
    ValidationError
        When the result has more than `max_rows` rows, and anything `merge` refuses.
    LakanaError
        If `merge` named no table, which it always does.
    """
    collected = _collect(batches, max_rows)
    creates = run.write_mode is WriteMode.CREATE
    merged = catalog.merge(
        principal,
        ref,
        pa.Table.from_batches(collected, schema=schema),
        key=run.merge_key or (),
        sources=sources,
        expected_snapshot_id="none" if creates else None,
        pin=None if creates else pin,
        query_run_id=run.id,
        watermark=watermark,
        advice=SQL_ADVICE,
    )
    if merged.table_id is None:  # pragma: no cover - `merge` always names the table
        raise LakanaError("A merge returned no table.")
    return WriteResult(
        table_id=merged.table_id,
        rows=merged.rows_inserted + merged.rows_updated,
        files=0,
        snapshot_id=merged.snapshot_id,
        schema_diff=SchemaDiff(),
    )


def _history_result(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    run: QueryRun,
    schema: pa.Schema,
    batches: Iterable[pa.RecordBatch],
    *,
    sources: Sequence[str],
    max_rows: int,
    pin: SnapshotPin | None,
) -> WriteResult:
    """Keep the versions of a SQL asset's rows in its table (`CatalogService.history`).

    The rows are collected into one Arrow table, at most `max_rows` of them, because a history
    write compares the whole current state with the open versions. A `CREATE` run (the asset's
    first) writes with ``expected_snapshot_id="none"``, which creates the table after the same
    checks as every later run; a `HISTORY` run commits against `pin` when its rows depend on the
    table. A watermark never applies: the deploy refuses one. The table may take on at most
    `LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS` open versions (`install_history_cap`). Whether or not
    the write lands, the heap pages it freed go back to the system (`trim_heap`), beside the
    Arrow pool's, which `history` returns itself.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The run's principal.
    ref
        The target.
    run
        The write run; `history` is set.
    schema
        The result's schema.
    batches
        The result's rows; consumed once.
    sources
        Dotted names of what the SELECT read, one lineage edge each.
    max_rows
        `QueryLimitsConfig.max_write_rows`.
    pin
        The snapshot the rows were computed from, or `None`.

    Returns
    -------
    WriteResult
        The table, the versions opened and closed together, and the snapshot.

    Raises
    ------
    ValidationError
        When the result has more than `max_rows` rows, and anything `history` refuses.
    LakanaError
        If the run's history cannot be read, or `history` named no table; neither happens.
    """
    spec = HistorySpec.from_json(run.history)
    if spec is None:  # pragma: no cover - `_prepare` writes it, `checked_write` read it first
        raise LakanaError("A history run has no readable history.")
    creates = run.write_mode is WriteMode.CREATE
    try:
        kept = catalog.history(
            principal,
            ref,
            pa.Table.from_batches(_collect(batches, max_rows), schema=schema),
            key=spec.key,
            valid_from=spec.valid_from,
            close_missing=spec.close_missing,
            sources=sources,
            expected_snapshot_id="none" if creates else None,
            pin=None if creates else pin,
            query_run_id=run.id,
            max_rows=max_rows,
            max_open_versions=_MAX_HISTORY_OPEN_VERSIONS,
            advice=SQL_ADVICE,
        )
    finally:
        trim_heap()
    if kept.table_id is None:  # pragma: no cover - `history` always names the table
        raise LakanaError("A history write returned no table.")
    return WriteResult(
        table_id=kept.table_id,
        rows=kept.versions_opened + kept.versions_closed,
        files=0,
        snapshot_id=kept.snapshot_id,
        schema_diff=SchemaDiff(),
    )


def _cancel_requested(session: Session, query_run_id: uuid.UUID) -> bool:
    """Read whether a cancel was requested for a running query.

    Called by the cancel watch's thread, about twice a second while the query's child runs, on
    a session that thread opened itself: a `SELECT` there sees what
    `QueryService.cancel_query` committed (Postgres reads committed data per statement; so
    does local mode's SQLite file while the session holds no write). Nothing is flushed, so
    that the function is also safe on a session whose run has an outcome half set.

    Parameters
    ----------
    session
        The session to read on.
    query_run_id
        The query run.

    Returns
    -------
    bool
        Whether `cancel_requested` is set; `False` for a run that does not exist.
    """
    with session.no_autoflush:
        flagged = session.execute(
            select(QueryRun.cancel_requested).where(QueryRun.id == query_run_id)
        ).scalar_one_or_none()
    return bool(flagged)


@contextmanager
def _watching_for_cancel(
    session: Session, query_run_id: uuid.UUID
) -> Generator[Callable[[], bool], None, None]:
    """Watch a run's cancel flag in a thread of its own while the block runs.

    The supervisor of a query's child asks `Stops.cancel_requested` on the thread that reads
    the child's result, where a database read that hangs would hold up the time limit and the
    kill. The answer therefore comes from memory: a small thread opens a session of its own on
    the run's engine (as the lease keeper opens one per renewal), reads the flag every
    `_CANCEL_POLL_SECONDS` and sets an event, which the callable reads. The session is closed
    after every read, so the thread holds no connection between two. The thread ends with the
    block on every path, and as soon as it has seen the flag. A read that fails is logged
    once and retried: an unreadable flag means "not asked". A private in-memory SQLite
    database (tests only) cannot be read from another thread: no thread is started and the
    callable answers `False`.

    Parameters
    ----------
    session
        The run's session; only its engine is used.
    query_run_id
        The query run.

    Yields
    ------
    Callable[[], bool]
        Says whether a cancel was requested; never touches the database.
    """
    bind = session.get_bind()
    url = bind.engine.url
    if url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:"):
        # A private in-memory database lives in one connection of one thread: no other thread
        # (and no other process) can read it, so there is nothing for a thread to watch.
        logger.debug(
            "Query run %s: no cancel watch, the database is private to a thread.", query_run_id
        )
        yield lambda: False
        return
    requested = threading.Event()
    stop = threading.Event()

    def poll() -> None:
        """Read the flag until it is set or the block ends."""
        failed = False
        while not stop.is_set():
            try:
                with Session(bind) as own:
                    if _cancel_requested(own, query_run_id):
                        requested.set()
                        return
            except Exception as exc:
                if not failed:
                    failed = True
                    logger.warning(
                        "Reading the cancel flag of query run %s failed (%s: %s); it is retried.",
                        query_run_id,
                        type(exc).__name__,
                        exc,
                    )
            stop.wait(_CANCEL_POLL_SECONDS)

    thread = threading.Thread(target=poll, name="lakana-cancel-poll", daemon=True)
    thread.start()
    try:
        yield requested.is_set
    finally:
        stop.set()
        thread.join(_CANCEL_WATCH_JOIN_SECONDS)


def run_one(
    session: Session,
    query_run_id: uuid.UUID,
    authz: AuthorizationService,
    key_provider: KeyProvider | None = None,
    limits: QueryLimitsConfig | None = None,
    *,
    worker_id: str | None = None,
    running_cap: int | None = None,
    on_cap_timeout: Callable[[uuid.UUID], None] | None = None,
    on_result: Callable[[ReadOutcome], None] | None = None,
) -> BaseException | None:
    """Run one already-queued query run to completion, marked as in flight.

    Same as `_run_one`; the run is registered with the memory watchdog meanwhile, so that
    a worker stopped for memory can fail it, and runs under the query run's cancel flag
    (`_watching_for_cancel`): a cancel request stops the query's child.

    Parameters
    ----------
    session
        Database session.
    query_run_id
        The queued run.
    authz
        The authorization service.
    key_provider
        Resolves storage secrets.
    limits
        Per-query limits.
    worker_id
        The id of the worker running the query; written with the start. `None` writes
        none, and a run without one is ended once it started a lease ago.
    running_cap
        The most interactive query runs one principal may have `RUNNING`; `None` applies
        no cap. See `_start`.
    on_cap_timeout
        Passed to `_run_one`.
    on_result
        Passed to `_run_one`.

    Returns
    -------
    BaseException or None
        What `_run_one` returns.
    """
    with (
        ACTIVITY.query(query_run_id),
        _watching_for_cancel(session, query_run_id) as requested,
        cancel_requests(requested),
    ):
        return _run_one(
            session,
            query_run_id,
            authz,
            key_provider,
            limits,
            worker_id=worker_id,
            running_cap=running_cap,
            on_cap_timeout=on_cap_timeout,
            on_result=on_result,
        )


def _run_one(
    session: Session,
    query_run_id: uuid.UUID,
    authz: AuthorizationService,
    key_provider: KeyProvider | None = None,
    limits: QueryLimitsConfig | None = None,
    *,
    worker_id: str | None = None,
    running_cap: int | None = None,
    on_cap_timeout: Callable[[uuid.UUID], None] | None = None,
    on_result: Callable[[ReadOutcome], None] | None = None,
) -> BaseException | None:
    """Run one already-queued query run to completion.

    Parameters
    ----------
    session
        Database session.
    query_run_id
        The `QueryRun` to run. Must currently be `QUEUED`.
    authz
        Authorization service used for every per-table check this run makes.
        Passed in, rather than constructed here, so a caller (a test, or a
        future policy change) controls exactly what this run is and is not
        permitted to read.
    key_provider
        Decrypts a secret-backed environment's storage credentials, if this
        environment's storage_secret_ref requires one. None otherwise.
    limits
        Passed to `execute_query`. `None` uses the defaults.
    worker_id
        The id of the worker running the query; the start update writes it with `RUNNING`.
    running_cap
        The most interactive query runs one principal may have `RUNNING`; `None` applies no
        cap. A run the cap holds back stays `QUEUED` and nothing is written (see `_start`).
    on_cap_timeout
        Called with the run's principal when the wait for the principal's cap lock timed out and
        the claim was refused (see `_start`); the poller passes that principal by for a while.
       `None` tells nobody.
    on_result
        For a read run only: called with the result, the snapshots read and the tables
        a policy applied to, instead of storing a result file; `result_location` stays unset.
        Only `lakana_worker.checks.count_check` passes it, for the count of a check; a write
        or maintenance run never calls it.

    Returns
    -------
    BaseException or None
        The exception that failed the run — the task runner classifies it
        for retries — or `None` when the run succeeded, was refused
        before execution (deleted or inactive principal, invalid task token),
        or was already finished by someone else (a task write the scheduler
        reclaimed before it started).
        For a failed resolution it is the underlying exception, whose text
        may carry a storage location: classify by type, show `error_message`.

    Raises
    ------
    NotFoundError
        If `query_run_id` does not reference an existing row. A caller-error
        precondition — the poller only ever passes an id it just read from
        `query_runs` — not a data-dependent outcome of running the query
        itself, so it is not folded into the `FAILED` handling below.

    Notes
    -----
    Every failure mode from parsing onward — a parse error, a blacklist hit,
    a denied table, a missing table, a DuckDB execution error — is caught
    here and recorded as `status=FAILED` with `error_message` set (`CANCELLED` for a cancel
    request, see below). Nothing
    from that point on propagates out of this function: a bad query must
    never take down the poller.

    A run whose principal has since been deleted is refused before any of
    that: there is no identity to authorize as (`_DELETED_PRINCIPAL_MESSAGE`).
    A PAT-submitted run whose principal has since been deactivated is
    refused the same way, before any parsing
    (`_INACTIVE_PRINCIPAL_MESSAGE`). A run submitted with a task token that
    has since been revoked, expired, deleted, or whose principal was
    deactivated is refused the same way, before any parsing
    (`_INVALID_TASK_TOKEN_MESSAGE`, `_task_scope_for_run`) — unlike the
    deleted-principal case, both of these failures still record
    `QUERY_COMPLETED`, since there is a real principal to attribute it to.
    Whatever reaches `error_message` has this run's own resolved storage
    locations redacted out of it first, and a failure
    during resolution reports only its exception's class name — see this
    module's docstring.

    A cancel request on the run (`QueryRun.cancel_requested`, read by `_watching_for_cancel`)
    stops the query's child and ends the run as `CANCELLED` with the cause `cancelled`
    (`QueryCancelRequested`); a stop for any other reason (the time limit, memory, a task's
    cancel, the drain) stays `FAILED`. A run cancelled while still queued is not started: the
    claim's `UPDATE ... WHERE status IN ('QUEUED', 'RUNNING')` matches nothing, and nothing
    is stored or audited here (the cancel itself was audited). A query the worker stopped is
    not diagnosed as a broken view.

    A write run (`write_mode` set) streams its result into its target
    through `_run_write` instead of persisting a result file; `row_count`
    in `QUERY_COMPLETED` is then the rows written.

    A run executed for a task run carries that run context (`_run_context`:
    the claim form for a query submitted with a bound task token, the
    query-run form for a SQL task's write run), and a queued break-glass
    write its reason; the protected-environment guard in core re-verifies
    both.
    """
    run = session.get(QueryRun, query_run_id)
    if run is None:
        raise NotFoundError(f"No query run {query_run_id}.")
    environment = session.get(Environment, run.environment_id)
    if environment is None:  # pragma: no cover
        # environment_id is a non-nullable FK with ON DELETE CASCADE
        # (models/query_run.py): deleting an Environment deletes every
        # QueryRun that references it in the same transaction, so a
        # QueryRun can never outlive its Environment. Unreachable in
        # practice; kept only so a type checker can narrow `environment`
        # from `Environment | None` for the rest of this function, the
        # same reasoning `catalog/datafiles.py` already documents for its
        # own untestable manifest-walking branch.
        raise NotFoundError(f"No environment {run.environment_id} for query run {query_run_id}.")
    principal_row = session.get(Principal, run.principal_id) if run.principal_id else None
    if principal_row is None:
        _refuse(session, run, _DELETED_PRINCIPAL_MESSAGE, None, worker_id=worker_id)
        return None
    if run.task_token_id is None and not principal_row.is_active:
        # A PAT-submitted run: mirror the task-token path below, since
        # `_task_scope_for_run`/`resolve_task_token_scope` already covers
        # this same check for a task-token-submitted run and must not be
        # duplicated here for it (its own message,
        # `_INVALID_TASK_TOKEN_MESSAGE`, already applies).
        _refuse(session, run, _INACTIVE_PRINCIPAL_MESSAGE, environment.name, worker_id=worker_id)
        return None
    task_scope = _task_scope_for_run(session, run)
    if task_scope == "invalid":
        _refuse(session, run, _INVALID_TASK_TOKEN_MESSAGE, environment.name, worker_id=worker_id)
        return None
    principal = PrincipalRef(
        id=principal_row.id,
        name=principal_row.name,
        type=principal_row.type,
        task_scope=task_scope,
        run_context=_run_context(run, task_scope),
        break_glass_reason=run.break_glass_reason,
        write_fence=WriteFence(query_run_id=run.id, worker_id=worker_id),
    )
    if not _start(
        session,
        run,
        principal_row.id,
        worker_id=worker_id,
        running_cap=running_cap,
        on_cap_timeout=on_cap_timeout,
    ):
        return None

    locations = [environment.storage_root, environment.storage_root.split("://", 1)[-1]]
    dotted_refs: list[str] = []
    row_count: int | None = None
    limits = limits or QueryLimitsConfig()
    # Only local `file://` mode lets a query spill to disk; elsewhere the worker's temp directory
    # may be a container's writable layer.
    spill = environment.storage_root.startswith("file://")
    prepared: _Prepared | None = None
    failure_exc: BaseException | None = None
    outcome = QueryRunStatus.SUCCEEDED
    try:
        # Credentials join the redaction list before anything that can fail
        # with one in its message runs: the rewrite, the query, the write and
        # maintenance alike.
        resolved_storage = _resolve_storage_opaquely(session, environment.name, key_provider)
        locations.extend(
            value
            for value in resolved_storage.config.options.values()
            if len(value) >= _MIN_SCRUBBED_OPTION_LENGTH
        )
        if run.write_mode == WriteMode.MAINTAIN:
            _run_maintenance(session, run, environment.name, principal, authz, key_provider)
        else:
            check_blacklist(run.sql_text)
            dotted_refs = extract_table_refs(run.sql_text)
            lineage = extract_lineage(run.sql_text)
            prepared = _resolve_and_rewrite(
                session,
                environment.name,
                principal,
                run.sql_text,
                authz,
                key_provider,
                locations,
                root=run.target_name if run.write_mode == WriteMode.VIEW else None,
                check_result_id=run.check_result_id,
            )
            if run.write_mode is None:
                result = execute_query(
                    prepared.rewritten.sql,
                    prepared.rewritten.parameters,
                    read_prefixes=prepared.read_prefixes,
                    spill=spill,
                    limits=limits,
                )
                if on_result is not None:
                    # A count for a check: the caller keeps the one row; nothing is stored.
                    on_result(
                        ReadOutcome(
                            result,
                            {ref.dotted(): pin.snapshot_id for ref, pin in prepared.pins.items()},
                            prepared.restricted,
                        )
                    )
                else:
                    result_path = f"_artifacts/queries/{run.id}/result.arrow"
                    _store_result(resolved_storage.storage, result_path, result)
                    run.result_location = result_path
                row_count = result.num_rows
            elif run.write_mode == WriteMode.VIEW:
                _create_view(
                    session,
                    run,
                    environment.name,
                    principal,
                    prepared,
                    dotted_refs,
                    authz,
                    key_provider,
                    limits,
                    spill=spill,
                )
                row_count = 0
            else:
                row_count = _run_write(
                    session,
                    run,
                    environment.name,
                    principal,
                    prepared,
                    dotted_refs,
                    authz,
                    key_provider,
                    limits,
                    spill=spill,
                )
            run.lineage_json = lineage
    except _ResolutionFailed as exc:
        # The opaque wrapper carries no type to classify by; its cause does.
        # The cause's message is for classification only, never for display.
        failure_exc = exc.__cause__ or exc
        outcome = QueryRunStatus.FAILED
        run.error_message = str(exc)
        run.error_cause = failure_cause(failure_exc).value
        gone = _snapshot_gone_after_failure(
            session, run, environment.name, principal, dotted_refs, authz, key_provider, failure_exc
        )
        if gone is not None:
            failure_exc = gone
            run.error_message = str(gone)
            run.error_cause = failure_cause(gone).value
    except LakanaError as exc:
        failure_exc = exc
        outcome = (
            QueryRunStatus.CANCELLED
            if isinstance(exc, QueryCancelRequested)
            else QueryRunStatus.FAILED
        )
        failure: LakanaError = exc
        not_shown = None if prepared is None else _withheld_column_failure(exc, prepared.withheld)
        if not_shown is None and prepared is not None:
            not_shown = _schema_mismatch_failure(
                exc, prepared.tables, prepared.projected, prepared.named
            )
        # A query the worker stopped (a cancel, the drain, the time limit, memory) did not fail
        # because of a view: the describe the diagnosis starts would be stopped too, and the
        # user would read "View ... no longer plans". A query bound to a check result is not
        # diagnosed either: the diagnosis plans each view against the tables as they are
        # now, and this query read one of them at a checked snapshot. It would blame the
        # view for a table that is not published, or for a column that only exists now, and
        # point the reader back at the command that is running. Its own error stands.
        if not_shown is None and not isinstance(exc, _STOPPED):
            not_shown = _snapshot_gone_after_failure(
                session, run, environment.name, principal, dotted_refs, authz, key_provider, exc
            )
        if not_shown is not None:
            failure = not_shown
        elif (
            prepared is not None
            and prepared.views
            and not prepared.restricted
            and run.check_result_id is None
            and isinstance(exc, ValidationError)
            and not isinstance(
                exc, (QueryCancelled, QueryTimeout, OutOfMemoryQuery, WorkerStaleQuery)
            )
        ):
            failure = (
                _diagnose_views(
                    session,
                    environment.name,
                    principal,
                    prepared.views,
                    authz,
                    key_provider,
                    limits,
                    locations,
                    spill=spill,
                )
                or exc
            )
        if prepared is not None:
            failure = _without_the_value(failure, prepared.restricted)
        run.error_message = _redact(str(failure), locations)
        run.error_cause = failure_cause(failure).value
    except SQL_SYNTAX_ERRORS as exc:
        failure_exc = exc
        outcome = QueryRunStatus.FAILED
        run.error_message = _redact(describe_syntax_error(exc), locations)
        run.error_cause = failure_cause(exc).value
    except MemoryError:
        # An allocation outside DuckDB (pyarrow, Python) failed: the machine or the container
        # itself has no memory left (the worker's own limit is a watchdog, not an allocation
        # limit). Report it like DuckDB's own out-of-memory error.
        failure_exc = OutOfMemoryQuery(
            f"Query failed: the worker ran out of memory. {MACHINE_MEMORY_ADVICE}"
        )
        outcome = QueryRunStatus.FAILED
        run.error_message = str(failure_exc)
        run.error_cause = failure_cause(failure_exc).value
    except Exception as exc:  # see docstring: nothing may propagate out of this function
        failure_exc = exc
        outcome = QueryRunStatus.FAILED
        run.error_message = _redact(f"Unexpected error: {exc}", locations)
        run.error_cause = failure_cause(exc).value
    if isinstance(failure_exc, WriteFenceRefused):
        logger.warning(
            "Query run %s was ended while this worker was not seen alive, so its write was "
            "refused and nothing was committed.",
            query_run_id,
        )
    if ACTIVITY.stopping:
        # The memory watchdog is stopping the worker and records this run's outcome itself.
        session.rollback()
        return failure_exc
    run.finished_at = datetime.now(UTC)
    if not _keep_a_prior_stamp(session, run, outcome, worker_id=worker_id):
        if run.status is QueryRunStatus.RUNNING:
            # Another worker holds the run (the stamp matches only this worker's id): its own
            # attempt records the outcome and the one completion event.
            logger.warning(
                "Query run %s is held by worker %s, not by this worker; the result of this "
                "attempt was discarded.",
                query_run_id,
                run.worker_id,
            )
            return failure_exc
        logger.warning(
            "Query run %s was ended by someone else (stored status %s) before this worker "
            "recorded its result; the result of this attempt was discarded.",
            query_run_id,
            run.status.value,
        )
        if _already_audited(run):
            # The sweep or a cancel ended this run and audited that; a second QUERY_COMPLETED
            # from the attempt it superseded would be untrue.
            return failure_exc
    if run.status is QueryRunStatus.SUCCEEDED and row_count is None:
        row_count = run.rows_written  # a failure that followed a commit: the run says SUCCEEDED
    if run.status is QueryRunStatus.SUCCEEDED:
        # Same transaction as the completion: a read is counted if and only if the run succeeded.
        record_table_reads(
            session,
            environment=environment.name,
            principal_id=principal.id,
            dotted_refs=dotted_refs,
            now=run.finished_at or datetime.now(UTC),
        )
    AuditService(session).record(
        run.principal_id,
        AuditEventType.QUERY_COMPLETED,
        securable=Securable.environment_scope(environment.name),
        detail=query_completed_detail(
            run, tables_touched=dotted_refs, row_count=row_count, status=run.status
        ),
    )
    session.commit()
    return failure_exc


def _start(
    session: Session,
    run: QueryRun,
    principal_id: uuid.UUID,
    *,
    worker_id: str | None,
    running_cap: int | None,
    on_cap_timeout: Callable[[uuid.UUID], None] | None = None,
) -> bool:
    """Claim a run for this worker: `RUNNING` under its id, unless the principal is at the cap.

    The condition lives in the statement that acts: the poller's list of capped principals is
    only a hint, read earlier. For an interactive run with a cap, the start update carries
    "this principal has fewer than `running_cap` interactive runs `RUNNING`" beside
    `_claimable_by`. Pipeline work (a task's write, maintenance, a Python task's input query)
    is never capped.

    Under READ COMMITTED two start updates for two runs of one principal share no row, so
    each would count the other's uncommitted start as absent. The principal's advisory
    transaction lock (`pools.lock_principal`), taken right before the update, in the claim's
    own transaction, serialises them: the second update starts after the first committed, and
    its count includes that run. The lock, and the ``SET LOCAL lock_timeout`` that bounds the
    wait for it, end with the commit or rollback below; a wait that times out is a refused claim
    (rollback, `False`, the run stays `QUEUED`, `on_cap_timeout` is told so that the poller does
    not select the same principal again at once). On
    SQLite the count and the update are one statement of the one writer. The transaction
    already holds the run's row lock (the dequeue took it with `SKIP LOCKED`, never waited
    for); the lock order is that row, then the advisory lock, then nothing.

    The update matches no row when another worker holds the run, when the scheduler's reclaim
    stamped a task write `FAILED` first (it stands), or when the principal reached the cap
    since the poller's read. That is not an error: nothing is executed, written, audited or
    logged, the transaction ends and the run stays `QUEUED` for the next poll, whose list
    names the principal as capped. A running query whose worker died still counts until the
    sweep ends it (about a lease); the cap accepts that.

    Parameters
    ----------
    session
        Database session; its transaction holds the run's row lock.
    run
        The run being claimed.
    principal_id
        The run's principal (a run without one is refused before this).
    worker_id
        The id of the claiming worker; `None` when it records none.
    running_cap
        The most interactive runs the principal may have `RUNNING`; `None` applies no cap.
    on_cap_timeout
        Called with `principal_id` after the rollback when the wait for the principal's lock
        timed out; `None` tells nobody.

    Returns
    -------
    bool
        `True` once the run is `RUNNING` and committed; `False` after a rollback, when the
        run is not this worker's to start.
    """
    conditions = [QueryRun.id == run.id, _claimable_by(worker_id)]
    if (
        running_cap is not None
        and pools.kind_of(run.task_run_id, run.write_mode) == pools.INTERACTIVE
    ):
        if not pools.lock_principal(session, principal_id):
            session.rollback()
            if on_cap_timeout is not None:
                on_cap_timeout(principal_id)
            return False
        conditions.append(pools.under_cap(principal_id, running_cap))
    started = session.execute(
        update(QueryRun)
        .where(*conditions)
        .values(status=QueryRunStatus.RUNNING, started_at=datetime.now(UTC), worker_id=worker_id)
        .execution_options(synchronize_session=False)
    )
    if rowcount(started) != 1:
        session.rollback()
        return False
    session.commit()
    return True


def _claimable_by(worker_id: str | None) -> ColumnElement[bool]:
    """Build the condition under which a worker may claim, or refuse, a query run.

    The one definition of "this run is mine to take", shared by the start and `_refuse`: it is
    `QUEUED`, or `RUNNING` under this worker's id (a SQL task's write run is inserted `RUNNING`
    under its worker's id). Any other `RUNNING` run belongs to the worker that took it; SQLite
    has no row lock between two workers' dequeues.

    Parameters
    ----------
    worker_id
        The id of the claiming worker; `None` when it records none.

    Returns
    -------
    ColumnElement
        The condition for a `WHERE`.
    """
    return or_(
        QueryRun.status == QueryRunStatus.QUEUED,
        and_(
            QueryRun.status == QueryRunStatus.RUNNING,
            QueryRun.worker_id.is_not_distinct_from(worker_id),
        ),
    )


def _refuse(
    session: Session,
    run: QueryRun,
    message: str,
    environment_name: str | None,
    *,
    worker_id: str | None = None,
) -> None:
    """Fail a run before execution, unless a reclaim already finished it or another worker holds it.

    Claims the run the way the start does (`_claimable_by`): a racing scheduler reclaim's
    `FAILED` stamp and its message stand, a run another worker holds is left to that worker, and
    nothing is audited for an attempt it superseded.

    Parameters
    ----------
    session
        Database session; committed (or rolled back) here.
    run
        The run.
    message
        The refusal, stored as `error_message`.
    environment_name
        Audit `QUERY_COMPLETED` in this environment, or `None` to audit
        nothing (a deleted principal: no one to attribute it to).
    worker_id
        The id of the refusing worker.
    """
    claimed = session.execute(
        update(QueryRun)
        .where(QueryRun.id == run.id, _claimable_by(worker_id))
        .values(
            status=QueryRunStatus.FAILED,
            error_message=message,
            error_cause=QueryErrorCause.PERMISSION_DENIED.value,
            finished_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    if rowcount(claimed) != 1:
        session.rollback()
        return
    if environment_name is not None:
        AuditService(session).record(
            run.principal_id,
            AuditEventType.QUERY_COMPLETED,
            securable=Securable.environment_scope(environment_name),
            detail=query_completed_detail(
                run, tables_touched=[], row_count=None, status=QueryRunStatus.FAILED
            ),
        )
    session.commit()


def _already_audited(run: QueryRun) -> bool:
    """Say whether whoever ended this run in the worker's place recorded its end.

    The sweep and a cancel of a lost run do (`QUERY_COMPLETED`, once per run); a task's lease
    reclaim (`store._apply_task`) does not, and neither does the sweep for a SQL task's own
    write. Nothing else ends a `RUNNING` run under another writer: the stamps `CANCELLED` and
    `SUCCEEDED` over a run still `RUNNING` come only from the lost-run cancel and the sweep.
    The run was expired by the rollback in `_keep_a_prior_stamp`; reading it loads the stored
    row.

    Parameters
    ----------
    run
        The run, as stored.

    Returns
    -------
    bool
        Whether the worker must write no completion event of its own.
    """
    own_task_write = run.task_run_id is not None and run.task_token_id is None
    if own_task_write:
        return False
    return (
        run.status in (QueryRunStatus.CANCELLED, QueryRunStatus.SUCCEEDED)
        or run.error_cause == QueryErrorCause.WORKER_LOST.value
    )


def _keep_a_prior_stamp(
    session: Session, run: QueryRun, outcome: QueryRunStatus, *, worker_id: str | None = None
) -> bool:
    """Let the run's outcome land only while the run is still this worker's `RUNNING` run.

    The scheduler's lost-lease reclaim stamps a task's write `FAILED` while
    a stalled worker may still be executing it, and the sweep ends a run whose worker the
    database counts as lost (a seconds-old run of a worker that is in fact alive, after a
    suspend); that stamp must stand, so the task is retried and the user reads one answer,
    rather than the attempt it superseded reporting again.
    A conditional `UPDATE … WHERE status = 'RUNNING' AND worker_id IS NOT DISTINCT FROM …`
    claims the row (on Postgres it also holds the row lock until the caller commits, so a
    reclaim arriving later finds the run finished and leaves it alone).
    When it matches nothing, the transaction is rolled back — discarding the
    outcome and whatever else was set on `run`, flushed or not — and the
    stored status is what the caller audits.

    A failure or a cancel outcome for a run that has its write evidence (`rows_written` is not
    null: the commit landed, and something failed after it) lands as `SUCCEEDED`, by a statement
    that carries the evidence in its own `WHERE`; the run's error fields are cleared, so the run
    is never `FAILED` over rows in the table.

    The outcome is passed in rather than set on `run` beforehand: a commit
    made while handling a failure (view diagnosis) would otherwise flush it
    ahead of this check.

    Parameters
    ----------
    session
        Database session; the caller commits.
    run
        The run, with the rest of its outcome set but not yet committed.
    outcome
        `SUCCEEDED`, `FAILED` or `CANCELLED`.
    worker_id
        The id this worker claimed the run under (`None` when it recorded none); a run
        stamped with another id is not this attempt's to finish.

    Returns
    -------
    bool
        Whether the outcome landed; `False` means somebody else ended the run first and the
        session was rolled back.
    """
    still_mine = (
        QueryRun.id == run.id,
        QueryRun.status == QueryRunStatus.RUNNING,
        QueryRun.worker_id.is_not_distinct_from(worker_id),
    )
    with session.no_autoflush:
        if outcome is not QueryRunStatus.SUCCEEDED:
            # A write that left its evidence did commit: whatever failed afterwards, the run is
            # not FAILED over rows that are in the table. The evidence is in this statement's
            # own WHERE, never read from `run` earlier.
            landed = session.execute(
                update(QueryRun)
                .where(*still_mine, QueryRun.rows_written.is_not(None))
                .values(status=QueryRunStatus.SUCCEEDED)
                .returning(QueryRun.rows_written)
                .execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            if landed is not None:
                run.status = QueryRunStatus.SUCCEEDED
                run.rows_written = landed
                run.error_message = None
                run.error_cause = None
                return True
        claimed = session.execute(
            update(QueryRun)
            .where(*still_mine)
            .values(status=outcome)
            .execution_options(synchronize_session=False)
        )
    if rowcount(claimed) == 1:
        run.status = outcome
        return True
    # Everything since the last commit is this attempt's outcome (the
    # catalog commits its own writes, a denial audits in its own
    # commit), so a rollback also discards fields an autoflush already
    # sent; expiring `run` alone would leave those to land.
    session.rollback()
    return False
