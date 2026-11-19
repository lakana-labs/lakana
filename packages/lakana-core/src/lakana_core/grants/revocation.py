"""Revoking grants and policies whose target has been dropped.

`Grant`, `RowFilter`, and `ColumnMask` bind to a securable's id, not its
name (`lakana_core.catalog.identity`) — but an id-bound row still keeps
applying forever unless something revokes it when its target goes away.
Without this, a target re-created under the same name can inherit its
predecessor's access, and the two securable kinds get there differently. A
re-created *table* gets a fresh id, so a stale grant's `table_id` never
matches it again — the authorization decision alone already fails closed.
A re-created *namespace* has no id binding at all (`Grant` has no
`namespace_id`; a NAMESPACE-kind grant matches on `environment_id` plus the
literal namespace string, per the design doc's "namespaces cannot be
renamed, so their paths are stable"), so a stale grant matches the new
namespace exactly as it matched the old one — nothing here fails closed on
its own. Either way, the stale row never stops looking "active"
(`revoked_at` stays `None`), so an admin listing by name
(`GrantService.list_for_securable`, `RowFilterService`/
`ColumnMaskService.list_page`) keeps showing it long after its target
is gone. `revoke_attached` is the one place `CatalogService.drop_table`,
`drop_namespace`, and `delete_environment` go to close both gaps, each
called after its own authorization and existence checks but before the
drop itself, in the same transaction as the drop's own commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import ColumnExpressionArgument, select
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import PrincipalRef, Securable, SecurableKind
from lakana_core.identifiers import NamespacePath
from lakana_core.models import AuditEventType, ColumnMask, Grant, RowFilter

_EVENT_TYPES: dict[type[Grant] | type[RowFilter] | type[ColumnMask], AuditEventType] = {
    Grant: AuditEventType.GRANT_REVOKED,
    RowFilter: AuditEventType.ROW_FILTER_REVOKED,
    ColumnMask: AuditEventType.COLUMN_MASK_REVOKED,
}

PolicyKind = Literal["grant", "row_filter", "column_mask"]
"""The name of each revocable model in `RevokedPolicy.kind`."""

POLICY_MODELS: dict[str, type[Grant] | type[RowFilter] | type[ColumnMask]] = {
    "grant": Grant,
    "row_filter": RowFilter,
    "column_mask": ColumnMask,
}
"""The model of each `RevokedPolicy.kind`."""

_KINDS: dict[type[Grant] | type[RowFilter] | type[ColumnMask], PolicyKind] = {
    Grant: "grant",
    RowFilter: "row_filter",
    ColumnMask: "column_mask",
}


@dataclass(frozen=True)
class RevokedPolicy:
    """One grant, row filter or column mask that `revoke_attached` revoked.

    Parameters
    ----------
    kind
        Which model it is.
    id
        The row's id.
    """

    kind: PolicyKind
    id: uuid.UUID

    def as_json(self) -> dict[str, str]:
        """Write it the way a tombstone and an audit event store it.

        Returns
        -------
        dict of str to str
            ``{"kind": ..., "id": ...}``.
        """
        return {"kind": self.kind, "id": str(self.id)}


def revoke_attached(
    session: Session,
    actor: PrincipalRef | None,
    *,
    environment_id: uuid.UUID | None = None,
    table_id: uuid.UUID | None = None,
    namespace: tuple[uuid.UUID, str] | None = None,
    now: datetime | None = None,
) -> list[RevokedPolicy]:
    """Soft-revoke every active grant, row filter, and column mask attached to one dropped target.

    Exactly one of `environment_id`, `table_id`, or `namespace` identifies
    the dropped object; which one selects what gets revoked:

    - `environment_id` revokes every active `Grant`, `RowFilter`, and
      `ColumnMask` with that `environment_id` — every grant kind bound to
      the environment, plus every table-scoped policy in it.
    - `table_id` revokes every active `Grant`, `RowFilter`, and
      `ColumnMask` with that `table_id`.
    - `namespace` revokes every active NAMESPACE-kind `Grant` with the
      given `environment_id` and exactly the given namespace path.
      `RowFilter` and `ColumnMask` are always table-scoped, so neither has
      a namespace-kind row to revoke here; nested namespaces need no
      separate call because the caller (`CatalogService.drop_namespace`)
      already refuses to drop a namespace that still contains one.

    Each revoked row gets `revoked_at` set to now and `revoked_by` set to
    `actor.id`, plus one audit event (`GRANT_REVOKED`, `ROW_FILTER_REVOKED`,
    or `COLUMN_MASK_REVOKED`) with `detail={"reason": "target_dropped",
    "id": str(row.id)}`. Nothing here commits; the caller's own subsequent
    commit persists the revocations atomically alongside the drop itself.

    Parameters
    ----------
    session
        Database session.
    actor
        The principal performing the drop; recorded as `revoked_by` and as
        the audit events' `principal_id`. `None` for a drop by the system (an expiry), which
        records no principal.
    environment_id
        The dropped environment's id, if that is the target.
    table_id
        The dropped table's id, if that is the target.
    namespace
        `(environment_id, joined_path)` for the dropped namespace, if that
        is the target. `joined_path` is `NamespacePath.joined()`'s
        unit-separator-joined storage form, matching `Grant.namespace`.
    now
        The time to stamp `revoked_at` with; the Python clock when `None`. `drop_table`
        passes its drop time, so an undrop can tell a row still as the drop left it.

    Returns
    -------
    list of RevokedPolicy
        Every row revoked, across all three models.

    Raises
    ------
    ValueError
        If zero or more than one of `environment_id`/`table_id`/`namespace`
        is given — a programming error in the caller, not a condition a
        real drop can trigger. A plain `raise`, not an `assert`: under
        `python -O`, an `assert` here would be stripped, and a no-target
        call would fall through to `bound.table_id == None`, which
        SQLAlchemy renders as `IS NULL` and so would mass-revoke every row
        of every kind that has no `table_id` at all (every `ENVIRONMENT`-
        and `NAMESPACE`-kind `Grant`) instead of doing nothing.
    """
    given = (environment_id is not None, table_id is not None, namespace is not None)
    if sum(given) != 1:
        raise ValueError("revoke_attached takes exactly one of environment_id/table_id/namespace.")
    now = now or datetime.now(UTC)
    audit = AuditService(session)
    if namespace is not None:
        env_id, joined_path = namespace
        return _revoke_matching(
            session,
            audit,
            actor,
            now,
            Grant,
            (Grant.kind == SecurableKind.NAMESPACE)
            & (Grant.environment_id == env_id)
            & (Grant.namespace == joined_path),
        )
    revoked: list[RevokedPolicy] = []
    for bound in (Grant, RowFilter, ColumnMask):
        if environment_id is not None:
            predicate = bound.environment_id == environment_id
        else:
            assert table_id is not None, (  # nosec B101 -- pure type-narrowing; see Raises above
                "guaranteed by the ValueError raised above"
            )
            predicate = bound.table_id == table_id
        revoked += _revoke_matching(session, audit, actor, now, bound, predicate)
    return revoked


def _revoke_matching(
    session: Session,
    audit: AuditService,
    actor: PrincipalRef | None,
    now: datetime,
    model: type[Grant] | type[RowFilter] | type[ColumnMask],
    predicate: ColumnExpressionArgument[bool],
) -> list[RevokedPolicy]:
    """Soft-revoke every active row of `model` matching `predicate`.

    Parameters
    ----------
    session
        Database session.
    audit
        The audit service to record one event per revoked row through.
    actor
        The principal performing the drop; `None` for the system.
    now
        The timestamp to stamp `revoked_at` with.
    model
        `Grant`, `RowFilter`, or `ColumnMask`.
    predicate
        The rows to consider; ANDed with `revoked_at.is_(None)` so an
        already-revoked row is never touched or re-audited.

    Returns
    -------
    list of RevokedPolicy
        The rows revoked.
    """
    rows = (
        session.execute(select(model).where(predicate, model.revoked_at.is_(None))).scalars().all()
    )
    actor_id = None if actor is None else actor.id
    for row in rows:
        row.revoked_at = now
        row.revoked_by = actor_id
        audit.record(
            actor_id,
            _EVENT_TYPES[model],
            securable=Securable(
                kind=row.kind if isinstance(row, Grant) else SecurableKind.TABLE,
                environment=row.environment,
                namespace=NamespacePath.parse(row.namespace) if row.namespace is not None else None,
                table=row.table,
            ),
            detail={"reason": "target_dropped", "id": str(row.id)},
        )
    return [RevokedPolicy(_KINDS[model], row.id) for row in rows]
