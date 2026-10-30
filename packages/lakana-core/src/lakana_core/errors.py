"""Domain errors raised by lakana-core.

These carry no HTTP knowledge; the server layer is responsible for mapping
them onto status codes and response bodies.
"""


class LakanaError(Exception):
    """Base class for every error raised by lakana-core."""


class NotFoundError(LakanaError):
    """A requested entity does not exist."""


class AlreadyExistsError(LakanaError):
    """An entity with the same identity already exists."""


class ConflictError(LakanaError):
    """A concurrent change was detected, or a stated requirement was not met."""


class ValidationError(LakanaError):
    """Input was structurally invalid."""


class PermissionDenied(LakanaError):
    """The principal is not permitted to perform this action."""


class AuthenticationError(LakanaError):
    """The request's credentials are missing or malformed.

    Distinct from `PermissionDenied`: this means the caller could not be
    identified at all (maps to HTTP 401), where `PermissionDenied` means a
    real, identified principal lacks a right (maps to 403). M1 has no real
    credential verification, so in practice this fires only when the
    `Authorization` header is absent or does not match the expected shape —
    the secret itself is never checked. M3 is expected to raise this for a
    revoked or expired PAT too, which is why the distinction exists now
    rather than being introduced later as a breaking change to the mapping.
    """


class StorageUnavailableError(LakanaError):
    """Storage could not be written or read right now (a full disk, an unreachable bucket).

    Carries a text that names no path and none of storage's own words; the server maps it to
    503 with a `Retry-After` header, so a client may try again.
    """


class StorageRefusedError(LakanaError):
    """The storage answered, and refused the environment's credentials.

    Asking again does not help until the credentials are fixed, so the server maps it to 502
    without a `Retry-After` header, apart from `StorageUnavailableError`. The text names no path
    and none of storage's own words.
    """


class AuditUnavailableError(LakanaError):
    """A read of other people's activity was refused because its audit row could not be written.

    Raised by `lakana_core.audit.views.record_view` before any of the activity is returned (the
    view audit fails closed). The text names the cause by its kind only, never the database's own
    words; the server maps it to 503 with a `Retry-After` header.
    """


class WriteOutcomeUnknownError(LakanaError):
    """A write's commit was not acknowledged and the table could not be asked what became of it.

    Raised by the catalog's write paths after the database failed on the way back from a commit
    and a fresh read of the table failed too. The write may have landed or not; its files are
    kept, because a deleted file a landed snapshot names breaks the table, while a kept file
    that turns out to be unreferenced is an orphan that `lakana table maintain` removes. The
    caller must not repeat the write blindly: an append could land twice. Mapped to 500 by the
    server (no mapped base class), and a permanent failure for a pipeline task, which therefore
    is not retried on its own.
    """


class UnsupportedTableError(LakanaError):
    """Lakana cannot read a catalog object as a table, or cannot read it correctly yet.

    Raised by `lakana_core.catalog.datafiles.list_data_files` when the current
    snapshot carries a live position- or equality-delete file: reading only
    the data files and ignoring the deletes would silently return rows the
    writer meant to remove. Refusing the read is the only correct behaviour
    until Lakana's query engine applies deletes itself, planned for M5.

    Also raised when SQL names an artifact, by the worker's source
    resolution and by `CatalogService.create_or_replace_view` for a view's
    reference: only a Python asset reads an artifact.
    """


class NotPublishedError(LakanaError):
    """A table cannot be read: its first build did not finish, or failed a blocking check.

    Raised by `CatalogService.load_table`, after the read grant was checked, for a table
    that is registered but not published (`Table.unpublished_run_id` is set): reading it
    would show an empty table, which a consumer cannot tell from "no data". The message
    names the check and the run. Mapped to 409 on the platform API and to 404 on the
    Iceberg REST catalog; a query that reads the table fails with it, and a task that does
    fails at once and is not retried.
    """


class KeptAfterCheckError(LakanaError):
    """A task cannot read a table: it was kept after a failed check and has not passed since.

    Raised by `CatalogService.load_table`, after the read grant was checked and after the
    not-published refusal, for a table that carries the note of a failed `keep` check
    (`Table.kept_run_id` is set) when the reader is a task (the principal has a run context)
    without standing: its run is not a run of the project that builds the table, or it is
    neither the table's own task, nor downstream of it in its run, nor the count of a check.
    A reader without a run context is never refused: the table keeps the failing data so that
    people can look at it. The message names the check, the run and the next step. Mapped to
    409 on the platform API and to 404 on the Iceberg REST catalog; a task that reads the
    table fails at once and is not retried.
    """


class CheckedSnapshotGoneError(LakanaError):
    """The snapshot a check result of a table names can no longer be read from it.

    Raised by `CatalogService.load_table_at_check`, after the read grant was checked and the
    result was found to be one of the named table, when the table's metadata no longer holds
    the snapshot (maintenance removed it), when the snapshot names no schema the table holds,
    or when the row is not a managed table with metadata. The text names the table, the
    result's run and the environment, and says where the number of rows remains
    (`lakana_core.runs.checks.checked_snapshot_gone_message`).

    Also raised by `lakana_core.catalog.datafiles.list_data_files` for a named snapshot its
    metadata does not hold, with a text that names no table; a caller that lists from what
    `load_table_at_check` returned never sees that one.
    """


class NoCheckedSnapshotError(LakanaError):
    """A check result names no snapshot, so there is nothing to read its rows from.

    A result has no checked snapshot when its table is a view, when the check's SQL does not
    read its own table, or when the check could not run. Raised by
    `CatalogService.load_table_at_check`, after the read grant was checked and the result was
    found to be one of the named table, with a text that names the run and gives no reason
    (`lakana_core.runs.checks.no_checked_snapshot_message`): the stored result does not say
    which of the three it was. Code that holds the check's definition tells them apart and
    words the reason and its next step (`no_snapshot_of_a_view_message`,
    `no_snapshot_not_read_message`, `no_snapshot_could_not_run_message`).
    """


class CheckResultGoneError(LakanaError):
    """The table a read names has no such check result.

    Raised by `CatalogService.load_table_at_check`, after the read grant was checked, when the
    result does not exist in the table's environment (it was removed with its run, or belongs
    to another environment), is of another table, names no table, or is of a table that was
    since dropped and built again under the name. All of these get one text, which names only
    the table the caller named (`lakana_core.runs.checks.check_result_gone_message`), so a
    caller cannot tell them apart and learns nothing of a result that is not this table's.
    """


class CheckNotInRunError(NotFoundError):
    """A run has no result for the check a caller named.

    Raised by `lakana_core.runs.check_rows.submit_check_rows`, after the caller was allowed
    to read the run and to submit a query, when none of that run's check results has the
    name. A not-found error, so the platform API answers 404; the text names the run and the
    command that lists its checks (`lakana_core.runs.checks.check_not_in_run_message`).
    """


class CheckSqlGoneError(ConflictError):
    """The SQL of a check can no longer be read from the project version its run used.

    Raised by `lakana_core.runs.check_rows.submit_check_rows` for a check result with a
    checked snapshot when the manifest of the run's project version holds no readable entry
    for the check: the result stores a count and never the SQL. A conflict, so the platform
    API answers 409; the text says where the count remains
    (`lakana_core.runs.checks.check_sql_gone_message`). Also for an entry whose SQL cannot be
    read, with its own text (`check_sql_unreadable_message`), which asks for a new deploy.
    """
