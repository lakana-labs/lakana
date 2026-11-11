"""What the web interface remembers for each person: the tour, Get started and the update note.

Self-service and not audited, like a person's own language (`PrincipalService.set_own_language`):
a person reads and changes only their own state, and the state changes nothing anyone else sees.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from lakana_core.auth import AuthorizationService, PrincipalRef
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import shown
from lakana_core.identity.sessions import utc_now
from lakana_core.models import InterfaceState, Principal

_RELEASE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
"""A release version, ``x.y.z``; a development build's version (``0.22.0.dev3+g…``) is none."""

_VERSION_MAX = 64
"""The longest version stored, the width of `InterfaceState.update_note_seen`."""


class Unset(enum.Enum):
    """The type of `UNSET`: a field a caller did not send, as opposed to one sent as `None`."""

    UNSET = "UNSET"


UNSET = Unset.UNSET
"""Leave the field as it is."""


@dataclass(frozen=True)
class InterfaceStateView:
    """A person's interface state.

    Parameters
    ----------
    tour_seen
        Whether the person finished or skipped the tour.
    get_started_hidden
        Whether the person hid Get started.
    update_note_seen
        The last release whose update note the person saw, or `None`.
    """

    tour_seen: bool
    get_started_hidden: bool
    update_note_seen: str | None


def require_self(p: PrincipalRef, name: str, what: str) -> None:
    """Refuse a caller asking about another person, or signed in with a task or CI token.

    Parameters
    ----------
    p
        The caller.
    name
        The person named in the request.
    what
        What is asked about, for the message: ``interface state``, ``recently opened list``.

    Raises
    ------
    PermissionDenied
        For a task or CI token, or a name that is not the caller's.
    """
    if p.task_scope is not None or p.ci_scope is not None:
        raise PermissionDenied(
            f"A task or CI token cannot read or change a person's {what}; "
            "sign in to the web interface or use a personal access token."
        )
    if name != p.name:
        raise PermissionDenied(f"You can only read and change your own {what}.")


def _view(row: InterfaceState | None) -> InterfaceStateView:
    """Describe a stored row, or the defaults without one.

    Parameters
    ----------
    row
        The row, or `None`.

    Returns
    -------
    InterfaceStateView
        The state.
    """
    if row is None:
        return InterfaceStateView(tour_seen=False, get_started_hidden=False, update_note_seen=None)
    return InterfaceStateView(
        tour_seen=row.tour_seen_at is not None,
        get_started_hidden=row.get_started_hidden,
        update_note_seen=row.update_note_seen,
    )


def _check_version(version: str) -> None:
    """Refuse anything but a release version.

    Parameters
    ----------
    version
        The version sent.

    Raises
    ------
    ValidationError
        Naming the form ``x.y.z``.
    """
    if len(version) > _VERSION_MAX:
        raise ValidationError(
            f"update_note_seen: a version is at most {_VERSION_MAX} characters; "
            "send the release version as x.y.z, for example 0.23.0."
        )
    if _RELEASE_VERSION.fullmatch(version) is None:
        raise ValidationError(
            f"update_note_seen: '{shown(version)}' is not a release version; send it as x.y.z, "
            "for example 0.23.0 (a development build has no update note to mark as seen)."
        )


class InterfaceStateService:
    """Read and change a person's own interface state.

    Parameters
    ----------
    session
        Database session; `update` commits.
    authz
        Authorization service. Nothing is asked of it: a person may always read and change their
        own state, and nobody another's.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def get(self, p: PrincipalRef, name: str) -> InterfaceStateView:
        """Read a person's interface state.

        Parameters
        ----------
        p
            The caller.
        name
            The person; must be the caller.

        Returns
        -------
        InterfaceStateView
            The stored state, or the defaults for a person without a row (nothing is written).

        Raises
        ------
        PermissionDenied
            For another person's name, or a task or CI token.
        """
        require_self(p, name, "interface state")
        return _view(self._session.get(InterfaceState, p.id))

    def update(
        self,
        p: PrincipalRef,
        name: str,
        *,
        tour_seen: bool | None = None,
        get_started_hidden: bool | None = None,
        update_note_seen: str | Unset | None = UNSET,
    ) -> InterfaceStateView:
        """Change the fields sent and keep the others.

        A refused request changes nothing, so a caller that marks the tour as seen sends that in
        a request of its own whenever the version it would add might be refused.

        Parameters
        ----------
        p
            The caller.
        name
            The person; must be the caller.
        tour_seen
            `True` once the tour was finished or skipped (the first time is kept), `False` to
            show it again; `None` leaves it.
        get_started_hidden
            Whether Get started is hidden; `None` leaves it.
        update_note_seen
            The release whose update note was seen (``x.y.z``), `None` to clear it, `UNSET` to
            leave it.

        Returns
        -------
        InterfaceStateView
            The state now stored.

        Raises
        ------
        PermissionDenied
            For another person's name, or a task or CI token.
        ValidationError
            For a version that is not ``x.y.z``.
        NotFoundError
            If the caller's principal no longer exists.
        """
        require_self(p, name, "interface state")
        if isinstance(update_note_seen, str):
            _check_version(update_note_seen)
        row = self._session.get(InterfaceState, p.id)
        now = utc_now()
        if row is None:
            if self._session.get(Principal, p.id) is None:
                raise NotFoundError("Your principal no longer exists.")
            row = InterfaceState(principal_id=p.id, get_started_hidden=False, updated_at=now)
            self._session.add(row)
        if tour_seen is not None:
            row.tour_seen_at = (row.tour_seen_at or now) if tour_seen else None
        if get_started_hidden is not None:
            row.get_started_hidden = get_started_hidden
        if not isinstance(update_note_seen, Unset):
            row.update_note_seen = update_note_seen
        row.updated_at = now
        self._session.commit()
        return _view(row)
