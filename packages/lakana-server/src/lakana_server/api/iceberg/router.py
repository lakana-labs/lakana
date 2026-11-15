"""The Iceberg REST Catalog router.

Mounted at `/v1`. Uses `IcebergRoute` so every endpoint's errors — including
ones raised inside a dependency like `get_principal` — come back in the
shape the Iceberg REST specification mandates.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.errors import ValidationError
from lakana_core.iceberg_json import (
    parse_partition_spec,
    parse_schema,
    parse_sort_order,
    parse_table_requirements,
    parse_table_updates,
)
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_server.api.iceberg.encoding import decode_namespace, encode_namespace
from lakana_server.api.iceberg.route import IcebergRoute
from lakana_server.api.iceberg.schemas import (
    CommitTableRequest,
    ConfigResponseBody,
    CreateNamespaceRequest,
    CreateTableRequest,
    ListNamespacesResponseBody,
    ListTablesResponseBody,
    NamespaceResponseBody,
    RenameTableRequest,
    TableResponseBody,
    UpdateNamespacePropertiesRequest,
    UpdateNamespacePropertiesResponseBody,
)
from lakana_server.deps import get_catalog, get_principal

router = APIRouter(route_class=IcebergRoute)


@router.get("/v1/config", response_model=ConfigResponseBody)
def get_config() -> ConfigResponseBody:
    """Return the Iceberg REST configuration.

    `RestCatalog.__init__` calls this unconditionally before anything else —
    confirmed by reading `_fetch_config`. No `endpoints` field is included
    (contract correction C3): the client then falls back to a default
    capability set with no `HEAD` entries, which is why this router
    implements no `HEAD` handlers.

    Returns
    -------
    ConfigResponseBody
        Empty defaults and overrides; M1 has no per-warehouse configuration.
    """
    return ConfigResponseBody(defaults={}, overrides={})


@router.post("/v1/{prefix}/namespaces", response_model=NamespaceResponseBody)
def create_namespace(
    prefix: str,
    body: CreateNamespaceRequest,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> NamespaceResponseBody:
    """Create a namespace.

    Parameters
    ----------
    prefix
        The environment name (the Iceberg REST spec's `{prefix}`).
    body
        The namespace to create.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    NamespaceResponseBody
        The created namespace.
    """
    path = NamespacePath.from_parts(body.namespace, allow_dots=True)
    ns = catalog.create_namespace(principal, prefix, path, body.properties)
    return NamespaceResponseBody(namespace=encode_namespace(path), properties=ns.properties)


@router.get("/v1/{prefix}/namespaces", response_model=ListNamespacesResponseBody)
def list_namespaces(
    prefix: str,
    parent: str | None = None,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> ListNamespacesResponseBody:
    """List namespaces in an environment, optionally scoped to one parent.

    Parameters
    ----------
    prefix
        The environment name.
    parent
        Optional `%1F`-joined namespace path segment, decoded the same way
        as the `{namespace}` path parameter. When given, only namespaces
        *directly* below it are returned, per the Iceberg REST spec.
        `CatalogService.list_namespaces` returns every strict descendant of
        `parent`, not just its direct children, so this endpoint filters
        that down to depth `len(parent) + 1`. A `parent` that does not
        exist is a 404 (`NoSuchNamespaceException` in the Iceberg REST
        OpenAPI spec, which pyiceberg's `RestCatalog` maps to
        `NoSuchNamespaceError`): this endpoint loads `parent` with
        `catalog.load_namespace` first, the same call `load_namespace`
        (this router's single-namespace endpoint) already uses to turn a
        missing namespace into 404, so a nonexistent parent doesn't read
        back identically to an existing one with no children.
        `load_namespace` authorizes at that namespace's own scope before
        checking existence, so the caller also needs read access to
        `parent` specifically — not just to the environment — to list its
        children.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    ListNamespacesResponseBody
        Matching namespaces, as lists of parts. Pagination (`page-token`)
        is not implemented at M1 — the response shape omits it, which is
        spec-compliant since the field is optional.
    """
    parent_path = None if parent is None else decode_namespace(parent)
    if parent_path is not None:
        catalog.load_namespace(principal, prefix, parent_path)
    paths = catalog.list_namespaces(principal, prefix, parent=parent_path)
    if parent_path is not None:
        depth = len(parent_path.parts) + 1
        paths = [p for p in paths if len(p.parts) == depth]
    return ListNamespacesResponseBody(namespaces=[encode_namespace(p) for p in paths])


@router.get("/v1/{prefix}/namespaces/{namespace}", response_model=NamespaceResponseBody)
def load_namespace(
    prefix: str,
    namespace: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> NamespaceResponseBody:
    """Fetch a namespace's properties.

    Parameters
    ----------
    prefix
        The environment name.
    namespace
        The `%1F`-joined namespace path segment.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    NamespaceResponseBody
        The namespace and its properties.
    """
    path = decode_namespace(namespace)
    ns = catalog.load_namespace(principal, prefix, path)
    return NamespaceResponseBody(namespace=encode_namespace(path), properties=ns.properties)


@router.post(
    "/v1/{prefix}/namespaces/{namespace}/properties",
    response_model=UpdateNamespacePropertiesResponseBody,
)
def update_namespace_properties(
    prefix: str,
    namespace: str,
    body: UpdateNamespacePropertiesRequest,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> UpdateNamespacePropertiesResponseBody:
    """Set and unset properties on a namespace.

    Parameters
    ----------
    prefix
        The environment name.
    namespace
        The `%1F`-joined namespace path segment.
    body
        Updates and removals.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    UpdateNamespacePropertiesResponseBody
        What actually changed.
    """
    path = decode_namespace(namespace)
    summary = catalog.update_namespace_properties(
        principal, prefix, path, body.updates, body.removals
    )
    return UpdateNamespacePropertiesResponseBody(
        updated=summary.updated, removed=summary.removed, missing=summary.missing
    )


@router.delete("/v1/{prefix}/namespaces/{namespace}", status_code=204)
def drop_namespace(
    prefix: str,
    namespace: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> None:
    """Drop a namespace.

    Parameters
    ----------
    prefix
        The environment name.
    namespace
        The `%1F`-joined namespace path segment.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.
    """
    catalog.drop_namespace(principal, prefix, decode_namespace(namespace))


def _whole_metadata(loaded: LoadedTable) -> dict[str, Any]:
    """Serialise a table's whole metadata, for a caller the load already let read it.

    Parameters
    ----------
    loaded
        The table, as `CatalogService.load_table` returns it.

    Returns
    -------
    dict
        The metadata exactly as pyiceberg's own `TableMetadata.model_dump_json` produces it,
        which is what makes the response parseable by pyiceberg's client on the other end.
    """
    return json.loads(loaded.metadata.model_dump_json(by_alias=True, exclude_none=True))


def _to_table_response(loaded: LoadedTable, metadata: dict[str, Any]) -> TableResponseBody:
    """Build the wire-format table response from a loaded table.

    The metadata is always passed in, never taken from `loaded`, so that each route says what
    its caller may see: the load the whole metadata (`_whole_metadata`), a commit or a create
    what `CatalogService.commit_answer` chose.

    Parameters
    ----------
    loaded
        The table, as `CatalogService` returns it.
    metadata
        The metadata to answer with, in its JSON form.

    Returns
    -------
    TableResponseBody
        The response body.
    """
    return TableResponseBody(
        metadata_location=loaded.metadata_location,
        metadata=metadata,
        config={},
        storage_credentials=[],
    )


@router.post("/v1/{prefix}/namespaces/{namespace}/tables", response_model=TableResponseBody)
def create_table(
    prefix: str,
    namespace: str,
    body: CreateTableRequest,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableResponseBody:
    """Create a table.

    Parameters
    ----------
    prefix
        The environment name.
    namespace
        The `%1F`-joined namespace path segment.
    body
        The table to create.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableResponseBody
        The created table; its properties only for a caller who may read it
        (`CatalogService.commit_answer`).

    Raises
    ------
    lakana_core.errors.ValidationError
        If `stage_create` is `True` — not implemented.

    Notes
    -----
    A name a deployed project declares is refused with a 409 `ConflictError` in the Iceberg
    error shape, as on every other route: only git may take it.
    """
    if body.stage_create:
        raise ValidationError("Staged table creation is not implemented.")
    ns = decode_namespace(namespace)
    ref = TableRef(environment=prefix, namespace=ns, name=body.name)
    loaded = catalog.create_table(
        principal,
        ref,
        schema=parse_schema(body.schema_),
        # `is not None`, not truthiness: an absent field decodes to `None`
        # and means unpartitioned/unsorted, whereas an explicit `{}` is a
        # valid, if trivial, spec that both parse functions accept — and
        # `parse_sort_order({})` is not the same as absent, since it yields
        # `order_id=1` where `UNSORTED_SORT_ORDER` is `order_id=0`. Passing
        # the empty object through keeps what the client actually said
        # instead of collapsing it into "absent".
        partition_spec=(
            parse_partition_spec(body.partition_spec) if body.partition_spec is not None else None
        ),
        sort_order=(parse_sort_order(body.write_order) if body.write_order is not None else None),
        properties=body.properties,
        location=body.location,
    )
    return _to_table_response(loaded, catalog.commit_answer(principal, loaded))


@router.get("/v1/{prefix}/namespaces/{namespace}/tables/{table}", response_model=TableResponseBody)
def load_table(
    prefix: str,
    namespace: str,
    table: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableResponseBody:
    """Fetch a table's current metadata.

    Parameters
    ----------
    prefix
        The environment name.
    namespace
        The `%1F`-joined namespace path segment.
    table
        Table name.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableResponseBody
        The table.
    """
    ref = TableRef(environment=prefix, namespace=decode_namespace(namespace), name=table)
    loaded = catalog.load_table(principal, ref)
    return _to_table_response(loaded, _whole_metadata(loaded))


@router.get("/v1/{prefix}/namespaces/{namespace}/tables", response_model=ListTablesResponseBody)
def list_tables(
    prefix: str,
    namespace: str,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> ListTablesResponseBody:
    """List the tables in a namespace.

    Parameters
    ----------
    prefix
        The environment name.
    namespace
        The `%1F`-joined namespace path segment.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    ListTablesResponseBody
        Every table's identifier. Views are left out: the Iceberg REST
        catalog exposes tables only.
    """
    ns = decode_namespace(namespace)
    refs = catalog.list_tables(principal, prefix, ns, include_views=False)
    return ListTablesResponseBody(
        identifiers=[{"namespace": encode_namespace(ns), "name": ref.name} for ref in refs]
    )


@router.delete("/v1/{prefix}/namespaces/{namespace}/tables/{table}", status_code=204)
def drop_table(
    prefix: str,
    namespace: str,
    table: str,
    purgeRequested: bool = False,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> None:
    """Drop a table; its files follow after the undo period, or at the next sweep with a purge.

    Parameters
    ----------
    prefix
        The environment name.
    namespace
        The `%1F`-joined namespace path segment.
    table
        Table name.
    purgeRequested
        When `true`, the table's files are due at once: the worker's next sweep deletes them,
        and the table cannot be restored. When `false` (the default) they are kept for
        `LAKANA_TABLES__UNDO_DAYS` and the table can be restored with `lakana table undrop`.
        A query parameter, not a body field — confirmed against a real client, which sends
        `DELETE .../tables/{table}?purgeRequested=<bool>`.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Notes
    -----
    Views are left out: the Iceberg REST catalog exposes tables only — a view answers
    `NotFoundError` (404) here, the
    same as `HEAD` already reports one absent. A table a deployed project builds answers a
    409 `ConflictError` in the Iceberg error shape, as `lakana table delete` does: only git
    may remove it.
    """
    ref = TableRef(environment=prefix, namespace=decode_namespace(namespace), name=table)
    catalog.drop_table(principal, ref, purge=purgeRequested, include_views=False)


@router.post("/v1/{prefix}/tables/rename")
def rename_table(
    prefix: str,
    body: RenameTableRequest,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> dict[str, Any]:
    """Rename a table.

    Parameters
    ----------
    prefix
        The environment name.
    body
        Source and destination identifiers.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    dict
        An empty object. pyiceberg's client does not parse this response —
        it immediately calls `load_table` on the destination itself
        (confirmed by reading `RestCatalog.rename_table`) — but an empty
        JSON object is returned rather than nothing, so a stricter client
        checking for a parseable body is not broken.

    Notes
    -----
    Moving away a table a deployed project builds, or onto a name one declares, answers a 409
    `ConflictError` in the Iceberg error shape: only git may change either name.
    """
    source = TableRef(
        environment=prefix,
        namespace=NamespacePath.from_parts(body.source.namespace, allow_dots=True),
        name=body.source.name,
    )
    destination = TableRef(
        environment=prefix,
        namespace=NamespacePath.from_parts(body.destination.namespace, allow_dots=True),
        name=body.destination.name,
    )
    catalog.rename_table(principal, source, destination)
    return {}


@router.post("/v1/{prefix}/namespaces/{namespace}/tables/{table}", response_model=TableResponseBody)
def commit_table(
    prefix: str,
    namespace: str,
    table: str,
    body: CommitTableRequest,
    principal: PrincipalRef = Depends(get_principal),
    catalog: CatalogService = Depends(get_catalog),
) -> TableResponseBody:
    """Apply updates to a table and publish them atomically.

    Parameters
    ----------
    prefix
        The environment name.
    namespace
        The `%1F`-joined namespace path segment.
    table
        Table name.
    body
        Requirements and updates. `body.identifier` is accepted but not
        cross-checked against `namespace`/`table` — the path already
        identifies the table.
    principal
        The authenticated caller.
    catalog
        The catalog service for this request.

    Returns
    -------
    TableResponseBody
        The table at its new metadata location. Its history (snapshots, refs, properties)
        only for a caller who may read the table now (`CatalogService.commit_answer`).
    """
    ref = TableRef(environment=prefix, namespace=decode_namespace(namespace), name=table)
    loaded = catalog.commit_table(
        principal,
        ref,
        requirements=parse_table_requirements(body.requirements),
        updates=parse_table_updates(body.updates),
        release_session=True,
    )
    return _to_table_response(loaded, catalog.commit_answer(principal, loaded))
