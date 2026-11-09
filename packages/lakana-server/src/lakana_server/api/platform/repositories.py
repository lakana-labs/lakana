"""The repositories resource of the platform API.

Instance-wide: a repository is registered once by an admin and named by
deployers in every environment.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.models import Repository, RepositoryKind
from lakana_core.projects.repositories import RepositoryService
from lakana_server.api.platform.schemas import RepositoryCreate, RepositoryOut, RepositoryUpdate
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_repository_service

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/repositories",
    tags=["repositories"],
    responses=errors(401),
)


def _to_out(repo: Repository) -> RepositoryOut:
    """Build a response body.

    Parameters
    ----------
    repo
        The row.

    Returns
    -------
    RepositoryOut
        The body.
    """
    return RepositoryOut(
        id=repo.id,
        name=repo.name,
        kind=repo.kind.value,
        url=repo.url,
        credential_secret=repo.credential_secret,
        credential_username=repo.credential_username,
        ssh_known_hosts=repo.ssh_known_hosts,
        default_ref=repo.default_ref,
        created_by=repo.created_by,
        created_at=repo.created_at,
        updated_at=repo.updated_at,
    )


@router.post("", response_model=RepositoryOut, status_code=201, responses=errors(403, 409))
def create_repository(
    body: RepositoryCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> RepositoryOut:
    """Register a git repository.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Registers a repository that environments can deploy, by name, URL and optionally a
    credential secret and known hosts. Deployers pick the name and never the URL. Needs
    `MANAGE_REPOSITORY` on the instance. Answers 409 when the name is taken and 422 for a
    field that is not valid.

    Parameters
    ----------
    body
        The repository.
    principal
        The authenticated caller.
    service
        The repository service.

    Returns
    -------
    RepositoryOut
        The registered repository.
    """
    repo = service.create(
        principal,
        name=body.name,
        kind=RepositoryKind(body.kind),
        url=body.url,
        credential_secret=body.credential_secret,
        credential_username=body.credential_username,
        ssh_known_hosts=body.ssh_known_hosts,
        default_ref=body.default_ref,
    )
    return _to_out(repo)


@router.get("", response_model=list[RepositoryOut], responses=errors(403))
def list_repositories(
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> list[RepositoryOut]:
    """List repositories.

    Needs `MANAGE_REPOSITORY` on the instance, or `READ_ENVIRONMENT` on any environment.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The repository service.

    Returns
    -------
    list of RepositoryOut
        Sorted by name.
    """
    return [_to_out(repo) for repo in service.list_repositories(principal)]


@router.get("/{name}", response_model=RepositoryOut, responses=errors(403, 404))
def get_repository(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> RepositoryOut:
    """Show a repository.

    Needs `MANAGE_REPOSITORY` on the instance, or `READ_ENVIRONMENT` on any environment.
    Answers 404 when no repository has that name.

    Parameters
    ----------
    name
        The repository's registered name.
    principal
        The authenticated caller.
    service
        The repository service.

    Returns
    -------
    RepositoryOut
        The repository.
    """
    return _to_out(service.get(principal, name))


@router.patch("/{name}", response_model=RepositoryOut, responses=errors(403, 404))
def update_repository(
    name: str,
    body: RepositoryUpdate,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> RepositoryOut:
    """Change a repository.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Changes its URL, credential secret, username, known hosts or default ref. Needs
    `MANAGE_REPOSITORY` on the instance; changing the URL of a repository whose secret is
    bound also needs `MANAGE_SECRET` in that environment. Answers 404 when no repository has
    that name and 422 when nothing is given or a field cannot be changed.

    Parameters
    ----------
    name
        The repository's registered name.
    body
        Only the fields present change; `null` clears.
    principal
        The authenticated caller.
    service
        The repository service.

    Returns
    -------
    RepositoryOut
        The updated repository.
    """
    return _to_out(service.update(principal, name, body.model_dump(exclude_unset=True)))


@router.delete("/{name}", status_code=204, responses=errors(403, 404, 409))
def delete_repository(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
) -> None:
    """Delete a repository.

    Needs `MANAGE_REPOSITORY` on the instance. Answers 404 when no repository has that name
    and 409 while an environment still deploys it: undeploy it there first.

    Parameters
    ----------
    name
        The repository's registered name.
    principal
        The authenticated caller.
    service
        The repository service.
    """
    service.delete(principal, name)
