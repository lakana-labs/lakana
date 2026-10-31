"""YAML loading for repository content: SafeLoader semantics, no anchors, no aliases.

Refusing anchors and aliases makes alias expansion ("billion laughs")
impossible, so the byte limit checked before parsing bounds memory. Duplicate
keys and ``<<`` merge keys are refused too: PyYAML would otherwise keep the
last of two equal keys and splice merged keys in silently, so what a reviewer
reads would not be what the platform applies.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import yaml
import yaml.reader
from yaml.events import AliasEvent, CollectionStartEvent, ScalarEvent
from yaml.nodes import MappingNode, Node, SequenceNode

from lakana_core.errors import ValidationError


class YamlError(ValidationError):
    """A YAML document that is malformed, too large or uses anchors/aliases.

    Parameters
    ----------
    message
        What is wrong.
    line
        1-based line within the document, if known.
    """

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(message)


class YamlTooManyEntries(YamlError):
    """A document with more keys and list items than its caller accepts, refused while parsing.

    Parameters
    ----------
    message
        What is wrong.
    line
        1-based line of the first entry past the limit.
    """


_MAX_DEPTH = 64
"""Deepest nesting accepted; stops recursion far below Python's limit."""

_MAX_KEY_CHARS = 80
"""The most characters of a key a message repeats; a longer key is cut and ends in ``…``."""


def shown_key(key: object) -> str:
    """Render a key or value from a document for a message: one line, at most 80 characters.

    Parameters
    ----------
    key
        A key, an index or a value from the document.

    Returns
    -------
    str
        Its text with each non-printable character ASCII-escaped, so that it cannot start a
        second line while the other letters stay readable, and cut to `_MAX_KEY_CHARS` with a
        trailing ``…``, never inside an escape.
    """
    # jscpd:ignore-start
    # The SDK may not import core, so this cut is copied in lakana/assets.py `_loc`; the cases of
    # test_a_bad_text_gets_the_same_words_from_deploy_and_the_sdk
    # (packages/lakana-worker/tests/test_discovery_harness_parity.py) pin the two.
    pieces: list[str] = []
    length = 0
    for char in str(key):
        piece = char if char.isprintable() else ascii(char)[1:-1]
        pieces.append(piece)
        length += len(piece)
        if length > _MAX_KEY_CHARS:  # cut: drop whole pieces until the ellipsis fits
            while length > _MAX_KEY_CHARS - 1:
                length -= len(pieces.pop())
            return "".join(pieces) + "…"
    return "".join(pieces)
    # jscpd:ignore-end


class _AliasRefused(yaml.MarkedYAMLError):
    """Raised by `_NoAliasLoader` at the first anchor or alias."""


class _TooDeep(yaml.MarkedYAMLError):
    """Raised by `_NoAliasLoader` when nesting exceeds `_MAX_DEPTH`."""


class _AmbiguousMapping(yaml.MarkedYAMLError):
    """Raised by `_NoAliasLoader` at a duplicate key or a merge key."""


class _TooMany(yaml.MarkedYAMLError):
    """Raised by `_NoAliasLoader` at the first key or list item past `max_entries`."""


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _NoAliasLoader(yaml.SafeLoader):
    """A `SafeLoader` that refuses anchors, aliases, merge keys, duplicate keys and deep nesting.

    With `max_entries` set it also counts every mapping key and list item as it composes them and
    stops at the first one past the limit, before any Python value is built.
    """

    _depth = 0
    _entries = 0
    max_entries: int | None = None

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[object, object]:
        """Refuse a merge key or a repeated key, then build the mapping.

        Parameters
        ----------
        node
            The mapping node.
        deep
            As PyYAML passes it.

        Returns
        -------
        dict
            The constructed mapping.

        Raises
        ------
        _AmbiguousMapping
            At the first merge key or the second occurrence of a key.
        """
        seen: set[object] = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                raise _AmbiguousMapping(
                    problem="merge keys (<<) are not allowed", problem_mark=key_node.start_mark
                )
            try:
                key = self.construct_object(key_node, deep=True)
                if key in seen:
                    shown = f"'{shown_key(key)}'" if isinstance(key, str) else shown_key(repr(key))
                    raise _AmbiguousMapping(
                        problem=f"duplicate key {shown}",
                        problem_mark=key_node.start_mark,
                    )
                seen.add(key)
            except TypeError:  # unhashable key: PyYAML's own refusal below names it
                break
        return super().construct_mapping(node, deep=deep)

    def compose_node(self, parent: Node | None, index: object) -> Node | None:
        """Compose one node, refusing it if it carries an anchor or is an alias.

        Parameters
        ----------
        parent
            The parent node, as PyYAML passes it.
        index
            The key or position within the parent, as PyYAML passes it.

        Returns
        -------
        yaml.nodes.Node or None
            The composed node.

        Raises
        ------
        _AliasRefused
            At the first anchor or alias.
        _TooDeep
            When nesting exceeds `_MAX_DEPTH`.
        _TooMany
            At the first key or list item past `max_entries`.
        """
        event = self.peek_event()
        anchored = isinstance(event, ScalarEvent | CollectionStartEvent) and event.anchor
        if isinstance(event, AliasEvent) or anchored:
            raise _AliasRefused(
                problem="anchors and aliases are not allowed", problem_mark=event.start_mark
            )
        if self._depth >= _MAX_DEPTH:
            raise _TooDeep(problem="nesting is too deep", problem_mark=event.start_mark)
        # A key is composed with index None and a list item with its position; a value gets its
        # key's node, so it is not counted twice. The count equals keys plus list items.
        if self.max_entries is not None and parent is not None and not isinstance(index, Node):
            self._entries += 1
            if self._entries > self.max_entries:
                raise _TooMany(problem="too many entries", problem_mark=event.start_mark)
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _reader_line(text: str, exc: yaml.reader.ReaderError) -> int | None:
    """Find the line of the character PyYAML's reader refused.

    Parameters
    ----------
    text
        The document.
    exc
        The reader's error.

    Returns
    -------
    int or None
        The 1-based line, or `None` when the error has no position.
    """
    position = getattr(exc, "position", None)
    return text.count("\n", 0, position) + 1 if isinstance(position, int) else None


def _load(
    text: str, *, what: str, max_bytes: int, max_entries: int | None = None
) -> tuple[object, Node | None]:
    """Parse one YAML document from untrusted repository content, keeping its node tree.

    Parameters
    ----------
    text
        The document.
    what
        Names the document in error messages, e.g. ``"lakana.yaml"``.
    max_bytes
        Largest accepted UTF-8 size.
    max_entries
        The most keys and list items accepted, counted while composing; `None` for no limit.

    Returns
    -------
    tuple of (object, Node or None)
        The loaded value (`None` for an empty document) and the composed node it was built from.

    Raises
    ------
    YamlError
        As `load_yaml` documents.
    YamlTooManyEntries
        At the first key or list item past `max_entries`.
    """
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise YamlError(f"{what} is larger than {max_bytes // 1024} KiB.")
    try:
        # Built inside the try: the reader refuses a control character in its constructor.
        loader = _NoAliasLoader(text)  # nosec B506 -- a SafeLoader subclass
    except yaml.reader.ReaderError as exc:
        raise YamlError(
            f"{what} is not valid YAML: it holds the character U+{exc.character:04X}, which "
            "YAML does not allow; remove it",
            _reader_line(text, exc),
        ) from exc
    loader.max_entries = max_entries
    try:
        node = loader.get_single_node()
        return (None if node is None else loader.construct_document(node)), node
    except _AliasRefused as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(f"{what}: anchors and aliases are not allowed.", line) from exc
    except _AmbiguousMapping as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(f"{what}: {exc.problem}.", line) from exc
    except _TooDeep as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(f"{what}: nesting is deeper than {_MAX_DEPTH} levels.", line) from exc
    except _TooMany as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlTooManyEntries(
            f"{what} has more than {max_entries} keys and list items.", line
        ) from exc
    except yaml.MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError(f"{what} is not valid YAML: {exc.problem}", line) from exc
    except (ValueError, TypeError, OverflowError) as exc:
        raise YamlError(f"{what} has an invalid value: {exc}") from exc
    except yaml.YAMLError as exc:  # pragma: no cover -- every PyYAML parse error is marked
        raise YamlError(f"{what} is not valid YAML.") from exc
    finally:
        loader.dispose()


@dataclass(frozen=True)
class KeyLines:
    """The line of every key of one document, from the parse that loaded it.

    `key_line` composes the document again on each call; a caller with many issues for one
    document uses this instead, so reporting them costs one parse, not one per issue.

    Parameters
    ----------
    lines
        Path of keys from the top → the 1-based line of its last key. A key inside a list is
        reached through the item's index.
    top
        Each top-level key as written in the document, with its line, in document order; the
        loaded mapping has its keys in the same order.
    """

    lines: Mapping[tuple[object, ...], int] = field(default_factory=dict)
    top: tuple[tuple[str, int], ...] = ()

    def line(self, path: Sequence[object]) -> int | None:
        """Find a key's line as `key_line` does: the deepest key of `path` that exists.

        Parameters
        ----------
        path
            The keys from the top.

        Returns
        -------
        int or None
            The 1-based line; `None` if not even the first key exists.
        """
        for end in range(len(path), 0, -1):
            found = self.lines.get(tuple(path[:end]))
            if found is not None:
                return found
        return None


def _key_lines(root: Node | None) -> KeyLines:
    """Index the line of every key of a composed document.

    Parameters
    ----------
    root
        The document's node, or `None` for an empty one.

    Returns
    -------
    KeyLines
        Every key path and its line; a key's first occurrence wins.
    """
    lines: dict[tuple[object, ...], int] = {}
    top = tuple(
        (str(key_node.value), key_node.start_mark.line + 1)
        for key_node, _ in (root.value if isinstance(root, MappingNode) else ())
    )
    stack: list[tuple[tuple[object, ...], Node | None]] = [((), root)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, MappingNode):
            # Every key is a scalar here: the load refused a list or mapping key as unhashable.
            for key_node, value_node in node.value:
                here = (*path, key_node.value)
                lines.setdefault(here, key_node.start_mark.line + 1)
                stack.append((here, value_node))
        elif isinstance(node, SequenceNode):
            stack.extend(((*path, index), item) for index, item in enumerate(node.value))
    return KeyLines(lines, top)


def load_yaml_with_lines(
    text: str, *, what: str, max_bytes: int, max_entries: int | None = None
) -> tuple[object, KeyLines]:
    """Parse one YAML document once, returning its value and the line of every key.

    Parameters
    ----------
    text
        The document.
    what
        Names the document in error messages.
    max_bytes
        Largest accepted UTF-8 size.
    max_entries
        The most keys and list items accepted, counted while composing; `None` for no limit.

    Returns
    -------
    tuple of (object, KeyLines)
        The loaded value (`None` for an empty document) and its keys' lines.

    Raises
    ------
    YamlError
        As `load_yaml` documents.
    YamlTooManyEntries
        At the first key or list item past `max_entries`, before any value is built.
    """
    value, node = _load(text, what=what, max_bytes=max_bytes, max_entries=max_entries)
    return value, _key_lines(node)


def load_yaml(text: str, *, what: str, max_bytes: int) -> object:
    """Parse one YAML document from untrusted repository content.

    Parameters
    ----------
    text
        The document.
    what
        Names the document in error messages, e.g. ``"lakana.yaml"``.
    max_bytes
        Largest accepted UTF-8 size.

    Returns
    -------
    object
        The loaded value; `None` for an empty document.

    Raises
    ------
    YamlError
        If the document is too large, malformed, nested too deeply, has a
        value PyYAML cannot construct, uses an anchor, alias or merge key, or
        repeats a key.
    """
    return _load(text, what=what, max_bytes=max_bytes)[0]


def _find_key(node: Node | None, name: object) -> tuple[Node, Node] | None:
    """Find a key in a mapping node.

    Parameters
    ----------
    node
        Any node of a composed document, or `None` for an empty one.
    name
        The key to look for.

    Returns
    -------
    tuple of Node or None
        The key node and its value node; `None` if `node` is not a mapping or has no such key.
    """
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if getattr(key_node, "value", None) == name:
            return key_node, value_node
    return None


def key_line(text: str, path: Sequence[object]) -> int | None:
    """Find the line of a key inside a document, to point an issue at it.

    Parameters
    ----------
    text
        The document, already accepted by `load_yaml`.
    path
        The keys from the top: ``("jobs", "daily", "schedule", "cron")``. A part that is not a
        key of the mapping at that point ends the walk.

    Returns
    -------
    int or None
        The 1-based line of the last key reached; `None` if not even the first key exists or the
        document cannot be read.
    """
    try:
        node = yaml.compose(text, Loader=_NoAliasLoader)
    except yaml.YAMLError:
        return None
    line: int | None = None
    for part in path:
        found = _find_key(node, part)
        if found is None:
            break
        key_node, node = found
        line = key_node.start_mark.line + 1
    return line
