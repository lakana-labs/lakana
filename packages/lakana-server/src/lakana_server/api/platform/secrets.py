"""The secrets resource of the platform API.

Secrets are always addressed through their environment
(`/api/v1/environments/{name}/secrets`), not as a standalone top-level
resource — a secret has no meaning outside the environment it is scoped
to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.errors import NotFoundError
from lakana_core.models import Secret, SecretUseKind
from lakana_core.secrets.service import SecretService
from lakana_server.api.platform.schemas import (
    SecretBind,
    SecretCreate,
    SecretOut,
    SecretRotate,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_secret_service

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{environment_name}/secrets",
    tags=["secrets"],
    responses=errors(401),
)


def _to_out(secret: Secret, environment_name: str, service: SecretService) -> SecretOut:
    """Build a response body from a Secret row.

    Parameters
    ----------
    secret
        The row to convert.
    environment_name
        The environment this secret belongs to, since `Secret` itself
        only carries `environment_id`.
    service
        Reads the bound index's or repository's name and URL.

    Returns
    -------
    SecretOut
        The response body. Structurally has no field for the plaintext
        or ciphertext.
    """
    return SecretOut(
        id=secret.id,
        name=secret.name,
        environment=environment_name,
        created_by=secret.created_by,
        created_at=secret.created_at,
        format_version=secret.format_version,
        key_id=secret.key_id,
        use=secret.use_kind.value if secret.use_kind is not None else None,
        use_target=service.use_target_name(secret),
        use_target_url=service.use_target_url(secret),
    )


def _find_by_name(secrets: list[Secret], name: str) -> Secret:
    """Find a secret by name within an already-fetched list.

    Parameters
    ----------
    secrets
        Secrets already fetched for one environment.
    name
        The name to look for.

    Returns
    -------
    Secret
        The matching secret.

    Raises
    ------
    lakana_core.errors.NotFoundError
        If no secret in `secrets` has that name.
    """
    for secret in secrets:
        if secret.name == name:
            return secret
    raise NotFoundError(f"No secret named {name!r}.")


@router.post("", response_model=SecretOut, status_code=201, responses=errors(403, 404, 409))
def create_secret(
    environment_name: str,
    body: SecretCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> SecretOut:
    """Create a secret.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Stores a secret value encrypted, for the environment's storage, a webhook, a package
    index or a repository. The value is never shown again. Needs `MANAGE_SECRET` in the
    environment. Answers 404 when the environment does not exist, 409 when a secret of that
    name exists, and 422 when the use names no target, or names one it must not. Answers 500
    when the instance has no secrets master key; that is a deployment fault the operator
    fixes.

    Parameters
    ----------
    environment_name
        The environment this secret is scoped to.
    body
        The secret to create.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Returns
    -------
    SecretOut
        The created secret's metadata.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.AlreadyExistsError
        Mapped to 409.
    lakana_core.errors.ValidationError
        Mapped to 422: a use that names no target, or a name for the environment use.
    lakana_core.errors.LakanaError
        Unmapped, so defaults to 500 — raised if no secrets master key is
        configured. That is a deployment-misconfiguration condition, not
        invalid caller input, so it deliberately is not a `ValidationError`
        (422); see `lakana_core.secrets.keys.EnvKeyProvider`.
    """
    use = (
        service.use_from_names(
            principal, environment_name, SecretUseKind(body.use.kind), body.use.target
        )
        if body.use is not None
        else None
    )
    created = service.create(
        principal, environment=environment_name, name=body.name, values=body.values, use=use
    )
    return _to_out(created, environment_name, service)


@router.get("", response_model=list[SecretOut], responses=errors(403, 404))
def list_secrets(
    environment_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> list[SecretOut]:
    """List the secrets of an environment.

    Lists the active secrets by name and use. Values are never included. Needs
    `MANAGE_SECRET` in the environment. Answers 404 when the environment does not exist.

    Parameters
    ----------
    environment_name
        The environment whose secrets to list.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Returns
    -------
    list of SecretOut
        Every active secret in this environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return [
        _to_out(secret, environment_name, service)
        for secret in service.list_for_environment(principal, environment_name)
    ]


@router.patch("/{secret_name}", response_model=SecretOut, responses=errors(403, 404))
def rotate_secret(
    environment_name: str,
    secret_name: str,
    body: SecretRotate,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> SecretOut:
    """Rotate a secret.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Replaces the secret's value; what uses it picks the new value up. Needs `MANAGE_SECRET`
    in the environment. Answers 404 when the environment does not exist or has no active
    secret of that name (a revoked secret counts as missing), and 500 when the instance has
    no secrets master key.

    Parameters
    ----------
    environment_name
        The environment the secret is scoped to.
    secret_name
        The secret's name.
    body
        The new plaintext values.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Returns
    -------
    SecretOut
        The rotated secret's metadata.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment or the secret name doesn't
        exist.
    lakana_core.errors.LakanaError
        Unmapped, so defaults to 500 — raised if no secrets master key is
        configured, the same deployment-misconfiguration condition
        `create_secret` documents.
    """
    existing = _find_by_name(service.list_for_environment(principal, environment_name), secret_name)
    rotated = service.rotate(principal, existing.id, values=body.values)
    return _to_out(rotated, environment_name, service)


@router.put("/{secret_name}/use", response_model=SecretOut, responses=errors(403, 404))
def bind_secret(
    environment_name: str,
    secret_name: str,
    body: SecretBind,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> SecretOut:
    """Bind a secret to a package index, repository or environment.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Chooses what uses the secret: a package index or repository by name, or the environment
    itself. Needs `MANAGE_SECRET` in the environment. Answers 404 when the environment, the
    active secret (a revoked secret counts as missing) or the target does not exist and 422
    for a name given for the environment use, none for an index or repository, or the
    environment's storage secret bound to an index or repository.

    Parameters
    ----------
    environment_name
        The environment the secret is scoped to.
    secret_name
        The secret's name.
    body
        The use.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Returns
    -------
    SecretOut
        The bound secret's metadata.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404: the environment, the secret or the named target does not exist.
    lakana_core.errors.ValidationError
        Mapped to 422: a name for the environment use, or none for an index or repository.
    lakana_core.errors.PermissionDenied
        Mapped to 403: the caller may not manage secrets in this environment.
    """
    existing = _find_by_name(service.list_for_environment(principal, environment_name), secret_name)
    use = service.use_from_names(principal, environment_name, SecretUseKind(body.kind), body.target)
    bound = service.bind(principal, existing.id, use)
    return _to_out(bound, environment_name, service)


@router.delete("/{secret_name}", status_code=204, responses=errors(403, 404))
def revoke_secret(
    environment_name: str,
    secret_name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: SecretService = Depends(get_secret_service),
) -> None:
    """Revoke a secret.

    Needs `MANAGE_SECRET` in the environment. Answers 404 when the environment or secret
    does not exist.

    Parameters
    ----------
    environment_name
        The environment the secret is scoped to.
    secret_name
        The secret's name.
    principal
        The authenticated caller.
    service
        The secret service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment or the secret name doesn't
        exist.
    """
    existing = _find_by_name(service.list_for_environment(principal, environment_name), secret_name)
    service.revoke(principal, existing.id)
