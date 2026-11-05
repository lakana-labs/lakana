"""A world for the access-as-code tests: principals, environments, tables, one of every fact.

`access_world` makes an administrator who holds the instance `ADMIN` role, the users ``alice``
and ``bob``, the service account ``etl-bot``, the environments ``prod`` and ``staging``, three
tables and the repository ``etl``. `seed_every_kind` then adds one access fact of every kind
(#46).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from core_test_helpers import FakeColumnCatalog, ensure_table
from pyiceberg.types import NestedField, StringType
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.ci_oidc.config import GITHUB_ISSUER
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath
from lakana_core.identity.groups import GroupService
from lakana_core.models import Principal, PrincipalType, Repository, RepositoryKind
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService

COLUMNS = ["id", "region", "email"]
"""The columns every table of the world has."""


class RenamingColumnCatalog(FakeColumnCatalog):
    """A `FakeColumnCatalog` that reports its fields and can rename a column keeping its id."""

    def policy_fields(self, p: PrincipalRef, table_id: uuid.UUID) -> tuple[NestedField, ...] | None:
        """Report the columns as Iceberg fields, as `CatalogService.policy_fields` does.

        Parameters
        ----------
        p
            The acting principal.
        table_id
            The table; every table has the same columns.

        Returns
        -------
        tuple of NestedField
            One optional string field per column, with the field id the columns have.
        """
        ids = self.field_ids or {name: i for i, name in enumerate(self.names, 1)}
        return tuple(
            NestedField(ids[name], name, StringType(), required=False) for name in self.names
        )

    def rename(self, old: str, new: str) -> None:
        """Rename a column, keeping its field id (a schema evolution).

        Parameters
        ----------
        old
            The column's current name.
        new
            Its new name.
        """
        ids = dict(self.field_ids or {name: i for i, name in enumerate(self.names, 1)})
        ids[new] = ids.pop(old)
        self.field_ids = ids
        self.names = [new if name == old else name for name in self.names]
        self.schema_id = (self.schema_id or 0) + 1


@dataclass
class AccessWorld:
    """What `access_world` made.

    Parameters
    ----------
    admin
        The administrator, who holds the instance `ADMIN` role.
    catalog
        The column source every policy service uses.
    authz
        The real authorization over the session's grants.
    refs
        Every seeded principal by name.
    """

    admin: PrincipalRef
    catalog: RenamingColumnCatalog
    authz: GrantAuthorization
    refs: dict[str, PrincipalRef]

    def ref(self, name: str) -> PrincipalRef:
        """Give a seeded principal.

        Parameters
        ----------
        name
            ``admin``, ``alice``, ``bob`` or ``etl-bot``.

        Returns
        -------
        PrincipalRef
            Its reference.
        """
        return self.refs[name]


def access_world(session: Session) -> AccessWorld:
    """Seed the principals, environments, tables and repository.

    Parameters
    ----------
    session
        Database session; committed.

    Returns
    -------
    AccessWorld
        The administrator, the column source, the authorization and every principal.
    """
    refs: dict[str, PrincipalRef] = {}
    for name, kind in (
        ("admin", PrincipalType.USER),
        ("alice", PrincipalType.USER),
        ("bob", PrincipalType.USER),
        ("etl-bot", PrincipalType.SERVICE_ACCOUNT),
    ):
        refs[name] = PrincipalRef(id=uuid.uuid4(), name=name, type=kind)
        session.add(Principal(id=refs[name].id, type=kind, name=name, is_active=True))
    session.add(
        Repository(id=uuid.uuid4(), name="etl", kind=RepositoryKind.HTTPS, url="https://h/etl")
    )
    session.commit()
    for environment, table in (
        ("prod", "orders"),
        ("prod", "customers"),
        ("staging", "orders"),
    ):
        ensure_table(session, environment, "gold", table)
    GrantService(session, AllowAllAuthorization()).grant(
        refs["admin"], principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    catalog = RenamingColumnCatalog(names=list(COLUMNS), schema_id=1)
    return AccessWorld(refs["admin"], catalog, GrantAuthorization(session), refs)


def seed_every_kind(session: Session, world: AccessWorld) -> None:
    """Add a group, memberships, grants, policies and a disabled CI rule, and one staging grant.

    Parameters
    ----------
    session
        Database session.
    world
        The world from `access_world`.
    """
    admin = world.admin
    allow = AllowAllAuthorization()
    groups = GroupService(session, allow)
    for name in ("finance", "nobody", "pii"):
        groups.create_group(admin, name=name)
    for member in ("alice", "bob"):
        groups.add_member(admin, "finance", member)
    grants = GrantService(session, allow)
    for _ in range(2):
        grants.grant(
            admin,
            group="finance",
            role=Role.ANALYST,
            kind=SecurableKind.ENVIRONMENT,
            environment="prod",
        )
    sales = NamespacePath.from_parts(("gold", "sales")).joined()
    ensure_table(session, "prod", sales, "x")
    grants.grant(
        admin,
        principal="etl-bot",
        action=Action.WRITE_TABLE,
        kind=SecurableKind.NAMESPACE,
        environment="prod",
        namespace=sales,
    )
    grants.grant(
        admin,
        principal="alice",
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="prod",
        namespace="gold",
        table="orders",
    )
    grants.grant(
        admin,
        principal="bob",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="staging",
    )
    RowFilterService(session, allow, world.catalog).create(
        admin,
        group="finance",
        environment="prod",
        namespace="gold",
        table="orders",
        expression="region = 'É''s'",
    )
    ColumnMaskService(session, allow, world.catalog).create(
        admin,
        group="pii",
        environment="prod",
        namespace="gold",
        table="customers",
        column="email",
        expression="'***'",
    )
    rules = CiTrustRuleService(session, allow, issuers=[GITHUB_ISSUER])
    rule = rules.create(
        admin,
        issuer="github",
        principal="etl-bot",
        environment="prod",
        repository="etl",
        kinds=["deploy", "dry_run"],
        conditions={"ref": "refs/heads/main", "repository_id": "123456", "event_name": "push"},
    )
    rules.disable(admin, rule.id)
