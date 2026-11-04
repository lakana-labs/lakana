"""The glossary routes of the platform API: an environment's business terms."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.glossary_read import GlossaryService, TermView
from lakana_core.errors import NotFoundError, PermissionDenied
from lakana_server.api.platform.languages import LANG_DESCRIPTION, reader_languages
from lakana_server.api.platform.schemas import (
    RelatedTermOut,
    TermLinkOut,
    TermOut,
    TermPage,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_glossary_service, get_main_language, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}",
    tags=["glossary"],
    responses=errors(401),
)


_NO_TERM = (
    "Term `{key}` does not exist in environment `{env}`. List the terms with: "
    "lakana term list --env {env}"
)
"""The answer for a term the caller cannot be told about; the text of a missing term."""


def _out(view: TermView) -> TermOut:
    """Build the response body of a term.

    Parameters
    ----------
    view
        From `GlossaryService`.

    Returns
    -------
    TermOut
        The body.
    """
    return TermOut(
        key=view.key,
        id=str(view.id),
        name=view.name,
        definition=view.definition,
        synonyms=list(view.synonyms),
        owner=view.owner,
        repository=view.repository,
        language=view.language,
        links=[TermLinkOut.model_validate(link, from_attributes=True) for link in view.links],
        related=[RelatedTermOut.model_validate(r, from_attributes=True) for r in view.related],
        related_by=list(view.related_by),
    )


@router.get("/terms", response_model=TermPage, responses=errors(403, 404))
def list_terms(
    request: Request,
    env: str,
    owner: str | None = Query(default=None, description="Only terms with exactly this owner."),
    repository: str | None = Query(default=None, description="Only terms this repository defines."),
    lang: str | None = Query(default=None, description=LANG_DESCRIPTION),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: GlossaryService = Depends(get_glossary_service),
    main_language: str = Depends(get_main_language),
) -> TermPage:
    """List the environment's glossary terms, sorted by key.

    Each term shows its texts in your language (`lang`, else your profile's language, else
    `Accept-Language`, else the instance's main language); a text without a translation shows in
    the main language. Without `READ_ENVIRONMENT` the page is empty, as for an environment that
    does not exist. A link appears only when you may read its table, or when its table does not
    exist (`missing: true`). Answers 404 when the environment does not exist and you may read
    environments, and 422 for a `lang` that is not a language tag, a cursor this list did
    not issue, or an `owner` (over 1,000 characters) or `repository` (over 255) no term can
    have, or one with a control character such as NUL.

    Parameters
    ----------
    request
        The request, for `Accept-Language`.
    env
        Environment name.
    owner
        Only terms with this owner.
    repository
        Only terms of this repository.
    lang
        The language of the texts.
    limit
        Most terms returned (1 to 200).
    cursor
        The `next_cursor` of the previous page.
    principal
        The authenticated caller.
    service
        The glossary reader.
    main_language
        The instance's main language.

    Returns
    -------
    TermPage
        The terms and the next cursor.
    """
    try:
        page = service.list(
            principal,
            env,
            owner=owner,
            repository=repository,
            languages=reader_languages(request, lang, principal, main_language),
            limit=limit,
            cursor=cursor,
        )
    except PermissionDenied:
        return TermPage(items=[], next_cursor=None)
    return TermPage(items=[_out(v) for v in page.items], next_cursor=page.next_cursor)


@router.get("/terms/{key}", response_model=TermOut, responses=errors(403, 404))
def get_term(
    request: Request,
    env: str,
    key: str,
    lang: str | None = Query(default=None, description=LANG_DESCRIPTION),
    principal: PrincipalRef = Depends(get_principal),
    service: GlossaryService = Depends(get_glossary_service),
    main_language: str = Depends(get_main_language),
) -> TermOut:
    """Show one glossary term.

    The texts come in your language (`lang`, else your profile's language, else
    `Accept-Language`, else the instance's main language); a text without a translation shows in
    the main language. Without `READ_ENVIRONMENT` the answer is the 404 of a missing term. A link
    appears only when you may read its table, or when its table does not exist (`missing: true`).
    Answers 404 when the environment or the term does not exist, and 422 for a `lang` that
    is not a language tag or a `key` no term can have: over 63 characters, or with a control
    character such as NUL.

    Parameters
    ----------
    request
        The request, for `Accept-Language`.
    env
        Environment name.
    key
        The term's id in the glossary.
    lang
        The language of the texts.
    principal
        The authenticated caller.
    service
        The glossary reader.
    main_language
        The instance's main language.

    Returns
    -------
    TermOut
        The term.

    Raises
    ------
    NotFoundError
        If the caller may not read the environment: the answer of a missing term.
    """
    languages = reader_languages(request, lang, principal, main_language)
    try:
        return _out(service.get(principal, env, key, languages=languages))
    except PermissionDenied:
        raise NotFoundError(_NO_TERM.format(key=key, env=env)) from None
