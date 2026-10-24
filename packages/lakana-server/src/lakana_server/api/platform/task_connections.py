"""The run-time credential fetch: a running task reads one of the connections it declares.

`GET /api/v1/tasks/self/connections/{name}` is for the helpers of `lakana.ingest` inside a task;
it answers only the task's own bound token (`lakana_core.connections.fetch`). Its answer is the
only one of the API that carries a secret's values: it is marked `no-store`, and the access log
records the request line and status only, never a body.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request, Response
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.connections.fetch import fetch_for_task
from lakana_core.secrets.keys import KeyProvider
from lakana_server.api.platform.schemas import EndpointOut, TaskConnectionOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_key_provider, get_principal, get_session

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/tasks/self/connections",
    tags=["connections"],
    responses=errors(401),
)


@router.get("/{name}", response_model=TaskConnectionOut, responses=errors(403))
def get_task_connection(
    request: Request,
    response: Response,
    name: str = Path(max_length=63, description="The connection's name."),
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> TaskConnectionOut:
    """Fetch a connection the running task's asset declares, with its secret.

    For the helpers of `lakana.ingest` inside a Lakana task; nothing else needs it. Answers
    only the task's own token while its attempt runs, for a connection its deployed asset
    declares in `@asset(connections=[...])`, in the task's environment. Every call is in the
    audit log: `CONNECTION_CREDENTIAL_FETCHED` (committed before the answer is sent) or
    `CONNECTION_CREDENTIAL_REFUSED`. Answers 403 with "only a running task can fetch a
    connection" for any other token, "connection `NAME` is not declared by asset `ASSET`" for
    a name the asset does not declare (whether or not a connection of that name exists), and
    "connection `NAME` is not in environment `ENV`" for a declared name the environment lacks;
    401 for a token whose attempt has ended or was claimed again. The answer is never cached
    (`Cache-Control: no-store`, `Pragma: no-cache`).

    Parameters
    ----------
    request
        The current request, to reach the process's denial audit limiter.
    response
        The response, to set the caching headers.
    name
        The connection's name.
    principal
        The authenticated caller.
    session
        The current request's database session.
    key_provider
        Decrypts the connection's secret.

    Returns
    -------
    TaskConnectionOut
        The connection, its endpoints and its secret's values.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    found = fetch_for_task(
        session, key_provider, principal, name, limiter=request.app.state.denial_audit_limiter
    )
    return TaskConnectionOut(
        name=found.name,
        kind=found.kind.value,
        address=found.address,
        settings=found.settings,
        endpoints=[
            EndpointOut(host=endpoint.host, port=endpoint.port, role=endpoint.role)
            for endpoint in found.endpoints
        ],
        secret=found.secret,
    )
