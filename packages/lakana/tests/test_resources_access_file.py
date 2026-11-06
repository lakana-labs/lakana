"""The SDK side of access as code (#46): `Client.access.export` and `plan`."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana.errors import InvalidResponse, ServerError
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


_APPLIED = {
    "apply_id": "6b0f4c1e-5d7a-4a43-8d5e-0a7f1c2d3e4f",
    "file_sha256": "b" * 64,
    "source_commit": "0123abc",
    "created": 2,
    "changed": 1,
    "removed": 3,
    "released": ["staging"],
    "applied_at": "2026-11-06T09:30:00+00:00",
}


def test_apply_posts_the_file_with_the_fingerprint_and_parses_the_result() -> None:
    """The body carries every field; the answer becomes an `AccessApplyResult`."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_APPLIED)

    result = _access(handler).apply(
        "version: 1\n", plan_fingerprint="f" * 64, source_commit="0123abc", allow_lockout=True
    )
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/v1/access/apply"
    assert json.loads(seen[0].content) == {
        "file_name": "access.yaml",
        "text": "version: 1\n",
        "plan_fingerprint": "f" * 64,
        "source_commit": "0123abc",
        "allow_lockout": True,
    }
    assert (result.created, result.changed, result.removed) == (2, 1, 3)
    assert result.released == ["staging"]
    assert result.source_commit == "0123abc"
    assert result.applied_at.year == 2026
    assert str(result.apply_id) == _APPLIED["apply_id"]


def test_apply_is_sent_once_even_when_the_server_fails() -> None:
    """`retry=False`: an apply that may have happened is never repeated by the SDK."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503, json={"detail": "busy"})

    with pytest.raises(ServerError):
        _access(handler).apply("x", plan_fingerprint="f" * 64)
    assert len(seen) == 1


def test_apply_refuses_an_odd_answer() -> None:
    """An answer that is not an apply result is named."""
    with pytest.raises(InvalidResponse):
        _access(lambda r: httpx.Response(200, json={"created": "x"})).apply(
            "x", plan_fingerprint="f" * 64
        )


def test_status_parses_scopes_and_overrides() -> None:
    """Both lists become dataclasses; null names stay `None`."""
    body = {
        "scopes": [
            {
                "scope": "prod",
                "file_name": "access.yaml",
                "file_sha256": "c" * 64,
                "source_commit": None,
                "applied_by": None,
                "applied_at": "2026-11-06T09:30:00+00:00",
            }
        ],
        "overrides": [
            {
                "scope": "prod",
                "change": "grant VIEWER to bob",
                "reason": "Incident 42",
                "by": "carol",
                "at": "2026-11-06T10:00:00+00:00",
            }
        ],
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    status = _access(handler).status()
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/access/status"
    assert status.scopes[0].scope == "prod"
    assert status.scopes[0].applied_by is None
    assert status.scopes[0].source_commit is None
    assert status.overrides[0].reason == "Incident 42"
    assert status.overrides[0].at.year == 2026


def test_status_refuses_an_odd_answer() -> None:
    """A body without the lists is named."""
    with pytest.raises(InvalidResponse):
        _access(lambda r: httpx.Response(200, json={"scopes": 3})).status()
