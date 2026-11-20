"""The grants of `lakana login` through a browser (`lakana_core.identity.cli_grants`, #32 PR 3).

A loopback code works once, for 2 minutes, and only with the command line's PKCE verifier; a
device grant waits 10 minutes for a person to approve or deny it, and its command line polls no
faster than every 5 seconds. Either one issues a token named for the host, for the person who
approved, while their browser session lasts; no secret is stored or audited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.bootstrap import bootstrap_if_needed, ensure_bootstrap_admin_grant
from lakana_core.errors import (
    AuthenticationError,
    CliGrantRefused,
    CliPollTooFast,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    StepUpRequired,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identity import cli_grants
from lakana_core.identity.cli_grants import (
    CODE_LIFETIME,
    DEVICE_LIFETIME,
    POLL_INTERVAL,
    TOKEN_LIFETIME,
    USER_CODE_ALPHABET,
    CliGrantService,
    DevicePoll,
    DeviceStart,
    clean_host,
    normalize_user_code,
    s256,
    token_name_for,
)
from lakana_core.identity.pats import PatService
from lakana_core.identity.sessions import SessionService
from lakana_core.identity.tokens import hash_secret, parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    BrowserSession,
    CliGrant,
    PersonalAccessToken,
    Principal,
    PrincipalType,
)

START = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
VERIFIER = "v" * 43
CHALLENGE = s256(VERIFIER)


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


class World:
    """The services and people of one test.

    Parameters
    ----------
    session
        Database session.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.clock = _Clock()
        bootstrap_if_needed(session, "admin", key_id="opkey", secret="opsecret")
        self.authz = GrantAuthorization(session)
        self.sessions = SessionService(session, self.authz, clock=self.clock)
        self.grants = CliGrantService(session, self.authz, clock=self.clock)

    def person(
        self, name: str, *, kind: PrincipalType = PrincipalType.USER, admin: bool = False
    ) -> Principal:
        """Insert a person.

        Parameters
        ----------
        name
            The name.
        kind
            The principal's type.
        admin
            Whether to grant ADMIN at instance scope.

        Returns
        -------
        Principal
            The row.
        """
        row = Principal(id=uuid.uuid4(), type=kind, name=name, is_active=True)
        self.session.add(row)
        self.session.commit()
        if admin:
            ensure_bootstrap_admin_grant(
                self.session,
                PrincipalRef(id=row.id, name=row.name, type=row.type),
                AllowAllAuthorization(),
            )
        return row

    def signed_in(self, row: Principal, *, touched: bool = True) -> PrincipalRef:
        """Start a browser session for a person and authenticate with it.

        Parameters
        ----------
        row
            The person.
        touched
            Whether the session has a fresh passkey touch.

        Returns
        -------
        PrincipalRef
            The person, as their session authenticates them.
        """
        started = self.sessions.start(
            row.id, via="passkey", replacing=None, peer="p", user_agent="u", touched=touched
        )
        self.session.commit()
        return self.sessions.verify(started.cookie_value, extend=False)

    def events(self, kind: AuditEventType) -> list[dict]:
        """Read the details of every event of one type, in order.

        Parameters
        ----------
        kind
            The type.

        Returns
        -------
        list of dict
            The details.
        """
        return [
            dict(detail)
            for detail in self.session.execute(
                select(AuditEvent.detail)
                .where(AuditEvent.event_type == kind)
                .order_by(AuditEvent.occurred_at)
            ).scalars()
        ]

    def tokens(self, row: Principal) -> list[PersonalAccessToken]:
        """List a person's tokens.

        Parameters
        ----------
        row
            The person.

        Returns
        -------
        list of PersonalAccessToken
            The rows, oldest first.
        """
        return list(
            self.session.execute(
                select(PersonalAccessToken)
                .where(PersonalAccessToken.principal_id == row.id)
                .order_by(PersonalAccessToken.created_at)
            ).scalars()
        )


@pytest.fixture
def world(session: Session) -> World:
    """Provide a bootstrapped instance.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    World
        The services.
    """
    return World(session)


def _approve(world: World, p: PrincipalRef, host: str = "laptop-7") -> str:
    """Confirm a loopback sign-in.

    Parameters
    ----------
    world
        The services.
    p
        The person.
    host
        The host name.

    Returns
    -------
    str
        The code.
    """
    return world.grants.approve_loopback(p, port=49152, code_challenge=CHALLENGE, host=host)


# -- loopback -----------------------------------------------------------------------------------


def test_loopback_code_exchanges_once_for_a_token_named_for_the_host(world: World) -> None:
    kim = world.person("kim")
    code = _approve(world, world.signed_in(kim))

    issued = world.grants.exchange_loopback(code, VERIFIER)

    assert issued.name == "lakana login on laptop-7"
    assert issued.token.startswith("lakana_pat_")
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    assert PatService(world.session, world.authz).verify(*parsed).name == "kim"
    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback(code, VERIFIER)
    grant = world.session.execute(select(CliGrant)).scalar_one()
    assert grant.pat_id == world.tokens(kim)[0].id
    assert world.events(AuditEventType.SIGN_IN_CLI_APPROVED) == [
        {"id": str(grant.id), "kind": "loopback", "host": "laptop-7"}
    ]
    assert world.events(AuditEventType.SIGN_IN_CLI_TOKEN_ISSUED) == [
        {"id": str(grant.id), "kind": "loopback", "host": "laptop-7", "key_id": issued.key_id}
    ]
    assert len(world.events(AuditEventType.PAT_ISSUED)) == 2  # the operator's, and kim's


def test_an_administrator_gets_a_token_too(world: World) -> None:
    ada = world.person("ada", admin=True)
    code = _approve(world, world.signed_in(ada))

    assert world.grants.exchange_loopback(code, VERIFIER).name == "lakana login on laptop-7"


def test_code_works_once(world: World) -> None:
    code = _approve(world, world.signed_in(world.person("kim")))
    world.grants.exchange_loopback(code, VERIFIER)

    with pytest.raises(CliGrantRefused, match="Run lakana login again"):
        world.grants.exchange_loopback(code, VERIFIER)


def test_code_without_the_right_verifier_is_refused_and_burnt(world: World) -> None:
    code = _approve(world, world.signed_in(world.person("kim")))

    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback(code, "w" * 43)
    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback(code, VERIFIER)


def test_unknown_or_empty_code_is_refused(world: World) -> None:
    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback("", VERIFIER)
    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback("nothing", VERIFIER)


def test_code_expires_after_two_minutes(world: World) -> None:
    code = _approve(world, world.signed_in(world.person("kim")))
    world.clock.now = START + CODE_LIFETIME

    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback(code, VERIFIER)


def test_approval_needs_step_up(world: World) -> None:
    kim = world.signed_in(world.person("kim"), touched=False)

    with pytest.raises(StepUpRequired):
        _approve(world, kim)
    assert world.session.execute(select(CliGrant)).first() is None


def test_automation_cannot_approve(world: World) -> None:
    kim = world.person("kim")
    robot = world.person("robot", kind=PrincipalType.SERVICE_ACCOUNT)
    token = PrincipalRef(id=kim.id, name="kim", type=PrincipalType.USER, credential="pat")
    machine = PrincipalRef(id=robot.id, name="robot", type=PrincipalType.SERVICE_ACCOUNT)

    with pytest.raises(PermissionDenied, match="an API token cannot confirm one"):
        _approve(world, token)
    with pytest.raises(PermissionDenied, match="service account"):
        _approve(world, machine)
    with pytest.raises(PermissionDenied):
        world.grants.approve_device(token, "BCDF-GHJK")
    with pytest.raises(PermissionDenied):
        world.grants.describe_device(machine, "BCDF-GHJK")
    with pytest.raises(PermissionDenied):
        world.grants.deny_device(token, "BCDF-GHJK")


@pytest.mark.parametrize("port", [0, 80, 1023, 65536])
def test_approval_refuses_a_port_outside_1024_to_65535(world: World, port: int) -> None:
    kim = world.signed_in(world.person("kim"))

    with pytest.raises(ValidationError, match="between 1024 and 65535"):
        world.grants.approve_loopback(kim, port=port, code_challenge=CHALLENGE, host="h")


def test_approval_refuses_what_is_no_challenge(world: World) -> None:
    kim = world.signed_in(world.person("kim"))

    with pytest.raises(ValidationError, match="S256 challenge"):
        world.grants.approve_loopback(kim, port=50000, code_challenge="short", host="h")


def test_no_token_once_the_approving_session_ended(world: World) -> None:
    kim_row = world.person("kim")
    kim = world.signed_in(kim_row)
    code = _approve(world, kim)
    assert kim.session is not None
    world.sessions.end(kim, kim.session.key_id)

    with pytest.raises(CliGrantRefused, match="has ended"):
        world.grants.exchange_loopback(code, VERIFIER)
    assert world.tokens(kim_row) == []


def test_no_token_when_the_approving_session_was_deleted(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    code = _approve(world, kim)
    world.session.query(BrowserSession).delete()
    world.session.commit()

    with pytest.raises(CliGrantRefused, match="has ended"):
        world.grants.exchange_loopback(code, VERIFIER)


def test_the_touch_is_measured_at_the_approval(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    code = _approve(world, kim)
    world.clock.now = START + timedelta(minutes=1)

    assert world.grants.exchange_loopback(code, VERIFIER).name == "lakana login on laptop-7"


def test_host_label_is_cleaned(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    code = _approve(world, kim, host=" lap‮top\n7 ")

    assert world.grants.exchange_loopback(code, VERIFIER).name == "lakana login on laptop7"
    assert clean_host("x" * 100) == "x" * 64
    assert clean_host("​\n ") == "?"
    assert clean_host("a" * 63 + " b") == "a" * 63


def test_taken_name_gets_a_number(world: World) -> None:
    kim_row = world.person("kim")
    kim = world.signed_in(kim_row)
    first = world.grants.exchange_loopback(_approve(world, kim), VERIFIER)
    second = world.grants.exchange_loopback(_approve(world, kim), VERIFIER)
    third = world.grants.exchange_loopback(_approve(world, kim), VERIFIER)

    assert [first.name, second.name, third.name] == [
        "lakana login on laptop-7",
        "lakana login on laptop-7 (2)",
        "lakana login on laptop-7 (3)",
    ]
    assert all(row.revoked_at is None for row in world.tokens(kim_row))
    assert token_name_for("h", lambda name: False) == "lakana login on h"


def test_a_revoked_or_expired_name_is_free_again(world: World) -> None:
    kim_row = world.person("kim")
    kim = world.signed_in(kim_row)
    world.grants.exchange_loopback(_approve(world, kim), VERIFIER)
    world.tokens(kim_row)[0].revoked_at = START
    world.session.commit()

    again = world.grants.exchange_loopback(_approve(world, kim), VERIFIER)

    assert again.name == "lakana login on laptop-7"


def test_only_hashes_are_stored(world: World) -> None:
    code = _approve(world, world.signed_in(world.person("kim")))
    started = world.grants.start_device("box")

    rows = world.session.execute(select(CliGrant).order_by(CliGrant.kind)).scalars().all()
    stored = " ".join(str(getattr(row, c.name)) for row in rows for c in CliGrant.__table__.c)
    assert code not in stored
    assert started.device_code not in stored
    assert normalize_user_code(started.user_code) not in stored
    assert rows[1].code_hash == hash_secret(code)
    assert rows[0].device_code_hash == hash_secret(started.device_code)
    assert started.device_code not in repr(started)
    assert started.user_code not in repr(started)


def test_token_lives_ninety_days(world: World) -> None:
    kim = world.person("kim")
    world.grants.exchange_loopback(_approve(world, world.signed_in(kim)), VERIFIER)

    assert world.tokens(kim)[0].expires_at == START + TOKEN_LIFETIME
    assert timedelta(days=90) == TOKEN_LIFETIME


def test_old_grants_are_purged(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    _approve(world, kim)
    world.clock.now = START + timedelta(days=2)

    world.grants.start_device("box")

    kinds = world.session.execute(select(CliGrant.kind)).scalars().all()
    assert kinds == ["device"]


# -- device -------------------------------------------------------------------------------------


def _poll(world: World, device_code: str) -> DevicePoll:
    """Poll, then move the clock past the interval.

    Parameters
    ----------
    world
        The services.
    device_code
        The device code.

    Returns
    -------
    DevicePoll
        The answer.
    """
    answer = world.grants.poll_device(device_code)
    world.clock.now += POLL_INTERVAL
    return answer


def test_device_grant_pending_then_approved_then_used(world: World) -> None:
    kim_row = world.person("kim")
    kim = world.signed_in(kim_row)
    started = world.grants.start_device("build-box")

    assert isinstance(started, DeviceStart)
    assert started.expires_at == START + DEVICE_LIFETIME
    assert _poll(world, started.device_code) == DevicePoll(state="pending")
    assert world.grants.describe_device(kim, started.user_code) == "build-box"
    world.grants.approve_device(kim, started.user_code)
    approved = _poll(world, started.device_code)

    assert approved.state == "approved"
    assert approved.token is not None
    assert approved.token.name == "lakana login on build-box"
    assert approved.token.token not in repr(approved)
    assert _poll(world, started.device_code) == DevicePoll(state="expired")
    assert [row.name for row in world.tokens(kim_row)] == ["lakana login on build-box"]
    assert world.events(AuditEventType.SIGN_IN_CLI_APPROVED)[0]["kind"] == "device"


def test_user_code_is_eight_letters_shown_with_a_dash(world: World) -> None:
    started = world.grants.start_device("box")

    assert len(started.user_code) == 9
    assert started.user_code[4] == "-"
    assert set(normalize_user_code(started.user_code)) <= set(USER_CODE_ALPHABET)


def test_user_code_matches_without_dash_and_case(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box")
    typed = f" {normalize_user_code(started.user_code).lower()} "

    assert world.grants.describe_device(kim, typed) == "box"
    world.grants.approve_device(kim, typed)
    assert _poll(world, started.device_code).state == "approved"


def test_approve_device_needs_step_up(world: World) -> None:
    kim = world.signed_in(world.person("kim"), touched=False)
    started = world.grants.start_device("box")

    with pytest.raises(StepUpRequired):
        world.grants.approve_device(kim, started.user_code)
    assert _poll(world, started.device_code).state == "pending"


def test_polling_faster_than_the_interval_is_slow_down(world: World) -> None:
    started = world.grants.start_device("box")
    world.grants.poll_device(started.device_code)
    world.clock.now += timedelta(seconds=2)

    with pytest.raises(CliPollTooFast):
        world.grants.poll_device(started.device_code)
    world.clock.now += POLL_INTERVAL - timedelta(milliseconds=500)
    assert world.grants.poll_device(started.device_code).state == "pending"


def test_denied_grant_never_yields_a_token(world: World) -> None:
    kim_row = world.person("kim")
    kim = world.signed_in(kim_row)
    started = world.grants.start_device("box")
    world.grants.deny_device(kim, started.user_code)

    assert _poll(world, started.device_code) == DevicePoll(state="denied")
    with pytest.raises(NotFoundError):
        world.grants.approve_device(kim, started.user_code)
    assert _poll(world, started.device_code) == DevicePoll(state="denied")
    assert world.tokens(kim_row) == []


def test_device_grant_expires_after_ten_minutes(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box")
    world.clock.now = START + DEVICE_LIFETIME

    with pytest.raises(NotFoundError):
        world.grants.describe_device(kim, started.user_code)
    with pytest.raises(NotFoundError):
        world.grants.approve_device(kim, started.user_code)
    with pytest.raises(NotFoundError):
        world.grants.deny_device(kim, started.user_code)
    assert world.grants.poll_device(started.device_code) == DevicePoll(state="expired")


def test_an_approved_grant_expires_unpolled(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box")
    world.grants.approve_device(kim, started.user_code)
    world.clock.now = START + DEVICE_LIFETIME

    assert world.grants.poll_device(started.device_code) == DevicePoll(state="expired")


def test_wrong_user_code_says_so(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    world.grants.start_device("box")

    with pytest.raises(NotFoundError, match="No sign-in is waiting for this code"):
        world.grants.describe_device(kim, "BBBB-BBBB")


def test_unknown_device_code_is_refused(world: World) -> None:
    with pytest.raises(CliGrantRefused, match="--device again"):
        world.grants.poll_device("nothing")
    with pytest.raises(CliGrantRefused):
        world.grants.poll_device("")


def test_a_poll_that_loses_the_race_gets_no_token(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box")
    world.grants.approve_device(kim, started.user_code)
    world.session.execute(update(CliGrant).values(used_at=START))  # another poll won
    world.session.commit()
    monkeypatch.setattr(CliGrantService, "_state", staticmethod(lambda grant, now: "approved"))

    assert world.grants.poll_device(started.device_code) == DevicePoll(state="expired")


def test_a_taken_user_code_is_drawn_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    codes = iter(["BBBBBBBB", "BBBBBBBB", "CCCCCCCC"])
    monkeypatch.setattr(cli_grants, "_new_user_code", lambda: next(codes))
    first = world.grants.start_device("one")

    second = world.grants.start_device("two")

    assert (first.user_code, second.user_code) == ("BBBB-BBBB", "CCCC-CCCC")


def test_three_taken_user_codes_give_up(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_grants, "_new_user_code", lambda: "BBBBBBBB")
    world.grants.start_device("one")

    with pytest.raises(ConflictError, match="free device code"):
        world.grants.start_device("two")


def test_a_session_id_that_names_no_session_is_not_known(world: World) -> None:
    with pytest.raises(AuthenticationError, match="not known"):
        world.sessions.verify_by_id(uuid.uuid4())
