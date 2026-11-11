"""The repository checks of the platform API: a host's SSH keys and "Test access".

The server only coordinates: it queues a check and answers it, and a worker runs it (the server
makes no outbound connection to a git host). `MANAGE_REPOSITORY` starts either check; an access
check also needs `READ_ENVIRONMENT` on its environment. Only the person who asked reads a
check; checks are kept for 24 hours.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.models import RepositoryProbe
from lakana_core.projects.probes import RepositoryProbeService
from lakana_server.api.platform.schemas import HostKeyOut, RepositoryProbeIn, RepositoryProbeOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_repository_probe_service

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/repository-probes",
    tags=["repositories"],
    responses=errors(401),
)


def _to_out(probe: RepositoryProbe) -> RepositoryProbeOut:
    """Build a response body from a probe row.

    Parameters
    ----------
    probe
        The probe.

    Returns
    -------
    RepositoryProbeOut
        The body.
    """
    return RepositoryProbeOut(
        id=probe.id,
        kind=probe.kind.value,
        state=probe.state.value,
        outcome=probe.outcome.value if probe.outcome is not None else None,
        message=probe.message,
        host_keys=None
        if probe.host_keys is None
        else [HostKeyOut(**key) for key in probe.host_keys],
        created_at=probe.created_at,
        finished_at=probe.finished_at,
    )


@router.post(
    "",
    response_model=RepositoryProbeOut,
    status_code=202,
    responses=errors(403, 404, 409, 503),
)
def request_repository_probe(
    body: RepositoryProbeIn,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryProbeService = Depends(get_repository_probe_service),
) -> RepositoryProbeOut:
    """Queue a repository check; poll it with `GET /api/v1/repository-probes/{probe_id}`.

    ``host_keys`` fetches the SSH host keys of `url`'s host and port, for the person to compare
    with the git host's published fingerprints before trusting any. ``access`` runs
    `git ls-remote` for registered `repository` with `environment`'s credential and the
    repository's own known hosts. Needs `MANAGE_REPOSITORY` (and `READ_ENVIRONMENT` on the
    environment for ``access``). Answers 422 for a body without exactly its kind's fields or an
    unsafe URL, 404 for an unknown repository or environment, 409 while the caller has five
    checks waiting or running, and 503 when no worker that takes pipeline work is running. A
    check no worker takes within two minutes ends ``no_worker``; one a worker took ends at most
    90 seconds later, so every check ends within three and a half minutes of its request.

    Parameters
    ----------
    body
        The check.
    principal
        The authenticated caller.
    service
        The probe service for this request.

    Returns
    -------
    RepositoryProbeOut
        The queued check.
    """
    if body.kind == "host_keys":
        assert body.url is not None  # nosec B101 -- the body's validator checked
        probe = service.request_host_keys(principal, body.url)
    else:
        assert body.repository is not None and body.environment is not None  # nosec B101
        probe = service.request_access(principal, body.repository, body.environment)
    return _to_out(probe)


@router.get("/{probe_id}", response_model=RepositoryProbeOut, responses=errors(404))
def get_repository_probe(
    probe_id: str,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryProbeService = Depends(get_repository_probe_service),
) -> RepositoryProbeOut:
    """Show one of the caller's repository checks.

    Answers 404 for a check of another person, an unknown id, or one older than 24 hours.

    Parameters
    ----------
    probe_id
        The check's id.
    principal
        The authenticated caller.
    service
        The probe service for this request.

    Returns
    -------
    RepositoryProbeOut
        The check.
    """
    return _to_out(service.get(principal, probe_id))
