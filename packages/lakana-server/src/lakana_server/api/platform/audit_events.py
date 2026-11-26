"""The audit-events resource of the platform API.

A top-level resource, not scoped under an environment prefix — reading the
audit log, like managing grants, is instance-scoped rather than tied to one
environment.

The listing pages by keyset: `next_cursor` is an opaque token encoding the
`(occurred_at, id)` of a page's last event, and the order when it is not the
newest first; passing it back as `?cursor=` resumes strictly after that event.
Filters are not part of the cursor; send the same ones with every page.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query

from lakana_core.audit.service import (
    AUDIT_DEFAULT_SORT,
    AuditCursor,
    AuditService,
    ShownAuditEvent,
)
from lakana_core.auth import PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.models import AuditEventFilter, AuditEventType
from lakana_server.api.platform.listing import OrderParam, SortParam
from lakana_server.api.platform.namespace_forms import dotted_namespace
from lakana_server.api.platform.schemas import AuditEventOut, AuditEventPage
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_audit_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/audit-events",
    tags=["audit"],
    responses=errors(401),
)

_BAD_CURSOR = (
    "The cursor is not valid. Start the listing again without `cursor`; pass the `next_cursor` of "
    "a page only to continue that listing."
)


def _encode_cursor(position: AuditCursor) -> str:
    """Encode a position as an opaque, URL-safe cursor.

    Parameters
    ----------
    position
        The position of the last event of the page being returned.

    Returns
    -------
    str
        Unpadded base64url of `{"o": occurred_at, "i": id}`, with `"s"` the order when it is
        not the newest first.
    """
    body = {"o": position.occurred_at.isoformat(), "i": str(position.id)}
    if position.sort != AUDIT_DEFAULT_SORT:
        body["s"] = position.sort
    raw = json.dumps(body).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> AuditCursor:
    """Decode a cursor produced by `_encode_cursor`.

    Parameters
    ----------
    cursor
        The `?cursor=` value a client sent back.

    Returns
    -------
    AuditCursor
        The position to resume after.

    Raises
    ------
    ValidationError
        If `cursor` is not one this server could have issued: not base64url,
        not the expected JSON shape, or carrying a timestamp without an
        offset or one that cannot be converted to UTC.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if not (
            isinstance(data, dict)
            and isinstance(data.get("o"), str)
            and isinstance(data.get("i"), str)
            and isinstance(data.get("s", AUDIT_DEFAULT_SORT), str)
        ):
            raise ValidationError(_BAD_CURSOR)
        occurred_at = datetime.fromisoformat(data["o"])
        if occurred_at.tzinfo is None:
            raise ValidationError(_BAD_CURSOR)
        # the database binds UTC: a time that cannot be converted would fail there, as a 500
        occurred_at = occurred_at.astimezone(UTC)
        event_id = uuid.UUID(data["i"])
    except (binascii.Error, ValueError, OverflowError) as exc:
        raise ValidationError(_BAD_CURSOR) from exc
    return AuditCursor(occurred_at=occurred_at, id=event_id, sort=data.get("s", AUDIT_DEFAULT_SORT))


def _to_out(event: ShownAuditEvent) -> AuditEventOut:
    """Build a response body from an event as its reader sees it.

    Parameters
    ----------
    event
        The event to convert: `AuditService.query` already hid what the reader may not see.

    Returns
    -------
    AuditEventOut
        The response body, with `securable_namespace` converted to its
        dotted form.
    """
    return AuditEventOut(
        id=event.id,
        occurred_at=event.occurred_at,
        principal_id=event.principal_id,
        principal_name=event.principal_name,
        event_type=event.event_type,
        event_type_raw=event.event_type_raw,
        securable_kind=event.securable_kind,
        securable_environment=event.securable_environment,
        securable_namespace=dotted_namespace(event.securable_namespace),
        securable_table=event.securable_table,
        detail=event.detail,
    )


@router.get("", response_model=AuditEventPage, responses=errors(403, 503))
def list_audit_events(
    event_type: AuditEventFilter | None = None,
    kind: str | None = Query(
        default=None,
        max_length=64,
        description="Only events of this kind: `connection` is every CONNECTION_* type.",
    ),
    environment: str | None = None,
    principal_id: uuid.UUID | None = None,
    principal_name: str | None = Query(
        default=None,
        max_length=255,
        description="Only events of the principal of this name; not with `principal_id`.",
    ),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=512),
    sort: SortParam = None,
    order: OrderParam = None,
    p: PrincipalRef = Depends(get_principal),
    audit: AuditService = Depends(get_audit_service),
) -> AuditEventPage:
    """List audit events.

    Lists one page of audit events, newest first (the oldest first with `order=asc`), with
    optional filters; every order lists the same events. The cursor of the next page is the
    body's `next_cursor`, null on the last page; send the same filters and order with it. Needs
    `READ_AUDIT_LOG` on the instance. Another principal's query text and column values are
    hidden unless the caller also has `READ_QUERY_TEXT`. Answers 422 for a malformed cursor or
    one of the other order, an unknown `sort` or `order`, a `kind` other than
    `connection`, an `event_type` that is not of `kind` or not a known type, `principal_name` with
    `principal_id`, and 503 when
    the read had to be recorded in the audit log and could not be; no events are returned then.
    An event whose type this release does not know (written by a newer release before a
    downgrade) is listed with `event_type` `UNKNOWN` and its stored type in `event_type_raw`.

    Parameters
    ----------
    event_type
        Only return events of this type, if given: any known type, never `UNKNOWN`.
    kind
        Only return events of this kind, if given: ``connection`` is every ``CONNECTION_*``
        type. Combines with the other filters; with `event_type`, the type must be of the kind.
    environment
        Only return events whose securable environment matches, if given.
    principal_id
        Only return events attributed to this principal, if given.
    principal_name
        Only return events attributed to the principal of this name, if given; a name nobody
        has matches no event. Not together with `principal_id`.
    since
        Only return events at or after this time, if given.
    until
        Only return events at or before this time, if given.
    limit
        The maximum number of events to return. Defaults to 100, must be
        between 1 and 500 inclusive; an out-of-range value is rejected with
        a 422 rather than silently clamped or ignored.
    cursor
        A previous page's `next_cursor`, to fetch the page after it.
    sort
        What the log is sorted by: `occurred`, the only field.
    order
        `desc` (the newest first, the default) or `asc` (the oldest first).
    p
        The acting principal.
    audit
        The audit service.

    Returns
    -------
    AuditEventPage
        Up to `limit` matching events, and the cursor for the next page.

    Raises
    ------
    PermissionDenied
        If `p` may not read the audit log.
    ValidationError
        If `cursor` is malformed or of the other order, `kind` is not a kind, `event_type` is
        not of `kind`, `sort` or `order` is unknown, or both `principal_name` and `principal_id` are
        given; mapped to 422. An `event_type` that is
        no `AuditEventFilter` value (`UNKNOWN` among them) is refused with 422 before this
        function runs.
    lakana_core.errors.AuditUnavailableError
        If the read had to be recorded in the audit log and could not be; mapped to 503 with
        `Retry-After`, and no event is returned.

    Notes
    -----
    Without `READ_QUERY_TEXT`, the content of another principal's event is hidden: a query's SQL
    text is shown as its SHA-256 and length, and a write's watermark as `{"hidden": true}`. The
    caller's own events are shown in full.
    A page that shows or filters on another principal's events is recorded as
    `ACTIVITY_VIEWED` before it is returned.
    """
    position = None if cursor is None else _decode_cursor(cursor)
    page = audit.query(
        p,
        event_type=None if event_type is None else AuditEventType(event_type.value),
        kind=kind,
        environment=environment,
        principal_id=principal_id,
        principal_name=principal_name,
        since=since,
        until=until,
        limit=limit,
        cursor=position,
        sort=sort,
        order=order,
    )
    next_cursor = None if page.next_cursor is None else _encode_cursor(page.next_cursor)
    return AuditEventPage(items=[_to_out(event) for event in page.items], next_cursor=next_cursor)
