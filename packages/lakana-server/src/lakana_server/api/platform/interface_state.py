"""A person's own interface state and recently opened list.

What the web interface remembers per person: whether they saw the tour, whether they hid Get
started, the last update note they saw, and what they opened recently (so the list follows them
to another browser). Each person reads and changes only their own (403 for another name, and for
a task or CI token); nothing here is audited, as a person's own language is not.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from lakana_core.auth import PrincipalRef
from lakana_core.home.recent import RecentService
from lakana_core.identity.interface_state import UNSET, InterfaceStateService, InterfaceStateView
from lakana_server.api.platform.schemas import (
    InterfaceStateIn,
    InterfaceStateOut,
    RecentIn,
    RecentOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_interface_state_service, get_principal, get_recent_service

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/principals",
    tags=["interface"],
    responses=errors(401, 403),
)

_NAME = Annotated[str, Path(min_length=1, max_length=255)]


def _out(view: InterfaceStateView) -> InterfaceStateOut:
    """Turn the service's view into the response body.

    Parameters
    ----------
    view
        The state.

    Returns
    -------
    InterfaceStateOut
        The body.
    """
    return InterfaceStateOut(
        tour_seen=view.tour_seen,
        get_started_hidden=view.get_started_hidden,
        update_note_seen=view.update_note_seen,
    )


@router.get("/{name}/interface-state", response_model=InterfaceStateOut)
def get_interface_state(
    name: _NAME,
    principal: PrincipalRef = Depends(get_principal),
    service: InterfaceStateService = Depends(get_interface_state_service),
) -> InterfaceStateOut:
    """Show what the web interface remembers for you.

    Only your own (403 for another person's name, and for a task or CI token). A person who
    never changed anything gets the defaults: the tour not seen, Get started shown, no update
    note seen.

    Parameters
    ----------
    name
        Your principal name.
    principal
        The authenticated caller.
    service
        The interface state service.

    Returns
    -------
    InterfaceStateOut
        Your interface state.
    """
    return _out(service.get(principal, name))


@router.patch("/{name}/interface-state", response_model=InterfaceStateOut)
def change_interface_state(
    name: _NAME,
    body: InterfaceStateIn,
    principal: PrincipalRef = Depends(get_principal),
    service: InterfaceStateService = Depends(get_interface_state_service),
) -> InterfaceStateOut:
    """Change what the web interface remembers for you.

    Changes only the fields sent. `update_note_seen` takes a release version, `x.y.z`; anything
    else, a development build's version too, is refused with 422 and nothing in the request is
    stored. So the interface records the tour in a request of its own, which no version can
    hold up. Only your own (403 otherwise, and for a task or CI token).

    Parameters
    ----------
    name
        Your principal name.
    body
        The fields to change.
    principal
        The authenticated caller.
    service
        The interface state service.

    Returns
    -------
    InterfaceStateOut
        Your interface state now.
    """
    sent = body.model_dump(exclude_unset=True)
    return _out(
        service.update(
            principal,
            name,
            tour_seen=sent.get("tour_seen"),
            get_started_hidden=sent.get("get_started_hidden"),
            update_note_seen=sent.get("update_note_seen", UNSET),
        )
    )


@router.get("/{name}/recent", response_model=list[RecentOut])
def list_recent(
    name: _NAME,
    principal: PrincipalRef = Depends(get_principal),
    service: RecentService = Depends(get_recent_service),
) -> list[RecentOut]:
    """List what you opened recently, newest first.

    At most 20. Something you may no longer read, or that no longer exists, is left out. Only
    your own (403 otherwise, and for a task or CI token).

    Parameters
    ----------
    name
        Your principal name.
    principal
        The authenticated caller.
    service
        The recently opened service.

    Returns
    -------
    list of RecentOut
        The objects, newest first.
    """
    return [
        RecentOut(kind=v.kind, environment=v.environment, ref=v.ref, opened_at=v.opened_at)
        for v in service.list(principal, name)
    ]


@router.post("/{name}/recent", status_code=204, responses=errors(404))
def record_recent(
    name: _NAME,
    body: RecentIn,
    principal: PrincipalRef = Depends(get_principal),
    service: RecentService = Depends(get_recent_service),
) -> None:
    """Record that you opened a table, a query, a job or a run.

    The web interface's detail pages call it. Opening the same object again within a minute
    changes nothing; only the newest 20 are kept. Answers 404 when the object does not exist or
    you may not read it (the same answer for both). Only your own (403 otherwise, and for a
    task or CI token).

    Parameters
    ----------
    name
        Your principal name.
    body
        What you opened.
    principal
        The authenticated caller.
    service
        The recently opened service.
    """
    service.record(principal, name, kind=body.kind, environment=body.environment, ref=body.ref)
