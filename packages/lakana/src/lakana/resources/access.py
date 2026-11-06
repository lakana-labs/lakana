"""Access as code (`Client.access`): export, plan, apply and status.

Every call needs the ADMIN role on the instance. The file is read on the server, so a plan
lists every problem of a file in one answer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import (
    AccessApplyResult,
    AccessChange,
    AccessDrift,
    AccessOverride,
    AccessPlan,
    AccessStatus,
    ManagedScope,
)
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
            starts=[str(x) for x in data["starts"]],
            drift=[_drift_from_json(x) for x in data["drift"]],
            problems=[str(x) for x in data["problems"]],
            fingerprint=str(data["fingerprint"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InvalidResponse(_ODD_ANSWER.format(what="an access plan")) from None


def _apply_from_json(data: dict[str, Any]) -> AccessApplyResult:
    """Build an `AccessApplyResult` from an `AccessApplyOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    AccessApplyResult
        The result.

    Raises
    ------
    InvalidResponse
        If `data` is not an apply result.
    """
    try:
        commit = data["source_commit"]
        return AccessApplyResult(
            apply_id=uuid.UUID(str(data["apply_id"])),
            file_sha256=str(data["file_sha256"]),
            source_commit=None if commit is None else str(commit),
            created=int(data["created"]),
            changed=int(data["changed"]),
            removed=int(data["removed"]),
            released=[str(x) for x in data["released"]],
            applied_at=datetime.fromisoformat(str(data["applied_at"])),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InvalidResponse(_ODD_ANSWER.format(what="an access apply result")) from None


def _optional(value: object) -> str | None:
    """Return a nullable body field as text.

    Parameters
    ----------
    value
        The decoded value.

    Returns
    -------
    str or None
        The text; `None` for null.
    """
    return None if value is None else str(value)


def _status_from_json(data: dict[str, Any]) -> AccessStatus:
    """Build an `AccessStatus` from an `AccessStatusOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    AccessStatus
        The status.

    Raises
    ------
    InvalidResponse
        If `data` is not an access status.
    """
    try:
        return AccessStatus(
            scopes=[
                ManagedScope(
                    scope=str(x["scope"]),
                    file_name=str(x["file_name"]),
                    file_sha256=str(x["file_sha256"]),
                    source_commit=_optional(x["source_commit"]),
                    applied_by=_optional(x["applied_by"]),
                    applied_at=datetime.fromisoformat(str(x["applied_at"])),
                )
                for x in data["scopes"]
            ],
            overrides=[
                AccessOverride(
                    scope=str(x["scope"]),
                    change=str(x["change"]),
                    reason=str(x["reason"]),
                    by=_optional(x["by"]),
                    at=datetime.fromisoformat(str(x["at"])),
                )
                for x in data["overrides"]
            ],
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InvalidResponse(_ODD_ANSWER.format(what="the access status")) from None


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

    def apply(
        self,
        text: str,
        *,
        plan_fingerprint: str,
        file_name: str = "access.yaml",
        source_commit: str | None = None,
        allow_lockout: bool = False,
    ) -> AccessApplyResult:
        """Make an access file's changes in one audited transaction, or none of them.

        The server plans the file again and refuses when the file or access changed since the
        plan with `plan_fingerprint`. The request is sent once and never retried.

        Parameters
        ----------
        text
            The file's content.
        plan_fingerprint
            The fingerprint of the plan that was reviewed.
        file_name
            Its base name, for the messages and the record.
        source_commit
            The commit the file came from, 7 to 64 hex digits; recorded with the apply.
        allow_lockout
            Accept a file that drops the caller's own ADMIN role while another administrator
            remains.

        Returns
        -------
        AccessApplyResult
            The apply's id and counts.
        """
        response = self._transport.request(
            ROUTES["access.apply"],
            json={
                "file_name": file_name,
                "text": text,
                "plan_fingerprint": plan_fingerprint,
                "source_commit": source_commit,
                "allow_lockout": allow_lockout,
            },
            retry=False,
        )
        return _apply_from_json(_object(response, "the access apply result"))

    def status(self) -> AccessStatus:
        """List the scopes access files manage and the open overrides.

        Returns
        -------
        AccessStatus
            Each managed scope with its last apply, and each override not yet taken into
            account.
        """
        return _status_from_json(
            _object(self._transport.request(ROUTES["access.status"]), "the access status")
        )
