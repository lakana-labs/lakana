"""The table of punctuation-like characters matches what its script makes of `confusables.txt`.

`confusables-excerpt.txt` holds the header and every line of Unicode's `confusables.txt`
(2026-08-06) whose source is a key of the table, and a few lines that must not make one: a
letter, a digit and punctuation that map to letters or punctuation, and a sequence. The test
never reads the network.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from lakana_core.identifiers import NAME_SYMBOLS as CORE_NAME_SYMBOLS
from lakana_core.punctuation_like import CONFUSABLES_DATE, PUNCTUATION_LIKE_CHARACTERS
from scripts.punctuation_like_characters import NAME_SYMBOLS, data_date, skeletons

_EXCERPT = Path(__file__).with_name("confusables-excerpt.txt")


def _known(table: dict[str, str]) -> dict[str, str]:
    """Keep the entries whose every character this Python's Unicode knows.

    The table was generated with the newest supported Python; an older one does not know some
    of the characters (``Cn``), and refuses them in every name anyway.

    Parameters
    ----------
    table
        Character to skeleton.

    Returns
    -------
    dict of str to str
        The entries this Python can judge.
    """
    return {
        character: skeleton
        for character, skeleton in table.items()
        if all(unicodedata.category(part) != "Cn" for part in character + skeleton)
    }


def test_the_script_makes_the_table_from_the_excerpt() -> None:
    """Every key and skeleton of the table, and nothing else, comes from the data."""
    lines = _EXCERPT.read_text(encoding="utf-8").splitlines()

    assert _known(skeletons(lines)) == _known(PUNCTUATION_LIKE_CHARACTERS)
    assert data_date(lines) == CONFUSABLES_DATE


def test_the_real_cases_are_in_the_table() -> None:
    """The Lisu tone letters, the augmentation dot and the Arabic-Indic zeros look like `.`."""
    for character in ("\ua4f8", "\U0001d16d", "\u0660", "\u06f0"):
        assert PUNCTUATION_LIKE_CHARACTERS[character] == "."
    assert PUNCTUATION_LIKE_CHARACTERS["\ua4f9"] == ","


def test_a_letter_digit_or_punctuation_that_maps_to_an_allowed_character_is_left_out() -> None:
    """Cyrillic a maps to Latin a, `1` to `l`, a dot leader is punctuation: no entries."""
    lines = _EXCERPT.read_text(encoding="utf-8").splitlines()
    found = skeletons(lines)

    for character in ("\u0430", "a", "1", "!", "\u2024"):
        assert character not in found


def test_the_script_and_the_core_agree_on_the_allowed_symbols() -> None:
    """The script repeats `NAME_SYMBOLS` so it runs without the package; the two must agree."""
    assert NAME_SYMBOLS == CORE_NAME_SYMBOLS
