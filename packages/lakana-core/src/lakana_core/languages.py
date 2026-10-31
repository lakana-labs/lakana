"""Language tags: the subset of BCP 47 Lakana takes, and how a reader's languages are ordered.

One rule for every place a language is named: `lakana.yaml`, a translation file's name, a
reader's `?lang=`, `Accept-Language` and profile, and the server's `LAKANA_MAIN_LANGUAGE`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from lakana_core.identifiers import shown

DEFAULT_MAIN_LANGUAGE = "en"
"""The instance's main language when `LAKANA_MAIN_LANGUAGE` is not set."""

MAX_LANGUAGE_CHARS = 35
"""The width of a stored tag: RFC 5646's recommended buffer, more than the subset needs."""

MAX_ACCEPTED_LANGUAGES = 10
"""The most tags read from one `Accept-Language` header."""

LANGUAGE_EXAMPLES = "en, de, fr-CH or zh-Hant"
"""The examples every text about a bad tag gives."""

_MAX_ACCEPT_ITEMS = 50
"""The most comma-separated items of `Accept-Language` looked at, valid or not."""

_TAG = re.compile(r"\A([A-Za-z]{2,3})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?\Z")
_WEIGHT = re.compile(r"\Aq=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)\Z")


def canonical_language(text: str) -> str | None:
    """Spell a tag canonically, or say it is not one Lakana takes.

    Parameters
    ----------
    text
        A tag as written.

    Returns
    -------
    str or None
        The tag with a lower-case language, a title-case script and an upper-case region;
        `None` for anything outside the subset (variants, extensions, private use included).
    """
    match = _TAG.match(text)
    if match is None:
        return None
    language, script, region = match.groups()
    parts = [language.lower()]
    if script:
        parts.append(script.title())
    if region:
        parts.append(region.upper())
    return "-".join(parts)


def language_problem(text: str) -> str | None:
    """Say what is wrong with a tag a person wrote, in the one text every caller uses.

    Parameters
    ----------
    text
        The tag as written.

    Returns
    -------
    str or None
        `None` for a canonical tag; else the reason, which names the tag (escaped) and either
        the examples or the canonical spelling.
    """
    canonical = canonical_language(text)
    if canonical is None:
        return (
            f"`{shown(text)}` is not a language tag Lakana takes: a language, then optionally a "
            f"script and a region (BCP 47), such as {LANGUAGE_EXAMPLES}"
        )
    if canonical != text:
        return f"`{shown(text)}` is written `{canonical}`; use that spelling"
    return None


def lookup_chain(tags: Iterable[str]) -> tuple[str, ...]:
    """Expand canonical tags by truncation, the RFC 4647 lookup: `fr-CH` → `fr-CH`, `fr`.

    Parameters
    ----------
    tags
        Canonical tags, most wanted first.

    Returns
    -------
    tuple of str
        Each tag followed by its shorter forms, without repeats.
    """
    chain: list[str] = []
    for tag in tags:
        parts = tag.split("-")
        for end in range(len(parts), 0, -1):
            candidate = "-".join(parts[:end])
            if candidate not in chain:
                chain.append(candidate)
    return tuple(chain)


def parse_accept_language(header: str | None) -> tuple[str, ...]:
    """Read an `Accept-Language` header leniently: skip what is malformed, never fail.

    Parameters
    ----------
    header
        The header's value, or `None`.

    Returns
    -------
    tuple of str
        Canonical tags by weight, then by position; `*`, malformed entries and `q=0` left out;
        at most `MAX_ACCEPTED_LANGUAGES`.
    """
    if not header:
        return ()
    weighted: list[tuple[float, int, str]] = []
    for position, item in enumerate(header.split(",")[:_MAX_ACCEPT_ITEMS]):
        tag, _, parameters = item.partition(";")
        weight = 1.0
        if parameters:
            match = _WEIGHT.match(parameters.replace(" ", ""))
            if match is None:
                continue
            weight = float(match.group(1))
        canonical = canonical_language(tag.strip())
        if canonical is None or weight == 0:
            continue
        weighted.append((-weight, position, canonical))
    ordered: list[str] = []
    for _, _, tag in sorted(weighted):
        if tag not in ordered:
            ordered.append(tag)
    return tuple(ordered[:MAX_ACCEPTED_LANGUAGES])
