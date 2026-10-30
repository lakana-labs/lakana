"""Telling a storage that cannot be reached from a file that is missing or wrong."""

from __future__ import annotations

TABLE_STORAGE_UNREACHABLE = (
    "The storage of environment '{environment}' could not be reached, so no table was read. Try "
    "again in a moment; if it keeps failing, tell the operator that this environment's storage "
    "may be unreachable."
)
"""What a table read says when the environment's storage did not answer. `{environment}` is the
name as `lakana_core.identifiers.shown` makes it safe; no path, host or storage text is in it."""

ORPHANED_STORAGE_REFUSED = (
    "The storage of environment '{environment}' refused access, so no directory was listed or "
    "deleted. Ask the operator to check the environment's storage credentials (its storage "
    "options, or the secret they name), then try again."
)
"""What the orphaned-locations routes say when the storage refused the environment's credentials.
`{environment}` is the shown name; no path, host or storage text is in it."""

ORPHANED_STORAGE_UNREACHABLE = (
    "The storage of environment '{environment}' could not be reached, so no directory was listed "
    "or deleted. Try again in a moment; if it keeps failing, tell the operator that this "
    "environment's storage may be unreachable."
)
"""What the orphaned-locations routes say when the environment's storage did not answer."""


def is_storage_unavailable(exc: BaseException) -> bool:
    """Tell whether a read failed because the storage did not answer.

    The storage clients raise an `OSError` for a refused or broken connection, a timeout, a
    name that does not resolve and an error answer of the storage service. A missing file
    (`FileNotFoundError`), a directory where a file was expected and a refusal of the
    credentials (`PermissionError`) are not that: the storage answered.

    Parameters
    ----------
    exc
        What the read raised.

    Returns
    -------
    bool
        Whether it is an `OSError` other than a missing file, a directory or a refusal.
    """
    return isinstance(exc, OSError) and not isinstance(
        exc, (FileNotFoundError, IsADirectoryError, NotADirectoryError, PermissionError)
    )
