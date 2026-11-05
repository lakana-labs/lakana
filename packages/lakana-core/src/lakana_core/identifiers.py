"""Value objects identifying namespaces and tables.

Namespaces are multi-level and are stored as a single string joined by the
ASCII unit separator, which is also how the Iceberg REST specification encodes
them in URLs. The display form is dotted; `TableRef.from_dotted` parses the
user-facing dotted form SQL and the SDK use, which is ambiguous for a
component that itself contains a dot.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from lakana_core.errors import ValidationError

NAMESPACE_SEPARATOR: Final[str] = "\x1f"
"""ASCII unit separator, matching the Iceberg REST namespace encoding."""


_SECRET_NAME = re.compile(r"\A[^\x00-\x20\x7f:]{1,255}\Z")

_SHOWN_CHARS = 200
_SHOWN_TAIL_CHARS = 120


NOTIFY_FORMATS = ("json", "slack", "teams")
"""The message formats a job's `notify:` can ask for."""


def is_secret_name(name: object) -> bool:
    """Say whether a value can name a secret that a setting or a file refers to.

    Parameters
    ----------
    name
        The candidate.

    Returns
    -------
    bool
        `True` for text of 1 to 255 characters with no space, no control character and no
        ``:``.
    """
    return isinstance(name, str) and _SECRET_NAME.match(name) is not None


def shown(path: object) -> str:
    """Make a name a caller sent safe to put into a message.

    Parameters
    ----------
    path
        What a request, manifest or URL gave as a name or path.

    Returns
    -------
    str
        The ASCII-escaped text, at most 200 characters; the type's name for
        anything that is not a string.
    """
    if not isinstance(path, str):
        return type(path).__name__
    return ascii(path)[1:-1][:_SHOWN_CHARS]


def shown_path(path: object) -> str:
    """Make a storage path safe to put into a message, keeping the file's name.

    Parameters
    ----------
    path
        A storage URI or path, for example a data file's.

    Returns
    -------
    str
        The ASCII-escaped path when it has at most 200 characters. A longer one keeps its
        start (the scheme and bucket) and its last component, cut from the front only when
        that component alone has more than 120 characters, and loses the middle, marked by
        ``…``. The type's name for anything that is not a string.
    """
    if not isinstance(path, str):
        return type(path).__name__
    text = ascii(path)[1:-1]
    if len(text) <= _SHOWN_CHARS:
        return text
    slash = text.rfind("/")
    tail = (text[slash:] if slash >= 0 else text)[-_SHOWN_TAIL_CHARS:]
    return f"{text[: _SHOWN_CHARS - 1 - len(tail)]}…{tail}"


def validate_path_component(component: str, *, what: str, allow_dots: bool = False) -> None:
    """Validate one segment of a namespace or table identifier.

    This is the fail-early, specific-error layer: a component containing
    ``/`` or a bare ``.``/``..`` traversal segment is meaningless regardless
    of storage, so it is rejected here with a precise message before it can
    reach anything that builds a filesystem path from it.

    A component containing a literal ``.`` is rejected too: the dotted
    display form would otherwise render ``["a.b"]`` and ``["a", "b"]``
    identically. Names created before that rule may still contain a dot, so
    code that only *looks up* or lists an existing name passes
    ``allow_dots=True``; only creating or renaming-to a name is strict.

    :meth:`NamespacePath.from_parts` calls this for every namespace
    component. It is also called directly by
    :meth:`lakana_core.storage.resolver.ResolvedStorage.table_location` to
    validate the table name, which is never wrapped in a `NamespacePath` and
    so would otherwise reach that choke point unchecked — see that method's
    docstring for why it must validate independently.

    Parameters
    ----------
    component
        The candidate component.
    what
        Noun describing the component, used to phrase the error message
        (for example ``"Namespace component"`` or ``"Table name"``).
    allow_dots
        Accept a ``.`` inside the component. Only for reading names that may
        predate the rule; never for a name being created.

    Raises
    ------
    ValidationError
        If the component is empty, whitespace-only, is exactly ``.`` or
        ``..``, or contains ``/``, a NUL byte, or the unit separator, or
        (unless `allow_dots`) contains ``.``. A ``%`` is refused only in a new name
        (`refuse_percent_in_new_name`).
    """
    if not component:
        raise ValidationError(f"{what} must not be empty.")
    if "\x00" in component:
        raise ValidationError(f"{what} must not contain a NUL byte.")
    if not component.strip():
        raise ValidationError(f"{what} must not be whitespace-only.")
    if NAMESPACE_SEPARATOR in component:
        raise ValidationError(f"{what} must not contain the unit separator.")
    if "/" in component:
        raise ValidationError(f"{what} must not contain '/'.")
    if component in (".", ".."):
        raise ValidationError(f"{what} must not be '.' or '..'.")
    if not allow_dots and "." in component:
        raise ValidationError(f"{what} '{shown(component)}' must not contain a '.'.")


ENVIRONMENT_NAME_MAX = 255
"""The longest environment name the access file can write."""


def environment_name_problem(name: str) -> tuple[str, str] | None:
    """Say why an environment name cannot be written in an access file.

    The access file names a target as ``instance``, ``ENV`` or ``ENV.NAMESPACE``, one name per
    line of text, so an environment called ``instance`` or containing a ``.`` would read back
    as something else.

    Parameters
    ----------
    name
        The environment name.

    Returns
    -------
    tuple of (str, str) or None
        The cause and the next step; `None` when the name is fine.
    """
    if name.casefold() == "instance":
        return (
            "is reserved: the access file uses 'instance' for the whole instance",
            "Pick another name, such as 'main'.",
        )
    if "." in name:
        return (
            "contains a '.', which the access file reads as the start of a namespace "
            f"({name.partition('.')[0]!r} and {name.partition('.')[2]!r})",
            f"Use a '-' or '_' instead, such as {name.replace('.', '-')!r}.",
        )
    if not 1 <= len(name) <= ENVIRONMENT_NAME_MAX:
        return (
            f"must be 1 to {ENVIRONMENT_NAME_MAX} characters long",
            "Pick a shorter name.",
        )
    if name != name.strip() or not name.isprintable():
        return (
            "must be printable characters without a blank at either end",
            "Remove the blanks and control characters.",
        )
    return None


def check_environment_name(name: str) -> None:
    """Refuse a new environment name the access file cannot write.

    Parameters
    ----------
    name
        The name of the environment being created.

    Raises
    ------
    ValidationError
        If `environment_name_problem` finds one; the message names the cause and the next step.
    """
    if (problem := environment_name_problem(name)) is not None:
        raise ValidationError(f"Environment name '{shown(name)}' {problem[0]}. {problem[1]}")


def refuse_percent_in_new_name(component: str, *, what: str) -> None:
    """Refuse a ``%`` in a table or namespace name that is being created.

    A storage may read ``%41`` as ``A``, so two new names would share one directory. Only the
    places that create a table or namespace name call this (`CatalogService.create_namespace`
    for each newly named component, `ResolvedStorage.table_location` for a new table, and the
    view, external-table, rename and undrop targets). Parsing, discovery, matching and looking
    up a name never do: names stored by an older release may hold a ``%`` and stay usable.

    Parameters
    ----------
    component
        The new name, already checked by `validate_path_component`.
    what
        Noun describing the component, as for `validate_path_component`.

    Raises
    ------
    ValidationError
        If the component contains ``%``; the message names every character that is allowed.
    """
    if "%" in component:
        raise ValidationError(
            f"{what} '{shown(component)}' must not contain '%': a storage may read '%41' as 'A', "
            "so two names would share one directory. Every other character is allowed except "
            "'.', '/', the unit separator and a NUL byte."
        )


@dataclass(frozen=True)
class NamespacePath:
    """A multi-level namespace, such as ``gold.sales``.

    Parameters
    ----------
    parts
        The namespace components, outermost first. Use :meth:`from_parts` or
        :meth:`parse` rather than constructing this directly, so that the
        components are validated.
    """

    parts: tuple[str, ...]

    @classmethod
    def from_parts(cls, parts: Sequence[str], *, allow_dots: bool = False) -> NamespacePath:
        """Build a path from its components.

        Parameters
        ----------
        parts
            Namespace components, outermost first.
        allow_dots
            Accept a ``.`` inside a component, so a namespace created before
            dots were refused can still be addressed. Leave ``False`` for a name being
            created.

        Returns
        -------
        NamespacePath
            The validated path.

        Raises
        ------
        ValidationError
            If there are no components, or a component is empty,
            whitespace-only, ``.``, ``..``, contains ``/`` or the unit
            separator, or (unless `allow_dots`) contains ``.``. Rejecting
            ``/`` here also prevents a namespace such as ``["a/b"]`` from
            colliding with ``["a", "b"]``, which would otherwise resolve to
            the identical storage location once a table location is built
            by joining the parts with ``/``.
        """
        if not parts:
            raise ValidationError("A namespace must have at least one component.")
        for part in parts:
            validate_path_component(part, what="Namespace component", allow_dots=allow_dots)
        return cls(tuple(parts))

    @classmethod
    def parse(cls, joined: str) -> NamespacePath:
        """Parse the separator-joined storage form.

        Stored and URL-encoded names may predate the rule against dots and contain a dot, so
        this never rejects one; the create path checks the new component
        itself.

        Parameters
        ----------
        joined
            A namespace in its stored form.

        Returns
        -------
        NamespacePath
            The validated path.

        Raises
        ------
        ValidationError
            If the joined form yields no components, or an empty one.
        """
        return cls.from_parts(joined.split(NAMESPACE_SEPARATOR), allow_dots=True)

    def joined(self) -> str:
        """Return the separator-joined storage form.

        Returns
        -------
        str
            The components joined by the unit separator.
        """
        return NAMESPACE_SEPARATOR.join(self.parts)

    def parent(self) -> NamespacePath | None:
        """Return the enclosing namespace.

        Returns
        -------
        NamespacePath or None
            The path without its final component, or ``None`` for a top-level
            namespace.
        """
        if len(self.parts) == 1:
            return None
        return NamespacePath(self.parts[:-1])

    def child(self, name: str) -> NamespacePath:
        """Return this path extended by one component.

        Parameters
        ----------
        name
            The component to append.

        Returns
        -------
        NamespacePath
            The extended path.

        Raises
        ------
        ValidationError
            If the component is empty, contains the separator, or contains
            ``.``.
        """
        validate_path_component(name, what="Namespace component")
        return NamespacePath.from_parts([*self.parts, name], allow_dots=True)

    def __str__(self) -> str:
        """Return the dotted display form.

        Returns
        -------
        str
            Components joined by dots. This form is for display only and is
            never parsed back, because a component may itself contain a dot.
        """
        return ".".join(self.parts)


@dataclass(frozen=True)
class TableRef:
    """A fully qualified reference to a table.

    Parameters
    ----------
    environment
        Name of the environment the table lives in.
    namespace
        The namespace containing the table.
    name
        The table's name within its namespace.
    """

    environment: str
    namespace: NamespacePath
    name: str

    def __post_init__(self) -> None:
        """Refuse a NUL character in the environment or the name.

        No name can hold one, and a database parameter that does is refused by Postgres as a
        server error instead of finding nothing. The namespace's components are checked when
        it is built.

        Raises
        ------
        ValidationError
            If the environment or the table name contains a NUL character.
        """
        if "\x00" in self.environment:
            raise ValidationError("An environment name must not contain a NUL byte.")
        if "\x00" in self.name:
            raise ValidationError("Table name must not contain a NUL byte.")

    @classmethod
    def from_dotted(cls, environment: str, dotted: str) -> TableRef:
        """Parse the dotted ``namespace.table`` form SQL and the SDK address tables by.

        Every dot separates a component and the last component is the table,
        so a component containing a literal dot is ambiguous — the same
        rule `lakana_worker.pipeline._resolve_sources` has always applied.

        Parameters
        ----------
        environment
            The environment the table lives in.
        dotted
            A dotted name such as ``"gold.sales.daily"``.

        Returns
        -------
        TableRef
            The reference.

        Raises
        ------
        ValidationError
            If `dotted` has no namespace, or any component is empty or
            otherwise invalid.
        """
        *namespace_parts, name = dotted.split(".")
        if not namespace_parts:
            raise ValidationError(
                f"Expected a dotted 'namespace.table' name, got '{shown(dotted)}'."
            )
        validate_path_component(name, what="Table name")
        return cls(
            environment=environment, namespace=NamespacePath.from_parts(namespace_parts), name=name
        )

    def dotted(self) -> str:
        """Return the dotted ``namespace.table`` display form.

        Returns
        -------
        str
            The namespace's dotted form, a dot, and the table name.
        """
        return f"{self.namespace}.{self.name}"
