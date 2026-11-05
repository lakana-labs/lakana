"""The SDK side of access as code (#46): `Client.access.export` and `plan`."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana.errors import InvalidResponse
from lakana.resources.access import Access

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")

_PLAN = {
    "file_name": "team.yaml",
    "file_sha256": "a" * 64,
    "manages": ["prod"],
    "creates": [{"op": "create", "entry": "grants[0]", "text": "grant role VIEWER on prod"}],
    "changed": [{"op": "change", "entry": "ci_trust[0]", "text": "rule x"}],
    "removals": [{"op": "remove", "entry": None, "text": "grant role ANALYST on prod"}],
    "releases": ["staging"],
    "drift": [
        {
            "scope": "prod",
            "text": "grant role VIEWER on prod to user:bob",
            "by": "carol",
            "reason": "incident",
            "at": "2026-11-01T10:00:00+00:00",
        },
        {"scope": "prod", "text": "other", "by": None, "reason": None, "at": None},
    ],
    "problems": ["team.yaml: x (line 3): unknown key"],
    "fingerprint": "f" * 64,
}


def _access(handler: Callable[[httpx.Request], httpx.Response]) -> Access:
    """Build the resource over a mock transport.

    Parameters
    ----------
    handler
        The mock transport's request handler.

    Returns
    -------
    Access
        The resource.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return Access(HttpTransport(_CONFIG, http_client=client))


def test_export_sends_every_scope_and_returns_the_text() -> None:
    """Scopes repeat in the query."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "version: 1\n"})

    assert _access(handler).export(["prod", "staging"]) == "version: 1\n"
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/access/export"
    assert seen[0].url.query == b"scope=prod&scope=staging"


def test_export_defaults_to_the_instance() -> None:
    """No argument asks for the instance."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": ""})

    _access(handler).export()
    assert seen[0].url.query == b"scope=instance"


def test_export_refuses_an_answer_without_text() -> None:
    """A server that does not answer like a Lakana server is named."""
    with pytest.raises(InvalidResponse):
        _access(lambda r: httpx.Response(200, json={"text": 3})).export()


def test_plan_posts_the_file_and_parses_every_field() -> None:
    """The body carries name, text and the lockout flag; the answer becomes dataclasses."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_PLAN)

    plan = _access(handler).plan("version: 1\n")
    assert json.loads(seen[0].content) == {
        "file_name": "access.yaml",
        "text": "version: 1\n",
        "allow_lockout": False,
    }
    assert seen[0].url.path == "/api/v1/access/plan"
    assert plan.file_name == "team.yaml"
    assert plan.manages == ["prod"]
    assert plan.creates[0].entry == "grants[0]"
    assert plan.changed[0].op == "change"
    assert plan.removals[0].entry is None
    assert plan.releases == ["staging"]
    assert (plan.drift[0].by, plan.drift[0].reason) == ("carol", "incident")
    assert plan.drift[0].at is not None and plan.drift[0].at.year == 2026
    assert plan.drift[1].at is None
    assert plan.problems == ["team.yaml: x (line 3): unknown key"]
    assert plan.fingerprint == "f" * 64


def test_plan_sends_a_name_and_the_lockout_flag() -> None:
    """Both keywords reach the body."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_PLAN)

    _access(handler).plan("x", file_name="team.yaml", allow_lockout=True)
    body = json.loads(seen[0].content)
    assert (body["file_name"], body["allow_lockout"]) == ("team.yaml", True)


def test_plan_refuses_an_odd_answer() -> None:
    """A body that is not a plan raises InvalidResponse."""
    with pytest.raises(InvalidResponse):
        _access(lambda r: httpx.Response(200, json={"file_name": "x"})).plan("x")
