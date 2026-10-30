"""The environments resource of the platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.orphaned_locations import DEFAULT_OLDER_THAN_DAYS
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_server.api.platform.schemas import (
    EnvironmentCreate,
    EnvironmentOut,
    EnvironmentPatch,
    EnvironmentRetentionIn,
    EnvironmentStorageUpdate,
    EnvironmentUsageOut,
    OrphanedLocationsDeleteIn,
    OrphanedLocationsOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_catalog, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments",
    tags=["environments"],
    responses=errors(401),
)


@router.post("", response_model=EnvironmentOut, status_code=201, responses=errors(403, 409))
def create_environment(
    body: EnvironmentCreate,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Create an environment.

    Needs `MANAGE_ENVIRONMENT` on the instance. Answers 409 when an environment of that name
    exists.

    Parameters
    ----------
    body
        The environment to create.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The created environment.

    Raises
    ------
    lakana_core.errors.AlreadyExistsError
        Mapped to 409 by `install_exception_handlers`.
    """
    env = catalog.create_environment(
        principal,
        name=body.name,
        storage=StorageConfig(
            scheme=body.storage.scheme, root=body.storage.root, options=body.storage.options
        ),
        is_protected=body.is_protected,
        properties=body.properties,
    )
    return EnvironmentOut.model_validate(env)


@router.patch("/{name}/storage", response_model=EnvironmentOut, responses=errors(403, 404))
def update_environment_storage(
    name: str,
    body: EnvironmentStorageUpdate,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Change an environment's storage.

    Replaces the environment's storage options and the secret that holds its storage
    credential. Needs `MANAGE_ENVIRONMENT` on the instance; while the environment has a
    storage secret, changing the options also needs `MANAGE_SECRET` in it. Answers 404 when
    there is no such environment.

    Parameters
    ----------
    name
        Environment name.
    body
        The fields to update; each is left unchanged if omitted.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The updated environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    env = catalog.update_environment_storage(
        principal, name, options=body.options, secret_ref=body.secret_ref
    )
    return EnvironmentOut.model_validate(env)


@router.patch("/{name}", response_model=EnvironmentOut, responses=errors(403, 404))
def set_environment_protection(
    name: str,
    body: EnvironmentPatch,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Protect or unprotect an environment.

    Sets the environment's protected flag. A protected environment refuses changes that
    would lose data (deleting it, a retention that expires tables) until the flag is
    cleared. Needs `MANAGE_ENVIRONMENT` on the instance. Answers 404 when there is no such
    environment.

    Parameters
    ----------
    name
        Environment name.
    body
        The new `is_protected` value.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The updated environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    env = catalog.set_environment_protection(principal, name, body.is_protected)
    return EnvironmentOut.model_validate(env)


@router.put("/{name}/retention", response_model=EnvironmentOut, responses=errors(403, 404))
def set_environment_retention(
    name: str,
    body: EnvironmentRetentionIn,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Set the default retention of an environment's tables.

    Sets, or turns off, the retention of the environment's tables that set none of their
    own. Needs `MANAGE_ENVIRONMENT`. Answers 404 when there is no such environment and 422
    when the days are out of range or the environment is protected.

    Parameters
    ----------
    name
        Environment name.
    body
        The days, or `null` for off.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the environment.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404.
    lakana_core.errors.ValidationError
        Mapped to 422: days out of range, or a protected environment.
    """
    env = catalog.set_environment_retention(principal, name, body.days)
    return EnvironmentOut.model_validate(env)


@router.get("/{name}", response_model=EnvironmentOut, responses=errors(403, 404))
def get_environment(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Show an environment.

    Needs `READ_ENVIRONMENT`. Answers 404 when there is no such environment.

    Parameters
    ----------
    name
        Environment name.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return EnvironmentOut.model_validate(catalog.get_environment(principal, name))


@router.get("", response_model=list[EnvironmentOut])
def list_environments(
    deleted: bool = False,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> list[EnvironmentOut]:
    """List environments.

    Lists the environments the caller may read. With `deleted` set, lists instead the
    deleted environments whose files are not deleted yet, oldest deletion first, each with
    its `purge_after`. A caller who may read none gets an empty list.

    Parameters
    ----------
    deleted
        `true` lists the deleted environments whose files are not deleted yet, oldest
        deletion first, each with its `purge_after`, instead.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    list of EnvironmentOut
        Every environment the caller may read.

    Notes
    -----
    The list is filtered by `CatalogService.list_environments`.
    """
    found = (
        catalog.list_deleted_environments(principal)
        if deleted
        else catalog.list_environments(principal)
    )
    return [EnvironmentOut.model_validate(env) for env in found]


@router.delete("/{name}", response_model=EnvironmentOut, responses=errors(403, 404, 409))
def delete_environment(
    name: str,
    purge: bool = False,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Delete an environment.

    Deletes the environment. Its tables can be restored until the `purge_after` time in the
    answer. Needs `MANAGE_ENVIRONMENT`. Answers 404 when there is no such environment (or it
    is already deleted and `purge` is not set), 409 while a repository is still deployed in
    it and 422 when it is protected.

    Parameters
    ----------
    name
        Environment name.
    purge
        `true` makes everything due at once: the worker's next clean-up deletes the tables'
        files and the environment cannot be restored. Also accepted for an environment that is
        already deleted.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the environment.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The deleted environment, with `deleted_at` and `purge_after`.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`; also for an environment already deleted
        without `purge`.
    lakana_core.errors.ConflictError
        A repository is still deployed; mapped to 409.
    lakana_core.errors.ValidationError
        The environment is protected; mapped to 422.
    """
    return EnvironmentOut.model_validate(catalog.delete_environment(principal, name, purge=purge))


@router.post("/{name}/restore", response_model=EnvironmentOut, responses=errors(403, 404, 409))
def restore_environment(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentOut:
    """Restore a deleted environment.

    Brings back an environment within its undo period, with the tables it dropped. Needs
    `MANAGE_ENVIRONMENT`. Answers 404 when there is no such environment and 409 when it is
    not deleted, its undo period has ended or the clean-up has started deleting its files.

    Parameters
    ----------
    name
        Environment name.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the environment.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentOut
        The restored environment.

    Raises
    ------
    lakana_core.errors.NotFoundError
        No environment of that name; mapped to 404.
    lakana_core.errors.ConflictError
        It is not deleted, its undo period has ended, or the clean-up has started deleting its
        files; mapped to 409.
    """
    return EnvironmentOut.model_validate(catalog.restore_environment(principal, name))


@router.get("/{name}/usage", response_model=EnvironmentUsageOut, responses=errors(403, 404))
def environment_usage(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> EnvironmentUsageOut:
    """Show how much storage an environment uses.

    Reports the size of the environment's tables from the catalog, without reading storage.
    Needs `MANAGE_ENVIRONMENT`. Answers 404 when there is no such environment.

    Parameters
    ----------
    name
        Environment name.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the environment.
    catalog
        The catalog service for this request.

    Returns
    -------
    EnvironmentUsageOut
        Its tables, dropped tables, artifacts and code archives, as the worker's housekeeping
        last measured them.

    Raises
    ------
    lakana_core.errors.NotFoundError
        No environment of that name; mapped to 404.
    """
    return EnvironmentUsageOut.model_validate(catalog.environment_usage(principal, name))


@router.get(
    "/{name}/orphaned-locations",
    response_model=OrphanedLocationsOut,
    responses=errors(403, 404, 503),
)
def list_orphaned_locations(
    name: str,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> OrphanedLocationsOut:
    """List table directories without a location record.

    Lists the directories that older releases left under the environment's storage with no
    table to own them. Deletes nothing. Needs `MANAGE_ENVIRONMENT` on the instance. Answers
    404 when there is no such environment, 422 when `older_than_days` is out of range, and 503
    when the environment's storage refused access or did not answer (the message says which,
    and what to do).

    Parameters
    ----------
    name
        Environment name; a deleted environment is accepted.
    older_than_days
        A directory with a file newer than this many days (1 to 3,650) is listed as recent.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the instance.
    catalog
        The catalog service for this request.

    Returns
    -------
    OrphanedLocationsOut
        The directories found within the time limit.

    Raises
    ------
    lakana_core.errors.NotFoundError
        No environment of that name; mapped to 404.
    lakana_core.errors.ValidationError
        `older_than_days` out of range; mapped to 422.
    lakana_core.errors.StorageUnavailableError
        The storage refused access or did not answer; mapped to 503.
    """
    found = catalog.find_orphaned_locations(principal, name, older_than_days=older_than_days)
    return OrphanedLocationsOut.model_validate(found)


@router.post(
    "/{name}/orphaned-locations/delete",
    response_model=OrphanedLocationsOut,
    responses=errors(403, 404, 503),
)
def delete_orphaned_locations(
    name: str,
    body: OrphanedLocationsDeleteIn,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> OrphanedLocationsOut:
    """Delete table directories without a location record.

    Deletes the directories that older releases left under the environment's storage and
    whose files are older than `older_than_days`. Needs `MANAGE_ENVIRONMENT` on the
    instance. Answers 404 when there is no such environment, 422 when `older_than_days` is
    out of range, and 503 when the environment's storage refused access or did not answer
    before anything was deleted (the message says which, and what to do).

    Parameters
    ----------
    name
        Environment name; a deleted environment is accepted.
    body
        How old the newest file of a directory must be.
    principal
        The authenticated caller; needs `MANAGE_ENVIRONMENT` on the instance.
    catalog
        The catalog service for this request.

    Returns
    -------
    OrphanedLocationsOut
        Each directory, deleted or with the reason it was kept; `complete` is false when the
        time limit stopped the call, and the next call continues.

    Raises
    ------
    lakana_core.errors.NotFoundError
        No environment of that name; mapped to 404.
    lakana_core.errors.ValidationError
        `older_than_days` out of range; mapped to 422.
    lakana_core.errors.StorageUnavailableError
        The storage refused access or did not answer; mapped to 503.
    """
    report = catalog.delete_orphaned_locations(
        principal, name, older_than_days=body.older_than_days
    )
    return OrphanedLocationsOut.model_validate(report)
