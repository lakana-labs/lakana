"""The `terms` resource: `Client.terms`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lakana._http import HttpTransport, every_page, query_params
from lakana._models import RelatedTerm, Term, TermLink, TermPage, known_fields
from lakana._routes import ROUTES
from lakana.errors import InvalidResponse, NotFound
from lakana.resources.artifacts import _ODD_ANSWER, _object


def _term_from_json(data: object) -> Term:
    """Build a `Term` from a `TermOut` body.

    A field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Term
        The term.

    Raises
    ------
    InvalidResponse
        If `data` is not an object with every field of a term.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; caught just below
        fields = known_fields(Term, data)
        fields["synonyms"] = tuple(fields["synonyms"])
        fields["related_by"] = tuple(fields["related_by"])
        fields["links"] = tuple(TermLink(**known_fields(TermLink, x)) for x in fields["links"])
        fields["related"] = tuple(
            RelatedTerm(**known_fields(RelatedTerm, x)) for x in fields["related"]
        )
        return Term(**fields)
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a term")) from None


def _page_from_json(data: dict[str, Any]) -> TermPage:
    """Build a `TermPage` from a `TermPage` body.

    Parameters
    ----------
    data
        The decoded object.

    Returns
    -------
    TermPage
        The page.

    Raises
    ------
    InvalidResponse
        If the object has no list of items or its cursor is not text.
    """
    odd = InvalidResponse(_ODD_ANSWER.format(what="the terms"))
    items = data.get("items")
    if not isinstance(items, list):
        raise odd
    cursor = data.get("next_cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise odd
    return TermPage(items=[_term_from_json(item) for item in items], next_cursor=cursor)


def _no_routes(error: NotFound) -> NotFound:
    """Tell a server without the glossary routes from a missing term.

    Parameters
    ----------
    error
        A 404 from a terms route.

    Returns
    -------
    NotFound
        An error that says to upgrade the server when the 404 is the framework's bare
        `Not Found` (the route does not exist), else `error` itself.
    """
    if error.detail != "Not Found":
        return error
    return NotFound(
        "This server has no glossary routes. Upgrade the server to a release with the "
        "business glossary (lakana-labs/lakana#375), or use an SDK that matches the server.",
        status=error.status,
        error_type=error.error_type,
    )


class Terms:
    """The `terms` resource of the platform API: the glossary of an environment.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(
        self,
        environment: str,
        *,
        owner: str | None = None,
        repository: str | None = None,
        lang: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> TermPage:
        """List one page of an environment's glossary terms, sorted by key.

        Parameters
        ----------
        environment
            The environment.
        owner
            Only terms with exactly this owner.
        repository
            Only terms this repository defines.
        lang
            Show the texts in this language, such as ``de`` or ``fr-CH``; your profile's
            language by default.
        limit
            Most terms returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`.

        Returns
        -------
        TermPage
            The terms, and the cursor of the next page (`None` on the last).

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist, or the server has no glossary routes.
        """
        try:
            response = self._transport.request(
                ROUTES["terms.list"],
                path_params={"env": environment},
                params=query_params(
                    owner=owner, repository=repository, lang=lang, limit=limit, cursor=cursor
                ),
            )
        except NotFound as error:
            translated = _no_routes(error)
            raise translated from None
        return _page_from_json(_object(response, "the terms"))

    def iter(
        self,
        environment: str,
        *,
        owner: str | None = None,
        repository: str | None = None,
        lang: str | None = None,
        page_size: int = 100,
    ) -> Iterator[Term]:
        """Yield every term of an environment, sorted by key, fetching pages as needed.

        Parameters
        ----------
        environment
            The environment.
        owner
            Only terms with exactly this owner.
        repository
            Only terms this repository defines.
        lang
            Show the texts in this language.
        page_size
            Terms per request (1 to 200).

        Returns
        -------
        Iterator of Term
            Each term, once.
        """
        return every_page(
            lambda cursor: self.list(
                environment,
                owner=owner,
                repository=repository,
                lang=lang,
                limit=page_size,
                cursor=cursor,
            )
        )

    def get(self, environment: str, key: str, *, lang: str | None = None) -> Term:
        """Show a glossary term.

        Parameters
        ----------
        environment
            The environment.
        key
            The term's id in the glossary.
        lang
            Show the texts in this language.

        Returns
        -------
        Term
            The term.

        Raises
        ------
        lakana.errors.NotFound
            If the environment or the term does not exist.
        """
        try:
            response = self._transport.request(
                ROUTES["terms.get"],
                path_params={"env": environment, "key": key},
                params=query_params(lang=lang),
            )
        except NotFound as error:
            translated = _no_routes(error)
            raise translated from None
        return _term_from_json(_object(response, "a term"))
