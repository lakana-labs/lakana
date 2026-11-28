"""The `audit_events` resource: `Client.audit_events`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport, query_params
from lakana._models import AuditEvent, AuditEventPage
from lakana._routes import ROUTES


def _audit_event_from_json(data: dict[str, Any]) -> AuditEvent:
    """Build an `AuditEvent` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `AuditEventOut` JSON body.

    Returns
    -------
    AuditEvent
        The parsed audit event.
    """
    return AuditEvent(
        id=data["id"],
        occurred_at=datetime.fromisoformat(data["occurred_at"]),
        principal_id=data["principal_id"],
        event_type=data["event_type"],
        securable_kind=data["securable_kind"],
        securable_environment=data["securable_environment"],
        securable_namespace=data["securable_namespace"],
        securable_table=data["securable_table"],
        detail=data["detail"],
        event_type_raw=data.get("event_type_raw"),
    )


class AuditEvents:
    """The `audit-events` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(
        self,
        *,
        event_type: str | None = None,
        environment: str | None = None,
        principal_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
        kind: str | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> AuditEventPage:
        """List one page of audit events, newest first, optionally filtered.

        Filters are not part of the cursor: pass the same ones with every
        page. `iter` does this for you.

        Parameters
        ----------
        event_type
            Only return events of this type, if given.
        environment
            Only return events whose securable environment matches, if
            given.
        principal_id
            Only return events attributed to this principal, if given.
        since
            Only return events at or after this time, if given.
        until
            Only return events at or before this time, if given.
        limit
            The maximum number of events to return. Must be between 1 and
            500 inclusive; an out-of-range value is rejected by the server
            with a 422.
        cursor
            A previous page's `next_cursor`, to fetch the page after it.
        kind
            Only return events of this kind, if given: ``connection`` is every
            ``CONNECTION_*`` type. With `event_type`, the type must be of the kind.
        sort
            What the log is sorted by: ``occurred`` (the default), ``event``, ``who`` or
            ``on`` (the environment, namespace and table); the events of one value come newest
            first.
        order
            ``desc`` (the default, the newest first) or ``asc``, the oldest first. Every order
            lists the same events.

        Returns
        -------
        AuditEventPage
            Up to `limit` matching events, and the cursor for the next page.

        Raises
        ------
        lakana.errors.PermissionDenied
            If the caller may not read the audit log.
        lakana.errors.ValidationError
            If `cursor` is not one the server issued in this order, `kind` is not a kind,
            `event_type` is not of `kind`, or `sort` or `order` is unknown.
        """
        response = self._transport.request(
            ROUTES["audit_events.list"],
            params=query_params(
                event_type=event_type,
                environment=environment,
                principal_id=principal_id,
                since=None if since is None else since.isoformat(),
                until=None if until is None else until.isoformat(),
                limit=limit,
                cursor=cursor,
                kind=kind,
                sort=sort,
                order=order,
            ),
        )
        body = response.json()
        return AuditEventPage(
            items=[_audit_event_from_json(item) for item in body["items"]],
            next_cursor=body["next_cursor"],
        )

    def iter(
        self,
        *,
        event_type: str | None = None,
        environment: str | None = None,
        principal_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        page_size: int = 100,
        kind: str | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> Iterator[AuditEvent]:
        """Yield every matching audit event, newest first, fetching pages as needed.

        Parameters
        ----------
        event_type
            Only yield events of this type, if given.
        environment
            Only yield events whose securable environment matches, if given.
        principal_id
            Only yield events attributed to this principal, if given.
        since
            Only yield events at or after this time, if given.
        until
            Only yield events at or before this time, if given.
        page_size
            Events per request, between 1 and 500 inclusive.
        kind
            Only yield events of this kind, if given: ``connection`` is every
            ``CONNECTION_*`` type.
        sort
            The field, as `list` takes it.
        order
            The direction, as `list` takes it. Every order lists the same events.

        Yields
        ------
        AuditEvent
            Each matching event, once.

        Raises
        ------
        lakana.errors.PermissionDenied
            If the caller may not read the audit log.
        """
        cursor: str | None = None
        while True:
            page = self.list(
                event_type=event_type,
                environment=environment,
                principal_id=principal_id,
                since=since,
                until=until,
                limit=page_size,
                cursor=cursor,
                kind=kind,
                sort=sort,
                order=order,
            )
            yield from page.items
            if page.next_cursor is None:
                return
            cursor = page.next_cursor
