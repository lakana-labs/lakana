"""Unit tests for the SDK's sign-in resources.

Covers `client.sign_in` (setup and local links), `client.invites`, `client.recovery_links`,
`client.sessions`, `client.passkeys` and `client.sign_in_settings`: the request each method
sends and the model it parses.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import (
    BrowserSession,
    IssuedLink,
    OpenInvite,
    OpenRecoveryLink,
    Passkey,
    SignInBounds,
    SignInSettings,
)
from lakana.client import Client
from lakana.resources.sign_in import (
    Invites,
    Passkeys,
    RecoveryLinks,
    Sessions,
    SignIn,
    SignInSettingsResource,
)

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_ID = "0b6f7e9e-3c39-4d55-9d3a-2a7c1d5e4f10"
_WHEN = "2026-11-08T10:00:00Z"
_AT = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
_LINK = "http://localhost:8765/sign-in/abc#s=topsecret"


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> HttpTransport:
    """Build an `HttpTransport` over `httpx.MockTransport(handler)`.

    Parameters
    ----------
    handler
        The mock transport's request handler.

    Returns
    -------
    HttpTransport
        The transport.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


def _recording(
    seen: list[httpx.Request], status: int, body: object = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a handler that records each request and answers with one response.

    Parameters
    ----------
    seen
        Where the requests are appended.
    status
        The response status.
    body
        The JSON body, or None for none.

    Returns
    -------
    Callable
        The handler.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    return handler


def _sent(request: httpx.Request) -> object:
    """Decode a request's JSON body.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    object
        The body, or None when it has none.
    """
    raw = request.read()
    return json.loads(raw) if raw else None


@pytest.mark.parametrize(
    ("method_name", "path"),
    [("setup_link", "/api/v1/sign-in/setup-link"), ("local_link", "/api/v1/sign-in/local-link")],
)
def test_sign_in_links_post_without_a_body_and_hide_the_url_in_repr(
    method_name: str, path: str
) -> None:
    """Both links are a bare POST; the link never appears in the model's repr."""
    seen: list[httpx.Request] = []
    sign_in = SignIn(_transport(_recording(seen, 200, {"url": _LINK, "expires_at": _WHEN})))

    issued = getattr(sign_in, method_name)()

    assert (seen[0].method, seen[0].url.path, _sent(seen[0])) == ("POST", path, None)
    assert issued == IssuedLink(url=_LINK, expires_at=_AT)
    assert "topsecret" not in repr(issued)


def test_invites_create_sends_the_name_and_display_name_and_parses_the_link() -> None:
    """`invites.create` POSTs `{name, display_name}`; the link is kept out of repr."""
    seen: list[httpx.Request] = []
    answer = {
        "id": _ID,
        "name": "alice",
        "display_name": "Alice A.",
        "url": _LINK,
        "expires_at": _WHEN,
    }
    invite = Invites(_transport(_recording(seen, 201, answer))).create(
        "alice", display_name="Alice A."
    )

    assert (seen[0].method, seen[0].url.path) == ("POST", "/api/v1/invites")
    assert _sent(seen[0]) == {"name": "alice", "display_name": "Alice A."}
    assert (invite.id, invite.name, invite.display_name, invite.url) == (
        _ID,
        "alice",
        "Alice A.",
        _LINK,
    )
    assert invite.expires_at == _AT
    assert "topsecret" not in repr(invite)


def test_invites_create_without_a_display_name_sends_only_the_name() -> None:
    """No `display_name` key when none is given."""
    seen: list[httpx.Request] = []
    answer = {"id": _ID, "name": "bob", "display_name": None, "url": _LINK, "expires_at": _WHEN}
    Invites(_transport(_recording(seen, 201, answer))).create("bob")
    assert _sent(seen[0]) == {"name": "bob"}


def test_invites_list_and_withdraw() -> None:
    """`list` GETs the open invites; `withdraw` DELETEs one by id."""
    seen: list[httpx.Request] = []
    row = {
        "id": _ID,
        "name": "alice",
        "display_name": None,
        "created_at": _WHEN,
        "expires_at": _WHEN,
    }
    invites = Invites(_transport(_recording(seen, 200, [row])))
    assert invites.list() == [
        OpenInvite(id=_ID, name="alice", display_name=None, created_at=_AT, expires_at=_AT)
    ]
    Invites(_transport(_recording(seen, 204))).withdraw(_ID)
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/v1/invites"),
        ("DELETE", f"/api/v1/invites/{_ID}"),
    ]


def test_recovery_links_create_list_and_withdraw() -> None:
    """Issue for a principal, list without links, withdraw by id."""
    seen: list[httpx.Request] = []
    issued = RecoveryLinks(
        _transport(
            _recording(seen, 201, {"id": _ID, "name": "bob", "url": _LINK, "expires_at": _WHEN})
        )
    ).create("bob")
    listed = RecoveryLinks(
        _transport(
            _recording(
                seen,
                200,
                [{"id": _ID, "name": "bob", "created_at": _WHEN, "expires_at": _WHEN}],
            )
        )
    ).list()
    RecoveryLinks(_transport(_recording(seen, 204))).withdraw(_ID)

    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/api/v1/principals/bob/recovery-link"),
        ("GET", "/api/v1/recovery-links"),
        ("DELETE", f"/api/v1/recovery-links/{_ID}"),
    ]
    assert (issued.id, issued.name, issued.url, issued.expires_at) == (_ID, "bob", _LINK, _AT)
    assert "topsecret" not in repr(issued)
    assert listed == [OpenRecoveryLink(id=_ID, name="bob", created_at=_AT, expires_at=_AT)]


def test_sessions_list_and_end_all() -> None:
    """A person's sessions are listed and ended under the principal."""
    seen: list[httpx.Request] = []
    row = {
        "id": _ID,
        "via": "passkey",
        "created_at": _WHEN,
        "last_seen_at": _WHEN,
        "absolute_expires_at": _WHEN,
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Firefox/158.0",
        "browser": "Firefox 158 on Linux",
        "peer": "127.0.0.1",
        "current": False,
    }
    listed = Sessions(_transport(_recording(seen, 200, [row]))).list("bob")
    Sessions(_transport(_recording(seen, 204))).end_all("bob")

    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/v1/principals/bob/sessions"),
        ("DELETE", "/api/v1/principals/bob/sessions"),
    ]
    assert listed == [
        BrowserSession(
            id=_ID,
            via="passkey",
            created_at=_AT,
            last_seen_at=_AT,
            absolute_expires_at=_AT,
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Firefox/158.0",
            browser="Firefox 158 on Linux",
            peer="127.0.0.1",
            current=False,
        )
    ]


def test_sessions_from_a_server_without_browser_names_show_the_user_agent() -> None:
    """A server before `browser` existed: the raw user agent stands in for it."""
    seen: list[httpx.Request] = []
    row = {
        "id": _ID,
        "via": "passkey",
        "created_at": _WHEN,
        "last_seen_at": _WHEN,
        "absolute_expires_at": _WHEN,
        "user_agent": "curl/8.5.0",
        "peer": "127.0.0.1",
        "current": False,
    }
    (listed,) = Sessions(_transport(_recording(seen, 200, [row]))).list("bob")

    assert listed.browser == "curl/8.5.0"


def test_passkeys_list_and_remove() -> None:
    """A person's passkeys are listed and removed one by one."""
    seen: list[httpx.Request] = []
    row = {
        "id": _ID,
        "name": "Laptop",
        "created_at": _WHEN,
        "last_used_at": None,
        "backup_eligible": True,
        "disabled_reason": None,
        "usable_here": True,
    }
    listed = Passkeys(_transport(_recording(seen, 200, [row]))).list("bob")
    Passkeys(_transport(_recording(seen, 204))).remove("bob", _ID)

    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/v1/principals/bob/passkeys"),
        ("DELETE", f"/api/v1/principals/bob/passkeys/{_ID}"),
    ]
    assert listed == [
        Passkey(
            id=_ID,
            name="Laptop",
            created_at=_AT,
            last_used_at=None,
            backup_eligible=True,
            disabled_reason=None,
            usable_here=True,
        )
    ]


_SETTINGS = {
    "idle_timeout_minutes": 720,
    "absolute_lifetime_minutes": 10080,
    "bounds": {
        "idle_timeout_minutes_min": 15,
        "idle_timeout_minutes_max": 1440,
        "absolute_lifetime_minutes_min": 60,
        "absolute_lifetime_minutes_max": 43200,
    },
}
_PARSED = SignInSettings(
    idle_timeout_minutes=720,
    absolute_lifetime_minutes=10080,
    bounds=SignInBounds(
        idle_timeout_minutes_min=15,
        idle_timeout_minutes_max=1440,
        absolute_lifetime_minutes_min=60,
        absolute_lifetime_minutes_max=43200,
    ),
)


def test_sign_in_settings_get_and_update_in_minutes() -> None:
    """`get` reads the lengths; `update` PATCHes only the fields given, in minutes."""
    seen: list[httpx.Request] = []
    resource = SignInSettingsResource(_transport(_recording(seen, 200, _SETTINGS)))

    assert resource.get() == _PARSED
    assert resource.update(idle_timeout_minutes=60) == _PARSED
    resource.update(absolute_lifetime_minutes=120)

    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/v1/settings/sign-in"),
        ("PATCH", "/api/v1/settings/sign-in"),
        ("PATCH", "/api/v1/settings/sign-in"),
    ]
    assert _sent(seen[1]) == {"idle_timeout_minutes": 60}
    assert _sent(seen[2]) == {"absolute_lifetime_minutes": 120}


def test_the_client_exposes_every_sign_in_resource() -> None:
    """The six resources hang off `Client` under the names the docs use."""
    with Client(url="http://x", token="lakana_pat_k_s") as client:
        assert isinstance(client.sign_in, SignIn)
        assert isinstance(client.invites, Invites)
        assert isinstance(client.recovery_links, RecoveryLinks)
        assert isinstance(client.sessions, Sessions)
        assert isinstance(client.passkeys, Passkeys)
        assert isinstance(client.sign_in_settings, SignInSettingsResource)
