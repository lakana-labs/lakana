"""The query submission resource of the platform API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.query_service import QUERY_LIST_MAX, QueryService, WriteTarget
from lakana_core.errors import ConflictError
from lakana_core.models import QueryRun, QueryRunStatus, WriteMode
from lakana_core.runs.waiting import WaitReason
from lakana_core.secrets.keys import KeyProvider
from lakana_core.storage.resolver import StorageResolver
from lakana_server.api.platform.listing import set_next_cursor, split_values
from lakana_server.api.platform.schemas import (
    QueryListOut,
    QueryStatusOut,
    QuerySubmit,
    QuerySubmitOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import (
    check_statement,
    get_key_provider,
    get_principal,
    get_query_service,
    get_session,
    with_break_glass,
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/queries",
    tags=["queries"],
    responses=errors(401),
)


def _status_out(run: QueryRun, waiting: WaitReason | None) -> QueryStatusOut:
    """Build the status document of a query run.

    Parameters
    ----------
    run
        The query run.
    waiting
        Why it has not started (`QueryService.waiting_for`), or `None`.

    Returns
    -------
    QueryStatusOut
        Its status, and its error or lineage once finished.
    """
    return QueryStatusOut(
        query_id=str(run.id),
        status=run.status.value,
        error_message=run.error_message,
        lineage=run.lineage_json if run.status == QueryRunStatus.SUCCEEDED else None,
        write_mode=None if run.write_mode is None else run.write_mode.value,
        target=run.target_name,
        rows_written=run.rows_written,
        snapshot_id=run.snapshot_id,
        watermark=run.watermark,
        schema_diff=run.schema_diff,
        maintenance=run.maintenance,
        error_cause=run.error_cause,
        waiting_for=None if waiting is None else waiting.to_json(),
    )


@router.get("", response_model=list[QueryListOut], responses=errors(403, 404, 503))
def list_queries(
    env: str,
    response: Response,
    include_ended: bool = Query(default=False),
    status: list[str] = Query(default=[], max_length=16),
    submitter: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=QUERY_LIST_MAX),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    queries: QueryService = Depends(get_query_service),
) -> list[QueryListOut]:
    """List the queries of an environment.

    Lists running queries first, then queued ones, then, when asked, ended ones. Needs
    `SUBMIT_QUERY` on the environment, or `MANAGE_ENVIRONMENT`. A holder of
    `MANAGE_ENVIRONMENT` sees every query of the environment; everyone else sees the queries
    they submitted. The list never shows SQL text, error messages or results. The cursor of
    the next page is in the `X-Next-Cursor` response header and is absent on the last page;
    send the same filters with every page. Answers 404 when the environment does not exist.
    Answers 503 when a manager's page shows someone else's queries and the audit entry for
    that could not be written; nothing is listed then.

    Parameters
    ----------
    env
        Environment name.
    response
        The response, to set the cursor header on.
    include_ended
        Also list the queries that ended in the last 24 hours.
    status
        Only queries in these statuses (`queued`, `running`, `succeeded`, `failed`,
        `cancelled`; any letter case); repeat the parameter or separate the values with commas.
        An ended status lists the ended queries of the last 24 hours, with or without
        `include_ended`.
    submitter
        Only queries submitted by the principal with this name.
    limit
        Most rows returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page.
    principal
        The authenticated caller.
    queries
        The query service for this request.

    Returns
    -------
    list of QueryListOut
        Running first (longest running first), then queued in the order they will be taken,
        then ended ones, newest first.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, if the caller may not read queries in this environment (as the status
        read).
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist.
    lakana_core.errors.AuditUnavailableError
        Mapped to 503 with `Retry-After`, if a manager's page shows or targets someone else's
        queries and its `ACTIVITY_VIEWED` audit row could not be written; nothing is listed.
    """
    items, next_cursor = queries.list_queries_page(
        principal,
        env,
        include_ended=include_ended,
        status=split_values(status),
        submitter=submitter,
        limit=limit,
        cursor=cursor,
    )
    set_next_cursor(response, next_cursor)
    return [
        QueryListOut(
            id=str(item.id),
            submitter=item.submitter,
            kind=item.kind,
            target=item.target,
            status=item.status,
            created_at=item.created_at,
            started_at=item.started_at,
            finished_at=item.finished_at,
            error_cause=item.error_cause,
            waiting_for=None if item.waiting_for is None else item.waiting_for.to_json(),
        )
        for item in items
    ]


@router.post("", response_model=QuerySubmitOut, status_code=202, responses=errors(403, 404))
def submit_query(
    env: str,
    body: QuerySubmit,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    queries: QueryService = Depends(get_query_service),
) -> QuerySubmitOut:
    """Submit a query.

    Queues SQL for a worker to run and answers 202 with the query's id; poll its status,
    then fetch its result. Needs `SUBMIT_QUERY` on the environment. Answers 404 when the
    environment does not exist and 422 when the SQL is longer than the server's statement
    limit.

    Parameters
    ----------
    env
        Environment name.
    body
        The query to submit. A body with `target` enqueues a write run
        .
    request
        The request, used to reach the statement length limit.
    principal
        The authenticated caller.
    queries
        The query service for this request.

    Returns
    -------
    QuerySubmitOut
        The submitted query's id.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ValidationError
        Mapped to 422: the SQL is longer than `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`.
    """
    check_statement(request, body.sql)
    if body.target is None:
        run = queries.submit_query(principal, env, body.sql)
    else:
        run = queries.submit_write(
            with_break_glass(principal, body.break_glass_reason),
            env,
            body.sql,
            WriteTarget(
                table=body.target.table,
                mode=WriteMode(body.target.mode),
                incremental_key=body.target.incremental_key,
                evolve_schema=body.target.evolve_schema,
            ),
        )
    return QuerySubmitOut(query_id=str(run.id))


@router.get("/{query_id}", response_model=QueryStatusOut, responses=errors(403, 404))
def get_query_status(
    env: str,
    query_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    queries: QueryService = Depends(get_query_service),
) -> QueryStatusOut:
    """Show a query's status.

    Needs `SUBMIT_QUERY` on the environment. Only the principal that submitted the query can
    see it; for anyone else the answer is 404, as for a query that does not exist.

    Parameters
    ----------
    env
        Environment name.
    query_id
        The query's id.
    principal
        The authenticated caller.
    queries
        The query service for this request.

    Returns
    -------
    QueryStatusOut
        The query's status, and its error or lineage once finished.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`, if the query does not
        exist or was submitted by another principal.
    """
    run = queries.get_query_run(principal, env, query_id)
    return _status_out(run, queries.waiting_for(run))


@router.post("/{query_id}/cancel", response_model=QueryStatusOut, responses=errors(403, 404, 409))
def cancel_query(
    env: str,
    query_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    queries: QueryService = Depends(get_query_service),
) -> QueryStatusOut:
    """Cancel a query.

    A queued query becomes `CANCELLED` at once. A running one is asked to stop: the answer
    is `RUNNING` and the worker ends it within a few seconds. A query that has already ended
    is answered with its status, which is not an error. The submitter and a holder of
    `MANAGE_ENVIRONMENT` may cancel; anyone else gets 404, and a caller with neither
    `SUBMIT_QUERY` nor `MANAGE_ENVIRONMENT` gets 403. Answers 422 for the write of a
    pipeline task (cancel the run instead) and for a maintenance run that has started, and
    409 when a worker is starting or finishing the query at that moment: ask for the status
    and cancel again if it still runs.

    Parameters
    ----------
    env
        Environment name.
    query_id
        The query's id.
    principal
        The authenticated caller.
    queries
        The query service for this request.

    Returns
    -------
    QueryStatusOut
        The query's status after the request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`, exactly as for the status read: if
        the query does not exist, or the caller neither submitted it nor may manage the
        environment.
    lakana_core.errors.PermissionDenied
        Mapped to 403, if the caller may not read queries in this environment.
    lakana_core.errors.ValidationError
        Mapped to 422: the query is the write of a pipeline task (cancel the run), or a
        maintenance run that has started.
    lakana_core.errors.ConflictError
        Mapped to 409: a worker is starting the query or finishing it at this moment, so the
        cancel found its row locked and changed nothing; ask for the status and cancel again
        if it is still running.
    """
    run = queries.cancel_query(principal, env, query_id)
    return _status_out(run, queries.waiting_for(run))


#: What a succeeded run without a result did instead, by write mode (`None` is a read); a verb
#: phrase that follows "Query ID". A test checks that every write mode has one.
_NO_RESULT_CLAUSES: dict[WriteMode | None, str] = {
    None: "has no stored result",
    WriteMode.CREATE: "wrote to a table",
    WriteMode.REPLACE: "wrote to a table",
    WriteMode.APPEND: "wrote to a table",
    WriteMode.MERGE: "wrote to a table",
    WriteMode.VIEW: "created a view",
    WriteMode.MAINTAIN: "was a maintenance run",
}


@router.get("/{query_id}/result", responses=errors(403, 404, 409))
def get_query_result(
    env: str,
    query_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    queries: QueryService = Depends(get_query_service),
    session: Session = Depends(get_session),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> Response:
    """Fetch a query's result.

    Streams the rows of a finished read as an Arrow IPC stream. Needs `SUBMIT_QUERY` on the
    environment. Only the submitter can fetch them; for anyone else the answer is 404.
    Answers 409 unless the query has succeeded, or when it succeeded without being a read
    (it wrote to a table, created a view or was maintenance): only a read has result rows.

    Parameters
    ----------
    env
        Environment name.
    query_id
        The query's id.
    principal
        The authenticated caller.
    queries
        The query service for this request.
    session
        The current request's database session, used to resolve the
        environment's storage the same way `get_catalog` does — this
        endpoint reads through `Storage.read`, never a raw filesystem path,
        because the worker writes the result through `Storage.write` and
        `run.result_location` is a root-relative path, not an absolute one
        (it must work identically for a local or an S3 environment).
    key_provider
        Decrypts the environment's storage credentials if it is a
        secret-backed environment — the query's own execution already
        used the same credentials, so its result lives behind them too.

    Returns
    -------
    Response
        The result's raw Arrow IPC bytes.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`, if the query does not
        exist or was submitted by another principal.
    lakana_core.errors.ConflictError
        Mapped to 409, if the query has not finished yet, or if it succeeded but is not a
        read (it wrote to a table, created a view or was a maintenance run): only a read has
        result rows, and the message names ``lakana query status ID --env ENV`` to see what
        it did.
    """
    run = queries.get_query_run(principal, env, query_id)
    if run.status != QueryRunStatus.SUCCEEDED:
        raise ConflictError(f"Query {query_id} has not succeeded yet (status: {run.status.value}).")
    if run.result_location is None:
        # Only a read stores a result; a succeeded write, view or maintenance run (also one
        # the scheduler's sweep ended) never has one.
        raise ConflictError(
            f"Query {query_id} {_NO_RESULT_CLAUSES[run.write_mode]}, so it has no result rows to "
            "download. "
            f"See what it did with: lakana query status {query_id} --env {env}"
        )
    resolved = StorageResolver(session, key_provider).for_environment(env)
    content = resolved.storage.read(run.result_location)
    return Response(content=content, media_type="application/vnd.apache.arrow.file")
