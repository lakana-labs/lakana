"""Tests for the shared token-masking helper."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import NEXT_CURSOR_HEADER, HttpTransport
from lakana._models import TaskToken, Token
from lakana._tokens import mask_token
from lakana.resources.task_tokens import TaskTokens
from lakana.resources.tokens import Tokens, _token_from_json


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("lakana_pat_abc_supersecret", "lakana_pat_abc_***"),
        ("lakana_task_xyz_supersecret", "lakana_task_xyz_***"),
        ("lakana_pat_abc123_x_y-z_secret", "lakana_pat_abc123_***"),
    ],
)
def test_mask_token_shows_only_prefix_and_key_id(token: str, expected: str) -> None:
    """Recognised token shapes mask everything after the key id."""
    assert mask_token(token) == expected


def test_mask_token_never_leaks_a_secret_with_underscores_or_dashes() -> None:
    """A secret containing underscores and dashes is fully hidden."""
    masked = mask_token("lakana_pat_abc123_x_y-z_secret")
    assert "x_y-z_secret" not in masked
    assert "secret" not in masked


def test_mask_token_hides_an_unrecognised_shape_entirely() -> None:
    """A token with no recognised prefix is fully masked."""
    assert mask_token("not-a-lakana-token") == "***"


def test_mask_token_hides_a_token_missing_its_secret_segment() -> None:
    """A token that looks like a prefix + key id but has no trailing secret is fully masked.

    The pattern requires a trailing underscore after the key id (the start of
    the secret segment); without it, the shape is not confidently
    recognised, so the whole thing is masked rather than partially shown.
    """
    assert mask_token("lakana_pat_abc123") == "***"


_ROW: dict[str, Any] = {
    "key_id": "k1",
    "name": "laptop",
    "owner": "bob",
    "created_at": "2026-11-01T09:00:00Z",
    "last_used_at": None,
    "expires_at": "2027-01-01T00:00:00Z",
    "revoked_at": None,
}


def _tokens(
    pages: list[tuple[list[dict[str, Any]], str | None]], seen: list[httpx.Request]
) -> Tokens:
    """Build a `Tokens` resource whose server answers successive requests with successive pages.

    Parameters
    ----------
    pages
        The body and the `X-Next-Cursor` header (or `None`) of each answer; the last repeats.
    seen
        Where to record the requests.

    Returns
    -------
    Tokens
        The resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body, cursor = pages[min(len(seen), len(pages) - 1)]
        seen.append(request)
        headers = {} if cursor is None else {NEXT_CURSOR_HEADER: cursor}
        return httpx.Response(200, json=body, headers=headers)

    config = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=config.url)
    return Tokens(HttpTransport(config, http_client=client))


def test_a_token_reads_its_owner_and_dates() -> None:
    """The four fields of a listed token are read as names and times."""
    assert _token_from_json(_ROW) == Token(
        key_id="k1",
        name="laptop",
        last_used_at=None,
        owner="bob",
        created_at=datetime(2026, 11, 1, 9, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        revoked_at=None,
    )


def test_an_older_servers_token_reads_them_as_none() -> None:
    """A body without the new keys still parses."""
    token = _token_from_json({"key_id": "k1", "name": "laptop", "last_used_at": None})
    assert (token.owner, token.created_at, token.expires_at, token.revoked_at) == (
        None,
        None,
        None,
        None,
    )


def test_list_all_sends_owner_and_reads_the_cursor_header() -> None:
    """`list_all_page` asks `GET /api/v1/tokens` with the filters and keeps the next cursor."""
    seen: list[httpx.Request] = []
    tokens = _tokens([([_ROW], "next-1")], seen)

    page = tokens.list_all_page(owner="bob", limit=5, cursor="c0")

    assert seen[0].url.path == "/api/v1/tokens"
    assert dict(seen[0].url.params) == {"owner": "bob", "limit": "5", "cursor": "c0"}
    assert page.next_cursor == "next-1"
    assert [t.owner for t in page.items] == ["bob"]
    assert [t.key_id for t in tokens.list_all()] == ["k1"]


def test_iter_all_follows_every_page() -> None:
    """`iter_all` fetches pages until the cursor runs out."""
    seen: list[httpx.Request] = []
    second = {**_ROW, "key_id": "k2"}
    tokens = _tokens([([_ROW], "p2"), ([second], None)], seen)

    assert [t.key_id for t in tokens.iter_all(owner="bob", page_size=1)] == ["k1", "k2"]
    assert dict(seen[1].url.params) == {"owner": "bob", "limit": "1", "cursor": "p2"}


_TASK_ROW: dict[str, Any] = {
    "key_id": "t1",
    "principal": "worker-1",
    "environment": "dev",
    "asset_scope": ["gold.*"],
    "issued_at": "2026-11-01T09:00:00Z",
    "expires_at": "2026-11-01T10:00:00Z",
    "run_id": "9f3c1f4e-0000-4000-8000-000000000001",
    "job": "nightly",
    "asset": "gold.scores",
}


def _task_tokens(
    pages: list[tuple[list[dict[str, Any]], str | None]], seen: list[httpx.Request]
) -> TaskTokens:
    """Build a `TaskTokens` resource whose server answers successive requests with successive pages.

    Parameters
    ----------
    pages
        The body and the `X-Next-Cursor` header (or `None`) of each answer; the last repeats.
    seen
        Where to record the requests.

    Returns
    -------
    TaskTokens
        The resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body, cursor = pages[min(len(seen), len(pages) - 1)]
        seen.append(request)
        headers = {} if cursor is None else {NEXT_CURSOR_HEADER: cursor}
        return httpx.Response(200, json=body, headers=headers)

    config = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=config.url)
    return TaskTokens(HttpTransport(config, http_client=client))


def test_task_tokens_list_sends_environment_and_reads_the_cursor_header() -> None:
    """`list_page` asks `GET /api/v1/task-tokens` with the filters and keeps the next cursor."""
    seen: list[httpx.Request] = []
    tokens = _task_tokens([([_TASK_ROW, {**_TASK_ROW, "run_id": None}], "next-1")], seen)

    page = tokens.list_page(environment="dev", limit=5, cursor="c0")

    assert seen[0].url.path == "/api/v1/task-tokens"
    assert dict(seen[0].url.params) == {"environment": "dev", "limit": "5", "cursor": "c0"}
    assert page.next_cursor == "next-1"
    first, bare = page.items
    assert first == TaskToken(
        key_id="t1",
        principal="worker-1",
        environment="dev",
        asset_scope=["gold.*"],
        issued_at=datetime(2026, 11, 1, 9, tzinfo=UTC),
        expires_at=datetime(2026, 11, 1, 10, tzinfo=UTC),
        run_id="9f3c1f4e-0000-4000-8000-000000000001",
        job="nightly",
        asset="gold.scores",
    )
    assert bare.run_id is None
    assert [t.key_id for t in tokens.list()] == ["t1", "t1"]
    assert "environment" not in dict(seen[-1].url.params)


def test_task_tokens_iter_follows_every_page() -> None:
    """`iter` fetches pages until the cursor runs out."""
    seen: list[httpx.Request] = []
    second = {**_TASK_ROW, "key_id": "t2"}
    tokens = _task_tokens([([_TASK_ROW], "p2"), ([second], None)], seen)

    assert [t.key_id for t in tokens.iter(environment="dev", page_size=1)] == ["t1", "t2"]
    assert dict(seen[1].url.params) == {"environment": "dev", "limit": "1", "cursor": "p2"}
