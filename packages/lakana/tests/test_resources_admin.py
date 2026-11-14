"""Unit tests for the SDK's admin resources.

Covers principals, groups, tokens, task tokens, grants, row filters,
column masks, secrets, and audit events.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import (
    AuditEvent,
    AuditEventPage,
    ColumnMask,
    Grant,
    Group,
    IssuedToken,
    MintedTaskToken,
    Principal,
    RowFilter,
    Secret,
    SecretCreate,
    SubjectPrincipal,
    Token,
)
from lakana.client import Client
from lakana.errors import PermissionDenied
from lakana.resources.audit_events import AuditEvents
from lakana.resources.column_masks import ColumnMasks
from lakana.resources.grants import Grants
from lakana.resources.groups import Groups
from lakana.resources.principals import Principals
from lakana.resources.row_filters import RowFilters
from lakana.resources.secrets import Secrets
from lakana.resources.task_tokens import TaskTokens
from lakana.resources.tokens import Tokens

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> HttpTransport:
    """Build an `HttpTransport` whose own `httpx.Client` uses a mock transport.

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


# --- Principals --------------------------------------------------------


def test_principals_create_sends_the_name_and_type() -> None:
    """`principals.create` POSTs `{name, type}`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(201, json={"name": "bob", "type": "USER", "is_active": True})

    principals = Principals(_transport(handler))
    result = principals.create("bob", "USER")

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/principals"
    assert seen["body"] == b'{"name":"bob","type":"USER"}'
    assert result == Principal(name="bob", type="USER", is_active=True)


def test_principals_get_sends_a_get_to_the_named_principal() -> None:
    """`principals.get` GETs `/api/v1/principals/{name}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/principals/bob"
        return httpx.Response(200, json={"name": "bob", "type": "USER", "is_active": True})

    principals = Principals(_transport(handler))
    result = principals.get("bob")

    assert result.name == "bob"


def test_principals_list_parses_every_item() -> None:
    """`principals.list` GETs the collection and parses each item."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/principals"
        return httpx.Response(200, json=[{"name": "admin", "type": "USER", "is_active": True}])

    principals = Principals(_transport(handler))
    result = principals.list()

    assert result == [Principal(name="admin", type="USER", is_active=True)]


def test_principals_deactivate_sends_a_delete() -> None:
    """`principals.deactivate` DELETEs `/api/v1/principals/{name}`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    principals = Principals(_transport(handler))
    principals.deactivate("bob")

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/api/v1/principals/bob"


# --- Groups --------------------------------------------------------------


def test_groups_create_sends_the_name() -> None:
    """`groups.create` POSTs `{name}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/groups"
        assert request.read() == b'{"name":"engineers"}'
        return httpx.Response(201, json={"name": "engineers"})

    groups = Groups(_transport(handler))
    result = groups.create("engineers")

    assert result == Group(name="engineers")


def test_groups_list_parses_every_item() -> None:
    """`groups.list` GETs the collection and parses each item."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/groups"
        return httpx.Response(200, json=[{"name": "engineers"}])

    groups = Groups(_transport(handler))
    result = groups.list()

    assert result == [Group(name="engineers")]


def test_groups_list_members_parses_each_principal() -> None:
    """`groups.list_members` GETs `/api/v1/groups/{name}/members` and parses principals."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/groups/engineers/members"
        return httpx.Response(200, json=[{"name": "bob", "type": "USER", "is_active": True}])

    result = Groups(_transport(handler)).list_members("engineers")

    assert result == [Principal(name="bob", type="USER", is_active=True)]


def test_groups_delete_sends_a_delete() -> None:
    """`groups.delete` DELETEs `/api/v1/groups/{name}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/groups/engineers"
        return httpx.Response(204)

    groups = Groups(_transport(handler))
    groups.delete("engineers")


def test_groups_add_member_sends_a_put() -> None:
    """`groups.add_member` PUTs `/api/v1/groups/{name}/members/{principal_name}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/v1/groups/engineers/members/bob"
        return httpx.Response(204)

    groups = Groups(_transport(handler))
    groups.add_member("engineers", "bob")


def test_groups_remove_member_sends_a_delete() -> None:
    """`groups.remove_member` DELETEs `/api/v1/groups/{name}/members/{principal_name}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/groups/engineers/members/bob"
        return httpx.Response(204)

    groups = Groups(_transport(handler))
    groups.remove_member("engineers", "bob")


# --- Tokens (PATs) ---------------------------------------------------------


def test_tokens_issue_sends_the_label_and_returns_the_full_token() -> None:
    """`tokens.issue` POSTs `.../principals/{name}/tokens` with `{name}`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            201, json={"key_id": "k1", "name": "laptop", "token": "lakana_pat_k1_supersecret"}
        )

    tokens = Tokens(_transport(handler))
    result = tokens.issue("bob", "laptop")

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/principals/bob/tokens"
    assert seen["body"] == b'{"name":"laptop"}'
    assert result == IssuedToken(key_id="k1", name="laptop", token="lakana_pat_k1_supersecret")


def test_issued_token_repr_never_includes_the_secret() -> None:
    """`repr(IssuedToken(...))` never includes the token's secret value."""
    issued = IssuedToken(key_id="k1", name="laptop", token="lakana_pat_k1_supersecret")

    assert "supersecret" not in repr(issued)


def test_tokens_list_parses_every_item_including_last_used_at() -> None:
    """`tokens.list` GETs `.../principals/{name}/tokens` and parses `last_used_at`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/principals/bob/tokens"
        return httpx.Response(
            200,
            json=[
                {"key_id": "k1", "name": "laptop", "last_used_at": "2024-01-01T00:00:00+00:00"},
                {"key_id": "k2", "name": "ci", "last_used_at": None},
            ],
        )

    tokens = Tokens(_transport(handler))
    result = tokens.list("bob")

    assert result == [
        Token(key_id="k1", name="laptop", last_used_at=datetime(2024, 1, 1, tzinfo=UTC)),
        Token(key_id="k2", name="ci", last_used_at=None),
    ]


def test_tokens_revoke_sends_a_delete() -> None:
    """`tokens.revoke` DELETEs `.../principals/{name}/tokens/{key_id}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/principals/bob/tokens/k1"
        return httpx.Response(204)

    tokens = Tokens(_transport(handler))
    tokens.revoke("bob", "k1")


# --- Task tokens ------------------------------------------------------------


def test_task_tokens_mint_sends_the_expected_body() -> None:
    """`task_tokens.mint` POSTs `/api/v1/task-tokens` with the mint request."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(201, json={"key_id": "t1", "token": "lakana_task_t1_supersecret"})

    task_tokens = TaskTokens(_transport(handler))
    result = task_tokens.mint("bob", "dev", ["bronze.*"], 300)

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/task-tokens"
    assert seen["body"] == (
        b'{"principal":"bob","environment":"dev","asset_scope":["bronze.*"],"ttl_seconds":300}'
    )
    assert result == MintedTaskToken(key_id="t1", token="lakana_task_t1_supersecret")


def test_task_tokens_mint_for_another_principal_surfaces_permission_denied() -> None:
    """A 403 from the mint rule reaches the caller as `PermissionDenied`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"detail": "may mint task tokens only for itself", "type": "PermissionDenied"}
        )

    with pytest.raises(PermissionDenied, match="only for itself"):
        TaskTokens(_transport(handler)).mint("bob", "dev", ["bronze.*"], 300)


def test_task_tokens_revoke_sends_a_delete() -> None:
    """`task_tokens.revoke` DELETEs `/api/v1/task-tokens/{key_id}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/task-tokens/t1"
        return httpx.Response(204)

    TaskTokens(_transport(handler)).revoke("t1")


def test_minted_task_token_repr_never_includes_the_secret() -> None:
    """`repr(MintedTaskToken(...))` never includes the token's secret value."""
    minted = MintedTaskToken(key_id="t1", token="lakana_task_t1_supersecret")

    assert "supersecret" not in repr(minted)


# --- Grants -----------------------------------------------------------------


def test_grants_grant_sends_the_expected_body() -> None:
    """`grants.grant` POSTs a grant, including unset fields as explicit nulls."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            201,
            json={
                "id": "11111111-1111-1111-1111-111111111111",
                "principal_id": "22222222-2222-2222-2222-222222222222",
                "group_id": None,
                "role": "VIEWER",
                "action": None,
                "kind": "ENVIRONMENT",
                "environment": "dev",
                "namespace": None,
                "table": None,
                "granted_by": "33333333-3333-3333-3333-333333333333",
                "granted_at": "2024-01-01T00:00:00+00:00",
            },
        )

    grants = Grants(_transport(handler))
    result = grants.grant(principal="bob", role="VIEWER", kind="ENVIRONMENT", environment="dev")

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/grants"
    assert seen["body"] == (
        b'{"principal":"bob","group":null,"role":"VIEWER","action":null,'
        b'"kind":"ENVIRONMENT","environment":"dev","namespace":null,"table":null}'
    )
    assert result == Grant(
        id="11111111-1111-1111-1111-111111111111",
        principal_id="22222222-2222-2222-2222-222222222222",
        group_id=None,
        role="VIEWER",
        action=None,
        kind="ENVIRONMENT",
        environment="dev",
        namespace=None,
        table=None,
        granted_by="33333333-3333-3333-3333-333333333333",
        granted_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_grants_list_sends_only_the_given_filters() -> None:
    """`grants.list` omits unset filters from the query string entirely."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    grants = Grants(_transport(handler))
    grants.list(principal_name="bob")

    assert seen["path"] == "/api/v1/grants"
    assert seen["params"] == {"principal_name": "bob"}


def test_grants_revoke_sends_a_delete() -> None:
    """`grants.revoke` DELETEs `/api/v1/grants/{grant_id}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/grants/g1"
        return httpx.Response(204)

    grants = Grants(_transport(handler))
    grants.revoke("g1")


# --- Row filters -------------------------------------------------------------


def test_row_filters_create_sends_the_expected_body() -> None:
    """`row_filters.create` POSTs the expected body."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            201,
            json={
                "id": "rf1",
                "principal_id": "p1",
                "group_id": None,
                "environment": "dev",
                "namespace": "gold",
                "table": "customers",
                "expression": "id > 1",
                "created_by": "admin1",
                "created_at": "2024-01-01T00:00:00+00:00",
            },
        )

    row_filters = RowFilters(_transport(handler))
    result = row_filters.create(
        principal="bob", environment="dev", namespace="gold", table="customers", expression="id > 1"
    )

    assert seen["path"] == "/api/v1/row-filters"
    assert seen["body"] == (
        b'{"principal":"bob","group":null,"environment":"dev","namespace":"gold",'
        b'"table":"customers","expression":"id > 1"}'
    )
    assert result == RowFilter(
        id="rf1",
        principal_id="p1",
        group_id=None,
        environment="dev",
        namespace="gold",
        table="customers",
        expression="id > 1",
        created_by="admin1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_row_filters_list_sends_only_the_given_filters() -> None:
    """`row_filters.list` omits unset filters from the query string."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    row_filters = RowFilters(_transport(handler))
    row_filters.list(environment="dev", namespace="gold", table="customers")

    assert seen["params"] == {"environment": "dev", "namespace": "gold", "table": "customers"}


def test_a_listed_policy_carries_bound_and_one_from_an_older_server_has_none() -> None:
    """`bound` is read from the answer; a server before this release sends none: `None`."""
    base = {
        "principal_id": "p1",
        "group_id": None,
        "environment": "dev",
        "namespace": "gold",
        "table": "customers",
        "expression": "id > 1",
        "created_by": "admin1",
        "created_at": "2024-01-01T00:00:00+00:00",
    }
    rows = [{"id": "a", **base, "bound": False}, {"id": "b", **base}]
    masks = [{**row, "column": "ssn"} for row in rows]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=masks if "column-masks" in request.url.path else rows)

    transport = _transport(handler)

    assert [f.bound for f in RowFilters(transport).list(environment="dev")] == [False, None]
    assert [m.bound for m in ColumnMasks(transport).list(environment="dev")] == [False, None]


def test_row_filters_revoke_sends_a_delete() -> None:
    """`row_filters.revoke` DELETEs `/api/v1/row-filters/{row_filter_id}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/row-filters/rf1"
        return httpx.Response(204)

    row_filters = RowFilters(_transport(handler))
    row_filters.revoke("rf1")


# --- Column masks -------------------------------------------------------------


def test_column_masks_create_sends_the_expected_body() -> None:
    """`column_masks.create` POSTs the expected body, including `column`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            201,
            json={
                "id": "cm1",
                "principal_id": "p1",
                "group_id": None,
                "environment": "dev",
                "namespace": "gold",
                "table": "customers",
                "column": "ssn",
                "expression": "NULL",
                "created_by": "admin1",
                "created_at": "2024-01-01T00:00:00+00:00",
            },
        )

    column_masks = ColumnMasks(_transport(handler))
    result = column_masks.create(
        principal="bob",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )

    assert seen["path"] == "/api/v1/column-masks"
    assert seen["body"] == (
        b'{"principal":"bob","group":null,"environment":"dev","namespace":"gold",'
        b'"table":"customers","column":"ssn","expression":"NULL"}'
    )
    assert result == ColumnMask(
        id="cm1",
        principal_id="p1",
        group_id=None,
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
        created_by="admin1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_column_masks_list_sends_only_the_given_filters() -> None:
    """`column_masks.list` omits unset filters from the query string."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    column_masks = ColumnMasks(_transport(handler))
    column_masks.list(group_name="admins")

    assert seen["params"] == {"group_name": "admins"}


def test_column_masks_revoke_sends_a_delete() -> None:
    """`column_masks.revoke` DELETEs `/api/v1/column-masks/{column_mask_id}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/column-masks/cm1"
        return httpx.Response(204)

    column_masks = ColumnMasks(_transport(handler))
    column_masks.revoke("cm1")


# --- Secrets -------------------------------------------------------------


def test_secret_create_repr_never_includes_the_plaintext_values() -> None:
    """`repr(SecretCreate(...))` never includes the plaintext values."""
    create = SecretCreate(name="s3-creds", values={"access_key_id": "AKIA-super-secret"})

    assert "AKIA-super-secret" not in repr(create)


def test_secrets_create_sends_the_name_and_values() -> None:
    """`secrets.create` POSTs `.../environments/{environment_name}/secrets`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            201,
            json={
                "id": "s1",
                "name": "s3-creds",
                "environment": "dev",
                "created_by": "admin1",
                "created_at": "2024-01-01T00:00:00+00:00",
                "format_version": 2,
                "key_id": "env:0123456789abcdef",
            },
        )

    secrets = Secrets(_transport(handler))
    result = secrets.create("dev", "s3-creds", {"access_key_id": "AKIA123"})

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/environments/dev/secrets"
    assert seen["body"] == b'{"name":"s3-creds","values":{"access_key_id":"AKIA123"}}'
    assert result == Secret(
        id="s1",
        name="s3-creds",
        environment="dev",
        created_by="admin1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        format_version=2,
        key_id="env:0123456789abcdef",
    )


def test_secrets_list_parses_every_item() -> None:
    """`secrets.list` GETs `.../environments/{environment_name}/secrets`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/environments/dev/secrets"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "s1",
                    "name": "s3-creds",
                    "environment": "dev",
                    "created_by": "admin1",
                    "created_at": "2024-01-01T00:00:00+00:00",
                    "format_version": 1,
                    "key_id": "env",
                }
            ],
        )

    secrets = Secrets(_transport(handler))
    result = secrets.list("dev")

    assert result[0].name == "s3-creds"
    assert (result[0].format_version, result[0].key_id) == (1, "env")


def test_secrets_rotate_sends_the_new_values() -> None:
    """`secrets.rotate` PATCHes `.../secrets/{secret_name}` with the new values."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "id": "s1",
                "name": "s3-creds",
                "environment": "dev",
                "created_by": "admin1",
                "created_at": "2024-01-01T00:00:00+00:00",
                "format_version": 2,
                "key_id": "env:0123456789abcdef",
            },
        )

    secrets = Secrets(_transport(handler))
    result = secrets.rotate("dev", "s3-creds", {"access_key_id": "NEWKEY"})

    assert seen["method"] == "PATCH"
    assert seen["path"] == "/api/v1/environments/dev/secrets/s3-creds"
    assert seen["body"] == b'{"values":{"access_key_id":"NEWKEY"}}'
    assert result.name == "s3-creds"


@pytest.mark.parametrize("call", ["create", "list", "rotate"])
def test_secrets_read_a_response_from_a_server_that_predates_the_format_fields(call: str) -> None:
    """An older server sends no `format_version` or `key_id`: the SDK reads `1` and `env`.

    Create and rotate must not fail after the server has already written.
    """
    body = {
        "id": "s1",
        "name": "s3-creds",
        "environment": "dev",
        "created_by": "admin1",
        "created_at": "2024-01-01T00:00:00+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[body] if call == "list" else body)

    secrets = Secrets(_transport(handler))
    if call == "create":
        result = secrets.create("dev", "s3-creds", {"k": "v"})
    elif call == "rotate":
        result = secrets.rotate("dev", "s3-creds", {"k": "v"})
    else:
        result = secrets.list("dev")[0]

    assert (result.format_version, result.key_id) == (1, "env")


def test_secrets_revoke_sends_a_delete() -> None:
    """`secrets.revoke` DELETEs `.../secrets/{secret_name}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/environments/dev/secrets/s3-creds"
        return httpx.Response(204)

    secrets = Secrets(_transport(handler))
    secrets.revoke("dev", "s3-creds")


# --- Audit events -------------------------------------------------------------


def _event_json(event_id: str) -> dict[str, Any]:
    """Build an `AuditEventOut` JSON body for a mocked response.

    Parameters
    ----------
    event_id
        The event's id.

    Returns
    -------
    dict of str to Any
        A `TABLE_APPENDED` event body with that id.
    """
    return {
        "id": event_id,
        "occurred_at": "2024-01-01T00:00:00+00:00",
        "principal_id": "p1",
        "event_type": "TABLE_APPENDED",
        "securable_kind": "TABLE",
        "securable_environment": "dev",
        "securable_namespace": "bronze",
        "securable_table": "sales",
        "detail": {"rows": 3},
    }


def test_audit_events_list_sends_the_default_limit_and_no_other_filters() -> None:
    """`audit_events.list` always sends `limit`, omitting unset filters."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    audit_events = AuditEvents(_transport(handler))
    audit_events.list()

    assert seen["path"] == "/api/v1/audit-events"
    assert seen["params"] == {"limit": "100"}


def test_audit_events_list_sends_given_filters_and_isoformats_datetimes() -> None:
    """`audit_events.list` sends every given filter, with `since`/`until` isoformatted."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [_event_json("e1")], "next_cursor": "c1"})

    audit_events = AuditEvents(_transport(handler))
    since = datetime(2024, 1, 1, tzinfo=UTC)
    until = datetime(2024, 6, 1, tzinfo=UTC)
    result = audit_events.list(
        event_type="TABLE_APPENDED",
        environment="dev",
        principal_id="p1",
        since=since,
        until=until,
        limit=10,
        cursor="c0",
    )

    assert seen["params"] == {
        "event_type": "TABLE_APPENDED",
        "environment": "dev",
        "principal_id": "p1",
        "since": since.isoformat(),
        "until": until.isoformat(),
        "limit": "10",
        "cursor": "c0",
    }
    assert result == AuditEventPage(
        items=[
            AuditEvent(
                id="e1",
                occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
                principal_id="p1",
                event_type="TABLE_APPENDED",
                securable_kind="TABLE",
                securable_environment="dev",
                securable_namespace="bronze",
                securable_table="sales",
                detail={"rows": 3},
            )
        ],
        next_cursor="c1",
    )


def test_audit_events_iter_follows_next_cursor_until_exhausted() -> None:
    """`audit_events.iter` requests page after page, passing each `next_cursor` back."""
    pages = {
        None: {"items": [_event_json("e1"), _event_json("e2")], "next_cursor": "c1"},
        "c1": {"items": [_event_json("e3")], "next_cursor": None},
    }
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        return httpx.Response(200, json=pages[params.get("cursor")])

    events = AuditEvents(_transport(handler)).iter(event_type="TABLE_APPENDED", page_size=2)

    assert [e.id for e in events] == ["e1", "e2", "e3"]
    assert seen == [
        {"event_type": "TABLE_APPENDED", "limit": "2"},
        {"event_type": "TABLE_APPENDED", "limit": "2", "cursor": "c1"},
    ]


# --- Client wiring -----------------------------------------------------


def test_client_exposes_the_admin_resources() -> None:
    """`Client` wires up every admin-resource attribute named in the brief."""
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    client = Client(url="http://api.example", token="lakana_pat_a_s", http_client=http_client)

    assert isinstance(client.principals, Principals)
    assert isinstance(client.groups, Groups)
    assert isinstance(client.tokens, Tokens)
    assert isinstance(client.task_tokens, TaskTokens)
    assert isinstance(client.grants, Grants)
    assert isinstance(client.row_filters, RowFilters)
    assert isinstance(client.column_masks, ColumnMasks)
    assert isinstance(client.secrets, Secrets)
    assert isinstance(client.audit_events, AuditEvents)


def test_secrets_create_with_a_use_sends_it_and_reads_it_back() -> None:
    """`secrets.create(kind=..., target=...)` adds the use to the body; none leaves it out."""
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return httpx.Response(
            201,
            json={
                "id": "s1",
                "name": "pypi",
                "environment": "dev",
                "created_by": "admin1",
                "created_at": "2024-01-01T00:00:00+00:00",
                "format_version": 2,
                "key_id": "env:0123456789abcdef",
                "use": "package_index",
                "use_target": "corp",
                "use_target_url": "https://corp.example.org/simple",
            },
        )

    secrets = Secrets(_transport(handler))
    bound = secrets.create("dev", "pypi", {"password": "pw"}, kind="package_index", target="corp")
    secrets.create("dev", "pypi", {"password": "pw"})

    assert json.loads(bodies[0])["use"] == {"kind": "package_index", "target": "corp"}
    assert "use" not in json.loads(bodies[1])
    assert (bound.use, bound.use_target) == ("package_index", "corp")
    assert bound.use_target_url == "https://corp.example.org/simple"


def test_sdk_reads_the_subject_and_tolerates_an_older_server() -> None:
    """The three models read `principal` and `group`; an older answer leaves them `None`."""
    who = {"id": "p1", "name": "kim.schulz", "principal_type": "user"}
    grant = {
        "id": "g1",
        "principal_id": "p1",
        "group_id": None,
        "role": "VIEWER",
        "action": None,
        "kind": "ENVIRONMENT",
        "environment": "dev",
        "namespace": None,
        "table": None,
        "granted_by": "a1",
        "granted_at": "2024-01-01T00:00:00+00:00",
    }
    policy = {
        "principal_id": None,
        "group_id": "g9",
        "environment": "dev",
        "namespace": "gold",
        "table": "customers",
        "expression": "id > 1",
        "created_by": "a1",
        "created_at": "2024-01-01T00:00:00+00:00",
    }
    named = [
        {**grant, "principal": who, "group": None},
        {**policy, "id": "rf1", "principal": None, "group": "analysts"},
        {**policy, "id": "cm1", "column": "ssn", "principal": None, "group": "analysts"},
    ]
    older = [grant, {**policy, "id": "rf1"}, {**policy, "id": "cm1", "column": "ssn"}]

    def read(answers: list[dict[str, Any]]) -> tuple[Grant, RowFilter, ColumnMask]:
        def handler(request: httpx.Request) -> httpx.Response:
            index = {"grants": 0, "row-filters": 1, "column-masks": 2}[
                request.url.path.split("/")[-1]
            ]
            return httpx.Response(200, json=[answers[index]])

        transport = _transport(handler)
        return (
            Grants(transport).list()[0],
            RowFilters(transport).list(environment="dev", namespace="gold", table="customers")[0],
            ColumnMasks(transport).list(environment="dev", namespace="gold", table="customers")[0],
        )

    grant_read, filter_read, mask_read = read(named)
    assert grant_read.principal == SubjectPrincipal(
        id="p1", name="kim.schulz", principal_type="user"
    )
    assert grant_read.group is None
    assert (filter_read.principal, filter_read.group) == (None, "analysts")
    assert (mask_read.principal, mask_read.group) == (None, "analysts")
    assert all((item.principal, item.group) == (None, None) for item in read(older))
    team = {**grant, "principal_id": None, "group_id": "g9", "principal": None, "group": "analysts"}
    team_read = read([team, *named[1:]])[0]
    assert (team_read.principal, team_read.group) == (None, "analysts")
