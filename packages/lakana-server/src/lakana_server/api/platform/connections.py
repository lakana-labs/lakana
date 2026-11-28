"""The connections resource of the platform API.

A connection is addressed through its environment (`/api/v1/environments/{name}/connections`):
it lives there as a secret does. `MANAGE_CONNECTION` creates, changes and deletes;
`READ_ENVIRONMENT` lists and shows. No answer carries a secret value or a CA certificate.
`/api/v1/connections` lists the connections of every environment the caller may read, and those
environments; `/api/v1/environments/{name}/connection-tests` tests every connection of one
environment (Test all).

Every route that answers a connection leaves unset fields out (`response_model_exclude_unset`),
so that the fields of `last_test` a reader may not see are absent, not `null`; every other field
is always set.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import SecretStr

from lakana_core.auth import PrincipalRef
from lakana_core.connections.service import TEST_ALL_MAX, ConnectionService, ConnectionView
from lakana_core.models import ConnectionTest
from lakana_server.api.platform.listing import CursorParam, OrderParam, SortParam
from lakana_server.api.platform.schemas import (
    ConnectionCreate,
    ConnectionEnvironmentOut,
    ConnectionLastTestOut,
    ConnectionOut,
    ConnectionPage,
    ConnectionQueuedTestOut,
    ConnectionQueuedTestPage,
    ConnectionRunFailureOut,
    ConnectionTestOut,
    ConnectionUpdate,
    DeclaringAssetOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_connection_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{environment_name}/connections",
    tags=["connections"],
    responses=errors(401),
)

all_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/connections",
    tags=["connections"],
    responses=errors(401),
)
"""The connections of every environment the caller may read, and those environments."""

tests_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{environment_name}/connection-tests",
    tags=["connections"],
    responses=errors(401),
)
"""Test all: a test of every connection of an environment, a page at a time.

Not `.../connections/tests`, which would read as the tests of a connection named ``tests``.
"""


def _to_out(view: ConnectionView) -> ConnectionOut:
    """Build a response body from what the service shows.

    Parameters
    ----------
    view
        The connection as its reader may see it.

    Returns
    -------
    ConnectionOut
        The body; it has no field for a secret value. Every field is set, and the id, request
        time, message and requester of `last_test` only when the view carries them, so that
        `response_model_exclude_unset` leaves them out otherwise.
    """
    last_test = None
    if view.last_test is not None:
        test = view.last_test
        detail: dict[str, Any] = {}
        if test.detail is not None:
            detail = {
                "id": test.detail.id,
                "created_at": test.detail.created_at,
                "message": test.detail.message,
                "requested_by": test.detail.requested_by,
            }
        last_test = ConnectionLastTestOut(
            state=test.state.value,
            outcome=None if test.outcome is None else test.outcome.value,
            finished_at=test.finished_at,
            **detail,
        )
    return ConnectionOut(
        name=view.name,
        kind=view.kind.value,
        address=view.address,
        settings=view.settings,
        secret=view.secret,
        declared_by=[
            DeclaringAssetOut(repository=repository, asset=asset)
            for repository, asset in view.declared_by
        ],
        created_by=view.created_by,
        created_at=view.created_at,
        updated_at=view.updated_at,
        environment=view.environment,
        last_test=last_test,
        actions=list(view.actions),
        last_run_failure=(
            None
            if view.last_run_failure is None
            else ConnectionRunFailureOut(**dataclasses.asdict(view.last_run_failure))
        ),
    )


def _plain(values: dict[str, SecretStr] | None) -> dict[str, str] | None:
    """Unwrap new secret values for the service, the only place they are read.

    Parameters
    ----------
    values
        The values as the body carried them.

    Returns
    -------
    dict of str to str or None
        The plain values, or `None` when none were given.
    """
    if values is None:
        return None
    return {key: value.get_secret_value() for key, value in values.items()}


@router.post(
    "",
    response_model=ConnectionOut,
    response_model_exclude_unset=True,
    status_code=201,
    responses=errors(403, 404, 409),
)
def create_connection(
    environment_name: str,
    body: ConnectionCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionOut:
    """Create a connection.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Creates a connection of the environment: its kind, address and settings, and its secret,
    either new values (stored as a new secret `<name>-connection`, or the next free
    `<name>-connection-N` when a secret holds that name, born bound to it, never shown again)
    or the name of an existing secret. Needs `MANAGE_CONNECTION` in the environment,
    checked before anything in the body; naming a secret that is not bound to this connection
    also needs `MANAGE_SECRET`. Answers 403 without the right, 404 when the environment or the
    named secret does not exist, 409 when the name is taken (or every secret name up to
    `<name>-connection-100` is), and 422 for
    a name, kind, address, settings or secret keys that are not valid, naming the field. Answers
    500 when the instance has no secrets master key.

    Parameters
    ----------
    environment_name
        The environment.
    body
        The connection.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionOut
        The created connection.
    """
    view = service.create(
        principal,
        environment_name,
        name=body.name,
        kind=body.kind,
        address=body.address,
        settings=body.settings,
        secret=body.secret,
        secret_values=_plain(body.secret_values),
    )
    return _to_out(view)


@router.get(
    "", response_model=ConnectionPage, response_model_exclude_unset=True, responses=errors(403, 404)
)
def list_connections(
    environment_name: str,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: CursorParam = None,
    sort: SortParam = None,
    order: OrderParam = None,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionPage:
    """List the connections of an environment.

    Lists one page of connections, newest first, with the assets that declare each, its last
    test and what you may do with it (`actions`). Needs `READ_ENVIRONMENT` in the environment; a
    task token is refused with 403. Every reader gets the last test's `state`, `outcome` and
    `finished_at`; its `id`, `created_at`, `message` and `requested_by` only a holder of
    `MANAGE_CONNECTION` in the environment, and for anyone else those keys are absent. `sort`
    and `order` order the list by another field; one state is ordered by name. Answers 404
    when the environment does not exist and 422 for an unknown `sort` or `order` or a cursor
    this list did not issue in this order.

    Parameters
    ----------
    environment_name
        The environment.
    limit
        Most connections returned (1 to 200).
    cursor
        The `next_cursor` of the previous page.
    sort
        What the list is sorted by: `created` (the default), `name` (in any letter case) or
        `status`, the last test: failed first, then queued or running, then passed, never
        tested last; `kind` or `environment`; one state, kind or environment by name.
    order
        `asc` or `desc`; by default `desc` for `created` (the newest first), `asc` for the
        others.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionPage
        The connections of this page, and the cursor of the next.
    """
    page = service.list(
        principal, environment_name, limit=limit, cursor=cursor, sort=sort, order=order
    )
    return ConnectionPage(
        items=[_to_out(view) for view in page.items], next_cursor=page.next_cursor
    )


@router.get(
    "/{connection_name}",
    response_model=ConnectionOut,
    response_model_exclude_unset=True,
    responses=errors(403, 404),
)
def get_connection(
    environment_name: str,
    connection_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionOut:
    """Show a connection.

    Shows its address, settings (a CA certificate as its SHA-256 fingerprint), the name of its
    secret, the assets that declare it, its last test and what you may do with it (`actions`).
    Needs `READ_ENVIRONMENT` in the environment; a task token is refused with 403, naming no
    connection. Every reader gets the last test's `state`, `outcome` and `finished_at`; its
    `id`, `created_at`, `message` and `requested_by` only a holder of `MANAGE_CONNECTION` in the
    environment, and for anyone else those keys are absent. Answers 404 when the environment or
    the connection does not exist; the answer names no other connection.

    Parameters
    ----------
    environment_name
        The environment.
    connection_name
        The connection's name.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionOut
        The connection.
    """
    return _to_out(service.get(principal, environment_name, connection_name))


@router.patch(
    "/{connection_name}",
    response_model=ConnectionOut,
    response_model_exclude_unset=True,
    responses=errors(403, 404, 409),
)
def update_connection(
    environment_name: str,
    connection_name: str,
    body: ConnectionUpdate,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionOut:
    """Change a connection.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Changes its address, settings or secret; its name and kind never change. New secret values
    set its own secret again (or create the next free `<name>-connection`); `clear` with
    `secret` stops using
    the secret and unbinds it. Needs `MANAGE_CONNECTION` in the environment, and also
    `MANAGE_SECRET` to name a secret not bound to it, or for a change that could send its
    secret somewhere new (its address, a weaker TLS, another CA certificate, another way of
    sending it) without new values. Answers 403 without the rights, 404 when the environment,
    the connection or the named secret does not exist, 409 when every secret name up to
    `<name>-connection-100` is taken or the connection changed at the same time, and 422 when
    nothing is given or
    the result is not valid, naming the field.

    Parameters
    ----------
    environment_name
        The environment.
    connection_name
        The connection's name.
    body
        Only what is given changes.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionOut
        The changed connection.
    """
    view = service.update(
        principal,
        environment_name,
        connection_name,
        address=body.address,
        settings=body.settings,
        secret=body.secret,
        secret_values=_plain(body.secret_values),
        clear=body.clear,
    )
    return _to_out(view)


@router.delete("/{connection_name}", status_code=204, responses=errors(403, 404, 409))
def delete_connection(
    environment_name: str,
    connection_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> None:
    """Delete a connection.

    Deletes it and unbinds its secret, which stays in the environment. Needs
    `MANAGE_CONNECTION` in the environment. Answers 404 when the environment or the connection
    does not exist, and 409, naming a declaring asset, while a deployed project version
    declares it: deploy a version without it first.

    Parameters
    ----------
    environment_name
        The environment.
    connection_name
        The connection's name.
    principal
        The authenticated caller.
    service
        The connection service for this request.
    """
    service.delete(principal, environment_name, connection_name)


def _test_out(test: ConnectionTest) -> ConnectionTestOut:
    """Build a response body from a test row.

    Parameters
    ----------
    test
        The test.

    Returns
    -------
    ConnectionTestOut
        The body.
    """
    return ConnectionTestOut(
        id=test.id,
        state=test.state.value,
        outcome=test.outcome.value if test.outcome is not None else None,
        message=test.message,
        created_at=test.created_at,
        finished_at=test.finished_at,
    )


@router.post(
    "/{connection_name}/tests",
    response_model=ConnectionTestOut,
    status_code=202,
    responses=errors(403, 404, 409),
)
def request_connection_test(
    environment_name: str,
    connection_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionTestOut:
    """Queue a test of a connection.

    A worker checks that the connection's host can be reached with its settings and secret and
    records an outcome; poll the test with `GET .../tests/{test_id}`. Needs
    `MANAGE_CONNECTION` in the environment. Answers 404 when the environment or the
    connection does not exist and 409, naming the open test, while a test of the connection that
    is under 10 minutes old is queued or running; an older open test is finished as failed first.

    Parameters
    ----------
    environment_name
        The environment.
    connection_name
        The connection's name.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionTestOut
        The queued test.
    """
    return _test_out(service.request_test(principal, environment_name, connection_name))


@tests_router.post(
    "",
    response_model=ConnectionQueuedTestPage,
    status_code=202,
    responses=errors(403, 404, 409),
)
def request_connection_tests(
    environment_name: str,
    limit: int = Query(default=TEST_ALL_MAX, ge=1, le=TEST_ALL_MAX),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionQueuedTestPage:
    """Queue a test of every connection of an environment (Test all), a page at a time.

    Takes one page of the environment's connection list, newest first (`limit` 1 to 200,
    default 200), and handles each connection in turn: a connection whose test is queued or
    running and under 10 minutes old is not queued again, and that test is returned with
    `queued: false`; an older open test is finished as failed first; every other connection
    gets a new test with `queued: true`. An open test is never a 409. Poll each test with
    `GET .../connections/{connection}/tests/{test_id}`, and send `next_cursor` back as
    `cursor` for the next page; it takes the cursors of
    `GET /api/v1/environments/{environment_name}/connections` and refuses others with 422.
    Needs `MANAGE_CONNECTION` in the environment, as a single test does. Answers 404 when the
    environment does not exist, and 409 when a connection changed at the same time: the
    connections before it stay queued, and sending the request again returns their tests with
    `queued: false`.

    Parameters
    ----------
    environment_name
        The environment.
    limit
        Most connections tested (1 to 200).
    cursor
        The `next_cursor` of the previous page.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionQueuedTestPage
        A test for each connection of this page, and the cursor of the next.
    """
    page = service.request_tests(principal, environment_name, limit=limit, cursor=cursor)
    return ConnectionQueuedTestPage(
        items=[
            ConnectionQueuedTestOut(
                connection=item.connection, test_id=item.test.id, queued=item.queued
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{connection_name}/tests/{test_id}",
    response_model=ConnectionTestOut,
    responses=errors(403, 404),
)
def get_connection_test(
    environment_name: str,
    connection_name: str,
    test_id: str,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionTestOut:
    """Show a test of a connection: its state and, once done, its outcome and message.

    Needs `MANAGE_CONNECTION` in the environment. Answers 404 when the environment or the
    connection does not exist, or the connection has no such test.

    Parameters
    ----------
    environment_name
        The environment.
    connection_name
        The connection's name.
    test_id
        The test's id.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionTestOut
        The test.
    """
    return _test_out(service.test_result(principal, environment_name, connection_name, test_id))


@all_router.get(
    "", response_model=ConnectionPage, response_model_exclude_unset=True, responses=errors(403, 404)
)
def list_all_connections(
    environment: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: CursorParam = None,
    sort: SortParam = None,
    order: OrderParam = None,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionPage:
    """List the connections of every environment you may read.

    Without `environment`, lists one page of the connections of every environment where you
    hold `READ_ENVIRONMENT`, newest first over all of them; each names its `environment`. An
    environment where you do not hold it is left out, so this is never a 404, and never a 403
    but for a task token, which is refused as every connection read is. With `environment`, it
    answers exactly what `GET /api/v1/environments/{environment_name}/connections` answers,
    with its 403 and 404. Each connection carries its last test and what you may do with it
    (`actions`), as in that list: the last test's `id`, `created_at`, `message` and
    `requested_by` only where you hold `MANAGE_CONNECTION`, and absent elsewhere. `sort` and
    `order` order the list as on the list of one environment. Send `next_cursor` back as
    `cursor` with the same sort for the next page; a cursor of the list of one environment or
    of another order is refused with 422 here.

    Parameters
    ----------
    environment
        Only this environment.
    limit
        Most connections returned (1 to 200).
    cursor
        The `next_cursor` of the previous page.
    sort
        What the list is sorted by, with the fields of the list of one environment.
    order
        `asc` or `desc`, each field's way by default, as on the list of one environment.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionPage
        The connections of this page, and the cursor of the next.
    """
    page = service.list_all(
        principal, environment=environment, limit=limit, cursor=cursor, sort=sort, order=order
    )
    return ConnectionPage(
        items=[_to_out(view) for view in page.items], next_cursor=page.next_cursor
    )


@all_router.get(
    "/environments", response_model=list[ConnectionEnvironmentOut], responses=errors(403)
)
def list_connection_environments(
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> list[ConnectionEnvironmentOut]:
    """List the environments whose connections you may read, and what you may do there.

    Every environment where you hold `READ_ENVIRONMENT`, sorted by name, with its `actions`:
    `create` and `test` where you hold `MANAGE_CONNECTION`, and `name_secret` (naming an
    existing secret for a connection) where you hold `MANAGE_SECRET` too. Empty when there is
    none. A task token is refused with 403, as every connection read is.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    list of ConnectionEnvironmentOut
        The environments.
    """
    return [
        ConnectionEnvironmentOut(name=env.name, actions=list(env.actions))
        for env in service.environments(principal)
    ]
