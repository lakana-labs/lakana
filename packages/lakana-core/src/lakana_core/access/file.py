"""Parse an access file: strict YAML, every problem at once, each naming its entry (#46)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from lakana_core.access.model import (
    INSTANCE,
    SUBJECT_KINDS,
    AccessState,
    CiTrustEntry,
    ColumnMaskEntry,
    Entry,
    GrantEntry,
    GroupEntry,
    MembershipEntry,
    RowFilterEntry,
    Scopes,
    ServiceAccountEntry,
    Subject,
    Target,
    environment_of,
)
from lakana_core.auth import Action
from lakana_core.errors import ValidationError
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.projects.yaml_loader import KeyLines, YamlError, load_yaml_with_lines, shown_key

MAX_FILE_BYTES = 1024 * 1024
MAX_ENTRIES = 10_000
_PARSE_LIMIT = 200_000
"""Keys and list items the loader composes before giving up; far above `MAX_ENTRIES` entries."""
_MAX_PROBLEMS = 50
"""The most problems one refusal lists; the rest are counted."""
_MAX_NAME = 255

_TOP_KEYS = ("version", "manages", "groups", "service_accounts", "grants", "policies", "ci_trust")
_GRANT_KEYS = ("to", "role", "action", "on", "table")
_ROW_FILTER_KEYS = ("kind", "table", "expression", "to")
_COLUMN_MASK_KEYS = ("kind", "table", "column", "expression", "to")
_CI_KEYS = (
    "issuer",
    "repository",
    "as",
    "on",
    "conditions",
    "kinds",
    "enabled",
    "start_on_deploy_jobs",
)
_CI_KINDS = ("deploy", "dry_run")
_PATH_TOKEN = re.compile(r"\[(\d+)\]|([^.\[\]]+)")

Path = tuple[object, ...]

_MISSING = object()
"""What `_Reader.required` returns for a key that is not there, so no second problem follows."""


class AccessFileError(ValidationError):
    """An access file with problems, every one found.

    Parameters
    ----------
    problems
        One sentence per problem, each naming the file, the entry, the cause and the next step.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


def cap_problems(file_name: str, problems: Sequence[str], count: int | None = None) -> list[str]:
    """Cap a list of problems at the most one answer lists.

    Parameters
    ----------
    file_name
        The file's name, for the closing line.
    problems
        The problems kept so far.
    count
        How many problems there were in all; `len(problems)` when omitted.

    Returns
    -------
    list of str
        At most `_MAX_PROBLEMS` problems and a closing line when there were more.
    """
    total = len(problems) if count is None else count
    if total <= _MAX_PROBLEMS:
        return list(problems)
    more = total - _MAX_PROBLEMS
    return [
        *problems[:_MAX_PROBLEMS],
        f"{file_name}: and {more} more problems; fix these first, then check again",
    ]


def validate_file_name(name: str) -> str:
    """Check the base name a file is announced under.

    Parameters
    ----------
    name
        The name the messages will use.

    Returns
    -------
    str
        The same name.

    Raises
    ------
    ValidationError
        If it is empty, longer than 255 characters, or holds a slash, a backslash or a control
        character.
    """
    if (
        not 1 <= len(name) <= _MAX_NAME
        or "/" in name
        or "\\" in name
        or any(not c.isprintable() for c in name)
    ):
        raise ValidationError(
            "file_name must be 1 to 255 characters without / or \\ or a control character; "
            "send the file's base name, such as access.yaml"
        )
    return name


def _path_text(path: Path) -> str:
    """Write a path of keys and positions as ``grants[3].to``.

    Parameters
    ----------
    path
        Keys (text) and list positions (integers) from the top.

    Returns
    -------
    str
        The path as a message names it.
    """
    text = ""
    for part in path:
        if isinstance(part, int) and not isinstance(part, bool):
            text += f"[{part}]"
        else:
            text += ("." if text else "") + shown_key(part)
    return text


@dataclass(frozen=True)
class _Locator:
    """Finds the line of a path in the file it was parsed from.

    Parameters
    ----------
    lines
        The lines of every key.
    firsts
        The first key of each mapping that sits in a list, so an entry is found by its first
        line.
    tuples
        Entry path text to path of keys, for names that hold a dot.
    """

    lines: KeyLines = field(default_factory=KeyLines)
    firsts: Mapping[Path, object] = field(default_factory=dict)
    tuples: Mapping[str, Path] = field(default_factory=dict)

    def line(self, path: Path) -> int | None:
        """Find the line of a path.

        Parameters
        ----------
        path
            Keys and list positions from the top.

        Returns
        -------
        int or None
            The line of the path's key, or of its first key for a list item; else the deepest
            key above it; `None` when the file has none of them.
        """
        first = self.firsts.get(path)
        return self.lines.line(path if first is None else (*path, first))


@dataclass(frozen=True)
class AccessFile:
    """A parsed access file.

    Parameters
    ----------
    file_name
        The base name the messages use.
    sha256
        Hash of the file's text.
    manages
        The scopes the file manages, sorted.
    entries
        Each entry with its path, such as ``grants[3]`` or ``groups.finance.members[1]``, in
        document order.
    locator
        Where each path sits in the text.
    """

    file_name: str
    sha256: str
    manages: tuple[str, ...]
    entries: tuple[tuple[str, Entry], ...]
    locator: _Locator = field(default_factory=_Locator, repr=False, compare=False)

    @property
    def state(self) -> AccessState:
        """Give the entries as a set.

        Returns
        -------
        AccessState
            Every entry of the file.
        """
        return AccessState(frozenset(entry for _, entry in self.entries))

    def path_of(self, entry: Entry) -> str | None:
        """Find where the file wrote an entry.

        Parameters
        ----------
        entry
            Any entry.

        Returns
        -------
        str or None
            Its path, or `None` when the file does not hold it.
        """
        return next((path for path, found in self.entries if found == entry), None)

    def line_of(self, path: str) -> int | None:
        """Find the line of a path.

        Parameters
        ----------
        path
            An entry's path, or a key below it such as ``grants[3].to``.

        Returns
        -------
        int or None
            The 1-based line; `None` when the path is unknown.
        """
        known = self.locator.tuples.get(path)
        if known is None:
            known = tuple(
                int(number) if number else word for number, word in _PATH_TOKEN.findall(path)
            )
        return self.locator.line(known)


class _Reader:
    """Reads the parsed YAML value and collects problems instead of stopping at the first.

    Parameters
    ----------
    file_name
        The base name the messages use.
    lines
        The lines of the file's keys.
    """

    def __init__(self, file_name: str, lines: KeyLines) -> None:
        self.file_name = file_name
        self.lines = lines
        self.firsts: dict[Path, object] = {}
        self.scopes = Scopes(frozenset())
        self.scopes_known = False
        self.problems: list[str] = []
        self.count = 0

    def locator(self, tuples: Mapping[str, Path]) -> _Locator:
        """Hand over what the reader learned about positions.

        Parameters
        ----------
        tuples
            Entry path text to path of keys.

        Returns
        -------
        _Locator
            For `AccessFile.line_of`.
        """
        return _Locator(self.lines, self.firsts, tuples)

    def problem(self, path: Path, message: str) -> None:
        """Record one problem.

        Parameters
        ----------
        path
            Where it is.
        message
            The cause and the next step.
        """
        self.count += 1
        if self.count > _MAX_PROBLEMS:
            return
        where = _path_text(path)
        line = self.locator({}).line(path)
        place = f"{where} (line {line})" if line and where else where
        self.problems.append(
            f"{self.file_name}: {place}: {message}" if place else f"{self.file_name}: {message}"
        )

    def done(self) -> list[str]:
        """Give the problems, with a count of those left out.

        Returns
        -------
        list of str
            At most `_MAX_PROBLEMS` problems and a closing line when there were more.
        """
        return cap_problems(self.file_name, self.problems, self.count)

    def mapping(
        self, value: object, path: Path, allowed: Sequence[str] | None, hint: str = ""
    ) -> Mapping[object, object] | None:
        """Check that a value is a mapping and has no unknown key.

        Parameters
        ----------
        value
            The loaded value.
        path
            Where it is.
        allowed
            The keys it may have; `None` for any.
        hint
            What to write instead, for the message.

        Returns
        -------
        Mapping or None
            The mapping, or `None` (after a problem) when the value is not one.
        """
        if value is _MISSING:
            return None
        if not isinstance(value, dict):
            self.problem(path, f"must be a mapping{hint}")
            return None
        if value and path:
            self.firsts[path] = next(iter(value))
        if allowed is not None:
            for key in value:
                if key not in allowed:
                    self.problem(
                        (*path, key), f"unknown key; the allowed keys are {', '.join(allowed)}"
                    )
        return value

    def sequence(self, value: object, path: Path, hint: str) -> list[object] | None:
        """Check that a value is a list.

        Parameters
        ----------
        value
            The loaded value.
        path
            Where it is.
        hint
            What to write instead.

        Returns
        -------
        list or None
            The list, or `None` after a problem.
        """
        if not isinstance(value, list):
            self.problem(path, f"must be a list; {hint}")
            return None
        return value

    def name(self, value: object, path: Path, what: str) -> str | None:
        """Check a name: text, one line, 1 to 255 characters, without edge blanks.

        Parameters
        ----------
        value
            The loaded value.
        path
            Where it is.
        what
            What the name is for, such as ``a group name``.

        Returns
        -------
        str or None
            The name, or `None` after a problem.
        """
        if value is _MISSING:
            return None
        if not isinstance(value, str):
            self.problem(path, f"{what} must be text, not {shown_key(value)}; put it in quotes")
        elif not 1 <= len(value) <= _MAX_NAME or value != value.strip() or not value.isprintable():
            self.problem(
                path,
                f"{what} {shown_key(value)!r} must be 1 to 255 printable characters without "
                "blanks at either end; rename it",
            )
        else:
            return value
        return None

    def free_text(self, value: object, path: Path, what: str) -> str | None:
        """Check a piece of text such as a SQL expression: text and not empty.

        Parameters
        ----------
        value
            The loaded value.
        path
            Where it is.
        what
            What it is, such as ``an expression``.

        Returns
        -------
        str or None
            The text, or `None` after a problem.
        """
        if isinstance(value, str) and value.strip():
            return value
        if value is _MISSING:
            return None
        self.problem(path, f"{what} must be text that is not empty; put it in quotes")
        return None

    def required(self, item: Mapping[object, object], key: str, path: Path, example: str) -> object:
        """Take a key that must be there.

        Parameters
        ----------
        item
            The mapping.
        key
            The key.
        path
            The mapping's path.
        example
            What to write.

        Returns
        -------
        object
            The value; a sentinel the checkers skip, after a problem, when the key is missing.
        """
        if key not in item:
            self.problem(path, f"missing key '{key}'; write {example}")
            return _MISSING
        return item[key]

    def subject(self, value: object, path: Path) -> Subject | None:
        """Read ``kind:name``.

        Parameters
        ----------
        value
            The loaded value.
        path
            Where it is.

        Returns
        -------
        Subject or None
            The subject, or `None` after a problem.
        """
        if value is _MISSING:
            return None
        if not isinstance(value, str):
            self.problem(path, f"must be text such as user:alice, not {shown_key(value)}")
            return None
        kind, colon, name = value.partition(":")
        shown = shown_key(value)
        if not colon:
            self.problem(
                path,
                f"'{shown}' names no kind; write user:{shown}, group:{shown} or service:{shown}",
            )
        elif kind not in SUBJECT_KINDS:
            self.problem(
                path,
                f"'{shown}' has the unknown kind '{shown_key(kind)}'; the kinds are "
                f"{', '.join(SUBJECT_KINDS)}",
            )
        elif checked := self.name(name, path, f"the name after {kind}:"):
            return Subject(kind, checked)
        return None

    def on(self, value: object, path: Path) -> Target | None:
        """Read ``instance``, an environment or ``environment.namespace``.

        Parameters
        ----------
        value
            The loaded value.
        path
            Where it is.

        Returns
        -------
        Target or None
            The target, or `None` after a problem.
        """
        text = self.name(value, path, "a target")
        if text is None:
            return None
        if text == INSTANCE:
            return Target()
        environment, _, namespace = text.partition(".")
        try:
            parts = NamespacePath.from_parts(namespace.split(".")).parts if "." in text else ()
            problem = "An environment name must not be empty." if not environment else ""
        except ValidationError as exc:
            parts, problem = (), str(exc)
        if problem:
            self.problem(
                path,
                f"'{shown_key(text)}' is not a target: {problem} Write instance, an "
                "environment or ENV.NAMESPACE",
            )
            return None
        return Target(environment, parts)

    def table(self, value: object, path: Path) -> Target | None:
        """Read ``environment.namespace.table``.

        Parameters
        ----------
        value
            The loaded value.
        path
            Where it is.

        Returns
        -------
        Target or None
            The table, or `None` after a problem.
        """
        text = self.name(value, path, "a table")
        if text is None:
            return None
        environment, _, dotted = text.partition(".")
        example = "for example prod.gold.orders"
        if not environment or "." not in dotted:
            self.problem(path, f"'{shown_key(text)}' is not ENV.NAMESPACE.TABLE; {example}")
            return None
        try:
            ref = TableRef.from_dotted(environment, dotted)
        except ValidationError as exc:
            self.problem(
                path,
                f"'{shown_key(text)}' is not a table: {exc} Write ENV.NAMESPACE.TABLE, {example}",
            )
            return None
        return Target(environment, ref.namespace.parts, ref.name)

    def owned(self, entry: Entry, path: Path) -> bool:
        """Check that an entry lies inside ``manages``.

        Parameters
        ----------
        entry
            The entry.
        path
            Where it is.

        Returns
        -------
        bool
            True when it is inside, or when ``manages`` itself is wrong (already reported).
        """
        if not self.scopes_known or self.scopes.owns(entry):
            return True
        where = environment_of(entry) or INSTANCE
        self.problem(
            path,
            f"{where} is outside manages ({', '.join(sorted(self.scopes.names))}); add {where} "
            "to manages or remove the entry",
        )
        return False


class _Collector:
    """Gathers the entries of one parse, refusing a repeat.

    Parameters
    ----------
    reader
        The reader that records problems.
    """

    def __init__(self, reader: _Reader) -> None:
        self.reader = reader
        self.pairs: list[tuple[str, Entry]] = []
        self.tuples: dict[str, Path] = {}
        self._seen: dict[object, str] = {}

    def add(self, path: Path, entry: Entry) -> None:
        """Keep an entry unless the file already holds it.

        Parameters
        ----------
        path
            Where the entry is.
        entry
            The entry.
        """
        text = _path_text(path)
        identity = entry.key if isinstance(entry, CiTrustEntry) else entry
        first = self._seen.setdefault(identity, text)
        if first != text:
            self.reader.problem(path, f"repeats {first}; remove one")
            return
        self.pairs.append((text, entry))
        self.tuples[text] = path


def _manages(reader: _Reader, document: Mapping[object, object]) -> None:
    """Read ``version`` and ``manages``.

    Parameters
    ----------
    reader
        The reader; its scopes are set when ``manages`` is right.
    document
        The top-level mapping.
    """
    version = document.get("version")
    if "version" not in document:
        reader.problem((), "version: missing; write version: 1")
    elif type(version) is not int or version != 1:
        reader.problem(
            ("version",), f"version {shown_key(version)} is not supported; write version: 1"
        )
    if "manages" not in document:
        reader.problem(
            (),
            "manages: missing; write manages: [instance] or a list of environments",
        )
        return
    items = reader.sequence(
        document["manages"], ("manages",), "write manages: [instance] or a list of environments"
    )
    if items is None:
        return
    names = [reader.name(item, ("manages", index), "a scope") for index, item in enumerate(items)]
    if None in names:
        return
    scopes = {name for name in names if name is not None}
    if not scopes:
        reader.problem(
            ("manages",), "is empty; write manages: [instance] or a list of environments"
        )
    elif len(scopes) < len(names):
        reader.problem(("manages",), "lists a scope twice; remove the repeat")
    elif INSTANCE in scopes and len(scopes) > 1:
        reader.problem(
            ("manages",),
            "instance already covers every environment; write manages: [instance] or list "
            "environments only",
        )
    else:
        reader.scopes = Scopes(frozenset(scopes))
        reader.scopes_known = True


def _instance_wide(reader: _Reader, section: str, what: str, step: str) -> bool:
    """Check that a section that exists instance-wide is allowed by ``manages``.

    Parameters
    ----------
    reader
        The reader.
    section
        The section's key.
    what
        The section's objects, for the message.
    step
        What to do instead.

    Returns
    -------
    bool
        True when ``instance`` is managed (or ``manages`` is wrong, already reported).
    """
    if not reader.scopes_known or reader.scopes.instance:
        return True
    reader.problem(
        (section,),
        f"{what} exist instance-wide; add instance to manages, or leave {section} out and {step}",
    )
    return False


def _groups(reader: _Reader, out: _Collector, value: object) -> None:
    """Read ``groups``.

    Parameters
    ----------
    reader
        The reader.
    out
        The collector.
    value
        The section's value.
    """
    if not _instance_wide(reader, "groups", "groups", "create groups with lakana group create"):
        return
    groups = reader.mapping(value, ("groups",), None, "; write finance: {members: [alice]}")
    for group, body in (groups or {}).items():
        path: Path = ("groups", group if isinstance(group, str) else str(group))
        name = reader.name(group, path, "a group name")
        members = reader.mapping(body, path, ("members",), "; write {members: [alice]}")
        if name is None or members is None:
            continue
        out.add(path, GroupEntry(name))
        listed = reader.sequence(
            members.get("members", []), (*path, "members"), "write [alice, bob]"
        )
        for index, member in enumerate(listed or []):
            checked = reader.name(member, (*path, "members", index), "a member")
            if checked is not None:
                out.add((*path, "members", index), MembershipEntry(name, checked))


def _service_accounts(reader: _Reader, out: _Collector, value: object) -> None:
    """Read ``service_accounts``.

    Parameters
    ----------
    reader
        The reader.
    out
        The collector.
    value
        The section's value.
    """
    if not _instance_wide(
        reader,
        "service_accounts",
        "service accounts",
        "create the account with lakana principal create",
    ):
        return
    for index, item in enumerate(
        reader.sequence(value, ("service_accounts",), "write [etl-bot]") or []
    ):
        name = reader.name(item, ("service_accounts", index), "a service account name")
        if name is not None:
            out.add(("service_accounts", index), ServiceAccountEntry(name))


def _grants(reader: _Reader, out: _Collector, value: object) -> None:
    """Read ``grants``.

    Parameters
    ----------
    reader
        The reader.
    out
        The collector.
    value
        The section's value.
    """
    roles = ", ".join(role.value for role in Role)
    actions = ", ".join(action.value for action in Action)
    for index, item in enumerate(
        reader.sequence(value, ("grants",), "write - {to: ..., ...}") or []
    ):
        path: Path = ("grants", index)
        before = reader.count
        body = reader.mapping(item, path, _GRANT_KEYS)
        if body is None:
            continue
        to = reader.subject(reader.required(body, "to", path, "to: group:NAME"), (*path, "to"))
        role = action = None
        if ("role" in body) == ("action" in body):
            reader.problem(path, "give exactly one of role or action")
        elif "role" in body:
            role = reader.name(body["role"], (*path, "role"), "a role")
            if role is not None and role not in {r.value for r in Role}:
                reader.problem(
                    (*path, "role"), f"unknown role '{shown_key(role)}'; the roles are {roles}"
                )
                role = None
        else:
            action = reader.name(body["action"], (*path, "action"), "an action")
            if action is not None and action not in {a.value for a in Action}:
                reader.problem(
                    (*path, "action"),
                    f"unknown action '{shown_key(action)}'; the actions are {actions}",
                )
                action = None
        target = None
        if ("on" in body) == ("table" in body):
            reader.problem(path, "give exactly one of on or table")
        elif "on" in body:
            target = reader.on(body["on"], (*path, "on"))
        else:
            target = reader.table(body["table"], (*path, "table"))
        if reader.count == before and to and target and (role or action):
            entry = GrantEntry(to, role, action, target)
            if reader.owned(entry, path):
                out.add(path, entry)


def _policies(reader: _Reader, out: _Collector, value: object) -> None:
    """Read ``policies``: row filters and column masks.

    Parameters
    ----------
    reader
        The reader.
    out
        The collector.
    value
        The section's value.
    """
    for index, item in enumerate(
        reader.sequence(value, ("policies",), "write - {kind: row_filter, ...}") or []
    ):
        path: Path = ("policies", index)
        before = reader.count
        kind = item.get("kind") if isinstance(item, dict) else None
        allowed = _ROW_FILTER_KEYS if kind == "row_filter" else _COLUMN_MASK_KEYS
        body = reader.mapping(item, path, allowed)
        if body is None:
            continue
        if kind not in ("row_filter", "column_mask"):
            if "kind" not in body:
                reader.problem(
                    path, "missing key 'kind'; write kind: row_filter or kind: column_mask"
                )
            else:
                reader.problem(
                    (*path, "kind"),
                    f"unknown kind '{shown_key(kind)}'; write row_filter or column_mask",
                )
            continue
        to = reader.subject(reader.required(body, "to", path, "to: group:NAME"), (*path, "to"))
        table = reader.table(
            reader.required(body, "table", path, "table: prod.gold.orders"), (*path, "table")
        )
        expression = reader.free_text(
            reader.required(body, "expression", path, "expression: ..."),
            (*path, "expression"),
            "an expression",
        )
        column = (
            reader.name(
                reader.required(body, "column", path, "column: email"),
                (*path, "column"),
                "a column",
            )
            if kind == "column_mask"
            else None
        )
        if reader.count != before or not (to and table and expression):
            continue
        if kind == "row_filter":
            entry: Entry = RowFilterEntry(to, table, expression)
        elif column is not None:
            entry = ColumnMaskEntry(to, table, column, expression)
        else:  # pragma: no cover -- a missing column was reported above
            continue
        if reader.owned(entry, path):
            out.add(path, entry)


def _conditions(reader: _Reader, value: object, path: Path) -> tuple[tuple[str, str], ...] | None:
    """Read a CI rule's claim conditions.

    Parameters
    ----------
    reader
        The reader.
    value
        The loaded value.
    path
        Where it is.

    Returns
    -------
    tuple of tuple of str, or None
        Claim and value pairs, sorted; `None` after a problem.
    """
    body = reader.mapping(value, path, None, "; write {ref: refs/heads/main}")
    if body is None:
        return None
    if not body:
        reader.problem(path, "needs at least one condition, for example ref: refs/heads/main")
        return None
    pairs = []
    for claim, required in body.items():
        key = reader.name(claim, path, "a claim name")
        text = reader.name(required, (*path, claim), "a claim value")
        if key is not None and text is not None:
            pairs.append((key, text))
    return tuple(sorted(pairs)) if len(pairs) == len(body) else None


def _ci_kinds(reader: _Reader, body: Mapping[object, object], path: Path) -> tuple[str, ...]:
    """Read a CI rule's kinds.

    Parameters
    ----------
    reader
        The reader.
    body
        The rule's mapping.
    path
        The rule's path.

    Returns
    -------
    tuple of str
        The kinds, sorted without repeats; ``deploy`` when the rule names none.
    """
    if "kinds" not in body:
        return ("deploy",)
    items = reader.sequence(body["kinds"], (*path, "kinds"), "write [deploy] or [deploy, dry_run]")
    kinds: set[str] = set()
    for index, kind in enumerate(items or []):
        if isinstance(kind, str) and kind in _CI_KINDS:
            kinds.add(kind)
        else:
            reader.problem(
                (*path, "kinds", index),
                f"unknown kind '{shown_key(kind)}'; the kinds are {', '.join(_CI_KINDS)}",
            )
    if items is not None and not items:
        reader.problem((*path, "kinds"), "needs at least one kind: deploy, dry_run or both")
    return tuple(sorted(kinds))


def _flag(
    reader: _Reader, body: Mapping[object, object], key: str, path: Path, default: bool
) -> bool:
    """Read a true-or-false setting.

    Parameters
    ----------
    reader
        The reader.
    body
        The rule's mapping.
    key
        The setting.
    path
        The rule's path.
    default
        The value when the file leaves it out.

    Returns
    -------
    bool
        The setting.
    """
    value = body.get(key, default)
    if not isinstance(value, bool):
        reader.problem((*path, key), "must be true or false")
        return default
    return value


def _ci_trust(reader: _Reader, out: _Collector, value: object) -> None:
    """Read ``ci_trust``.

    Parameters
    ----------
    reader
        The reader.
    out
        The collector.
    value
        The section's value.
    """
    for index, item in enumerate(
        reader.sequence(value, ("ci_trust",), "write - {issuer: ..., ...}") or []
    ):
        path: Path = ("ci_trust", index)
        before = reader.count
        body = reader.mapping(item, path, _CI_KEYS)
        if body is None:
            continue
        issuer = reader.name(
            reader.required(body, "issuer", path, "issuer: github"), (*path, "issuer"), "an issuer"
        )
        repository = reader.name(
            reader.required(body, "repository", path, "repository: org/repo"),
            (*path, "repository"),
            "a repository",
        )
        acts_as = body.get("as")
        account = None
        if "as" not in body:
            reader.problem(path, "missing key 'as'; write as: service:NAME")
        elif not isinstance(acts_as, str) or not acts_as.startswith("service:"):
            reader.problem((*path, "as"), "a CI job acts as a service account; write service:NAME")
        else:
            account = reader.name(
                acts_as.removeprefix("service:"), (*path, "as"), "an account name"
            )
        environment = reader.name(
            reader.required(body, "on", path, "on: prod"), (*path, "on"), "an environment"
        )
        if environment is not None and (environment == INSTANCE or "." in environment):
            reader.problem((*path, "on"), "a CI trust rule names one environment, such as prod")
            environment = None
        conditions = _conditions(
            reader,
            reader.required(body, "conditions", path, "conditions: {ref: refs/heads/main}"),
            (*path, "conditions"),
        )
        kinds = _ci_kinds(reader, body, path)
        enabled = _flag(reader, body, "enabled", path, True)
        starts = _flag(reader, body, "start_on_deploy_jobs", path, False)
        if reader.count != before or not (
            issuer and repository and account and environment and conditions
        ):
            continue
        entry = CiTrustEntry(
            issuer, repository, account, environment, conditions, kinds, enabled, starts
        )
        if reader.owned(entry, path):
            out.add(path, entry)


_YAML_STEPS = (
    ("duplicate key", "remove the repeat"),
    ("anchors and aliases", "write the entries out instead"),
    ("merge keys", "write the entries out instead"),
    ("nesting", "flatten the file"),
    ("more than", "split access across fewer, broader grants"),
    ("determine a constructor", "remove the tag"),
    ("remove it", ""),
)


def _yaml_problem(file_name: str, exc: YamlError) -> str:
    """Say a YAML-level refusal in the shape of every other problem.

    Parameters
    ----------
    file_name
        The base name the messages use.
    exc
        The loader's refusal.

    Returns
    -------
    str
        The file, the line when known, the cause and the next step.
    """
    cause = str(exc).removeprefix(file_name).lstrip(": ").removesuffix(".")
    step = next((step for part, step in _YAML_STEPS if part in cause), "fix the YAML at that line")
    place = f"{file_name} (line {exc.line})" if exc.line else file_name
    return f"{place}: {cause}; {step}" if step else f"{place}: {cause}"


def parse_access_file(text: str, *, file_name: str) -> AccessFile:
    """Parse and check one access file, collecting every problem before refusing.

    Parameters
    ----------
    text
        The file's content.
    file_name
        Its base name, for the messages (`validate_file_name` has checked it).

    Returns
    -------
    AccessFile
        The entries, each with its path.

    Raises
    ------
    AccessFileError
        With every problem: YAML (size, syntax, anchors, aliases, merge keys, duplicate keys),
        unknown keys, wrong types, unknown roles, actions or kinds, a target outside `manages`,
        instance-wide sections without ``instance``, repeated entries, more than `MAX_ENTRIES`.
    """
    try:
        encoded = text.encode()
    except UnicodeEncodeError as exc:
        raise AccessFileError(
            [
                f"{file_name}: the file holds a character that is not valid UTF-8 at position "
                f"{exc.start}; remove it"
            ]
        ) from exc
    if len(encoded) > MAX_FILE_BYTES:
        raise AccessFileError(
            [
                f"{file_name}: the file is larger than {MAX_FILE_BYTES} bytes; use a group "
                "instead of many people, or remove entries that no longer apply"
            ]
        )
    try:
        raw, lines = load_yaml_with_lines(
            text,
            what=file_name,
            max_bytes=MAX_FILE_BYTES,
            max_entries=_PARSE_LIMIT,
            yaml12_booleans=True,
        )
    except YamlError as exc:
        raise AccessFileError([_yaml_problem(file_name, exc)]) from exc
    reader = _Reader(file_name, lines)
    if raw is None:
        raise AccessFileError(
            [f"{file_name}: the file is empty; start with version: 1 and manages: [instance]"]
        )
    document = reader.mapping(raw, (), _TOP_KEYS, "; start with version: 1 and manages: [instance]")
    out = _Collector(reader)
    if document is not None:
        _manages(reader, document)
        sections = (
            ("groups", _groups),
            ("service_accounts", _service_accounts),
            ("grants", _grants),
            ("policies", _policies),
            ("ci_trust", _ci_trust),
        )
        for key, read in sections:
            if key in document:
                read(reader, out, document[key])
    if reader.count:
        raise AccessFileError(reader.done())
    if len(out.pairs) > MAX_ENTRIES:
        raise AccessFileError(
            [
                f"{file_name}: the file has {len(out.pairs)} entries, more than {MAX_ENTRIES}; "
                "split access across fewer, broader grants (a group instead of many people)"
            ]
        )
    return AccessFile(
        file_name=file_name,
        sha256=hashlib.sha256(encoded).hexdigest(),
        manages=tuple(sorted(reader.scopes.names)),
        entries=tuple(out.pairs),
        locator=reader.locator(out.tuples),
    )
