"""The repositories resource of the platform API.

Instance-wide: a repository is registered once by an admin and named by
deployers in every environment.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.models import Repository, RepositoryKind
from lakana_core.projects.deploy_keys import DeployKeyService
from lakana_core.projects.repositories import RepositoryService
from lakana_server.api.platform.schemas import (
    DeployKeyIn,
    DeployKeyMadeOut,
    DeployKeyOut,
    RepositoryCreate,
    RepositoryDeployKeyOut,
    RepositoryOut,
    RepositoryUpdate,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_deploy_key_service, get_principal, get_repository_service

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/repositories",
    tags=["repositories"],
    responses=errors(401),
)


def _to_out(
    repos: list[Repository], principal: PrincipalRef, keys: DeployKeyService
) -> list[RepositoryOut]:
    """Build response bodies, each with the deploy keys the caller may see.

    Parameters
    ----------
    repos
        The rows.
    principal
        The caller; a deploy key is shown only for an environment they may read.
    keys
        Lists the deploy keys.

    Returns
    -------
    list of RepositoryOut
        The bodies, in the order of `repos`.
    """
    shown = keys.deploy_keys(principal, repos)
    return [
        RepositoryOut(
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
            deploy_keys=[
                RepositoryDeployKeyOut(
                    environment=key.environment,
                    public_key=key.public_key,
                    fingerprint=key.fingerprint,
                )
                for key in shown.get(repo.id, ())
            ],
        )
        for repo in repos
    ]


@router.post("", response_model=RepositoryOut, status_code=201, responses=errors(403, 409))
def create_repository(
    body: RepositoryCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
    keys: DeployKeyService = Depends(get_deploy_key_service),
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
    keys
        Lists the deploy keys shown.

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
    return _to_out([repo], principal, keys)[0]


@router.get("", response_model=list[RepositoryOut], responses=errors(403))
def list_repositories(
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
    keys: DeployKeyService = Depends(get_deploy_key_service),
) -> list[RepositoryOut]:
    """List repositories.

    Needs `MANAGE_REPOSITORY` on the instance, or `READ_ENVIRONMENT` on any environment.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The repository service.
    keys
        Lists the deploy keys shown.

    Returns
    -------
    list of RepositoryOut
        Sorted by name.
    """
    return _to_out(service.list_repositories(principal), principal, keys)


@router.get("/{name}", response_model=RepositoryOut, responses=errors(403, 404))
def get_repository(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
    keys: DeployKeyService = Depends(get_deploy_key_service),
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
    keys
        Lists the deploy keys shown.

    Returns
    -------
    RepositoryOut
        The repository.
    """
    return _to_out([service.get(principal, name)], principal, keys)[0]


@router.patch("/{name}", response_model=RepositoryOut, responses=errors(403, 404))
def update_repository(
    name: str,
    body: RepositoryUpdate,
    principal: PrincipalRef = Depends(get_principal),
    service: RepositoryService = Depends(get_repository_service),
    keys: DeployKeyService = Depends(get_deploy_key_service),
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
    keys
        Lists the deploy keys shown.

    Returns
    -------
    RepositoryOut
        The updated repository.
    """
    repo = service.update(principal, name, body.model_dump(exclude_unset=True))
    return _to_out([repo], principal, keys)[0]


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


@router.post(
    "/{name}/deploy-key",
    response_model=DeployKeyOut,
    status_code=201,
    responses=errors(403, 404, 409),
)
def make_deploy_key(
    name: str,
    body: DeployKeyIn | None = None,
    principal: PrincipalRef = Depends(get_principal),
    keys: DeployKeyService = Depends(get_deploy_key_service),
) -> DeployKeyOut:
    """Make an SSH deploy key for each environment, kept as that environment's secret.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Each environment gets a key pair of its own; the private key is stored as the environment's
    secret at once and never shown, the public keys are answered to add at the git host as
    read-only deploy keys. By default every environment where you may manage secrets and the
    repository has no key yet gets one, and the others are listed unchanged (`made` false)
    or as `skipped`. `environments` narrows the call; with `replace` their keys are
    replaced, and the old keys stop working. The repository then names the secret as its
    credential.

    Needs `MANAGE_REPOSITORY` on the instance and `MANAGE_SECRET` in each environment written,
    and an ssh repository with confirmed host keys. Answers 404 when no repository (or named
    environment) has that name, 409 when a secret of that name there is revoked, is not a key
    made for this repository or the repository names another secret, and 422 for `replace`
    without `environments` or a secret name that cannot be a credential.

    Parameters
    ----------
    name
        The repository's registered name.
    body
        The secret name, the environments and `replace`; all optional.
    principal
        The authenticated caller.
    keys
        The deploy key service.

    Returns
    -------
    DeployKeyOut
        The public keys, by environment.
    """
    request = body if body is not None else DeployKeyIn()
    result = keys.generate(
        principal,
        name,
        secret=request.secret,
        environments=request.environments,
        replace=request.replace,
    )
    return DeployKeyOut(
        secret=result.secret,
        keys=[
            DeployKeyMadeOut(
                environment=key.environment,
                public_key=key.public_key,
                fingerprint=key.fingerprint,
                made=key.made,
            )
            for key in result.keys
        ],
        skipped=list(result.skipped),
        replaced=list(result.replaced),
        notes=list(result.notes),
    )
