"""The namespaces resource of the platform API.

Paths here are dot-joined for human readability (`gold.sales`), converted to
and from `NamespacePath` at this boundary. The Iceberg router uses a
different, spec-mandated encoding for the same underlying value.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.identifiers import NamespacePath
from lakana_server.api.platform.schemas import NamespaceCreate, NamespaceOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_catalog, get_principal, get_write_principal, with_break_glass

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/namespaces",
    tags=["namespaces"],
    responses=errors(401),
)


def _to_out(path: NamespacePath, properties: dict[str, str]) -> NamespaceOut:
    """Build a response body from a path and its properties.

    Parameters
    ----------
    path
        The namespace path.
    properties
        Its properties.

    Returns
    -------
    NamespaceOut
        The dot-joined response body.
    """
    return NamespaceOut(path=".".join(path.parts), properties=properties)


@router.post("", response_model=NamespaceOut, status_code=201, responses=errors(403, 404, 409))
def create_namespace(
    env: str,
    body: NamespaceCreate,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> NamespaceOut:
    """Create a namespace.

    Needs `CREATE_NAMESPACE`. Answers 404 when the environment does not exist and 409 when
    the namespace exists.

    Parameters
    ----------
    env
        Environment name.
    body
        The namespace to create.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    NamespaceOut
        The created namespace.

    Raises
    ------
    lakana_core.errors.AlreadyExistsError
        Mapped to 409 by `install_exception_handlers` if the namespace
        already exists.
    """
    path = NamespacePath.from_parts(body.path.split("."))
    ns = catalog.create_namespace(
        with_break_glass(principal, body.break_glass_reason), env, path, body.properties
    )
    return _to_out(path, ns.properties)


@router.get("/{path}", response_model=NamespaceOut, responses=errors(403, 404))
def get_namespace(
    env: str,
    path: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> NamespaceOut:
    """Show a namespace.

    The namespace is named by its dotted path. Needs `READ_NAMESPACE`. Answers 404 when it
    does not exist.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    NamespaceOut
        The namespace.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    parsed = NamespacePath.from_parts(path.split("."))
    ns = catalog.load_namespace(principal, env, parsed)
    return _to_out(parsed, ns.properties)


@router.get("", response_model=list[NamespaceOut], responses=errors(403, 404))
def list_namespaces(
    env: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> list[NamespaceOut]:
    """List the namespaces of an environment.

    Lists every namespace of the environment at every depth, not only the top level. Needs
    `READ_NAMESPACE` on the environment. Answers 404 when the environment does not exist.

    Parameters
    ----------
    env
        Environment name.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    list of NamespaceOut
        Every namespace, each with its own properties fetched individually.
    """
    paths = catalog.list_namespaces(principal, env)
    return [
        _to_out(path, catalog.load_namespace(principal, env, path).properties) for path in paths
    ]


@router.delete("/{path}", status_code=204, responses=errors(403, 404, 409))
def drop_namespace(
    env: str,
    path: str,
    principal: PrincipalRef = Depends(get_write_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> None:
    """Drop a namespace.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Needs `DROP_NAMESPACE`. Answers 404 when the namespace does not exist and 409 while it
    still holds tables or child namespaces.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    catalog
        The catalog service for this request.

    Raises
    ------
    lakana_core.errors.StepUpRequired
        Mapped to 403, for a browser session whose last passkey touch is older than 15
        minutes.
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ConflictError
        Mapped to 409 if the namespace still contains tables or child
        namespaces.
    """
    catalog.drop_namespace(principal, env, NamespacePath.from_parts(path.split(".")))
