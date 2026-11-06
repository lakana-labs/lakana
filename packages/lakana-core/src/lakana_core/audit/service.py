"""Recording and querying audit events.

`record` never authorizes anything — it is a side effect of an action whose
own authorization already happened (or was denied) one call earlier, and
requiring authorization to log a denial would be circular. `query` is the
one method here that is itself a principal-facing, authorized operation,
gated by `Action.READ_AUDIT_LOG`, and the one read path of the log: it records
the read (`ACTIVITY_VIEWED`, view `audit_log`) and hides the content of other
principals' events from a reader without `Action.READ_QUERY_TEXT`
(`lakana_core.audit.content`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from lakana_core.audit.content import hidden_fields, shown_detail, warn_unclassified
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, SecurableKind
from lakana_core.config import ViewAuditConfig
from lakana_core.errors import ValidationError
from lakana_core.models import AuditEvent, AuditEventType

ACCESS_APPLY_KEY = "lakana.access_apply_id"
"""The `Session.info` key under which `lakana_core.access.managed.applying` keeps the apply's id."""


@dataclass(frozen=True)
class AuditCursor:
    """A position in `AuditService.query`'s newest-first order, for keyset paging.

    Parameters
    ----------
    occurred_at
        The `occurred_at` of the last event already seen. Must be aware.
    id
        The `id` of that event, breaking ties between events that share an
        `occurred_at`.
    """

    occurred_at: datetime
    id: uuid.UUID


@dataclass(frozen=True)
class ShownAuditEvent:
    """One audit event as a reader of the log sees it.

    A copy, never the stored row: changing it changes nothing in the database.

    Parameters
    ----------
    id
        The event's id.
    occurred_at
        When it happened.
    principal_id
        Whom it is attributed to, or `None`.
    event_type
        Its type.
    securable_kind
        The kind of object it targets, or `None`.
    securable_environment
        The object's environment, or `None`.
    securable_namespace
        The object's namespace, unit-separator-joined, or `None`.
    securable_table
        The object's table, or `None`.
    detail
        Its detail, with another principal's content hidden unless the reader holds
        `READ_QUERY_TEXT` (`lakana_core.audit.content.shown_detail`).
    """

    id: uuid.UUID
    occurred_at: datetime
    principal_id: uuid.UUID | None
    event_type: AuditEventType
    securable_kind: SecurableKind | None
    securable_environment: str | None
    securable_namespace: str | None
    securable_table: str | None
    detail: dict


@dataclass(frozen=True)
class AuditPage:
    """One page of the audit log.

    Parameters
    ----------
    items
        The events, newest first.
    next_cursor
        Where the next page starts, or `None` on the last page.
    """

    items: list[ShownAuditEvent]
    next_cursor: AuditCursor | None


class AuditService:
    """Record audit events, and read them back with authorization.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization service consulted by `query`. Optional and `None` by
        default: `record` never uses it, and five of this plan's six call
        sites only ever call `record` — requiring a real
        `AuthorizationService` there would force each of them to construct
        or pass through one purely as ceremony. A `ValidationError` is
        raised instead, at the point `query` is actually called without one.
    view_audit
        Whether and how often a read of other people's events is recorded
        (`LAKANA_VIEW_AUDIT__*`); the defaults when `None`. Only `query` uses it.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService | None = None,
        *,
        view_audit: ViewAuditConfig | None = None,
    ) -> None:
        self._session = session
        self._authz = authz
        self._view_audit = view_audit or ViewAuditConfig()

    def record(
        self,
        principal_id: uuid.UUID | None,
        event_type: AuditEventType,
        *,
        securable: Securable | None = None,
        detail: dict | None = None,
        commit: bool = False,
    ) -> AuditEvent:
        """Create one audit event.

        Parameters
        ----------
        principal_id
            The principal the event is attributed to, or `None` for a
            failed authentication (no principal was ever verified).
        event_type
            The kind of event.
        securable
            The object the event's action targets, if any. Populates the
            four `securable_*` columns; left `None` for events with no
            natural target (authentication).
        detail
            Event-specific facts not already captured by the columns
            above. Defaults to `{}`.
        commit
            If `True`, commits immediately after adding the row — for a
            call site with no other action already about to commit in the
            same transaction (an authentication check, an authorization
            denial). If `False` (the default), the row is only added to
            the session; the caller's own subsequent commit persists it
            atomically alongside whatever action it records.

        Returns
        -------
        AuditEvent
            The created (not yet necessarily committed) row.

        Warns
        -----
        UnclassifiedAuditFieldWarning
            If `detail` has a field that `lakana_core.audit.content` does not classify.
        """
        apply_id = self._session.info.get(ACCESS_APPLY_KEY)
        if apply_id is not None:
            detail = {**(detail or {}), "access_apply_id": str(apply_id)}
        warn_unclassified(event_type, detail or {})
        event = AuditEvent(
            id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            principal_id=principal_id,
            event_type=event_type,
            securable_kind=securable.kind if securable is not None else None,
            securable_environment=securable.environment if securable is not None else None,
            securable_namespace=(
                securable.namespace.joined()
                if securable is not None and securable.namespace is not None
                else None
            ),
            securable_table=securable.table if securable is not None else None,
            detail=detail or {},
        )
        self._session.add(event)
        if commit:
            self._session.commit()
        return event

    def query(
        self,
        p: PrincipalRef,
        *,
        event_type: AuditEventType | None = None,
        environment: str | None = None,
        principal_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        cursor: AuditCursor | None = None,
        kind: str | None = None,
    ) -> AuditPage:
        """Read one page of audit events, most recently occurred first.

        A page that shows another principal's event, or a filter on another principal, is
        recorded as `ACTIVITY_VIEWED` (view `audit_log`, instance scope) before the page is
        returned, at most once per reader and targeting key within
        `LAKANA_VIEW_AUDIT__WINDOW_MINUTES` (`lakana_core.audit.views.record_view`); a page of
        only the reader's own events, or a filter on the reader, records nothing. The content of
        another principal's event (`lakana_core.audit.content.CONTENT_FIELDS`, and any field not
        classified) is hidden unless `p` holds `READ_QUERY_TEXT` on the instance: query text as
        its SHA-256 and length, any other value as a marker with nothing derived from it; the
        reader's own events are shown in full.

        Parameters
        ----------
        p
            The acting principal.
        event_type
            Only return events of this type, if given.
        environment
            Only return events whose `securable_environment` matches, if
            given.
        principal_id
            Only return events attributed to this principal, if given.
        since
            Only return events at or after this time, if given.
        until
            Only return events at or before this time, if given.
        limit
            The maximum number of events to return. Defaults to 100. The
            caller (the REST endpoint) is responsible for bounding this to a
            sane range before it reaches here.
        cursor
            Only return events strictly after this position in newest-first
            order, if given — the last event of the previous page. Keyset
            paging on `(occurred_at, id)`: a later write never shifts a page
            boundary, since it sorts before any cursor already handed out.
        kind
            Only return events of this kind, if given: ``connection`` is every
            ``CONNECTION_*`` type. It combines with `event_type`, which must then be of the
            kind. A new kind is added in `EVENT_KINDS`.

        Returns
        -------
        AuditPage
            Matching events, newest `occurred_at` first, and the cursor of the next page.

        Raises
        ------
        ValidationError
            If this `AuditService` was constructed with no
            `AuthorizationService`, `kind` is not a kind, or `event_type` is not of `kind`.
        PermissionDenied
            If `p` may not read the audit log.
        AuditUnavailableError
            If the read had to be recorded and could not be; no event is returned.
        """
        if self._authz is None:
            raise ValidationError(
                "AuditService.query requires an AuthorizationService; "
                "this instance was constructed with none."
            )
        self._authz.authorize(p, Action.READ_AUDIT_LOG, Securable.instance_scope())
        stmt = select(AuditEvent)
        if kind is not None:
            types = _kind_types(kind)
            if event_type is not None and event_type not in types:
                raise ValidationError(
                    f"event_type {event_type.value} is not of kind {kind}; leave out one of the "
                    "two filters"
                )
            stmt = stmt.where(AuditEvent.event_type.in_(types))
        if event_type is not None:
            stmt = stmt.where(AuditEvent.event_type == event_type)
        if environment is not None:
            stmt = stmt.where(AuditEvent.securable_environment == environment)
        if principal_id is not None:
            stmt = stmt.where(AuditEvent.principal_id == principal_id)
        if since is not None:
            stmt = stmt.where(AuditEvent.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AuditEvent.occurred_at <= until)
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    AuditEvent.occurred_at < cursor.occurred_at,
                    and_(AuditEvent.occurred_at == cursor.occurred_at, AuditEvent.id < cursor.id),
                )
            )
        stmt = stmt.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
        found = list(self._session.execute(stmt).scalars().all())
        rows = found[:limit]
        self._record_read(
            p,
            rows,
            filters={
                "event_type": None if event_type is None else event_type.value,
                "kind": kind,
                "environment": environment,
                "since": None if since is None else since.isoformat(),
                "until": None if until is None else until.isoformat(),
                "limit": limit,
            },
            principal_id=principal_id,
        )
        items = _shown(self._authz, p, rows)
        last = rows[-1] if len(found) > limit else None
        next_cursor = None if last is None else AuditCursor(last.occurred_at, last.id)
        return AuditPage(items=items, next_cursor=next_cursor)

    def _record_read(
        self,
        p: PrincipalRef,
        rows: Sequence[AuditEvent],
        *,
        filters: dict[str, object],
        principal_id: uuid.UUID | None,
    ) -> None:
        """Record a read of the audit log that shows or targets another principal.

        Parameters
        ----------
        p
            The reader.
        rows
            The events of the page.
        filters
            The filters of the read, without the `principal_id` filter and the cursor.
        principal_id
            The `principal_id` filter; the targeting key unless it is the reader's own id.

        Raises
        ------
        AuditUnavailableError
            If the row could not be written; the page is not returned.
        """
        # `views` builds on this module (`AuditService.record`), so it is imported here.
        from lakana_core.audit.views import View, ViewDetail, record_view, sessions_like

        others = [row.principal_id for row in rows if row.principal_id != p.id]
        record_view(
            sessions_like(self._session),
            p,
            View.AUDIT_LOG,
            Securable.instance_scope(),
            None
            if principal_id is None or principal_id == p.id
            else {"principal_id": str(principal_id)},
            ViewDetail(
                filters=filters,
                rows_shown=len(rows),
                others_shown=len(others),
                distinct_others=len(set(others)),
            ),
            config=self._view_audit,
        )


EVENT_KINDS: dict[str, frozenset[AuditEventType]] = {
    "connection": frozenset(t for t in AuditEventType if t.name.startswith("CONNECTION_")),
}
"""The kinds `AuditService.query` filters by, each a set of event types; new kinds go here."""


def _kind_types(kind: str) -> frozenset[AuditEventType]:
    """Name the event types of a kind.

    Parameters
    ----------
    kind
        The kind, as a caller wrote it.

    Returns
    -------
    frozenset of AuditEventType
        Its event types.

    Raises
    ------
    ValidationError
        If `kind` is not one of `EVENT_KINDS`.
    """
    types = EVENT_KINDS.get(kind)
    if types is None:
        raise ValidationError(f"kind must be {' or '.join(sorted(EVENT_KINDS))}")
    return types


def _shown(
    authz: AuthorizationService, p: PrincipalRef, rows: Sequence[AuditEvent]
) -> list[ShownAuditEvent]:
    """Copy the events of a page for their reader, hiding what the reader may not see.

    `READ_QUERY_TEXT` is asked about only when a row of another principal has a field to
    hide, and then once for the page.

    Parameters
    ----------
    authz
        Answers whether `p` holds `READ_QUERY_TEXT`.
    p
        The reader.
    rows
        The stored events.

    Returns
    -------
    list of ShownAuditEvent
        The copies, in the same order.
    """
    sees_text: bool | None = None
    items = []
    for row in rows:
        hidden = {} if row.principal_id == p.id else hidden_fields(row.event_type, row.detail)
        if hidden:
            if sees_text is None:
                sees_text = authz.permits(p, Action.READ_QUERY_TEXT, Securable.instance_scope())
            if sees_text:
                hidden = {}
        items.append(
            ShownAuditEvent(
                id=row.id,
                occurred_at=row.occurred_at,
                principal_id=row.principal_id,
                event_type=row.event_type,
                securable_kind=row.securable_kind,
                securable_environment=row.securable_environment,
                securable_namespace=row.securable_namespace,
                securable_table=row.securable_table,
                detail=shown_detail(row.detail, hidden),
            )
        )
    return items
