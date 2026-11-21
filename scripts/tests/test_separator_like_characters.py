"""The table of separator-like characters matches what its script makes of `confusables.txt`.

`confusables-excerpt.txt` holds the header and every line of Unicode's `confusables.txt`
(2026-08-06) whose source is a key of the table, and a few lines that must not make one: a
letter, a digit and punctuation that map to letters or punctuation, and a sequence. The test
never reads the network.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from lakana_core.identifiers import NAME_SYMBOLS as CORE_NAME_SYMBOLS
from lakana_core.separator_like import CONFUSABLES_DATE, SEPARATOR_LIKE_CHARACTERS
from scripts.separator_like_characters import NAME_SYMBOLS, data_date, skeletons

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

    assert _known(skeletons(lines)) == _known(SEPARATOR_LIKE_CHARACTERS)
    assert data_date(lines) == CONFUSABLES_DATE


def test_the_real_cases_are_in_the_table() -> None:
    """The Lisu tone letter, the augmentation dot and the zeros look like `.`; `no` like `/`."""
    for character in ("\ua4f8", "\U0001d16d", "\u0660", "\u06f0"):
        assert SEPARATOR_LIKE_CHARACTERS[character] == "."
    assert SEPARATOR_LIKE_CHARACTERS["\u30ce"] == "/"
    assert SEPARATOR_LIKE_CHARACTERS["\u0903"] == ":"


def test_a_character_whose_skeleton_holds_no_separator_is_left_out() -> None:
    """Other punctuation (eth, a counting rod, a comma, '?', an apostrophe) makes no entry."""
    lines = _EXCERPT.read_text(encoding="utf-8").splitlines()
    found = skeletons(lines)

    for character in (
        "\u0430",  # Cyrillic a, which looks like Latin a
        "1",  # which looks like l
        "\u2024",  # one dot leader, punctuation itself
        "\xf0",  # eth
        "\u6b63",  # a CJK ideograph that looks like a counting rod
        "\ua4f9",  # a Lisu tone letter that looks like ','
        "\u0294",  # glottal stop, '?'
        "\u02bc",  # modifier letter apostrophe
        "\u1427",  # a Canadian syllabic that looks like a middle dot
        "\u0140",  # l with a middle dot
    ):
        assert character not in found
        assert character not in SEPARATOR_LIKE_CHARACTERS


def test_the_script_and_the_core_agree_on_the_allowed_symbols() -> None:
    """The script repeats `NAME_SYMBOLS` so it runs without the package; the two must agree."""
    assert NAME_SYMBOLS == CORE_NAME_SYMBOLS
