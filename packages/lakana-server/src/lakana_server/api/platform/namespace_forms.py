"""A namespace path in its two forms: dotted on the wire, unit-separator-joined in the database.

Grants, row filters, column masks and audit events store a namespace joined with the unit
separator (`lakana_core.identifiers.NamespacePath.joined`) and show it dotted (``gold.sales``).
These two functions are the one conversion every router uses.
"""

from __future__ import annotations

from typing import overload

from lakana_core.identifiers import NamespacePath


@overload
def joined_namespace(namespace: str) -> str: ...
@overload
def joined_namespace(namespace: None) -> None: ...
@overload
def joined_namespace(namespace: str | None) -> str | None: ...
def joined_namespace(namespace: str | None) -> str | None:
    """Convert a dot-joined namespace path to its unit-separator storage form.

    Parameters
    ----------
    namespace
        A dotted path such as ``"gold.sales"``, or `None`.

    Returns
    -------
    str or None
        The unit-separator-joined storage form, or `None` unchanged.
    """
    if namespace is None:
        return None
    return NamespacePath.from_parts(namespace.split(".")).joined()


@overload
def dotted_namespace(namespace: str) -> str: ...
@overload
def dotted_namespace(namespace: None) -> None: ...
@overload
def dotted_namespace(namespace: str | None) -> str | None: ...
def dotted_namespace(namespace: str | None) -> str | None:
    """Convert a stored unit-separator namespace path to its dotted form.

    Parameters
    ----------
    namespace
        The unit-separator-joined storage form, or `None`.

    Returns
    -------
    str or None
        The dot-joined display form, or `None` unchanged.
    """
    if namespace is None:
        return None
    return ".".join(NamespacePath.parse(namespace).parts)
