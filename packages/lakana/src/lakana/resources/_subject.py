"""Read the principal of a grant, row filter or column mask from an answer."""

from __future__ import annotations

from typing import Any

from lakana._models import SubjectPrincipal


def subject_principal(data: dict[str, Any] | None) -> SubjectPrincipal | None:
    """Build a `SubjectPrincipal` from the API's `principal` object.

    Parameters
    ----------
    data
        The decoded `principal` object, or `None` when the answer names none: the row is for a
        group, the principal is gone, or the server is older than this release.

    Returns
    -------
    SubjectPrincipal or None
        The principal, or `None` when there is none to read.
    """
    if data is None:
        return None
    return SubjectPrincipal(id=data["id"], name=data["name"], principal_type=data["principal_type"])
