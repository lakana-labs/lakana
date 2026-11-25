"""`lakana audit list`: read the audit log."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

import typer

from lakana._models import AuditEvent
from lakana.cli import _output
from lakana.cli._state import CliError, state_of

audit_app = typer.Typer(help="Read the audit log.", no_args_is_help=True)

_EVENT_COLUMNS = [
    "occurred_at",
    "event_type",
    "principal_id",
    "securable_environment",
    "securable_namespace",
    "securable_table",
]
_EVENT_DROPPABLE = [
    "securable_table",
    "securable_namespace",
    "securable_environment",
    "principal_id",
]
"""What the audit listing leaves out first: the time and the kind of event always stay."""


class EventKind(StrEnum):
    """The values `--kind` accepts: a group of event types."""

    connection = "connection"


def _event_type_cell(event: AuditEvent) -> str:
    """Name an event's type for the table, with the stored type of an unknown one.

    Parameters
    ----------
    event
        The audit event.

    Returns
    -------
    str
        `event_type`, or `UNKNOWN (<raw>)` when the server did not know the stored type.
    """
    if event.event_type == "UNKNOWN" and event.event_type_raw:
        return f"UNKNOWN ({event.event_type_raw})"
    return event.event_type


_EVENT_DERIVED = {"event_type": _event_type_cell}


def parse_timestamp(value: str | None, flag: str) -> datetime | None:
    """Parse an ISO 8601 timestamp, treating one without an offset as UTC.

    Parameters
    ----------
    value
        The raw option value.
    flag
        The option's name, for the error message.

    Returns
    -------
    datetime or None
        An aware datetime, or `None`.

    Raises
    ------
    CliError
        If the value isn't ISO 8601 (exit 2).
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CliError(f"{flag} expects an ISO 8601 timestamp, got {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@audit_app.command("list")
def list_events(
    ctx: typer.Context,
    event_type: Annotated[
        str | None, typer.Option("--event-type", help="e.g. GRANT_CREATED")
    ] = None,
    kind: Annotated[
        EventKind | None,
        typer.Option("--kind", help="Only events of this kind: connection is every CONNECTION_*."),
    ] = None,
    env: Annotated[str | None, typer.Option("--env", help="Only this environment.")] = None,
    principal_id: Annotated[
        str | None, typer.Option("--principal-id", help="Only this principal's events.")
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="ISO 8601; UTC if no offset.")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="ISO 8601; UTC if no offset.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500, help="Events per page.")] = 100,
    cursor: Annotated[
        str | None, typer.Option("--cursor", help="Continue from a previous page.")
    ] = None,
    all_pages: Annotated[bool, typer.Option("--all", help="Follow every page.")] = False,
    order: Annotated[
        str | None,
        typer.Option("--order", help="desc (the default, newest first) or asc (oldest first)."),
    ] = None,
) -> None:
    """List audit events, newest first (oldest first with --order asc).

    `--output json` shows each event's `detail`. Query text and column values in someone else's
    events are hidden unless you hold READ_QUERY_TEXT: a query's SQL text shows as its SHA-256
    and length, an incremental write's watermark as {"hidden": true}. Your own events show in
    full. A page that shows other people's events, or `--principal-id` of someone else, is
    itself recorded in the audit log (ACTIVITY_VIEWED). An event of a type this release does not
    know shows as `UNKNOWN (<type as stored>)`.

    Parameters
    ----------
    ctx
        The Typer context.
    event_type
        Only events of this type.
    kind
        Only events of this kind (``connection``); combines with the other filters.
    env
        Only events in this environment.
    principal_id
        Only events by this principal.
    since
        Only events at or after this time.
    until
        Only events at or before this time.
    limit
        Events per page.
    cursor
        A previous page's `next_cursor`.
    all_pages
        Follow `next_cursor` to the end.
    order
        ``desc`` or ``asc``.

    Raises
    ------
    CliError
        For `--cursor` with `--all`, or a bad timestamp (exit 2).
    """
    state = state_of(ctx)
    if all_pages and cursor:
        raise CliError("--cursor and --all can't be combined")
    filters: dict[str, Any] = {
        "event_type": event_type,
        "kind": None if kind is None else kind.value,
        "environment": env,
        "principal_id": principal_id,
        "since": parse_timestamp(since, "--since"),
        "until": parse_timestamp(until, "--until"),
        "order": order,
    }
    events = state.client().audit_events
    if all_pages:
        _output.emit(
            state,
            list(events.iter(**filters, page_size=limit)),
            columns=_EVENT_COLUMNS,
            droppable=_EVENT_DROPPABLE,
            derived=_EVENT_DERIVED,
        )
        return
    page = events.list(**filters, limit=limit, cursor=cursor)
    if state.output == "json":
        _output.print_json(page)
        return
    _output.emit(
        state,
        page.items,
        columns=_EVENT_COLUMNS,
        droppable=_EVENT_DROPPABLE,
        derived=_EVENT_DERIVED,
    )
    if page.next_cursor:
        sys.stdout.write(f"More: --cursor {page.next_cursor}\n")


def register(app: typer.Typer) -> None:
    """Add the `audit` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(audit_app, name="audit")
