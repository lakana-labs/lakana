"""The SDK side of #147: grants without a filter, permissions, searching principals and groups."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from lakana._config import ResolvedConfig
from lakana._http import NEXT_CURSOR_HEADER, HttpTransport
from lakana._models import (
    Group,
    HeldGrant,
    Permission,
    Permissions,
    PermissionVia,
    Principal,
    Scope,
)
from lakana.resources.grants import Grants
from lakana.resources.groups import Groups
from lakana.resources.principals import Principals

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")

_GRANT = {
    "id": "g1",
    "principal_id": "p1",
    "group_id": None,
    "role": "VIEWER",
    "action": None,
    "kind": "INSTANCE",
    "environment": None,
    "namespace": None,
    "table": None,
    "granted_by": "p0",
    "granted_at": "2026-10-01T12:00:00+00:00",
}


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> HttpTransport:
    """Build a transport whose client answers with `handler`.

    Parameters
    ----------
    handler
        The mock transport's request handler.

    Returns
    -------
    HttpTransport
        A transport backed by `httpx.MockTransport(handler)`.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


def _pages(
    path: str, item: dict[str, Any], seen: list[dict[str, str]]
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer two pages of one item each, recording the query of each request.

    Parameters
    ----------
    path
        The path every request must go to.
    item
        The item of each page.
    seen
        Receives the query parameters of each request.

    Returns
    -------
    Callable
        The handler.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        params = dict(request.url.params)
        seen.append(params)
        headers = {} if "cursor" in params else {NEXT_CURSOR_HEADER: "next"}
        return httpx.Response(200, json=[item], headers=headers)

    return handler


def test_grants_list_page_sends_every_filter_and_reads_the_cursor() -> None:
    seen: list[dict[str, str]] = []
    grants = Grants(_transport(_pages("/api/v1/grants", _GRANT, seen)))
    page = grants.list_page(
        principal_name="alice",
        action="READ_TABLE",
        environment="dev",
        namespace="gold",
        table="orders",
        limit=5,
    )
    assert [g.id for g in page.items] == ["g1"]
    assert page.next_cursor == "next"
    assert seen == [
        {
            "principal_name": "alice",
            "action": "READ_TABLE",
            "environment": "dev",
            "namespace": "gold",
            "table": "orders",
            "limit": "5",
        }
    ]
    assert [g.id for g in grants.list(group_name="team", cursor="next")] == ["g1"]
    assert seen[-1] == {"group_name": "team", "cursor": "next"}


def test_grants_list_by_kind_sends_it_in_capitals() -> None:
    seen: list[dict[str, str]] = []
    grants = Grants(_transport(_pages("/api/v1/grants", _GRANT, seen)))
    grants.list(kind="instance")
    assert seen == [{"kind": "INSTANCE"}]
    list(grants.iter(kind="TABLE", page_size=1))
    assert seen[1] == {"kind": "TABLE", "limit": "1"}


def test_grants_iter_walks_every_page() -> None:
    seen: list[dict[str, str]] = []
    grants = Grants(_transport(_pages("/api/v1/grants", _GRANT, seen)))
    assert [g.id for g in grants.iter(action="DEPLOY", page_size=1)] == ["g1", "g1"]
    assert seen == [
        {"action": "DEPLOY", "limit": "1"},
        {"action": "DEPLOY", "limit": "1", "cursor": "next"},
    ]


def test_principals_search_and_page() -> None:
    seen: list[dict[str, str]] = []
    item = {"name": "alice", "type": "USER", "is_active": True}
    principals = Principals(_transport(_pages("/api/v1/principals", item, seen)))
    page = principals.list_page(search="ali", limit=1)
    assert page.items == [Principal(name="alice", type="USER", is_active=True)]
    assert page.next_cursor == "next"
    assert principals.list(search="ali", cursor="next") == page.items
    assert [p.name for p in principals.iter(search="ali")] == ["alice", "alice"]
    assert seen[:2] == [{"q": "ali", "limit": "1"}, {"q": "ali", "cursor": "next"}]
    assert seen[2:] == [
        {"q": "ali", "limit": "100"},
        {"q": "ali", "limit": "100", "cursor": "next"},
    ]


def test_groups_search_and_page() -> None:
    seen: list[dict[str, str]] = []
    groups = Groups(_transport(_pages("/api/v1/groups", {"name": "team"}, seen)))
    page = groups.list_page(search="te")
    assert page.items == [Group(name="team")]
    assert groups.list() == [Group(name="team")]
    assert [g.name for g in groups.iter(page_size=2)] == ["team", "team"]
    assert seen[0] == {"q": "te"}
    assert seen[1] == {}


def test_principals_permissions_parse_the_answer() -> None:
    seen: list[httpx.Request] = []
    scope = {"kind": "TABLE", "environment": "dev", "namespace": "gold", "table": "orders"}
    body = {
        "principal": "alice",
        "active": True,
        "scope": scope,
        "permissions": [
            {
                "action": "READ_TABLE",
                "allowed": True,
                "via": [
                    {
                        "kind": "group",
                        "grant_id": "g1",
                        "group": "team",
                        "role": "VIEWER",
                        "scope": {
                            "kind": "ENVIRONMENT",
                            "environment": "dev",
                            "namespace": None,
                            "table": None,
                        },
                    }
                ],
                "reason": None,
            },
            {"action": "DROP_TABLE", "allowed": False, "via": [], "reason": "no"},
        ],
        "note": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    principals = Principals(_transport(handler))
    answer = principals.permissions("alice", environment="dev", namespace="gold", table="orders")
    assert seen[0].url.path == "/api/v1/principals/alice/permissions"
    assert dict(seen[0].url.params) == {
        "environment": "dev",
        "namespace": "gold",
        "table": "orders",
    }
    assert answer == Permissions(
        principal="alice",
        active=True,
        scope=Scope("TABLE", "dev", "gold", "orders"),
        permissions=[
            Permission(
                action="READ_TABLE",
                allowed=True,
                via=[
                    PermissionVia(
                        kind="group",
                        grant_id="g1",
                        group="team",
                        role="VIEWER",
                        scope=Scope("ENVIRONMENT", "dev", None, None),
                    )
                ],
                reason=None,
            ),
            Permission(action="DROP_TABLE", allowed=False, via=[], reason="no"),
        ],
        note=None,
    )
    principals.permissions("alice")
    assert dict(seen[1].url.params) == {}


def test_principals_grants_reads_group_and_scope() -> None:
    seen: list[httpx.Request] = []
    base = {
        "role": "ANALYST",
        "action": None,
        "kind": "ENVIRONMENT",
        "environment": "dev",
        "namespace": None,
        "table": None,
        "note": None,
    }
    body = [
        {
            **base,
            "id": "g2",
            "kind": "NAMESPACE",
            "namespace": "sales.eu",
            "granted_at": "2026-03-01T00:00:00Z",
            "group": "analysts",
        },
        {**base, "id": "g1", "granted_at": "2026-02-01T00:00:00Z", "group": None},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    held = Principals(_transport(handler)).grants("bob")
    assert seen[0].url.path == "/api/v1/principals/bob/grants"
    assert [(h.id, h.group, h.namespace) for h in held] == [
        ("g2", "analysts", "sales.eu"),
        ("g1", None, None),
    ]
    assert held[1] == HeldGrant(
        id="g1",
        role="ANALYST",
        action=None,
        kind="ENVIRONMENT",
        environment="dev",
        namespace=None,
        table=None,
        granted_at=datetime(2026, 2, 1, tzinfo=UTC),
        note=None,
        group=None,
    )


def test_groups_list_by_member_sends_it_on_every_page() -> None:
    seen: list[dict[str, str]] = []
    groups = Groups(_transport(_pages("/api/v1/groups", {"name": "team"}, seen)))
    assert groups.list(member="maria") == [Group(name="team")]
    assert [g.name for g in groups.iter(member="maria", page_size=1)] == ["team", "team"]
    assert seen == [
        {"member": "maria"},
        {"member": "maria", "limit": "1"},
        {"member": "maria", "limit": "1", "cursor": "next"},
    ]


def test_grants_list_by_role_sends_it() -> None:
    seen: list[dict[str, str]] = []
    grants = Grants(_transport(_pages("/api/v1/grants", _GRANT, seen)))
    grants.list(role="ANALYST")
    assert seen == [{"role": "ANALYST"}]
