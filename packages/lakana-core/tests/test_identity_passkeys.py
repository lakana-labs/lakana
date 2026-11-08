"""Tests for passkeys (`lakana_core.identity.passkeys`): WebAuthn ceremonies and step-up.

Every attestation and assertion comes from a software authenticator
(`scripts.test_support.webauthn_test_helpers`) and is verified by the real WebAuthn library.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from scripts.test_support.webauthn_test_helpers import SoftAuthenticator, b64url, unb64url
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import ORMExecuteState, Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.bootstrap import bootstrap_if_needed, ensure_bootstrap_admin_grant
from lakana_core.errors import (
    AlreadyExistsError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PasskeyRefused,
    PermissionDenied,
    SessionEndedError,
    StepUpRequired,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identity.notices import NoticeService
from lakana_core.identity.passkeys import (
    CHALLENGE_LIFETIME,
    FIRST_PASSKEY_WINDOW,
    PasskeyService,
    sign_challenge,
)
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.sessions import SessionService, StartedSession
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    BrowserSession,
    Passkey,
    Principal,
    PrincipalType,
    SignInNotice,
    WebauthnChallenge,
)

START = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
ORIGIN = "http://localhost:8000"
RP_ID = "localhost"


class _Clock:
    """A settable clock for the services."""

    def __init__(self) -> None:
        """Start at `START`."""
        self.now = START

    def __call__(self) -> datetime:
        """Read the clock.

        Returns
        -------
        datetime
            The time set.
        """
        return self.now


@dataclass
class Signed:
    """A person signed in to a browser session.

    Parameters
    ----------
    started
        The started session.
    row
        The person.
    """

    started: StartedSession
    row: Principal


class World:
    """The services and people of one test.

    Parameters
    ----------
    session
        Database session.
    clock
        The clock.
    """

    def __init__(self, session: Session, clock: _Clock) -> None:
        self.session = session
        self.clock = clock
        bootstrap_if_needed(session, "admin", key_id="opkey", secret="opsecret")
        self.authz = GrantAuthorization(session)
        self.sessions = SessionService(session, self.authz, clock=clock)
        self.passkeys = self.service()
        self.admin = session.execute(
            select(Principal).where(Principal.name == "admin")
        ).scalar_one()

    def service(self, *, rp_id: str = RP_ID, origin: str = ORIGIN) -> PasskeyService:
        """Build a passkey service for a public address.

        Parameters
        ----------
        rp_id
            The relying-party id.
        origin
            The origin.

        Returns
        -------
        PasskeyService
            The service.
        """
        return PasskeyService(
            self.session,
            self.authz,
            self.sessions,
            NoticeService(self.session, clock=self.clock),
            rp_id=rp_id,
            origin=origin,
            clock=self.clock,
        )

    def person(self, name: str, *, admin: bool = False) -> Principal:
        """Insert a person, optionally an instance administrator.

        Parameters
        ----------
        name
            The name.
        admin
            Whether to grant ADMIN at instance scope.

        Returns
        -------
        Principal
            The row.
        """
        row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=True)
        self.session.add(row)
        self.session.commit()
        if admin:
            ensure_bootstrap_admin_grant(
                self.session,
                PrincipalRef(id=row.id, name=row.name, type=row.type),
                AllowAllAuthorization(),
            )
        return row

    def start(self, row: Principal, *, via: str = "setup", touched: bool = False) -> Signed:
        """Start a browser session for a person.

        Parameters
        ----------
        row
            The person.
        via
            How the session began.
        touched
            Whether it starts with a passkey touch.

        Returns
        -------
        Signed
            The session.
        """
        started = self.sessions.start(
            row.id, via=via, replacing=None, peer="p", user_agent="u", touched=touched
        )
        self.session.commit()
        return Signed(started, row)

    def ref(self, signed: Signed) -> PrincipalRef:
        """Authenticate with a session, as a request would now.

        Parameters
        ----------
        signed
            The session.

        Returns
        -------
        PrincipalRef
            The person, with the session's current context.
        """
        return self.sessions.verify(signed.started.cookie_value, extend=False)

    def register(
        self,
        signed: Signed,
        auth: SoftAuthenticator | None = None,
        **create: Any,  # ruff: ignore[any-type]
    ) -> tuple[Passkey, SoftAuthenticator]:
        """Register a passkey in a session.

        Parameters
        ----------
        signed
            The session.
        auth
            The authenticator; a new one by default.
        **create
            Overrides for `SoftAuthenticator.create`.

        Returns
        -------
        tuple of (Passkey, SoftAuthenticator)
            The stored passkey and its authenticator.
        """
        auth = auth or SoftAuthenticator(origin=ORIGIN)
        ceremony, options = self.passkeys.registration_options(self.ref(signed))
        stored = self.passkeys.register(
            self.ref(signed), ceremony, auth.create(options, **create), name=None, user_agent="u"
        )
        return stored, auth

    def sign_in(self, auth: SoftAuthenticator, **get: Any) -> StartedSession:  # ruff: ignore[any-type]
        """Sign in with a passkey.

        Parameters
        ----------
        auth
            The authenticator.
        **get
            Overrides for `SoftAuthenticator.get`.

        Returns
        -------
        StartedSession
            The new session.
        """
        ceremony, options = self.passkeys.sign_in_options()
        return self.passkeys.sign_in(
            ceremony, auth.get(options, **get), replacing=None, peer="p", user_agent="u"
        )

    def step_up(self, signed: Signed, auth: SoftAuthenticator, **get: Any) -> datetime:  # ruff: ignore[any-type]
        """Confirm a session with a passkey touch.

        Parameters
        ----------
        signed
            The session.
        auth
            The authenticator.
        **get
            Overrides for `SoftAuthenticator.get`.

        Returns
        -------
        datetime
            The new touch time.
        """
        ceremony, options = self.passkeys.step_up_options(self.ref(signed))
        return self.passkeys.step_up(self.ref(signed), ceremony, auth.get(options, **get))

    def events(self, kind: AuditEventType) -> list[AuditEvent]:
        """Read the audit events of one type.

        Parameters
        ----------
        kind
            The type.

        Returns
        -------
        list of AuditEvent
            The events, oldest first.
        """
        return list(
            self.session.execute(
                select(AuditEvent).where(AuditEvent.event_type == kind).order_by(AuditEvent.id)
            ).scalars()
        )

    def notices(self, row: Principal) -> list[SignInNotice]:
        """Read a person's notices.

        Parameters
        ----------
        row
            The person.

        Returns
        -------
        list of SignInNotice
            Their notices.
        """
        return list(
            self.session.execute(
                select(SignInNotice).where(SignInNotice.recipient_id == row.id)
            ).scalars()
        )


@pytest.fixture
def clock() -> _Clock:
    """Provide the services' clock.

    Returns
    -------
    _Clock
        Set `.now` to move time.
    """
    return _Clock()


@pytest.fixture
def world(session: Session, clock: _Clock) -> World:
    """Provide a bootstrapped instance.

    Parameters
    ----------
    session
        Database session.
    clock
        The clock.

    Returns
    -------
    World
        The services.
    """
    return World(session, clock)


@pytest.fixture
def alice(world: World) -> Signed:
    """Provide alice, just signed in with a setup link.

    Parameters
    ----------
    world
        The instance.

    Returns
    -------
    Signed
        Her session.
    """
    return world.start(world.person("alice"), via="invite")


@pytest.fixture
def enrolled(world: World, alice: Signed) -> tuple[Signed, SoftAuthenticator, Passkey]:
    """Provide alice with one registered passkey.

    Parameters
    ----------
    world
        The instance.
    alice
        Her link session.

    Returns
    -------
    tuple of (Signed, SoftAuthenticator, Passkey)
        Her session, her authenticator and the stored passkey.
    """
    stored, auth = world.register(alice)
    return alice, auth, stored


def test_registration_options_require_uv_resident_key_and_no_attestation(
    world: World, alice: Signed
) -> None:
    """Discoverable credential, user verification required, no attestation, our RP id."""
    _, opts = world.passkeys.registration_options(world.ref(alice))
    assert opts["authenticatorSelection"] == {
        "residentKey": "required",
        "requireResidentKey": True,
        "userVerification": "required",
    }
    assert opts["attestation"] == "none" and opts["rp"]["id"] == "localhost"
    assert opts["user"]["name"] == "alice"


def test_user_handle_is_random_and_not_the_name(world: World, alice: Signed) -> None:
    """32 random bytes, stable per person, never derived from the name."""
    _, first = world.passkeys.registration_options(world.ref(alice))
    _, second = world.passkeys.registration_options(world.ref(alice))
    handle = unb64url(first["user"]["id"])
    assert len(handle) == 32 and handle != b"alice" and b"alice" not in handle
    assert second["user"]["id"] == first["user"]["id"]
    world.session.refresh(alice.row)
    assert alice.row.webauthn_handle == handle
    bob = world.start(world.person("bob"), via="invite")
    _, other = world.passkeys.registration_options(world.ref(bob))
    assert other["user"]["id"] != first["user"]["id"]


def test_first_passkey_needs_a_fresh_link_session(world: World, alice: Signed) -> None:
    """A link session may add the first passkey for 15 minutes, then only with step-up."""
    world.clock.now = START + FIRST_PASSKEY_WINDOW + timedelta(minutes=1)
    with pytest.raises(StepUpRequired, match="recovery link"):
        world.passkeys.registration_options(world.ref(alice))


def test_a_session_without_a_link_needs_step_up_for_the_first_passkey(world: World) -> None:
    """A passkey session of a person whose passkeys were all removed needs a touch."""
    signed = world.start(world.person("dora"), via="passkey")
    with pytest.raises(StepUpRequired):
        world.passkeys.registration_options(world.ref(signed))


def test_a_token_cannot_register_a_passkey(world: World) -> None:
    """A ceremony belongs to a browser session; a token caller has none."""
    row = world.person("erin")
    token = PrincipalRef(id=row.id, name=row.name, type=row.type, credential="pat")
    with pytest.raises(ConflictError, match="browser session"):
        world.passkeys.registration_options(token)


def test_local_session_cannot_add_a_second_passkey(world: World) -> None:
    """A local link signs in without a touch, so it may add only the first passkey."""
    row = world.person("frank")
    local = world.start(row, via="local")
    world.register(local)
    again = world.start(row, via="local", touched=True)
    with pytest.raises(PermissionDenied, match="local sign-in link"):
        world.passkeys.registration_options(world.ref(again))


def test_a_second_passkey_needs_step_up(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """Once a person has a passkey, adding another needs a fresh touch."""
    alice, auth, _ = enrolled
    world.clock.now = START + timedelta(minutes=20)
    with pytest.raises(StepUpRequired):
        world.passkeys.registration_options(world.ref(alice))
    world.step_up(alice, auth)
    second, _ = world.register(alice)
    assert second.principal_id == alice.row.id


@pytest.mark.parametrize(
    "fault",
    [{"origin": "http://localhost:9999"}, {"origin": "http://evil.test:8000"}, {"rp_id": "evil"}],
    ids=["other-port", "other-host", "other-rp-id"],
)
def test_registration_refuses_another_origin_or_rp_id(
    world: World, alice: Signed, fault: dict[str, str]
) -> None:
    """The origin is checked exactly, port included, and the RP id hash too."""
    with pytest.raises(PasskeyRefused):
        world.register(alice, None, origin=fault.get("origin"), rp_id=fault.get("rp_id"))
    assert world.session.scalar(select(func.count()).select_from(Passkey)) == 0


def test_registration_refuses_a_cross_origin_answer(world: World, alice: Signed) -> None:
    """A ceremony run in a frame of another site is refused."""
    with pytest.raises(PasskeyRefused):
        world.register(alice, cross_origin=True)


def test_a_local_session_after_the_window_names_a_new_local_link(world: World) -> None:
    """The next step after a local sign-in is another local sign-in, not a recovery link."""
    local = world.start(world.person("gus"), via="local")
    world.clock.now = START + FIRST_PASSKEY_WINDOW + timedelta(minutes=1)
    with pytest.raises(StepUpRequired, match="lakana server start --local"):
        world.passkeys.registration_options(world.ref(local))


def test_a_counter_changed_meanwhile_refuses_the_sign_in(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """Two uses at once cannot both pass on the same stored counter."""
    _, auth, stored = enrolled
    moved: list[bool] = []

    def other_use_first(state: ORMExecuteState) -> None:
        """Let another use move the counter after this one read it, before it writes.

        Parameters
        ----------
        state
            The statement about to run.
        """
        target = getattr(state.statement, "table", None)
        if state.is_update and getattr(target, "name", None) == "passkeys" and not moved:
            moved.append(True)
            state.session.connection().execute(
                update(Passkey).where(Passkey.id == stored.id).values(sign_count=7)
            )

    event.listen(world.session, "do_orm_execute", other_use_first)
    try:
        with pytest.raises(AuthenticationError, match="at the same moment"):
            world.sign_in(auth, sign_count=5)
    finally:
        event.remove(world.session, "do_orm_execute", other_use_first)
    world.session.refresh(stored)
    assert stored.disabled_at is None
    assert len(world.events(AuditEventType.SESSION_STARTED)) == 1  # alice's link session only


def test_registration_refuses_without_user_verification(world: World, alice: Signed) -> None:
    """A touch without a PIN or biometric is not enough."""
    with pytest.raises(PasskeyRefused):
        world.register(alice, uv=False)


def test_credential_id_already_stored_is_refused(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """The same credential id is refused for the same person and for another one."""
    alice, auth, _ = enrolled
    world.step_up(alice, auth)
    with pytest.raises(AlreadyExistsError):
        world.register(alice, auth)
    bob = world.start(world.person("bob"), via="invite")
    with pytest.raises(AlreadyExistsError):
        world.register(bob, auth)
    assert world.session.scalar(select(func.count()).select_from(Passkey)) == 1


def test_challenge_is_single_use_and_expires_after_5_minutes(world: World, alice: Signed) -> None:
    """A used challenge and a challenge older than 5 minutes are refused."""
    auth = SoftAuthenticator(origin=ORIGIN)
    ceremony, options = world.passkeys.registration_options(world.ref(alice))
    answer = auth.create(options)
    world.passkeys.register(world.ref(alice), ceremony, answer, name="laptop", user_agent="u")
    world.clock.now += timedelta(seconds=1)
    with pytest.raises(PasskeyRefused, match="expired or was used"):
        world.passkeys.register(world.ref(alice), ceremony, answer, name=None, user_agent="u")
    ceremony, options = world.passkeys.sign_in_options()
    world.clock.now += CHALLENGE_LIFETIME
    with pytest.raises(AuthenticationError, match="expired or was used"):
        world.passkeys.sign_in(
            ceremony, auth.get(options), replacing=None, peer="p", user_agent="u"
        )


def test_a_failed_answer_still_uses_up_its_challenge(world: World, alice: Signed) -> None:
    """A wrong answer burns the challenge, so it cannot be tried again."""
    auth = SoftAuthenticator(origin=ORIGIN)
    ceremony, options = world.passkeys.registration_options(world.ref(alice))
    with pytest.raises(PasskeyRefused):
        world.passkeys.register(
            world.ref(alice), ceremony, auth.create(options, uv=False), name=None, user_agent="u"
        )
    with pytest.raises(PasskeyRefused, match="expired or was used"):
        world.passkeys.register(
            world.ref(alice), ceremony, auth.create(options), name=None, user_agent="u"
        )


def test_a_challenge_of_another_session_is_refused(world: World, alice: Signed) -> None:
    """A registration challenge belongs to the session that asked for it."""
    other = world.start(alice.row, via="invite")
    auth = SoftAuthenticator(origin=ORIGIN)
    ceremony, options = world.passkeys.registration_options(world.ref(alice))
    with pytest.raises(PasskeyRefused, match="expired or was used"):
        world.passkeys.register(
            world.ref(other), ceremony, auth.create(options), name=None, user_agent="u"
        )


def test_challenge_is_32_random_bytes(world: World, alice: Signed) -> None:
    """A stored (registration) challenge is 32 random bytes; a sign-in nonce is too."""
    first, _ = world.passkeys.registration_options(world.ref(alice))
    second, _ = world.passkeys.registration_options(world.ref(alice))
    rows = {r.id: r.challenge for r in world.session.execute(select(WebauthnChallenge)).scalars()}
    assert len(rows[first]) == len(rows[second]) == 32
    assert rows[first] != rows[second]
    _, one = world.passkeys.sign_in_options()
    _, two = world.passkeys.sign_in_options()
    assert one["challenge"] != two["challenge"]
    assert len(unb64url(one["challenge"])) == 1 + 32 + 8 + 32


def test_expired_challenges_are_purged_on_insert(world: World, alice: Signed) -> None:
    """Expired rows go with each insert."""
    for _ in range(3):
        world.passkeys.registration_options(world.ref(alice))
    world.clock.now += CHALLENGE_LIFETIME
    world.passkeys.registration_options(world.ref(alice))
    assert world.session.scalar(select(func.count()).select_from(WebauthnChallenge)) == 1


def test_sign_in_options_store_nothing(world: World) -> None:
    """Anonymous options calls cannot grow the table: a sign-in challenge is signed, not kept."""
    for _ in range(3):
        world.passkeys.sign_in_options()
    assert world.session.scalar(select(func.count()).select_from(WebauthnChallenge)) == 0


def test_a_flood_of_options_never_invalidates_a_real_challenge(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """However many anonymous challenges are issued, an earlier one still signs in."""
    _, auth, _ = enrolled
    ceremony, options = world.passkeys.sign_in_options()
    for _ in range(1500):
        world.passkeys.sign_in_options()
    started = world.passkeys.sign_in(
        ceremony, auth.get(options), replacing=None, peer="p", user_agent="u"
    )
    assert started.principal.name == "alice"


def test_a_sign_in_challenge_cannot_be_replayed(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """The nonce is recorded when answered; a second answer to it is refused."""
    _, auth, _ = enrolled
    ceremony, options = world.passkeys.sign_in_options()
    world.passkeys.sign_in(ceremony, auth.get(options), replacing=None, peer="p", user_agent="u")
    with pytest.raises(AuthenticationError, match="expired or was used"):
        world.passkeys.sign_in(
            ceremony, auth.get(options), replacing=None, peer="p", user_agent="u"
        )


def _tampered(challenge: str) -> str:
    """Flip one bit of a challenge's nonce.

    Parameters
    ----------
    challenge
        The challenge, base64url.

    Returns
    -------
    str
        The changed challenge.
    """
    raw = bytearray(unb64url(challenge))
    raw[5] ^= 1
    return b64url(bytes(raw))


@pytest.mark.parametrize(
    "fault", ["tampered", "expired", "wrong_purpose", "wrong_rp", "ceremony", "ten_years"]
)
def test_a_forged_or_stale_sign_in_challenge_is_refused(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey], fault: str
) -> None:
    """The signature, purpose, RP id, expiry and ceremony id are checked before anything else."""
    _, auth, _ = enrolled
    ceremony, options = world.passkeys.sign_in_options()
    if fault == "tampered":
        options = {**options, "challenge": _tampered(options["challenge"])}
    elif fault == "expired":
        world.clock.now += CHALLENGE_LIFETIME
    elif fault == "wrong_purpose":
        key = world.passkeys._challenge_key()
        raw = unb64url(options["challenge"])
        forged = sign_challenge(
            key, "step_up", RP_ID, raw[1:33], int(world.clock.now.timestamp()) + 300
        )
        options = {**options, "challenge": b64url(forged)}
    elif fault == "ten_years":
        key = world.passkeys._challenge_key()
        raw = unb64url(options["challenge"])
        later = int(world.clock.now.timestamp()) + 10 * 365 * 24 * 3600
        options = {
            **options,
            "challenge": b64url(sign_challenge(key, "sign_in", RP_ID, raw[1:33], later)),
        }
    elif fault == "wrong_rp":
        moved = world.service(rp_id="lakana.example.org", origin="https://lakana.example.org")
        ceremony, options = moved.sign_in_options()
        options = {**options, "rpId": RP_ID}
    else:
        ceremony = uuid.uuid4()
    with pytest.raises(AuthenticationError, match="expired or was used"):
        world.passkeys.sign_in(
            ceremony, auth.get(options), replacing=None, peer="p", user_agent="u"
        )
    signed_in = select(func.count()).where(WebauthnChallenge.purpose == "sign_in")
    assert world.session.scalar(signed_in) == 0


def test_used_sign_in_nonces_are_purged_after_they_expire(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """Storage grows only with answered attempts, and only for 5 minutes."""
    _, auth, _ = enrolled
    world.sign_in(auth)
    world.sign_in(auth)
    world.clock.now += CHALLENGE_LIFETIME
    world.sign_in(auth)
    assert world.session.scalar(select(func.count()).select_from(WebauthnChallenge)) == 1
    assert world.session.scalar(select(WebauthnChallenge.purpose)) == "sign_in"


def test_sign_in_is_usernameless(world: World) -> None:
    """No credential list and no name: the browser offers its discoverable passkeys."""
    _, options = world.passkeys.sign_in_options()
    assert options["allowCredentials"] == []
    assert options["userVerification"] == "required" and options["rpId"] == "localhost"


def test_sign_in_with_a_passkey_starts_a_touched_session(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """A verified passkey starts a session with a touch and records the counter."""
    _, auth, stored = enrolled
    world.clock.now += timedelta(minutes=1)
    started = world.sign_in(auth)
    assert started.principal.name == "alice"
    session = started.principal.session
    assert session is not None and session.via == "passkey"
    assert session.last_touch_at == world.clock.now
    world.session.refresh(stored)
    assert stored.sign_count == 1 and stored.last_used_at == world.clock.now


FAULTS = {
    "uv_off": {"uv": False},
    "cross_origin": {"cross_origin": True},
    "origin_other_port": {"origin": "http://localhost:9999"},
    "origin_other_host": {"origin": "http://evil.test:8000"},
    "rp_id_other": {"rp_id": "evil"},
}


@pytest.mark.parametrize("ceremony", ["sign_in", "step_up"])
@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_assertion_refused(
    world: World,
    enrolled: tuple[Signed, SoftAuthenticator, Passkey],
    ceremony: str,
    fault: str,
) -> None:
    """Each fault is refused; no session starts, no touch is recorded, the counter stays."""
    alice, auth, stored = enrolled
    world.clock.now += timedelta(minutes=30)
    touched = world.ref(alice).session
    assert touched is not None
    before = world.session.scalar(select(func.count()).select_from(BrowserSession))
    with pytest.raises(AuthenticationError):
        if ceremony == "sign_in":
            world.sign_in(auth, **FAULTS[fault])
        else:
            world.step_up(alice, auth, **FAULTS[fault])
    assert world.session.scalar(select(func.count()).select_from(BrowserSession)) == before
    after = world.ref(alice).session
    assert after is not None and after.last_touch_at == touched.last_touch_at
    world.session.refresh(stored)
    assert stored.sign_count == 0 and stored.disabled_at is None


def test_step_up_with_another_persons_passkey_is_refused(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """Bob's valid passkey does not confirm alice's session."""
    alice, _, _ = enrolled
    bob = world.start(world.person("bob"), via="invite")
    _, bobs = world.register(bob)
    world.clock.now += timedelta(minutes=30)
    before = world.ref(alice).session
    with pytest.raises(PasskeyRefused, match="not one of yours"):
        world.step_up(alice, bobs)
    after = world.ref(alice).session
    assert before is not None and after is not None
    assert after.last_touch_at == before.last_touch_at


def test_step_up_sets_the_touch_time(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """A step-up records the touch, and step-up actions work again for 15 minutes."""
    alice, auth, _ = enrolled
    world.clock.now += timedelta(minutes=30)
    touched = world.step_up(alice, auth)
    assert touched == world.clock.now
    session = world.ref(alice).session
    assert session is not None and session.last_touch_at == world.clock.now


def test_sign_in_requires_the_user_handle_of_the_credential(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """The library does not compare the user handle; the service does."""
    _, auth, _ = enrolled
    with pytest.raises(AuthenticationError):
        world.sign_in(auth, user_handle=bytes(32))
    with pytest.raises(AuthenticationError):
        world.sign_in(auth, user_handle=None)


def test_sign_in_refused_for_a_deactivated_person(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """A deactivated person's passkey signs nobody in."""
    _, auth, _ = enrolled
    PrincipalService(world.session, AllowAllAuthorization()).deactivate_principal(
        PrincipalRef(id=world.admin.id, name="admin", type=PrincipalType.USER), "alice"
    )
    with pytest.raises(AuthenticationError, match="deactivated") as refused:
        world.sign_in(auth)
    assert "alice" not in str(refused.value)


def test_unknown_passkey_is_refused(world: World) -> None:
    """A passkey registered nowhere here signs nobody in."""
    stranger = SoftAuthenticator(origin=ORIGIN)
    stranger.user_handle = bytes(32)
    with pytest.raises(AuthenticationError, match="not registered"):
        world.sign_in(stranger)


def test_counter_going_down_disables_the_passkey_and_tells_admins(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """A counter at or below the stored one means a copy: disable, audit, tell everyone."""
    alice, auth, stored = enrolled
    signed_in = world.sign_in(auth, sign_count=5)
    with pytest.raises(AuthenticationError, match="copied"):
        world.sign_in(auth, sign_count=4)
    world.session.refresh(stored)
    assert stored.disabled_at is not None and stored.disabled_reason == "possible_clone"
    [disabled] = world.events(AuditEventType.PASSKEY_DISABLED)
    assert disabled.detail["passkey_id"] == str(stored.id)
    assert disabled.detail["sessions_ended"] == 2
    for cookie in (alice.started.cookie_value, signed_in.cookie_value):
        with pytest.raises(SessionEndedError) as ended:
            world.sessions.verify(cookie, extend=False)
        assert ended.value.reason == "passkey_copied"
    message = (
        "A copy of a passkey was detected; it is disabled and every session of alice was ended."
    )
    for person in (alice.row, world.admin):
        [notice] = world.notices(person)
        assert notice.kind == "passkey_disabled" and notice.detail["message"] == message
    with pytest.raises(AuthenticationError, match="disabled"):
        world.sign_in(auth, sign_count=9)
    assert not world.passkeys.has_usable_passkey(alice.row.id)


def test_zero_counters_are_accepted(world: World, alice: Signed) -> None:
    """A synced passkey always reports 0; that is no clone."""
    _, auth = world.register(alice, SoftAuthenticator(origin=ORIGIN, counts=False))
    world.sign_in(auth)
    world.sign_in(auth)
    assert world.passkeys.has_usable_passkey(alice.row.id)


def test_forged_assertion_cannot_disable_a_passkey(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """A low counter with a bad signature is refused before the clone rule looks at it."""
    _, auth, stored = enrolled
    world.sign_in(auth, sign_count=5)
    with pytest.raises(AuthenticationError):
        world.sign_in(auth, sign_count=1, forge=True)
    world.session.refresh(stored)
    assert stored.disabled_at is None and stored.sign_count == 5
    assert world.events(AuditEventType.PASSKEY_DISABLED) == []


def test_a_passkey_of_another_public_address_is_not_usable(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """Changing the public address leaves passkeys registered for the old one unusable."""
    alice, auth, _ = enrolled
    moved = world.service(rp_id="lakana.example.org", origin="https://lakana.example.org")
    assert world.passkeys.has_usable_passkey(alice.row.id)
    assert not moved.has_usable_passkey(alice.row.id)
    ceremony, options = moved.sign_in_options()
    with pytest.raises(AuthenticationError, match="public address"):
        moved.sign_in(
            ceremony,
            auth.get(options, origin="https://lakana.example.org"),
            replacing=None,
            peer="p",
            user_agent="u",
        )


def test_list_own_and_as_admin_only(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """A person lists their own passkeys; another person's only an administrator."""
    alice, _, stored = enrolled
    assert [p.id for p in world.passkeys.list_passkeys(world.ref(alice), "alice")] == [stored.id]
    bob = world.start(world.person("bob"), via="passkey", touched=True)
    with pytest.raises(PermissionDenied):
        world.passkeys.list_passkeys(world.ref(bob), "alice")
    admin = world.start(world.admin, via="passkey", touched=True)
    assert [p.id for p in world.passkeys.list_passkeys(world.ref(admin), "alice")] == [stored.id]


def test_removing_ones_last_passkey_is_refused(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """The last active passkey stays; with a second one, the first can go."""
    alice, auth, stored = enrolled
    world.step_up(alice, auth)
    with pytest.raises(ConflictError, match="Add another passkey first"):
        world.passkeys.remove(world.ref(alice), "alice", stored.id)
    second, _ = world.register(alice)
    world.passkeys.remove(world.ref(alice), "alice", stored.id)
    assert [p.id for p in world.passkeys.list_passkeys(world.ref(alice), "alice")] == [second.id]
    assert len(world.events(AuditEventType.PASSKEY_REMOVED)) == 1


def test_a_passkey_of_an_earlier_address_does_not_count_as_another_one(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """After an address change the old passkey cannot sign in, so the new one is the last."""
    alice, auth, stored = enrolled
    old = world.service(rp_id="old.example", origin="https://old.example")
    ceremony, options = old.registration_options(world.ref(alice))
    earlier = old.register(
        world.ref(alice),
        ceremony,
        SoftAuthenticator(origin="https://old.example").create(options),
        name="old laptop",
        user_agent="u",
    )
    world.step_up(alice, auth)
    with pytest.raises(ConflictError, match="Add another passkey first"):
        world.passkeys.remove(world.ref(alice), "alice", stored.id)
    world.passkeys.remove(world.ref(alice), "alice", earlier.id)
    assert world.passkeys.has_usable_passkey(alice.row.id)


def test_removing_a_passkey_needs_step_up(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """Removal, too, needs a touch within 15 minutes."""
    alice, _, stored = enrolled
    world.clock.now += timedelta(minutes=16)
    with pytest.raises(StepUpRequired):
        world.passkeys.remove(world.ref(alice), "alice", stored.id)


def test_another_persons_passkey_is_not_found_for_a_non_admin(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """A non-administrator cannot remove someone else's passkey."""
    _, _, stored = enrolled
    bob = world.start(world.person("bob"), via="passkey", touched=True)
    with pytest.raises(PermissionDenied):
        world.passkeys.remove(world.ref(bob), "alice", stored.id)
    admin = world.start(world.admin, via="passkey", touched=True)
    with pytest.raises(NotFoundError):
        world.passkeys.remove(world.ref(admin), "alice", uuid.uuid4())


def test_admin_removing_a_passkey_ends_that_persons_sessions(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """Even the last one; every session of that person ends at once."""
    alice, _, stored = enrolled
    admin = world.start(world.admin, via="passkey", touched=True)
    world.passkeys.remove(world.ref(admin), "alice", stored.id)
    with pytest.raises(SessionEndedError) as ended:
        world.ref(alice)
    assert ended.value.reason == "passkey_removed"
    assert world.ref(admin).name == "admin"
    [event] = world.events(AuditEventType.PASSKEY_REMOVED)
    assert event.detail["subject"] == "alice" and event.principal_id == world.admin.id


def test_passkey_from_an_older_session_leaves_a_notice(
    world: World, enrolled: tuple[Signed, SoftAuthenticator, Passkey]
) -> None:
    """A passkey added later than 15 minutes after a link sign-in tells its person."""
    alice, auth, _ = enrolled
    assert world.notices(alice.row) == []
    world.clock.now += timedelta(minutes=30)
    world.step_up(alice, auth)
    world.register(alice)
    assert [n.kind for n in world.notices(alice.row)] == ["passkey_added"]
    assert len(world.events(AuditEventType.PASSKEY_ADDED)) == 2
