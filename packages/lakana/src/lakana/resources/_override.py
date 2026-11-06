"""The query parameters of the managed-scope override, shared by the access resources (#46)."""

from __future__ import annotations


def override_params(reason: str | None) -> dict[str, object] | None:
    """Build the query parameters that override a scope an access file manages.

    Parameters
    ----------
    reason
        Why, 10 to 500 characters on one line; `None` for a change without the override.

    Returns
    -------
    dict or None
        ``override_managed=true`` and ``override_reason``, or `None` without a reason.
    """
    if reason is None:
        return None
    return {"override_managed": "true", "override_reason": reason}
