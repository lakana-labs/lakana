"""The token window reaches what a linked person's personal token made (review of #780).

A task token minted with the token and a browser session a local link started with it stop when
the window ends, as the token itself does; a CI token acts for a service account, which is never
linked, and is untouched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ci_trust_test_helpers import add_rule, authenticate, ci_world, exchange
from sqlalchemy.orm import Session
from test_identity_task_tokens import _admin, _signed_in_with_pat, _verify

from lakana_core.auth import AllowAllAuthorization
from lakana_core.errors import AuthenticationError, PermissionDenied, SessionEndedError
from lakana_core.identity.sessions import (
    TOKEN_WINDOW_KEY,
    SessionService,
    within_token_window,
)
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import InstanceSetting, PersonalAccessToken, Principal

_ISSUER = "https://idp.example.com"


def _link(session: Session, principal_id: object, *, signed_in_ago: timedelta) -> None:
    """Link a person to the provider, with their last sign-in there a while ago.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The person.
    signed_in_ago
        How long ago they last signed in there.
    """
    row = session.get(Principal, principal_id)
    assert row is not None
    row.oidc_issuer, row.oidc_subject = _ISSUER, row.name
    row.last_provider_sign_in_at = datetime.now(UTC) - signed_in_ago
    session.commit()


def test_a_task_token_minted_with_a_linked_persons_token_ends_with_the_window(
    session: Session,
) -> None:
    admin = _admin(session)
    signed_in, _ = _signed_in_with_pat(session, admin)
    service = TaskTokenService(session, AllowAllAuthorization())
    minted = service.mint(signed_in, admin.name, environment="dev", asset_scope=[], ttl_seconds=600)
    _link(session, admin.id, signed_in_ago=timedelta(days=29))
    _verify(service, minted.token)
    _link(session, admin.id, signed_in_ago=timedelta(days=30, seconds=1))
    with pytest.raises(AuthenticationError):
        _verify(service, minted.token)


def test_a_local_link_session_ends_with_the_window(session: Session) -> None:
    admin = _admin(session)
    _, pat = _signed_in_with_pat(session, admin)
    sessions = SessionService(session, AllowAllAuthorization())
    started = sessions.start(
        admin.id,
        via="local",
        source_pat_id=pat.id,
        replacing=None,
        peer="p",
        user_agent="u",
        touched=False,
    )
    session.commit()
    _link(session, admin.id, signed_in_ago=timedelta(days=1))
    assert sessions.verify(started.cookie_value, extend=False).id == admin.id
    session.add(InstanceSetting(key=TOKEN_WINDOW_KEY, value=1, updated_at=datetime.now(UTC)))
    session.commit()
    assert sessions.list_sessions(admin, admin.name) == []
    with pytest.raises(SessionEndedError) as ended:
        sessions.verify(started.cookie_value, extend=False)
    assert ended.value.reason == "token_window"
    assert "identity provider" in str(ended.value)
    # Nor does the token start a new one.
    with pytest.raises(PermissionDenied, match="token window"):
        sessions.start(
            admin.id,
            via="local",
            source_pat_id=pat.id,
            replacing=None,
            peer="p",
            user_agent="u",
            touched=False,
        )


def test_an_unlinked_or_missing_owner_is_always_within(session: Session) -> None:
    admin = _admin(session)
    _, pat = _signed_in_with_pat(session, admin)
    assert within_token_window(session, pat, datetime.now(UTC))
    orphan = PersonalAccessToken(principal_id=uuid.uuid4())
    assert within_token_window(session, orphan, datetime.now(UTC))


def test_ci_trust_tokens_ignore_the_window(session: Session, tmp_path: Path) -> None:
    """A CI token acts for its rule's service account; a window of one day changes nothing."""
    world = ci_world(session, tmp_path)
    add_rule(session, world)
    _link(session, world.admin.id, signed_in_ago=timedelta(days=400))
    session.add(InstanceSetting(key=TOKEN_WINDOW_KEY, value=1, updated_at=datetime.now(UTC)))
    session.commit()
    issued = exchange(session)
    assert authenticate(session, issued).id == world.deployer.id
