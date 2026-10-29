"""Tests for the top-level Client: construction, repr masking, and lifecycle."""

from pathlib import Path

import httpx
import pytest

from lakana._models import InstanceInfo, TaskTokenScope, Whoami
from lakana.client import Client
from lakana.errors import ConfigError


def _client_with_mock_transport(token: str = "lakana_pat_abc_supersecret") -> Client:
    """Build a `Client` whose transport never hits the network.

    Parameters
    ----------
    token
        The token to configure the client with.

    Returns
    -------
    Client
        A client backed by an `httpx.MockTransport`.
    """
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    return Client(url="http://api.example", token=token, http_client=http_client)


def test_repr_masks_the_token() -> None:
    """repr() shows the masked token, never the secret part."""
    client = _client_with_mock_transport()
    try:
        text = repr(client)
        assert "lakana_pat_abc_***" in text
        assert "supersecret" not in text
        assert "http://api.example" in text
    finally:
        client.close()


def test_repr_masks_a_task_token() -> None:
    """A task token is masked the same way as a PAT."""
    client = _client_with_mock_transport(token="lakana_task_xyz_supersecret")
    try:
        assert "lakana_task_xyz_***" in repr(client)
        assert "supersecret" not in repr(client)
    finally:
        client.close()


def test_repr_masks_an_unrecognised_token_shape() -> None:
    """A token that doesn't match the known prefixes is fully masked."""
    client = _client_with_mock_transport(token="not-a-lakana-token")
    try:
        assert "***" in repr(client)
        assert "not-a-lakana-token" not in repr(client)
    finally:
        client.close()


def test_repr_masks_a_secret_containing_underscores_and_dashes() -> None:
    """A secret with its own underscores/dashes is fully hidden, not just truncated.

    A naive `rsplit("_", 1)` mask would only drop the text after the last
    underscore, leaving most of a `secrets.token_urlsafe`-shaped secret (which
    routinely contains underscores and dashes) exposed in the repr.
    """
    token = "lakana_pat_abc123_x_y-z_secret"
    client = _client_with_mock_transport(token=token)
    try:
        text = repr(client)
        assert "lakana_pat_abc123_***" in text
        assert "x_y-z_secret" not in text
        assert "secret" not in text
    finally:
        client.close()


def test_context_manager_does_not_close_a_caller_supplied_client() -> None:
    """A caller-supplied httpx.Client is never closed by exiting the `with` block."""
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    with Client(url="http://api.example", token="lakana_pat_a_s", http_client=http_client):
        assert not http_client.is_closed
    assert not http_client.is_closed
    http_client.close()


def test_context_manager_closes_a_client_it_built_itself() -> None:
    """Without a caller-supplied client, exiting the `with` block closes the one built."""
    with Client(url="http://api.example", token="lakana_pat_a_s") as client:
        internal_client = client._transport._client
        assert not internal_client.is_closed
    assert internal_client.is_closed


def test_close_is_idempotent_enough_to_call_directly() -> None:
    """`close()` can be called directly, without a `with` block."""
    client = _client_with_mock_transport()
    client.close()


def test_construction_resolves_config_and_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no resolvable configuration, construction raises ConfigError."""
    monkeypatch.delenv("LAKANA_URL", raising=False)
    monkeypatch.delenv("LAKANA_TOKEN", raising=False)
    monkeypatch.delenv("LAKANA_PROFILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ConfigError):
        Client()


def test_whoami_parses_a_task_token_scope() -> None:
    """`Client.whoami` GETs `/api/v1/whoami` and parses the task-token scope."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/whoami"
        return httpx.Response(
            200,
            json={
                "name": "bob",
                "type": "SERVICE_ACCOUNT",
                "task_token": {"environment": "dev", "asset_scope": ["bronze.*"]},
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    with Client(url="http://x", token="lakana_pat_k_s", http_client=http) as client:
        assert client.whoami() == Whoami(
            name="bob",
            type="SERVICE_ACCOUNT",
            task_token=TaskTokenScope(environment="dev", asset_scope=["bronze.*"]),
        )


def test_whoami_parses_a_pat_caller() -> None:
    """A PAT caller has no task-token scope."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "admin", "type": "USER", "task_token": None})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    with Client(url="http://x", token="lakana_pat_k_s", http_client=http) as client:
        assert client.whoami() == Whoami(name="admin", type="USER", task_token=None)


def test_info_returns_the_instances_version() -> None:
    """`Client.info` GETs `/api/v1/info`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", "/api/v1/info")
        return httpx.Response(200, json={"version": "1.2.3"})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    with Client(url="http://x", token="lakana_pat_k_s", http_client=http) as client:
        assert client.info() == InstanceInfo(version="1.2.3")


_SEARCH_HITS: list[dict[str, str | None]] = [
    {
        "kind": "table",
        "name": "silver.orders",
        "matched": "description",
        "snippet": "Purchases",
        "namespace": "silver",
        "table": "orders",
        "table_kind": "TABLE",
        "column": None,
        "column_type": None,
        "run_id": None,
        "job": None,
        "repository": None,
        "status": None,
        "principal": None,
        "principal_type": None,
    },
    {
        "kind": "run",
        "name": "0b0e3c5e-6a7e-4f26-9d1e-0a3f0f5f6a11",
        "matched": "job",
        "snippet": None,
        "namespace": None,
        "table": None,
        "table_kind": None,
        "column": None,
        "column_type": None,
        "run_id": "0b0e3c5e-6a7e-4f26-9d1e-0a3f0f5f6a11",
        "job": "nightly",
        "repository": "analytics",
        "status": "succeeded",
        "principal": None,
        "principal_type": None,
    },
]

_SEARCH_BODY = {
    "results": _SEARCH_HITS,
    "limit": 20,
    "offset": 0,
    "has_more": True,
}


def test_search_gets_the_search_route_with_only_the_arguments_given_and_parses_it() -> None:
    """`Client.search` GETs `/environments/{env}/search`, sending `kind` only when given."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_SEARCH_BODY)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    with Client(url="http://x", token="lakana_pat_k_s", http_client=http) as client:
        page = client.search("dev", "order")
        client.search("dev", "order", kind="table", limit=5, offset=10)

    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/environments/dev/search"
    assert dict(seen[0].url.params) == {"q": "order", "limit": "20", "offset": "0"}
    assert dict(seen[1].url.params) == {
        "q": "order",
        "kind": "table",
        "limit": "5",
        "offset": "10",
    }
    assert (page.limit, page.offset, page.has_more) == (20, 0, True)
    table, run = page.results
    assert (table.kind, table.name, table.matched, table.snippet) == (
        "table",
        "silver.orders",
        "description",
        "Purchases",
    )
    assert (table.namespace, table.table, table.table_kind) == ("silver", "orders", "TABLE")
    assert (run.run_id, run.job, run.repository, run.status) == (
        "0b0e3c5e-6a7e-4f26-9d1e-0a3f0f5f6a11",
        "nightly",
        "analytics",
        "succeeded",
    )


def test_search_ignores_a_field_of_a_hit_it_does_not_know() -> None:
    """The REST contract adds fields; a client ignores the ones it does not know."""
    body = {
        **_SEARCH_BODY,
        "results": [{**hit, "added_later": "x"} for hit in _SEARCH_HITS],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    with Client(url="http://x", token="lakana_pat_k_s", http_client=http) as client:
        page = client.search("dev", "order")
    assert [hit.name for hit in page.results] == [
        "silver.orders",
        "0b0e3c5e-6a7e-4f26-9d1e-0a3f0f5f6a11",
    ]
    assert not hasattr(page.results[0], "added_later")
