"""The SDK's `raw` resource: the listing of `raw/` (#672)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import RawClaim, RawEntry, RawPage
from lakana.errors import InvalidResponse
from lakana.resources.raw import Raw

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")


def _raw(*bodies: Any, sent: list[httpx.Request]) -> Raw:  # ruff: ignore[any-type]
    """Build the resource over a transport that records requests and answers in turn.

    Parameters
    ----------
    *bodies
        The JSON the fake server sends, one per request.
    sent
        Receives every request.

    Returns
    -------
    Raw
        The resource.
    """
    answers = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=answers.pop(0))

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return Raw(HttpTransport(_CONFIG, http_client=client))


def test_list_sends_the_folder_and_reads_a_page() -> None:
    sent: list[httpx.Request] = []
    page = _raw(
        {
            "path": "raw/feeds",
            "items": [
                {"name": "orders", "kind": "folder", "size": None, "claimed_by": {"table": None}},
                {"name": "a.csv", "kind": "file", "size": 5, "claimed_by": None, "new": 1},
            ],
            "next_cursor": "c1",
        },
        sent=sent,
    ).list("dev", "raw/feeds", limit=2, cursor="c0")
    assert sent[0].url.path == "/api/v1/environments/dev/raw"
    assert dict(sent[0].url.params) == {"path": "raw/feeds", "limit": "2", "cursor": "c0"}
    assert page == RawPage(
        path="raw/feeds",
        items=[
            RawEntry("orders", "folder", None, RawClaim(None)),
            RawEntry("a.csv", "file", 5, None),
        ],
        next_cursor="c1",
    )


def test_iter_reads_every_page() -> None:
    sent: list[httpx.Request] = []
    entry = {"name": "a.csv", "kind": "file", "size": 1, "claimed_by": None}
    raw = _raw(
        {"path": "raw", "items": [entry], "next_cursor": "c1"},
        {"path": "raw", "items": [{**entry, "name": "b.csv"}], "next_cursor": None},
        sent=sent,
    )
    assert [e.name for e in raw.iter("dev")] == ["a.csv", "b.csv"]
    assert [dict(r.url.params) for r in sent] == [
        {"path": "raw", "limit": "100"},
        {"path": "raw", "limit": "100", "cursor": "c1"},
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"path": "raw", "items": "x", "next_cursor": None},
        {"path": "raw", "items": [], "next_cursor": 3},
        {"path": "raw", "items": [{"name": "a"}], "next_cursor": None},
        {"items": [], "next_cursor": None},
    ],
)
def test_an_odd_answer_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(InvalidResponse, match="raw/"):
        _raw(body, sent=[]).list("dev")
