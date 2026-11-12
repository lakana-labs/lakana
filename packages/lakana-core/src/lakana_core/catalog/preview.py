"""The query that shows a person the first rows of a table: its preview.

A preview is an ordinary ad-hoc query run of the caller's own, with two differences: the server
writes its SQL from the catalog row (the client sends no SQL, limit or columns), and the run is
bound to the table's id (`QueryRun.preview_table_id`). The worker compares the id of the object
the SQL names with the binding before it reads, and reads as the caller, with their row filters
and column masks.

This module decides who may ask and what is queued, and is the only place that sets the
binding. It imports nothing from `lakana_core.runs` and nothing from the catalog service, so
neither can be part of an import cycle with it.
"""

from __future__ import annotations

import uuid

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef, shown
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    Environment,
    Namespace,
    QueryRun,
    QueryRunStatus,
    Table,
    TableType,
)

PREVIEW_ROW_LIMIT: int = 100
"""The most rows a preview shows: a constant in the server-built SQL, never from the client."""

PREVIEW_TABLE_CHANGED: str = (
    "The table changed since the preview was asked for; run the preview again."
)
"""The worker's failure when the object the SQL names is no longer the one previewed."""

PREVIEW_NOT_FOR_A_TASK: str = (
    "A task cannot preview a table: a preview is a person's own query, never a pipeline's. "
    "Run `lakana table preview` yourself, outside the task."
)
"""The refusal for a run context or a task-kind token, bound to a task or not."""


def preview_needs_submit_query_message(environment: str) -> str:
    """Say that a preview needs the right to run queries, and who can give it.

    Parameters
    ----------
    environment
        The environment's name.

    Returns
    -------
    str
        The message.
    """
    return (
        f"Preview runs a query as you, which needs the right to run queries in "
        f"{shown(environment)}. Ask an administrator for the Analyst role there."
    )


def preview_of_an_artifact_message(dotted: str, environment: str) -> str:
    """Say that an artifact has no rows, and how to download it instead.

    Parameters
    ----------
    dotted
        The artifact's dotted name.
    environment
        The environment's name.

    Returns
    -------
    str
        The message.
    """
    return (
        "An artifact holds files, not rows; download it with "
        f"`lakana artifact download {shown(dotted)} DIRECTORY --env {shown(environment)}`."
    )


def preview_of_a_dotted_name_message(dotted: str) -> str:
    """Say that a table whose namespace has a dot in its name cannot be previewed yet.

    Parameters
    ----------
    dotted
        The table's dotted name.

    Returns
    -------
    str
        The message, naming the next step: renaming the namespace.
    """
    return (
        f"{shown(dotted)} is in a namespace whose name contains a dot, which a query cannot "
        "name yet; rename the namespace to preview it."
    )


def preview_of_a_dotted_table_name_message(dotted: str) -> str:
    """Say that a table whose own name has a dot in it cannot be previewed yet.

    Parameters
    ----------
    dotted
        The table's dotted name.

    Returns
    -------
    str
        The message, naming the next step: renaming the table.
    """
    return (
        f"{shown(dotted)} has a table name that contains a dot, which a query cannot name yet; "
        "rename the table to preview it."
    )


def preview_sql(namespace: NamespacePath, name: str) -> str:
    """Write the SQL of a preview, every part of the name quoted.

    Parameters
    ----------
    namespace
        The table's namespace.
    name
        The table's name.

    Returns
    -------
    str
        ``SELECT * FROM "<part>"…."<name>" LIMIT 100``, in DuckDB's dialect.
    """
    quoted = ".".join(
        exp.to_identifier(part, quoted=True).sql("duckdb") for part in (*namespace.parts, name)
    )
    # Every part is a quoted identifier and the limit a constant: no text reaches it unquoted.
    return f"SELECT * FROM {quoted} LIMIT {PREVIEW_ROW_LIMIT}"  # nosec B608


def preview_parts(sql: str) -> tuple[str, ...]:
    """Read back every part of the one table a preview's SQL names.

    Parameters
    ----------
    sql
        A preview's SQL, as `preview_sql` wrote it.

    Returns
    -------
    tuple of str
        The namespace's parts and the table's name, unquoted, outermost first.

    Raises
    ------
    ValueError
        If the SQL names no table, so it is not a preview's.
    """
    table = sqlglot.parse_one(sql, read="duckdb").find(exp.Table)
    if table is None:
        raise ValueError("The SQL names no table, so it is not a preview's.")
    return tuple(part.name for part in table.parts)


def previewed_table(run: QueryRun) -> str | None:
    """Name the table a run previews, from the run's own server-built SQL.

    The name is read from the SQL, never looked up by the id, so it reveals nothing the
    submitter did not ask for, even after the table was renamed.

    Parameters
    ----------
    run
        A query run.

    Returns
    -------
    str or None
        The dotted name for a preview; `None` for every other run.
    """
    if run.preview_table_id is None:
        return None
    return ".".join(preview_parts(run.sql_text))


def submit_preview(
    session: Session, authorization: AuthorizationService, p: PrincipalRef, ref: TableRef
) -> QueryRun:
    """Queue the caller's own query that shows the first rows of a table or view.

    Reading the table is authorized first and submitting a query second, both before the
    catalog is read, so a caller who may not read the table is refused alike whether or not it
    exists. The worker authorizes the read again when it runs the query, which also applies
    the caller's row filters and column masks.

    Parameters
    ----------
    session
        Database session; commits.
    authorization
        Authorization service.
    p
        The caller: the query run is theirs.
    ref
        The table or view to preview.

    Returns
    -------
    QueryRun
        The inserted row: `QUEUED`, submitted by the caller, without a task token or task run,
        with the server-built SQL (`preview_sql`) as its text and the table's id as
        `preview_table_id`.

    Raises
    ------
    PermissionDenied
        Without READ_TABLE on the table; then without SUBMIT_QUERY on the environment
        (`preview_needs_submit_query_message`); then for a task, a run context or a task-kind
        token bound to a task or not (`PREVIEW_NOT_FOR_A_TASK`).
    NotFoundError
        If the environment is missing or deleted, or the namespace or table does not exist.
    ConflictError
        For an artifact, which has no rows (`preview_of_an_artifact_message`), and for a
        namespace or table name that contains a dot, which no query can name yet
        (`preview_of_a_dotted_name_message`, `preview_of_a_dotted_table_name_message`).
    """
    authorization.authorize(
        p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
    )
    try:
        authorization.authorize(
            p, Action.SUBMIT_QUERY, Securable.environment_scope(ref.environment)
        )
    except PermissionDenied as denied:
        raise PermissionDenied(preview_needs_submit_query_message(ref.environment)) from denied
    # Any task-kind token is a task: one bound to no task has no run context, and is still
    # meant for automation, not for a person's reads.
    if p.run_context is not None or p.task_scope is not None:
        raise PermissionDenied(PREVIEW_NOT_FOR_A_TASK)
    found = session.execute(
        select(Table, Namespace.path, Environment.id)
        .join(Namespace, Table.namespace_id == Namespace.id)
        .join(Environment, Namespace.environment_id == Environment.id)
        .where(
            Environment.name == ref.environment,
            LIVE_ENVIRONMENT,
            Namespace.path == ref.namespace.joined(),
            Table.name == ref.name,
        )
    ).one_or_none()
    if found is None:
        raise NotFoundError(f"Table '{shown(ref.name)}' not found in {shown(str(ref.namespace))}.")
    table, path, environment_id = found
    namespace = NamespacePath.parse(path)
    dotted = f"{namespace}.{table.name}"
    if table.table_type == TableType.ARTIFACT:
        raise ConflictError(preview_of_an_artifact_message(dotted, ref.environment))
    # The query path names tables by dotted strings, so a dot inside a part would make the
    # worker look for another object; no run is queued that it could not check. The refusal
    # names what to rename: the namespace first, should both hold a dot.
    if any("." in part for part in namespace.parts):
        raise ConflictError(preview_of_a_dotted_name_message(dotted))
    if "." in table.name:
        raise ConflictError(preview_of_a_dotted_table_name_message(dotted))
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=environment_id,
        principal_id=p.id,
        sql_text=preview_sql(namespace, table.name),
        status=QueryRunStatus.QUEUED,
        task_token_id=None,
        preview_table_id=table.id,
    )
    session.add(run)
    session.commit()
    return run
