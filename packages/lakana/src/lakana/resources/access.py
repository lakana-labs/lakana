"""Access as code (`Client.access`): export the live access as a file, plan a file's changes.

Both calls need the ADMIN role on the instance. The file is read on the server, so a plan
lists every problem of a file in one answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import AccessChange, AccessDrift, AccessPlan
from lakana._routes import ROUTES
from lakana.errors import InvalidResponse
from lakana.resources.artifacts import _ODD_ANSWER, _object


def _change_from_json(data: dict[str, Any]) -> AccessChange:
    """Build an `AccessChange` from an `AccessChangeOut` body.

    Parameters
    ----------
    data
        The decoded change.

    Returns
    -------
    AccessChange
        The change.
    """
    entry = data["entry"]
    return AccessChange(
        op=str(data["op"]), entry=None if entry is None else str(entry), text=str(data["text"])
    )


def _drift_from_json(data: dict[str, Any]) -> AccessDrift:
    """Build an `AccessDrift` from an `AccessDriftOut` body.

    Parameters
    ----------
    data
        The decoded drift.

    Returns
    -------
    AccessDrift
        The drift.
    """
    by, reason, at = data["by"], data["reason"], data["at"]
    return AccessDrift(
        scope=str(data["scope"]),
        text=str(data["text"]),
        by=None if by is None else str(by),
        reason=None if reason is None else str(reason),
        at=None if at is None else datetime.fromisoformat(str(at)),
    )


def _plan_from_json(data: dict[str, Any]) -> AccessPlan:
    """Build an `AccessPlan` from an `AccessPlanOut` body; unknown fields are ignored.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    AccessPlan
        The plan.

    Raises
    ------
    InvalidResponse
        If `data` is not a plan.
    """
    try:
        return AccessPlan(
            file_name=str(data["file_name"]),
            file_sha256=str(data["file_sha256"]),
            manages=[str(x) for x in data["manages"]],
            creates=[_change_from_json(x) for x in data["creates"]],
            changed=[_change_from_json(x) for x in data["changed"]],
            removals=[_change_from_json(x) for x in data["removals"]],
            releases=[str(x) for x in data["releases"]],
            drift=[_drift_from_json(x) for x in data["drift"]],
            problems=[str(x) for x in data["problems"]],
            fingerprint=str(data["fingerprint"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InvalidResponse(_ODD_ANSWER.format(what="an access plan")) from None


class Access:
    """The access-as-code calls of the platform API; instance administrators only.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def export(self, scopes: Sequence[str] = ("instance",)) -> str:
        """Write the live access of some scopes as an access file.

        Parameters
        ----------
        scopes
            ``instance`` alone (the default), or environment names.

        Returns
        -------
        str
            The file's text; planning it unedited plans nothing.

        Raises
        ------
        InvalidResponse
            If the answer has no text.
        """
        response = self._transport.request(ROUTES["access.export"], params={"scope": list(scopes)})
        text = _object(response, "the exported access").get("text")
        if not isinstance(text, str):
            raise InvalidResponse(_ODD_ANSWER.format(what="the exported access"))
        return text

    def plan(
        self, text: str, *, file_name: str = "access.yaml", allow_lockout: bool = False
    ) -> AccessPlan:
        """Say what applying an access file would change, and every problem that stops it.

        A file with problems is a plan with problems, not an error; nothing is changed.

        Parameters
        ----------
        text
            The file's content.
        file_name
            Its base name, for the messages.
        allow_lockout
            Accept a file that drops the caller's own ADMIN role while another administrator
            remains.

        Returns
        -------
        AccessPlan
            The changes, drift and problems.
        """
        response = self._transport.request(
            ROUTES["access.plan"],
            json={"file_name": file_name, "text": text, "allow_lockout": allow_lockout},
        )
        return _plan_from_json(_object(response, "the access plan"))
