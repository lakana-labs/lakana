r"""Generate `lakana_core.separator_like` from Unicode's `confusables.txt` (#609).

A new namespace, table or view name may hold letters, combining marks, decimal digits and
`lakana_core.identifiers.NAME_SYMBOLS`. Some of those characters look like a separator of a
path: U+A4F8 LISU LETTER TONE MYA TI and U+0660 ARABIC-INDIC DIGIT ZERO look like ``.``, so
``sales`` + U+A4F8 + ``daily`` reads as the dotted path ``sales.daily``, and U+30CE KATAKANA
LETTER NO looks like ``/``. Unicode's `confusables.txt` maps each such character to its
*skeleton*. This script keeps every single code point the allowlist accepts whose skeleton holds
one of `SEPARATORS` (``.``, ``/``, ``\``, ``:``), and writes them, with their skeletons, to
`packages/lakana-core/src/lakana_core/separator_like.py`, pinned to the data's date. A letter
whose skeleton holds other punctuation (U+00F0 eth, U+0294 glottal stop) stays allowed (#789,
option A).

Categories come from the running Python's `unicodedata`, but a source character that Python does
not know yet (``Cn``) is a candidate too, so the table does not depend on the Python that writes
it: U+11DD9 TOLONG SIKI SIGN SELA, a letter of Unicode 17 that looks like ``:``, is in it even when
Python 3.14 (Unicode 16) writes it, and an older Python refuses it as ``Cn`` anyway. The module
records the Unicode version it was written under (`UNICODE_VERSION`); a test fails on a Python
whose Unicode is newer, asking for a newer `confusables.txt` and a review. ``--check`` compares
the entries and the data's date, so it passes on every supported Python.

Usage: ``uv run --python 3.14 python scripts/separator_like_characters.py CONFUSABLES_TXT``
(``--check`` writes nothing and fails when the module differs). The file is
https://www.unicode.org/Public/security/latest/confusables.txt; the script never downloads it.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path

MODULE = Path("packages/lakana-core/src/lakana_core/separator_like.py")
"""The module this script writes, relative to the repository root."""

NAME_SYMBOLS = frozenset({"_", "-", " "})
"""`lakana_core.identifiers.NAME_SYMBOLS`, repeated so the script needs no installed package
(`test_separator_like_characters.py` checks that the two agree)."""

SEPARATORS = frozenset({".", "/", "\\", ":"})
"""The skeleton characters that make a character separator-like: the dot of the dotted display
form, the slashes of a path, and the colon."""

_DATE = re.compile(r"^# Date: (\d{4}-\d{2}-\d{2})")


def allowed(character: str) -> bool:
    """Say whether the table-name allowlist accepts a character, by its category alone.

    Parameters
    ----------
    character
        One character.

    Returns
    -------
    bool
        `True` for a letter, a combining mark, a decimal digit and a `NAME_SYMBOLS` character,
        and for a character this Python does not know (``Cn``): a newer Unicode may make it a
        letter.
    """
    category = unicodedata.category(character)
    return category[0] in "LM" or category in ("Nd", "Cn") or character in NAME_SYMBOLS


def skeletons(lines: Iterable[str]) -> dict[str, str]:
    """Pick the allowed single characters whose skeleton holds one of `SEPARATORS`.

    A line whose source is a sequence of characters is skipped: the rule judges one character
    at a time.

    Parameters
    ----------
    lines
        The lines of `confusables.txt` (``source ; target ; type # comment``).

    Returns
    -------
    dict of str to str
        Each such character and its skeleton, in code point order.
    """
    found: dict[str, str] = {}
    for line in lines:
        data = line.split("#", 1)[0].strip()
        if not data:
            continue
        source, target, *_ = (field.strip() for field in data.split(";"))
        codes = source.split()
        if len(codes) != 1:
            continue
        character = chr(int(codes[0], 16))
        skeleton = "".join(chr(int(code, 16)) for code in target.split())
        if allowed(character) and any(part in SEPARATORS for part in skeleton):
            found[character] = skeleton
    return dict(sorted(found.items()))


def data_date(lines: Iterable[str]) -> str:
    """Read the date line of `confusables.txt`.

    Parameters
    ----------
    lines
        The file's lines.

    Returns
    -------
    str
        The date, such as ``2026-08-06``.

    Raises
    ------
    ValueError
        If the file has no ``# Date:`` line.
    """
    for line in lines:
        match = _DATE.match(line.lstrip("\ufeff"))
        if match is not None:
            return match.group(1)
    raise ValueError("confusables.txt has no '# Date:' line")


def _escape(text: str) -> str:
    r"""Write text as a Python string literal of ASCII escapes.

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    str
        The literal, such as ``"\u0660"``.
    """
    plain = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + plain.encode("ascii", "backslashreplace").decode("ascii") + '"'


def render(table: dict[str, str], date: str) -> str:
    """Write the module's source.

    Parameters
    ----------
    table
        The characters and their skeletons (`skeletons`).
    date
        The data's date (`data_date`).

    Returns
    -------
    str
        The module's text.
    """
    rows = "".join(
        f"    {_escape(character)}: {_escape(skeleton)},"
        f"  # {unicodedata.name(character, f'U+{ord(character):04X}')}\n"
        for character, skeleton in table.items()
    )
    return (
        '"""Letters, marks and digits that look like a separator (#609); generated, do not edit.\n'
        "\n"
        "Written by `scripts/separator_like_characters.py` from Unicode's `confusables.txt` of\n"
        f"{date}.\n"
        "Each key is a character the table-name allowlist would accept, and its value the\n"
        "skeleton `confusables.txt` gives it, which holds `.`, `/`, a backslash or `:`.\n"
        '"""\n'
        "\n"
        "from typing import Final\n"
        "\n"
        f'CONFUSABLES_DATE: Final[str] = "{date}"\n'
        '"""The date of the `confusables.txt` the table was generated from."""\n'
        "\n"
        f'UNICODE_VERSION: Final[str] = "{unicodedata.unidata_version}"\n'
        '"""The Unicode version of the categories it was generated with; a newer Python asks for\n'
        'a regeneration (`test_separator_like_characters.py`)."""\n'
        "\n"
        "SEPARATOR_LIKE_CHARACTERS: Final[dict[str, str]] = {\n"
        f"{rows}"
        "}\n"
        '"""Character to skeleton; `lakana_core.identifiers.character_outside_name_allowlist`\n'
        'refuses the keys in a new namespace, table or view name."""\n'
    )


def main(argv: list[str] | None = None) -> int:
    """Write or check the module.

    Parameters
    ----------
    argv
        The arguments; `sys.argv` when `None`.

    Returns
    -------
    int
        0 on success, 1 when ``--check`` finds a difference.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("confusables", type=Path, help="a local copy of confusables.txt")
    parser.add_argument("--check", action="store_true", help="fail when the module differs")
    parser.add_argument(
        "--output", type=Path, default=MODULE, help=f"the module to write or check ({MODULE})"
    )
    args = parser.parse_args(argv)
    lines = args.confusables.read_text(encoding="utf-8-sig").splitlines()
    table, date = skeletons(lines), data_date(lines)
    if args.check:
        spec = importlib.util.spec_from_file_location("separator_like", args.output)
        assert spec is not None and spec.loader is not None
        written = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(written)
        if table != written.SEPARATOR_LIKE_CHARACTERS or date != written.CONFUSABLES_DATE:
            print(
                f"{args.output} differs; run scripts/separator_like_characters.py to regenerate it"
            )
            return 1
        return 0
    text = render(table, date)
    args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
