"""The canonical spelling of a language tag, as the server writes it.

The SDK's copy of the server's casing rule: a lower-case language, a title-case script and an
upper-case region. The server stays the judge of which tags it takes; this only spells one.
"""

from __future__ import annotations

import re

_TAG = re.compile(r"\A([A-Za-z]{2,3})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?\Z")
"""A language, then optionally a script and a region: the subset of BCP 47 the server takes."""


def canonical_language(text: str) -> str:
    """Spell a tag as the server stores it.

    Parameters
    ----------
    text
        A tag as written, such as ``FR-ch``.

    Returns
    -------
    str
        The tag spelled canonically, such as ``fr-CH``; a text that is not a tag the server takes,
        without its surrounding spaces, as it is.
    """
    stripped = text.strip()
    match = _TAG.match(stripped)
    if match is None:
        return stripped
    language, script, region = match.groups()
    parts = [language.lower()]
    if script:
        parts.append(script.title())
    if region:
        parts.append(region.upper())
    return "-".join(parts)
