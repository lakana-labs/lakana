"""Value objects identifying namespaces and tables.

Namespaces are multi-level and are stored as a single string joined by the
ASCII unit separator, which is also how the Iceberg REST specification encodes
them in URLs. The display form is dotted; `TableRef.from_dotted` parses the
user-facing dotted form SQL and the SDK use, which is ambiguous for a
component that itself contains a dot.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

from lakana_core.errors import ValidationError

NAMESPACE_SEPARATOR: Final[str] = "\x1f"
"""ASCII unit separator, matching the Iceberg REST namespace encoding."""


_SECRET_NAME = re.compile(
    r"\A[^\x00-\x20\x7f-\x9f:\ud800-\udfff\u061c\u200e\u200f\u2028\u2029\u202a-\u202e"
    r"\u2066-\u2069]{1,255}\Z"
)
"""1 to 255 characters: no space, control, ``:``, lone surrogate (PostgreSQL cannot store one)
or bidirectional control (it would reorder the name where it is shown)."""

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
        `True` for text of 1 to 255 characters with no space, no control character, no ``:``,
        no lone surrogate and no bidirectional control character.
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
            "so two names would share one directory. A new namespace, table or view name may hold "
            "only letters, decimal digits, '_', '-' and the plain space."
        )


HIDDEN_CHARACTER_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
"""Unicode categories a new name may not hold: control, format and surrogate characters and the
line and paragraph separators. Each is invisible or breaks a line, so a name holding one can look
like another. A repository file's identifiers (`projects.project_config.unsafe_identifier`)
refuse the same ones."""


def hidden_character(name: str) -> tuple[int, str] | None:
    """Find the first character of `HIDDEN_CHARACTER_CATEGORIES` in a name.

    Parameters
    ----------
    name
        The name.

    Returns
    -------
    tuple of (int, str) or None
        The character's 1-based position and the character; `None` when there is none.
    """
    for position, character in enumerate(name, start=1):
        if unicodedata.category(character) in HIDDEN_CHARACTER_CATEGORIES:
            return position, character
    return None


def refuse_hidden_characters(name: str, *, what: str) -> None:
    """Refuse a new name that holds an invisible or line-breaking character.

    Only the places that create a name call this: a namespace, table, view or column name
    (through `check_new_name` for namespaces and tables, and the column checks of
    `catalog.metadata` and `CatalogService.create_or_replace_view`), an artifact name, and a new
    repository, package index, group, secret or token name. Names stored by an older release
    are never judged: they are loaded, matched, renamed away and deleted as before.

    Parameters
    ----------
    name
        The new name.
    what
        Noun describing it, such as ``"Table name"`` or ``"Column name"``.

    Raises
    ------
    ValidationError
        If `hidden_character` finds one; the message names it as ``U+XXXX`` with its
        position and says what to do.
    """
    found = hidden_character(name)
    if found is None:
        return
    position, character = found
    raise ValidationError(
        f"{what} '{shown(name)}' holds U+{ord(character):04X} at character {position}, a "
        "control, format or line-separator character that is invisible or breaks the line, so "
        "the name could look like another one. Choose a name without it: retype the name with "
        "visible characters only, such as letters, digits and '_'."
    )


def refuse_outer_whitespace(name: str, *, what: str) -> None:
    """Refuse a new name that starts or ends with whitespace.

    ``" gold"`` and ``"orders "`` look like ``gold`` and ``orders`` in every list and are
    almost always a typing mistake. The same places that call `refuse_hidden_characters`
    call this, so a stored name is never judged: it is loaded, renamed and dropped as before.
    A space inside a name stays allowed.

    Parameters
    ----------
    name
        The new name.
    what
        Noun describing it, such as ``"Table name"`` or ``"Column name"``.

    Raises
    ------
    ValidationError
        If the name starts or ends with a character `str.isspace` accepts; the message says
        where and names the name to use instead, or, for a name of only spaces, asks for
        visible characters.
    """
    starts = name[:1].isspace()
    ends = name[-1:].isspace()
    if not (starts or ends):
        return
    if not name.strip():
        # A namespace or table name of only spaces is refused earlier, by
        # `validate_path_component`; a column name is not, and has no name to suggest.
        raise ValidationError(
            f"{what} '{shown(name)}' is only spaces, so it would look empty in every list. "
            "Name it with visible characters, such as letters, digits and '_'."
        )
    where = "starts and ends" if starts and ends else "starts" if starts else "ends"
    raise ValidationError(
        f"{what} '{shown(name)}' {where} with a space, so it would look like "
        f"'{shown(name.strip())}' in every list but be a different name. Use "
        f"'{shown(name.strip())}' instead, or another name without a space at either end."
    )


NAME_SYMBOLS: Final[frozenset[str]] = frozenset({"_", "-", " "})
"""The only characters other than letters, decimal digits and combining marks a new namespace,
table or view name may hold (`refuse_characters_outside_name_allowlist`): ``_``, ``-``
(U+002D) and the plain space U+0020, which a name may hold inside it but not at either end
(`refuse_outer_whitespace`). A column name is not held to this list."""

_DRAWN_AS_NOTHING: Final[tuple[tuple[int, int], ...]] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x16FE4, 0x16FE4),
    (0x1BCA0, 0x1BCA3),
    (0x1D159, 0x1D159),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
"""Unicode's default-ignorable code points (`DerivedCoreProperties.txt`, Unicode 16), which a
font draws as nothing whatever their category: the combining grapheme joiner U+034F, the
variation selectors, the Mongolian free variation selectors, the Hangul fillers U+115F, U+1160,
U+3164 and U+FFA0 (letters, ``Lo``) and the format characters. Added to them, three characters
designed as a blank glyph that are not default-ignorable: U+2800 BRAILLE PATTERN BLANK and
U+1D159 MUSICAL SYMBOL NULL NOTEHEAD (symbols, ``So``) and U+16FE4 KHITAN SMALL SCRIPT FILLER
(a mark, ``Mn``). NFC keeps every one of them."""

MAX_COMBINING_MARKS: Final[int] = 4
"""The most combining marks a letter of a new name may carry (UTS #39, section 5.4, suggests
limiting a run of them); five or more overprint in most fonts."""

DOT_LIKE_DIGITS: Final[frozenset[str]] = frozenset({"\u0660", "\u06f0"})
"""The decimal digits Unicode's `confusables.txt` (version of 2026-08-06) maps to a
punctuation mark: U+0660 ARABIC-INDIC DIGIT ZERO and U+06F0 EXTENDED ARABIC-INDIC DIGIT ZERO,
both confusable with ``.`` FULL STOP. They are drawn as a dot on the baseline, so
``sales\u0660daily`` reads as the dotted path ``sales.daily``. No other ``Nd`` has a skeleton
that holds anything but letters, marks and digits. A new namespace, table or view name may not
hold them (`character_outside_name_allowlist`); a column name may."""


class LookAlike(Enum):
    """What `look_alike_character` found; each has its own sentence in a refusal."""

    DRAWN_AS_NOTHING = "drawn as nothing"
    OTHER_SPACE = "a space other than the plain space"
    LOOSE_MARK = "a combining mark with no letter before it"
    REPEATED_MARK = "the same combining mark twice in a row"
    TOO_MANY_MARKS = "too many combining marks on one letter"
    UNASSIGNED = "an unassigned code point"
    PRIVATE_USE = "a private-use character"


_LOOK_ALIKE_SENTENCES: Final[dict[LookAlike, str]] = {
    LookAlike.DRAWN_AS_NOTHING: (
        "a character drawn as nothing, so the name could look like another one or look empty. "
        "Remove it."
    ),
    LookAlike.OTHER_SPACE: (
        "a space other than the plain space, so the name looks like one with a plain space but "
        "is a different name. Type a plain space (U+0020) instead."
    ),
    LookAlike.LOOSE_MARK: (
        "a combining mark with no letter before it, so it is drawn on the character before it "
        "or on nothing. Put the mark right after a letter, or remove it."
    ),
    LookAlike.REPEATED_MARK: (
        "the same combining mark a second time in a row, so it is drawn over the first and the "
        "name looks like one with a single mark. Remove the repeated mark."
    ),
    LookAlike.TOO_MANY_MARKS: (
        f"a combining mark beyond the {MAX_COMBINING_MARKS} one letter may carry, so the marks "
        "overprint and the name could look like another one. Use fewer marks on the letter."
    ),
    LookAlike.UNASSIGNED: (
        "a code point Unicode has not assigned (or a noncharacter), which has no agreed glyph and "
        "may be drawn as nothing or as a box. Remove it."
    ),
    LookAlike.PRIVATE_USE: (
        "a private-use character, which each font draws differently or as nothing. Remove it."
    ),
}


def _drawn_as_nothing(character: str) -> bool:
    """Say whether a character is one of `_DRAWN_AS_NOTHING`.

    Parameters
    ----------
    character
        One character.

    Returns
    -------
    bool
        `True` when a font draws it as nothing.
    """
    code = ord(character)
    return any(low <= code <= high for low, high in _DRAWN_AS_NOTHING)


def _blank_kind(character: str, category: str) -> LookAlike | None:
    """Say whether a character is blank or has no agreed glyph, whatever comes around it.

    Parameters
    ----------
    character
        One character.
    category
        Its Unicode general category.

    Returns
    -------
    LookAlike or None
        `DRAWN_AS_NOTHING`, `OTHER_SPACE`, `UNASSIGNED` (``Cn``, which holds the
        noncharacters) or `PRIVATE_USE` (``Co``); `None` for any other character.
    """
    if _drawn_as_nothing(character):
        return LookAlike.DRAWN_AS_NOTHING
    if category == "Zs" and character != " ":
        return LookAlike.OTHER_SPACE
    if category == "Cn":
        return LookAlike.UNASSIGNED
    if category == "Co":
        return LookAlike.PRIVATE_USE
    return None


def look_alike_character(name: str) -> tuple[int, str, LookAlike] | None:
    """Find the first character that is drawn as nothing or passes for another one.

    Refused: a character `_DRAWN_AS_NOTHING`, whatever its category (the Hangul fillers are
    letters, the variation selectors marks); every space but U+0020; an unassigned code point
    or noncharacter (``Cn``) and a private-use character (``Co``); a combining mark (``M*``)
    that does not follow a letter or another such mark; the same nonspacing mark twice in a
    row; and more than `MAX_COMBINING_MARKS` marks on one letter. NFC composes what it can
    (``e`` and U+0301 become ``é``), and the marks it leaves belong to letters of scripts such
    as Devanagari or Arabic, so a mark on a letter is allowed. Every other character, a visible
    symbol such as ``(`` or ``$`` included, passes.

    Parameters
    ----------
    name
        The name.

    Returns
    -------
    tuple of (int, str, LookAlike) or None
        The character's 1-based position, the character and what it is; `None` when there is
        none.
    """
    after_letter = False
    # The marks the current letter carries so far, counting those composed into it by NFC
    # (``é`` carries U+0301), so ``é`` + U+0301 is a repeated mark like ``e`` + U+0301 + U+0301.
    marks: list[str] = []
    for position, character in enumerate(name, start=1):
        category = unicodedata.category(character)
        problem = _blank_kind(character, category)
        if problem is None and category.startswith("M"):
            if not after_letter:
                problem = LookAlike.LOOSE_MARK
            elif category == "Mn" and marks and marks[-1] == character:
                problem = LookAlike.REPEATED_MARK
            elif len(marks) >= MAX_COMBINING_MARKS:
                problem = LookAlike.TOO_MANY_MARKS
            marks.append(character)
        else:
            marks = [
                part
                for part in unicodedata.normalize("NFD", character)
                if unicodedata.category(part).startswith("M")
            ]
        if problem is not None:
            return position, character, problem
        after_letter = category.startswith(("L", "M"))
    return None


def refuse_look_alike_characters(name: str, *, what: str) -> None:
    """Refuse a new namespace, table, view or column name that could look like another (#609).

    The name may hold no character `look_alike_character` finds, and must be in Unicode NFC,
    since an NFD spelling looks the same but compares unequal. `check_new_name` and
    `check_new_column_name` call this last, so the earlier checks keep their own messages and
    the NFC spelling it suggests holds no character they refuse. A stored name is never judged.

    Parameters
    ----------
    name
        The new name.
    what
        Noun describing it, such as ``"Table name"`` or ``"Column name"``.

    Raises
    ------
    ValidationError
        If `look_alike_character` finds a character (the message names it as ``U+XXXX`` with
        its position, and has a sentence for each `LookAlike`) or, failing that, the name is not
        in NFC (the message names its NFC form).
    """
    found = look_alike_character(name)
    if found is not None:
        position, character, problem = found
        raise ValidationError(
            f"{what} '{shown(name)}' holds U+{ord(character):04X} at character {position}, "
            f"{_LOOK_ALIKE_SENTENCES[problem]}"
        )
    # Checked second, so the composed spelling it suggests holds only allowed characters.
    if not unicodedata.is_normalized("NFC", name):
        composed = unicodedata.normalize("NFC", name)
        raise ValidationError(
            f"{what} '{shown(name)}' is not in Unicode NFC, the composed spelling, so it looks "
            f"like '{shown(composed)}' but is a different name. Use '{shown(composed)}' instead."
        )


def character_outside_name_allowlist(name: str) -> tuple[int, str] | None:
    """Find the first character a new namespace, table or view name may not hold.

    Such a name may hold letters (``L*``), combining marks (``M*``), decimal digits (``Nd``)
    but `DOT_LIKE_DIGITS`, and `NAME_SYMBOLS`. A character `look_alike_character` judges
    whatever comes around it (drawn as nothing, a space, unassigned, private use) is left to
    it, which names it more exactly.

    Parameters
    ----------
    name
        The name.

    Returns
    -------
    tuple of (int, str) or None
        The character's 1-based position and the character; `None` when there is none.
    """
    for position, character in enumerate(name, start=1):
        category = unicodedata.category(character)
        if character in DOT_LIKE_DIGITS:
            return position, character
        if (
            category.startswith(("L", "M"))
            or category == "Nd"
            or character in NAME_SYMBOLS
            or _blank_kind(character, category) is not None
        ):
            continue
        return position, character
    return None


def refuse_characters_outside_name_allowlist(name: str, *, what: str) -> None:
    """Refuse a new namespace, table or view name with a symbol or punctuation mark (#609).

    Only `check_new_name` calls this; a column name may hold visible symbols, such as the
    ``count(*)`` of an unaliased query column. A stored name is never judged.

    Parameters
    ----------
    name
        The new name.
    what
        Noun describing it, such as ``"Table name"``.

    Raises
    ------
    ValidationError
        If `character_outside_name_allowlist` finds a character; the message names it as
        ``U+XXXX`` with its position and every character that is allowed.
    """
    found = character_outside_name_allowlist(name)
    if found is None:
        return
    position, character = found
    if character in DOT_LIKE_DIGITS:
        raise ValidationError(
            f"{what} '{shown(name)}' holds U+{ord(character):04X} at character {position}, a "
            "digit zero drawn as a dot on the baseline, so the name would read as a dotted path "
            "of two names. Use '0' or another name."
        )
    raise ValidationError(
        f"{what} '{shown(name)}' holds U+{ord(character):04X} at character {position}, which is "
        "neither a letter nor a decimal digit. A new namespace, table or view name may hold only "
        "letters, decimal digits, '_', '-' and the plain space."
    )


def check_new_column_name(name: str) -> None:
    """Apply the rules a column name that is being created must meet.

    A column name may hold visible symbols (`refuse_characters_outside_name_allowlist` is not
    applied), but nothing invisible, no outer space and no look-alike.

    Parameters
    ----------
    name
        The new column name.

    Raises
    ------
    ValidationError
        From `refuse_hidden_characters`, `refuse_outer_whitespace` or
        `refuse_look_alike_characters`.
    """
    refuse_hidden_characters(name, what="Column name")
    refuse_outer_whitespace(name, what="Column name")
    refuse_look_alike_characters(name, what="Column name")


def check_new_name(component: str, *, what: str) -> None:
    """Apply the rules only a namespace or table name that is being created must meet.

    Parameters
    ----------
    component
        The new name, already checked by `validate_path_component`.
    what
        Noun describing the component, as for `validate_path_component`.

    Raises
    ------
    ValidationError
        From `refuse_percent_in_new_name`, `refuse_hidden_characters`,
        `refuse_outer_whitespace`, `refuse_characters_outside_name_allowlist` or
        `refuse_look_alike_characters`.
    """
    refuse_percent_in_new_name(component, what=what)
    refuse_hidden_characters(component, what=what)
    refuse_outer_whitespace(component, what=what)
    refuse_characters_outside_name_allowlist(component, what=what)
    refuse_look_alike_characters(component, what=what)


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
