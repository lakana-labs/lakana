"""The OIDC sign-in's flow state (`lakana_core.identity.oidc_flows`) and the identity columns."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.identity.oidc_flows import (
    FLOW_LIFETIME,
    OidcFlowStore,
    ProviderIdentity,
    principal_for,
    safe_return_to,
    sign_in_outcome,
)
from lakana_core.identity.sessions import VIAS
from lakana_core.identity.tokens import hash_secret
from lakana_core.models import AuditEventType, OidcFlow, Principal, PrincipalType

START = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


class Clock:
    """A settable clock."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        """Tell the time.

        Returns
        -------
        datetime
            The current time.
        """
        return self.now


@pytest.fixture
def clock() -> Clock:
    """Provide a clock at `START`.

    Returns
    -------
    Clock
        The clock.
    """
    return Clock()


@pytest.fixture
def store(session: Session, clock: Clock) -> OidcFlowStore:
    """Provide a flow store on the test database.

    Parameters
    ----------
    session
        The database session.
    clock
        The clock.

    Returns
    -------
    OidcFlowStore
        The store.
    """
    return OidcFlowStore(session, clock=clock)


def test_a_flow_is_called_back_once(store: OidcFlowStore, session: Session) -> None:
    opened = store.open("sign_in", "/catalog")
    session.commit()
    flow = store.called_back(opened.state, opened.browser_secret)
    assert flow is not None
    assert (flow.nonce, flow.code_verifier, flow.return_to) == (
        opened.nonce,
        opened.code_verifier,
        "/catalog",
    )
    assert flow.called_back_at == START
    assert store.called_back(opened.state, opened.browser_secret) is None


def test_a_flow_expires_after_ten_minutes(
    store: OidcFlowStore, session: Session, clock: Clock
) -> None:
    assert timedelta(minutes=10) == FLOW_LIFETIME
    late = store.open("sign_in", "/")
    soon = store.open("sign_in", "/")
    session.commit()
    clock.now = START + FLOW_LIFETIME - timedelta(seconds=1)
    assert store.called_back(soon.state, soon.browser_secret) is not None
    clock.now = START + FLOW_LIFETIME + timedelta(seconds=1)
    assert store.called_back(late.state, late.browser_secret) is None
    assert store.finish(soon.browser_secret) is None


def test_state_of_another_browser_does_not_match(store: OidcFlowStore, session: Session) -> None:
    victim = store.open("sign_in", "/")
    attacker = store.open("sign_in", "/")
    session.commit()
    assert store.called_back(attacker.state, victim.browser_secret) is None
    assert store.called_back(victim.state, attacker.browser_secret) is None
    assert store.called_back(victim.state, "") is None
    assert store.called_back(victim.state, victim.browser_secret) is not None


def test_finish_works_once(store: OidcFlowStore, session: Session, clock: Clock) -> None:
    opened = store.open("sign_in", "/")
    session.commit()
    assert store.finish(opened.browser_secret) is None  # not called back yet
    flow = store.called_back(opened.state, opened.browser_secret)
    assert flow is not None
    store.record(flow.id, {"ok": False, "reason": "not_linked"})
    clock.now += timedelta(seconds=5)
    finished = store.finish(opened.browser_secret)
    assert finished is not None
    assert finished.outcome == {"ok": False, "reason": "not_linked"}
    assert finished.finished_at == START + timedelta(seconds=5)
    assert store.finish(opened.browser_secret) is None
    assert store.finish("") is None


def test_only_hashes_of_state_and_browser_are_stored(
    store: OidcFlowStore, session: Session
) -> None:
    opened = store.open("sign_in", "/")
    session.commit()
    row = session.execute(select(OidcFlow)).scalar_one()
    assert row.state_hash == hash_secret(opened.state)
    assert row.browser_hash == hash_secret(opened.browser_secret)
    stored = " ".join(str(value) for value in vars(row).values())
    assert opened.state not in stored
    assert opened.browser_secret not in stored
    shown = repr(opened)
    for secret in (opened.state, opened.browser_secret, opened.nonce, opened.code_verifier):
        assert secret not in shown
        assert len(secret) >= 43
    assert len(opened.code_verifier) <= 128


def test_open_purges_flows_a_day_past_their_expiry(
    store: OidcFlowStore, session: Session, clock: Clock
) -> None:
    store.open("sign_in", "/")
    session.commit()
    clock.now = START + FLOW_LIFETIME + timedelta(days=1, seconds=1)
    store.open("sign_in", "/")
    session.commit()
    assert len(session.execute(select(OidcFlow)).scalars().all()) == 1
    assert store.purge(clock.now + timedelta(days=2)) == 1


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("/catalog/prod?x=1#y", "/catalog/prod?x=1#y"),
        ("/", "/"),
        ("/queries", "/queries"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example/", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("/%2F%2Fevil", "/"),
        ("/%252F%252Fevil", "/"),
        ("/%5Cevil", "/"),
        ("javascript:alert(1)", "/"),
        ("/" + "a" * 3000, "/"),
        ("/sign-in/oidc", "/"),
        ("/sign-in", "/"),
        ("/%73ign-in", "/"),
        ("/a\nb", "/"),
        ("/a​b", "/"),
        ("/a\x7fb", "/"),
        ("catalog", "/"),
        ("/catalog/../..//evil", "/"),
    ],
)
def test_return_to_is_kept_only_when_it_is_an_app_path(given: str | None, kept: str) -> None:
    assert safe_return_to(given) == kept


def test_a_kept_path_is_at_most_2048_characters() -> None:
    longest = "/" + "a" * 2047
    assert safe_return_to(longest) == longest
    assert safe_return_to(longest + "a") == "/"


def _person(session: Session, name: str) -> Principal:
    """Add a person.

    Parameters
    ----------
    session
        The database session.
    name
        The name.

    Returns
    -------
    Principal
        The row.
    """
    person = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=True)
    session.add(person)
    session.commit()
    return person


def test_one_identity_per_account(session: Session) -> None:
    first, second = _person(session, "ada"), _person(session, "bob")
    first.oidc_issuer, first.oidc_subject = "https://idp.example", "248289761001"
    first.oidc_email, first.oidc_name = "ada@example.com", "Ada"
    first.last_provider_sign_in_at = START
    session.commit()
    second.oidc_issuer, second.oidc_subject = "https://idp.example", "248289761001"
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    second.oidc_issuer, second.oidc_subject = "https://other.example", "248289761001"
    session.commit()
    assert session.get(Principal, first.id) is not None


def test_a_flow_goes_with_its_principal(session: Session, store: OidcFlowStore) -> None:
    person = _person(session, "ada")
    store.open("sign_in", "/", principal_id=person.id)
    session.commit()
    session.delete(person)
    session.commit()
    assert session.execute(select(OidcFlow)).scalars().all() == []


def test_the_oidc_names_exist() -> None:
    assert "oidc" in VIAS
    assert AuditEventType.SIGN_IN_OIDC == "SIGN_IN_OIDC"
    assert AuditEventType.SIGN_IN_OIDC_REFUSED == "SIGN_IN_OIDC_REFUSED"


@pytest.mark.parametrize(
    ("outcome", "found"),
    [
        (None, "flow_expired"),
        ({}, "flow_expired"),
        ({"ok": False, "reason": "not_linked"}, "not_linked"),
        ({"ok": False, "reason": "token_invalid"}, "token_invalid"),
        ({"ok": False, "reason": "something-else"}, "flow_expired"),
        ({"ok": "yes", "iss": "i", "sub": "s"}, "flow_expired"),
        ({"ok": True, "iss": "https://idp.example"}, "flow_expired"),
        ({"ok": True, "iss": "https://idp.example", "sub": 7}, "flow_expired"),
    ],
)
def test_an_unusable_outcome_is_a_refusal(outcome: dict[str, object] | None, found: str) -> None:
    assert sign_in_outcome(outcome) == found


def test_a_verified_outcome_is_the_providers_identity() -> None:
    outcome = {
        "ok": True,
        "iss": "https://idp.example",
        "sub": "248289761001",
        "email": "ada@example.com",
        "name": None,
        "auth_time": 1_763_629_200,
    }
    assert sign_in_outcome(outcome) == ProviderIdentity(
        issuer="https://idp.example",
        subject="248289761001",
        email="ada@example.com",
        name=None,
        auth_time=datetime.fromtimestamp(1_763_629_200, UTC),
    )
    outcome["auth_time"] = True
    found = sign_in_outcome(outcome)
    assert isinstance(found, ProviderIdentity) and found.auth_time is None


def test_a_person_is_found_by_issuer_and_subject_only(session: Session) -> None:
    ada = _person(session, "ada")
    ada.oidc_issuer, ada.oidc_subject = "https://idp.example", "248289761001"
    ada.oidc_email = "ada@example.com"
    service = Principal(
        id=uuid.uuid4(),
        type=PrincipalType.SERVICE_ACCOUNT,
        name="robot",
        is_active=True,
        oidc_issuer="https://idp.example",
        oidc_subject="robot-sub",
    )
    session.add(service)
    session.commit()

    def identity(issuer: str, subject: str) -> ProviderIdentity:
        return ProviderIdentity(
            issuer=issuer, subject=subject, email="ada@example.com", name="Ada", auth_time=None
        )

    found = principal_for(session, identity("https://idp.example", "248289761001"))
    assert found is not None and found.name == "ada"
    assert principal_for(session, identity("https://other.example", "248289761001")) is None
    assert principal_for(session, identity("https://idp.example", "someone-else")) is None
    assert principal_for(session, identity("https://idp.example", "robot-sub")) is None
