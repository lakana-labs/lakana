"""The alias-refusing YAML loader."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from lakana_core.errors import ValidationError
from lakana_core.projects import yaml_loader
from lakana_core.projects.yaml_loader import (
    KeyLines,
    YamlError,
    YamlTooManyEntries,
    key_line,
    load_yaml,
    load_yaml_with_lines,
    shown_key,
)


def test_loads_plain_mappings_and_lists() -> None:
    """Ordinary YAML loads to Python values."""
    assert load_yaml("a: 1\nb: [x, y]\n", what="t", max_bytes=1024) == {"a": 1, "b": ["x", "y"]}


def test_empty_document_is_none() -> None:
    """An empty file is `None`, which callers treat as an empty mapping."""
    assert load_yaml("", what="t", max_bytes=10) is None


def test_refuses_an_anchor() -> None:
    """An anchor alone is refused: it is the first half of every alias bomb."""
    with pytest.raises(YamlError, match="anchors and aliases are not allowed") as info:
        load_yaml("a: &x 1\n", what="lakana.yaml", max_bytes=1024)
    assert info.value.line == 1


def test_refuses_an_alias() -> None:
    """An alias is refused, wherever it appears."""
    with pytest.raises(YamlError, match="anchors and aliases"):
        load_yaml("a: 1\nb: *x\n", what="lakana.yaml", max_bytes=1024)


def test_billion_laughs_is_refused_before_expansion() -> None:
    """The classic bomb fails on its first anchor, without building anything."""
    bomb = 'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]\n'
    bomb += "".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [{','.join(['*' + chr(97 + i)] * 9)}]\n" for i in range(8)
    )
    with pytest.raises(YamlError, match="anchors and aliases"):
        load_yaml(bomb, what="lakana.yaml", max_bytes=64 * 1024)


def test_refuses_a_document_over_the_size_limit() -> None:
    """The byte limit applies before parsing."""
    with pytest.raises(YamlError, match="larger than 1 KiB"):
        load_yaml("a: " + "x" * 2000, what="lakana.yaml", max_bytes=1024)


def test_syntax_error_reports_its_line() -> None:
    """A malformed document is a `ValidationError` carrying the 1-based line."""
    with pytest.raises(ValidationError, match=r"lakana\.yaml is not valid YAML") as info:
        load_yaml("a: 1\nb: [unclosed\n", what="lakana.yaml", max_bytes=1024)
    assert isinstance(info.value, YamlError)
    assert info.value.line is not None


def test_refuses_python_tags() -> None:
    """SafeLoader semantics: no arbitrary Python object construction."""
    with pytest.raises(YamlError):
        load_yaml("a: !!python/object/apply:os.system ['true']\n", what="t", max_bytes=1024)


@pytest.mark.parametrize(
    "text",
    ["a: 2024-13-45\n", "a: !!int abc\n", "a: !!float abc\n", "a: " + "9" * 5000 + "\n"],
)
def test_unconstructable_values_are_yaml_errors(text: str) -> None:
    """PyYAML's bare `ValueError` from a constructor never escapes."""
    with pytest.raises(YamlError, match="invalid value"):
        load_yaml(text, what="lakana.yaml", max_bytes=64 * 1024)


@pytest.mark.parametrize(
    "text", ["[" * 2000 + "]" * 2000, "".join(f"{' ' * i}b:\n" for i in range(200))]
)
def test_deep_nesting_is_a_yaml_error(text: str) -> None:
    """Nesting is bounded before Python's recursion limit is reachable."""
    with pytest.raises(YamlError, match="nesting is deeper than 64") as info:
        load_yaml(text, what="lakana.yaml", max_bytes=64 * 1024)
    assert info.value.line is not None


def test_moderate_nesting_still_loads() -> None:
    """Realistic depth is unaffected."""
    assert load_yaml("[[[[1]]]]", what="t", max_bytes=100) == [[[[1]]]]


def test_refuses_a_duplicate_key_on_its_line() -> None:
    """PyYAML keeps the last of two equal keys; a repository file must say which it means."""
    with pytest.raises(YamlError, match=r"lakana\.yaml: duplicate key 'owner'") as info:
        load_yaml(
            "defaults:\n  owner: a\n  tags: []\n  owner: b\n", what="lakana.yaml", max_bytes=1024
        )
    assert info.value.line == 4


def test_refuses_a_duplicate_key_at_the_top_level() -> None:
    """Duplicates are refused at every depth, the first level included."""
    with pytest.raises(YamlError, match="duplicate key 'repo'") as info:
        load_yaml("repo: a\nrepo: b\n", what="lakana.yaml", max_bytes=1024)
    assert info.value.line == 2


def test_same_key_in_different_mappings_is_fine() -> None:
    """Only keys of one mapping collide."""
    assert load_yaml("a: {x: 1}\nb: {x: 2}\n", what="t", max_bytes=1024) == {
        "a": {"x": 1},
        "b": {"x": 2},
    }


def test_refuses_a_merge_key() -> None:
    """`<<` would splice keys in from elsewhere, hiding which keys a mapping really has."""
    with pytest.raises(YamlError, match=r"merge keys \(<<\) are not allowed") as info:
        load_yaml("a:\n  <<: {x: 1}\n  y: 2\n", what="lakana.yaml", max_bytes=1024)
    assert info.value.line == 2


def test_an_unhashable_key_is_still_an_error() -> None:
    """A list used as a key keeps PyYAML's own refusal, as a `YamlError`."""
    with pytest.raises(YamlError):
        load_yaml("? [a, b]\n: 1\n", what="t", max_bytes=1024)


def test_key_line_finds_the_line_of_a_nested_key() -> None:
    text = "a:\n  b:\n    c: 1\n  d: [x, y]\ne: 2\n"
    assert key_line(text, ("a", "b", "c")) == 3
    assert key_line(text, ("a", "d")) == 4
    assert key_line(text, ("e",)) == 5


def test_key_line_falls_back_to_the_deepest_key_that_exists() -> None:
    text = "a:\n  b:\n    c: 1\n"
    assert key_line(text, ("a", "b", "missing", "deeper")) == 2
    assert key_line(text, ("a", "b", "c", "[key]")) == 3


def test_key_line_is_none_for_a_missing_first_key_or_bad_yaml() -> None:
    assert key_line("a: 1\n", ("z",)) is None
    assert key_line("a: [\n", ("a",)) is None
    assert key_line("a: &x 1\n", ("a",)) is None
    assert key_line("- 1\n", ("a",)) is None


def test_one_parse_gives_the_value_and_every_key_line() -> None:
    text = "a:\n  b:\n    c: 1\n  d: [x, {e: 2}]\n'7': 3\n"
    value, lines = load_yaml_with_lines(text, what="t", max_bytes=1024)
    assert value == {"a": {"b": {"c": 1}, "d": ["x", {"e": 2}]}, "7": 3}
    for path in [("a", "b", "c"), ("a", "d"), ("a", "b", "missing", "deeper"), ("7",), ("z",)]:
        assert lines.line(path) == key_line(text, path)
    assert lines.line(("a", "d", 1, "e")) == 4
    assert lines.line(()) is None


def test_key_lines_of_an_empty_document_find_nothing() -> None:
    value, lines = load_yaml_with_lines("", what="t", max_bytes=10)
    assert (value, lines) == (None, KeyLines())
    assert lines.line(("a",)) is None


def test_load_with_lines_refuses_what_load_refuses() -> None:
    with pytest.raises(YamlError, match="anchors and aliases") as info:
        load_yaml_with_lines("a: 1\nb: &x 2\n", what="t", max_bytes=1024)
    assert info.value.line == 2


def _load_value(text: str, *, what: str, max_bytes: int) -> object:
    """Load through `load_yaml_with_lines`, keeping only the value.

    Parameters
    ----------
    text
        The document.
    what
        Its name.
    max_bytes
        Its size limit.

    Returns
    -------
    object
        The value.
    """
    return load_yaml_with_lines(text, what=what, max_bytes=max_bytes)[0]


@pytest.mark.parametrize("load", [load_yaml, _load_value])
@pytest.mark.parametrize(
    ("char", "code"), [("\x00", "0000"), ("\x01", "0001"), ("\x0c", "000C"), ("\ufffe", "FFFE")]
)
def test_a_control_character_is_not_valid_yaml(
    load: Callable[..., object], char: str, code: str
) -> None:
    """PyYAML's reader refuses it while the loader is built; that is still a YAML error."""
    with pytest.raises(YamlError) as info:
        load(f"a: 1\nb: x{char}y\n", what="t", max_bytes=1024)
    assert str(info.value) == (
        f"t is not valid YAML: it holds the character U+{code}, which YAML does not allow; "
        "remove it"
    )
    assert info.value.line == 2


def test_entries_are_counted_while_composing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap stops the parse at the first entry past it, before any value is built."""
    text = "x:\n- a\n- b\n- c\n"
    assert load_yaml_with_lines(text, what="t", max_bytes=1024, max_entries=4)[0] == {
        "x": ["a", "b", "c"]
    }

    def never(*args: object, **kwargs: object) -> None:
        """Fail: no value may be built.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError("a value was built")

    monkeypatch.setattr(yaml_loader._NoAliasLoader, "construct_document", never)
    with pytest.raises(YamlTooManyEntries) as info:
        load_yaml_with_lines(text, what="t", max_bytes=1024, max_entries=3)
    assert (str(info.value), info.value.line) == ("t has more than 3 keys and list items.", 4)


def test_a_long_duplicate_key_is_cut_in_the_message() -> None:
    key = "k" * 5000
    with pytest.raises(YamlError) as info:
        load_yaml(f'? "{key}"\n: 1\n? "{key}"\n: 2\n', what="t", max_bytes=100_000)
    assert str(info.value) == f"t: duplicate key '{'k' * 79}…'."


def test_shown_key_is_one_short_line() -> None:
    assert shown_key("a\nb") == "a\\nb"
    assert shown_key("é") == "é"
    assert shown_key(3) == "3"
    assert shown_key("x" * 100) == "x" * 79 + "…"
    assert len(shown_key("x" * 80)) == 80


def test_shown_key_escapes_each_character_and_never_cuts_an_escape() -> None:
    assert shown_key("gold." + "é" * 100 + "\u202e") == "gold." + "é" * 74 + "…"
    assert shown_key("a\u202eb") == "a\\u202eb"
    assert shown_key("a" * 77 + "\u202e" + "b") == "a" * 77 + "…"
