"""The table of separator-like characters matches what its script makes of `confusables.txt`.

`confusables-excerpt.txt` holds the header and every line of Unicode's `confusables.txt`
(18.0.0, 2026-08-06) whose source is a key of the table, U+11DD9 included, which Python 3.14
does not know yet, and a few lines that must not make one: letters, a digit and punctuation
whose skeleton holds no separator. The file has no line whose source is a sequence, so the test
of that branch writes its own. The test never reads the network.
"""

from __future__ import annotations

import importlib.util
import unicodedata
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from lakana_core.identifiers import NAME_SYMBOLS as CORE_NAME_SYMBOLS
from lakana_core.separator_like import (
    CONFUSABLES_DATE,
    SEPARATOR_LIKE_CHARACTERS,
    UNICODE_VERSION,
)
from scripts.separator_like_characters import NAME_SYMBOLS, data_date, main, skeletons

_EXCERPT = Path(__file__).with_name("confusables-excerpt.txt")


def test_the_script_makes_the_table_from_the_excerpt() -> None:
    """Every key and skeleton of the table, and nothing else, comes from the data."""
    lines = _EXCERPT.read_text(encoding="utf-8").splitlines()

    assert skeletons(lines) == SEPARATOR_LIKE_CHARACTERS
    assert data_date(lines) == CONFUSABLES_DATE


def _version(text: str) -> tuple[int, ...]:
    """Read a Unicode version such as ``16.0.0``.

    Parameters
    ----------
    text
        The version.

    Returns
    -------
    tuple of int
        Its numbers.
    """
    return tuple(int(part) for part in text.split("."))


def test_this_pythons_unicode_is_not_newer_than_the_table() -> None:
    """A newer Unicode may add letters that look like a separator: regenerate and review."""
    assert _version(unicodedata.unidata_version) <= _version(UNICODE_VERSION), (
        f"This Python's Unicode {unicodedata.unidata_version} is newer than the Unicode "
        f"{UNICODE_VERSION} lakana_core/separator_like.py was generated with. Download a "
        "confusables.txt at least as new, run scripts/separator_like_characters.py with this "
        "Python, and review the new entries (docs/agent/security.md, separator-like characters)."
    )


def test_a_character_this_python_does_not_know_is_a_candidate() -> None:
    """U+11DD9 TOLONG SIKI SIGN SELA (Unicode 17, a letter) looks like `:`; it is in the table."""
    assert SEPARATOR_LIKE_CHARACTERS["\U00011dd9"] == ":"


def test_a_line_whose_source_is_a_sequence_is_skipped() -> None:
    """The rule judges one character at a time, so a sequence makes no entry."""
    sequence = "0061 0062 ;\t002E ;\tMA\t# ( ab \u2192 . ) a sequence \u2192 FULL STOP"
    single = "A4F8 ;\t002E ;\tMA\t# ( \ua4f8 \u2192 . ) LISU LETTER TONE MYA TI \u2192 FULL STOP"

    assert skeletons([sequence, single]) == {"\ua4f8": "."}


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


def _load(path: Path) -> ModuleType:
    """Import a generated module from a file.

    Parameters
    ----------
    path
        The module's file.

    Returns
    -------
    ModuleType
        The module.
    """
    spec = importlib.util.spec_from_file_location("written", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_writes_a_module_with_the_table(tmp_path: Path) -> None:
    """The written module imports and holds the table, a backslash skeleton escaped."""
    output = tmp_path / "separator_like.py"

    assert main([str(_EXCERPT), "--output", str(output)]) == 0

    written = _load(output)
    assert written.SEPARATOR_LIKE_CHARACTERS == SEPARATOR_LIKE_CHARACTERS
    assert written.CONFUSABLES_DATE == CONFUSABLES_DATE
    assert unicodedata.unidata_version == written.UNICODE_VERSION
    assert '"\\u4e36": "\\\\",' in output.read_text(encoding="utf-8")


def test_check_passes_on_the_repositorys_module() -> None:
    """``--check`` against the checked-in module and the excerpt finds no difference."""
    assert main([str(_EXCERPT), "--check"]) == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace('"\\u30ce": "/",', ""),
        lambda text: text.replace(
            'CONFUSABLES_DATE: Final[str] = "', 'CONFUSABLES_DATE: Final[str] = "1'
        ),
    ],
    ids=["an entry missing", "another date"],
)
def test_check_fails_when_the_module_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], change: Callable[[str], str]
) -> None:
    """A module without an entry, or of another date, fails ``--check`` with the way to fix it."""
    output = tmp_path / "separator_like.py"
    main([str(_EXCERPT), "--output", str(output)])
    output.write_text(change(output.read_text(encoding="utf-8")), encoding="utf-8")

    assert main([str(_EXCERPT), "--check", "--output", str(output)]) == 1
    assert "differs; run scripts/separator_like_characters.py" in capsys.readouterr().out


def test_a_file_without_a_date_line_is_refused() -> None:
    """The date pins the table, so a file without one cannot make it."""
    with pytest.raises(ValueError, match="no '# Date:' line"):
        data_date(["# confusables.txt", "A4F8 ;\t002E ;\tMA\t#"])
