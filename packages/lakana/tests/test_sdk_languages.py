"""The SDK's spelling of a language tag matches the server's."""

from __future__ import annotations

import pytest

from lakana._languages import canonical_language


@pytest.mark.parametrize(
    ("written", "canonical"),
    [
        ("EN", "en"),
        (" fr-ch ", "fr-CH"),
        ("ZH-hant", "zh-Hant"),
        ("zh-HANT-tw", "zh-Hant-TW"),
        ("es-419", "es-419"),
        ("fr_FR", "fr_FR"),
        (" not a tag ", "not a tag"),
    ],
)
def test_a_tag_is_spelled_as_the_server_stores_it(written: str, canonical: str) -> None:
    """Language lower case, script title case, region upper case; anything else as written."""
    assert canonical_language(written) == canonical
