"""The connections resource of the platform API.

A connection is addressed through its environment (`/api/v1/environments/{name}/connections`):
it lives there as a secret does. `MANAGE_CONNECTION` creates, changes and deletes;
`READ_ENVIRONMENT` lists and shows. No answer carries a secret value or a CA certificate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import SecretStr

from lakana_core.auth import PrincipalRef
from lakana_core.connections.service import ConnectionService, ConnectionView
from lakana_core.models import ConnectionTest
from lakana_server.api.platform.schemas import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionPage,
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


def _to_out(view: ConnectionView) -> ConnectionOut:
    """Build a response body from what the service shows.

    Parameters
    ----------
    view
        The connection as its reader may see it.

    Returns
    -------
    ConnectionOut
        The body; it has no field for a secret value.
    """
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


@router.post("", response_model=ConnectionOut, status_code=201, responses=errors(403, 404, 409))
def create_connection(
    environment_name: str,
    body: ConnectionCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionOut:
    """Create a connection.

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


@router.get("", response_model=ConnectionPage, responses=errors(403, 404))
def list_connections(
    environment_name: str,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionPage:
    """List the connections of an environment.

    Lists one page of connections, newest first, with the assets that declare each. Needs
    `READ_ENVIRONMENT` in the environment; a task token is refused with 403. Answers 404 when
    the environment does not exist and 422 for a cursor this list did not issue.

    Parameters
    ----------
    environment_name
        The environment.
    limit
        Most connections returned (1 to 200).
    cursor
        The `next_cursor` of the previous page.
    principal
        The authenticated caller.
    service
        The connection service for this request.

    Returns
    -------
    ConnectionPage
        The connections of this page, and the cursor of the next.
    """
    page = service.list(principal, environment_name, limit=limit, cursor=cursor)
    return ConnectionPage(
        items=[_to_out(view) for view in page.items], next_cursor=page.next_cursor
    )


@router.get("/{connection_name}", response_model=ConnectionOut, responses=errors(403, 404))
def get_connection(
    environment_name: str,
    connection_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionOut:
    """Show a connection.

    Shows its address, settings (a CA certificate as its SHA-256 fingerprint), the name of its
    secret and the assets that declare it. Needs `READ_ENVIRONMENT` in the environment; a task
    token is refused with 403, naming no connection. Answers 404 when the environment or the
    connection does not exist; the answer names no other connection.

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


@router.patch("/{connection_name}", response_model=ConnectionOut, responses=errors(403, 404, 409))
def update_connection(
    environment_name: str,
    connection_name: str,
    body: ConnectionUpdate,
    principal: PrincipalRef = Depends(get_principal),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionOut:
    """Change a connection.

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
    connection does not exist and 409 while a test of the connection is queued or running.

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
