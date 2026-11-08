"""The package-indexes resource of the platform API.

Instance-wide: an index is registered once by an administrator; a project names it by URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.models import PackageIndex
from lakana_core.projects.package_indexes import PackageIndexService
from lakana_server.api.platform.schemas import (
    PackageIndexCreate,
    PackageIndexOut,
    PackageIndexUpdate,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_package_index_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/package-indexes",
    tags=["package-indexes"],
    responses=errors(401),
)


def _to_out(index: PackageIndex) -> PackageIndexOut:
    """Build a response body.

    Parameters
    ----------
    index
        The row.

    Returns
    -------
    PackageIndexOut
        The body.
    """
    return PackageIndexOut(
        id=index.id,
        name=index.name,
        url=index.url,
        credential_secret=index.credential_secret,
        created_by=index.created_by,
        created_at=index.created_at,
        updated_at=index.updated_at,
    )


@router.post("", response_model=PackageIndexOut, status_code=201, responses=errors(403, 409))
def create_package_index(
    body: PackageIndexCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: PackageIndexService = Depends(get_package_index_service),
) -> PackageIndexOut:
    """Register a package index.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Registers an index that projects may install packages from, by name and URL, optionally
    with a credential secret. Only a registered URL is accepted. Needs
    `MANAGE_PACKAGE_INDEX` on the instance. Answers 409 when the name or URL is taken and
    422 for a name, URL or secret name that is not valid.

    Parameters
    ----------
    body
        The index.
    principal
        The authenticated caller.
    service
        The package index service.

    Returns
    -------
    PackageIndexOut
        The registered index.
    """
    index = service.create(
        principal, name=body.name, url=body.url, credential_secret=body.credential_secret
    )
    return _to_out(index)


@router.get("", response_model=list[PackageIndexOut], responses=errors(403))
def list_package_indexes(
    principal: PrincipalRef = Depends(get_principal),
    service: PackageIndexService = Depends(get_package_index_service),
) -> list[PackageIndexOut]:
    """List package indexes.

    Needs `MANAGE_PACKAGE_INDEX` on the instance, or `READ_ENVIRONMENT` on any environment.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The package index service.

    Returns
    -------
    list of PackageIndexOut
        Sorted by name.
    """
    return [_to_out(index) for index in service.list_indexes(principal)]


@router.get("/{name}", response_model=PackageIndexOut, responses=errors(403, 404))
def get_package_index(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PackageIndexService = Depends(get_package_index_service),
) -> PackageIndexOut:
    """Show a package index.

    Needs `MANAGE_PACKAGE_INDEX` on the instance, or `READ_ENVIRONMENT` on any environment.
    Answers 404 when no index has that name.

    Parameters
    ----------
    name
        The package index's registered name.
    principal
        The authenticated caller.
    service
        The package index service.

    Returns
    -------
    PackageIndexOut
        The index.
    """
    return _to_out(service.get(principal, name))


@router.patch("/{name}", response_model=PackageIndexOut, responses=errors(403, 404, 409))
def update_package_index(
    name: str,
    body: PackageIndexUpdate,
    principal: PrincipalRef = Depends(get_principal),
    service: PackageIndexService = Depends(get_package_index_service),
) -> PackageIndexOut:
    """Change a package index.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Changes its URL or credential secret. Needs `MANAGE_PACKAGE_INDEX` on the instance.
    Answers 404 when no index has that name, 409 when the new URL belongs to another index,
    and 422 when nothing is given or a field cannot be changed.

    Parameters
    ----------
    name
        The package index's registered name.
    body
        Only the fields present change; `null` clears the secret.
    principal
        The authenticated caller.
    service
        The package index service.

    Returns
    -------
    PackageIndexOut
        The updated index.
    """
    return _to_out(service.update(principal, name, body.model_dump(exclude_unset=True)))


@router.delete("/{name}", status_code=204, responses=errors(403, 404))
def delete_package_index(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PackageIndexService = Depends(get_package_index_service),
) -> None:
    """Delete a package index.

    Projects whose lock file names the index fail their next execution until it is
    registered again. Needs `MANAGE_PACKAGE_INDEX` on the instance. Answers 404 when no
    index has that name.

    Parameters
    ----------
    name
        The package index's registered name.
    principal
        The authenticated caller.
    service
        The package index service.
    """
    service.delete(principal, name)
