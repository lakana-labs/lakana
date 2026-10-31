"""Language tags: the BCP 47 subset Lakana takes, truncation and Accept-Language."""

from __future__ import annotations

import pytest

from lakana_core.languages import (
    MAX_ACCEPTED_LANGUAGES,
    canonical_language,
    language_problem,
    lookup_chain,
    parse_accept_language,
)

_NOT_A_TAG = (
    "is not a language tag Lakana takes: a language, then optionally a script and a region "
    "(BCP 47), such as en, de, fr-CH or zh-Hant"
)


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("de", "de"),
        ("DE", "de"),
        ("fr-ch", "fr-CH"),
        ("zh-hant", "zh-Hant"),
        ("SR-latn-rs", "sr-Latn-RS"),
        ("es-419", "es-419"),
        ("gsw", "gsw"),
    ],
)
def test_a_tag_is_spelled_canonically(text: str, canonical: str) -> None:
    assert canonical_language(text) == canonical


@pytest.mark.parametrize(
    "text",
    [
        "",
        "e",
        "english",
        "de_DE",
        "de-CH-1996",
        "x-private",
        "en-",
        "-en",
        "de-12",
        "dé",
        "i-klingon",
    ],
)
def test_anything_outside_the_subset_is_not_a_tag(text: str) -> None:
    assert canonical_language(text) is None
    problem = language_problem(text)
    assert problem is not None and problem.endswith(_NOT_A_TAG)


def test_a_tag_in_the_wrong_case_names_its_spelling() -> None:
    assert language_problem("DE-ch") == "`DE-ch` is written `de-CH`; use that spelling"
    assert language_problem("de-CH") is None


def test_a_tag_with_a_control_character_is_shown_escaped() -> None:
    problem = language_problem("de\n")
    assert problem is not None and "\n" not in problem and problem.startswith("`de\\n`")


def test_lookup_chain_truncates_each_tag_and_drops_repeats() -> None:
    assert lookup_chain(["zh-Hant-TW", "fr-CH", "fr", "zh"]) == (
        "zh-Hant-TW",
        "zh-Hant",
        "zh",
        "fr-CH",
        "fr",
    )


def test_accept_language_is_ordered_by_weight_then_position() -> None:
    assert parse_accept_language("fr-CH, de;q=0.8, en;q=0.9, it;q=0") == ("fr-CH", "en", "de")


def test_accept_language_skips_junk_and_caps_the_list() -> None:
    assert parse_accept_language(None) == ()
    assert parse_accept_language("*;q=abc, ,,,en-, *, de;q=2, fr;q=0.5") == ("fr",)
    many = ", ".join(f"a{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(500))
    assert len(parse_accept_language(many)) == MAX_ACCEPTED_LANGUAGES


def test_accept_language_lists_a_repeated_tag_once() -> None:
    assert parse_accept_language("de, fr;q=0.8, de;q=0.5") == ("de", "fr")
