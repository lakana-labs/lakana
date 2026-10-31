"""The reader's languages: `?lang=`, else the profile, else `Accept-Language`, else the main one."""

from __future__ import annotations

from fastapi import Request

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.languages import canonical_language, language_problem, parse_accept_language

LANG_DESCRIPTION = (
    "The language of the texts, such as de or fr-CH; without it, your profile's language, then "
    "Accept-Language, then the instance's main language."
)
"""The description of every `lang` query parameter."""


def reader_languages(
    request: Request, lang: str | None, principal: PrincipalRef, main_language: str
) -> tuple[str, ...]:
    """Order the languages a reader wants, most wanted first.

    A malformed `Accept-Language` is ignored, never refused: browsers and proxies send it
    without the reader choosing it.

    Parameters
    ----------
    request
        The request, for `Accept-Language`.
    lang
        `?lang=`, any case; `None` when absent.
    principal
        The caller, for the profile language.
    main_language
        The instance's main language.

    Returns
    -------
    tuple of str
        Canonical tags; never empty.

    Raises
    ------
    ValidationError
        Mapped to 422, for a `lang` that is not a tag.
    """
    if lang is not None:
        canonical = canonical_language(lang.strip())
        if canonical is None:
            raise ValidationError(
                f"lang: {language_problem(lang)}; ask for a language such as de, or leave lang out"
            )
        return (canonical,)
    if principal.language:
        return (principal.language,)
    return parse_accept_language(request.headers.get("accept-language")) or (main_language,)
