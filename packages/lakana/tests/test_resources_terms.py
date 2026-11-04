"""The SDK's `terms` resource: what it sends and how it reads the answers."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import RelatedTerm, TermLink
from lakana.errors import InvalidResponse, NotFound
from lakana.resources.terms import Terms

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_TERM: dict[str, Any] = {
    "key": "net_revenue",
    "id": "5f1c0000-0000-0000-0000-000000000001",
    "name": "Net revenue",
    "definition": "Revenue after returns.",
    "synonyms": ["revenue", "sales"],
    "owner": "finance",
    "repository": "shop",
    "language": "en",
    "links": [
        {"table": "silver.gone", "column": None, "kind": "table", "missing": True},
        {"table": "silver.orders", "column": "order_id", "kind": "column", "missing": False},
    ],
    "related": [{"key": "gross_revenue", "name": "Gross revenue", "missing": False}],
    "related_by": ["gross_revenue"],
}


def _terms(body: Any, sent: list[httpx.Request]) -> Terms:  # ruff: ignore[any-type]
    """Build the resource over a transport that records requests and answers with `body`.

    Parameters
    ----------
    body
        The JSON the fake server sends.
    sent
        Receives every request.

    Returns
    -------
    Terms
        The resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return Terms(HttpTransport(_CONFIG, http_client=client))


def test_list_sends_the_filters_and_reads_a_page() -> None:
    sent: list[httpx.Request] = []
    page = _terms({"items": [_TERM], "next_cursor": "c1"}, sent).list(
        "dev", owner="finance", repository="shop", lang="fr", limit=5
    )
    assert sent[0].url.path == "/api/v1/environments/dev/terms"
    assert dict(sent[0].url.params) == {
        "owner": "finance",
        "repository": "shop",
        "lang": "fr",
        "limit": "5",
    }
    assert page.next_cursor == "c1"
    term = page.items[0]
    assert (term.key, term.links[1], term.related[0]) == (
        "net_revenue",
        TermLink("silver.orders", "order_id", "column", False),
        RelatedTerm("gross_revenue", "Gross revenue", False),
    )


def test_get_reads_a_term_and_ignores_unknown_fields() -> None:
    sent: list[httpx.Request] = []
    term = _terms({**_TERM, "new_field": 1}, sent).get("dev", "net_revenue", lang="fr")
    assert sent[0].url.path == "/api/v1/environments/dev/terms/net_revenue"
    assert term.name == "Net revenue"


def test_iter_follows_the_cursor() -> None:
    sent: list[httpx.Request] = []
    keys = [t.key for t in _terms({"items": [_TERM], "next_cursor": None}, sent).iter("dev")]
    assert keys == ["net_revenue"]
    assert dict(sent[0].url.params) == {"limit": "100"}


def test_an_answer_without_items_is_invalid() -> None:
    with pytest.raises(InvalidResponse):
        _terms({"next_cursor": None}, []).list("dev")


def test_a_term_without_its_fields_is_invalid() -> None:
    with pytest.raises(InvalidResponse, match="a term"):
        _terms({"key": "x"}, []).get("dev", "x")


def test_a_cursor_that_is_not_text_is_invalid() -> None:
    with pytest.raises(InvalidResponse, match="the terms"):
        _terms({"items": [], "next_cursor": 5}, []).list("dev")


def _server_without_terms() -> Terms:
    """Build the resource over a server that answers 404 `Not Found` to every route.

    Returns
    -------
    Terms
        The resource.
    """
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"detail": "Not Found"})),
        base_url=_CONFIG.url,
    )
    return Terms(HttpTransport(_CONFIG, http_client=client))


def test_a_server_without_the_glossary_routes_says_to_upgrade() -> None:
    terms = _server_without_terms()
    for call in (lambda: terms.list("dev"), lambda: terms.get("dev", "x")):
        with pytest.raises(NotFound, match=r"no glossary routes.*Upgrade the server"):
            call()


def test_a_missing_term_keeps_the_servers_own_message() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(404, json={"detail": "Term `x` does not exist."})
        ),
        base_url=_CONFIG.url,
    )
    with pytest.raises(NotFound, match="Term `x` does not exist"):
        Terms(HttpTransport(_CONFIG, http_client=client)).get("dev", "x")
