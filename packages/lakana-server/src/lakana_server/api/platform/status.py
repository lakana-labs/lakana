"""The instance status of the platform API: healthy, degraded or down, and why.

Home and `lakana status` read it. Any authenticated caller may ask; the causes beyond a
missing or lost worker and the detail (slots, memory, the workers) are only for a caller who may
manage environments on the instance (`lakana_core.runs.instance_status`). The readiness checks
behind it are the ones `/readyz` runs, from the same cache, so a page that polls this route
costs no more than one check per `LAKANA_READINESS__CACHE_SECONDS`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from lakana_core.auth import PrincipalRef
from lakana_core.runs.service import RunService
from lakana_server.api.platform.runs import worker_out
from lakana_server.api.platform.schemas import InstanceStatusOut, StatusDetailOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_run_service

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/status",
    tags=["status"],
    responses=errors(401),
)


@router.get("", response_model=InstanceStatusOut)
async def instance_status(
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> InstanceStatusOut:
    """Show whether the instance is healthy.

    Says whether the instance is healthy, degraded or down, and why. Any authenticated caller
    may ask. A caller with `MANAGE_ENVIRONMENT` on the instance also sees every cause and the
    detail: busy and total task slots, the workers' memory against their limits, and the
    workers.

    Parameters
    ----------
    request
        The current request, used to reach the server's cached readiness check.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    InstanceStatusOut
        The state, the causes the caller may see, the counts of workers and, for an
        administrator, the detail.
    """
    failed = await request.app.state.readiness.failed()
    status = await run_in_threadpool(service.instance_status, principal, failed)
    detail = status.detail
    return InstanceStatusOut(
        state=status.state,
        causes=list(status.causes),
        workers_alive=status.workers_alive,
        workers_total=status.workers_total,
        detail=None
        if detail is None
        else StatusDetailOut(
            slots_busy=detail.slots_busy,
            slots_total=detail.slots_total,
            memory_used_bytes=detail.memory_used_bytes,
            memory_limit_bytes=detail.memory_limit_bytes,
            workers=[worker_out(w) for w in detail.workers],
        ),
    )
