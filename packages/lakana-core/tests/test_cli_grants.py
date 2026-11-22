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
from sqlalchemy import event, select, update
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
    DeviceDescription,
    DevicePoll,
    DeviceStart,
    check_host,
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


@pytest.mark.parametrize(
    "host",
    [
        "lap\u202etop",
        "kim-laptop” for kim, started by you just now. Create",
        "laptop 7",
        "",
        "x" * 65,
        "laptöp",
    ],
)
def test_a_host_that_is_no_host_name_is_refused(world: World, host: str) -> None:
    kim = world.signed_in(world.person("kim"))

    with pytest.raises(ValidationError, match="A-Z, a-z, 0-9"):
        world.grants.approve_loopback(kim, port=49152, code_challenge=CHALLENGE, host=host)
    with pytest.raises(ValidationError, match="A-Z, a-z, 0-9"):
        world.grants.start_device(host, peer="203.0.113.7")
    assert world.session.execute(select(CliGrant)).first() is None


def test_a_host_name_of_64_characters_is_kept(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    host = "build-runner.eu_west." + "x" * 43

    code = world.grants.approve_loopback(kim, port=49152, code_challenge=CHALLENGE, host=host)

    assert world.grants.exchange_loopback(code, VERIFIER).name == f"lakana login on {host}"
    assert check_host(host) is None


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
    started = world.grants.start_device("box", peer="203.0.113.7")

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

    world.grants.start_device("box", peer="203.0.113.7")

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
    started = world.grants.start_device("build-box", peer="203.0.113.7")

    assert isinstance(started, DeviceStart)
    assert started.expires_at == START + DEVICE_LIFETIME
    assert _poll(world, started.device_code) == DevicePoll(state="pending")
    assert world.grants.describe_device(kim, started.user_code) == DeviceDescription(
        host="build-box", requested_from="203.0.113.7", requested_at=START, age_seconds=5
    )
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
    started = world.grants.start_device("box", peer="203.0.113.7")

    assert len(started.user_code) == 9
    assert started.user_code[4] == "-"
    assert set(normalize_user_code(started.user_code)) <= set(USER_CODE_ALPHABET)


def test_user_code_matches_without_dash_and_case(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box", peer="203.0.113.7")
    typed = f" {normalize_user_code(started.user_code).lower()} "

    assert world.grants.describe_device(kim, typed).host == "box"
    world.grants.approve_device(kim, typed)
    assert _poll(world, started.device_code).state == "approved"


def test_approve_device_needs_step_up(world: World) -> None:
    kim = world.signed_in(world.person("kim"), touched=False)
    started = world.grants.start_device("box", peer="203.0.113.7")

    with pytest.raises(StepUpRequired):
        world.grants.approve_device(kim, started.user_code)
    assert _poll(world, started.device_code).state == "pending"


def test_polling_faster_than_the_interval_is_slow_down(world: World) -> None:
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.grants.poll_device(started.device_code)
    world.clock.now += timedelta(seconds=2)

    with pytest.raises(CliPollTooFast):
        world.grants.poll_device(started.device_code)
    world.clock.now += POLL_INTERVAL - timedelta(milliseconds=500)
    assert world.grants.poll_device(started.device_code).state == "pending"


def test_denied_grant_never_yields_a_token(world: World) -> None:
    kim_row = world.person("kim")
    kim = world.signed_in(kim_row)
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.grants.deny_device(kim, started.user_code)

    assert _poll(world, started.device_code) == DevicePoll(state="denied")
    with pytest.raises(NotFoundError):
        world.grants.approve_device(kim, started.user_code)
    assert _poll(world, started.device_code) == DevicePoll(state="denied")
    assert world.tokens(kim_row) == []


def test_device_grant_expires_after_ten_minutes(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box", peer="203.0.113.7")
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
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.grants.approve_device(kim, started.user_code)
    world.clock.now = START + DEVICE_LIFETIME

    assert world.grants.poll_device(started.device_code) == DevicePoll(state="expired")


def test_wrong_user_code_says_so(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    world.grants.start_device("box", peer="203.0.113.7")

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
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.grants.approve_device(kim, started.user_code)
    world.session.execute(update(CliGrant).values(used_at=START))  # another poll won
    world.session.commit()
    monkeypatch.setattr(CliGrantService, "_state", staticmethod(lambda grant, now: "approved"))

    assert world.grants.poll_device(started.device_code) == DevicePoll(state="expired")


def test_a_taken_user_code_is_drawn_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    codes = iter(["BBBBBBBB", "BBBBBBBB", "CCCCCCCC"])
    monkeypatch.setattr(cli_grants, "_new_user_code", lambda: next(codes))
    first = world.grants.start_device("one", peer="203.0.113.7")

    second = world.grants.start_device("two", peer="203.0.113.7")

    assert (first.user_code, second.user_code) == ("BBBB-BBBB", "CCCC-CCCC")


def test_three_taken_user_codes_give_up(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_grants, "_new_user_code", lambda: "BBBBBBBB")
    world.grants.start_device("one", peer="203.0.113.7")

    with pytest.raises(ConflictError, match="free device code"):
        world.grants.start_device("two", peer="203.0.113.7")


def test_a_session_id_that_names_no_session_is_not_known(world: World) -> None:
    with pytest.raises(AuthenticationError, match="not known"):
        world.sessions.verify_by_id(uuid.uuid4())


# -- the pre-review's findings (#32, PR 3) ------------------------------------------------------


def test_the_requesting_address_is_kept_and_cut(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box", peer="2001:db8::" + "f" * 80)

    described = world.grants.describe_device(kim, started.user_code)

    assert len(described.requested_from) == 64


def test_a_denied_device_sign_in_is_audited(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box", peer="203.0.113.7")

    world.grants.deny_device(kim, started.user_code)

    grant = world.session.execute(select(CliGrant)).scalar_one()
    assert world.events(AuditEventType.SIGN_IN_CLI_DENIED) == [
        {"id": str(grant.id), "kind": "device", "host": "box", "requested_from": "203.0.113.7"}
    ]


@pytest.mark.parametrize("reason", ["wrong_verifier", "used", "expired"])
def test_a_refused_exchange_is_audited_without_secrets(world: World, reason: str) -> None:
    code = _approve(world, world.signed_in(world.person("kim")))
    if reason == "used":
        world.grants.exchange_loopback(code, VERIFIER)
    if reason == "expired":
        world.clock.now = START + CODE_LIFETIME

    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback(code, "w" * 43 if reason == "wrong_verifier" else VERIFIER)

    grant = world.session.execute(select(CliGrant)).scalar_one()
    refused = world.events(AuditEventType.SIGN_IN_CLI_REFUSED)
    assert refused == [
        {"id": str(grant.id), "kind": "loopback", "host": "laptop-7", "reason": reason}
    ]
    assert code not in str(refused)


def test_an_unknown_code_is_refused_without_an_event(world: World) -> None:
    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback("nothing", VERIFIER)

    assert world.events(AuditEventType.SIGN_IN_CLI_REFUSED) == []


def test_every_exchange_refusal_says_the_same_sentence(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    ended = _approve(world, kim)
    assert kim.session is not None
    world.sessions.end(kim, kim.session.key_id)
    sentences = set()
    for code, verifier in (("nothing", VERIFIER), (ended, VERIFIER)):
        with pytest.raises(CliGrantRefused) as refused:
            world.grants.exchange_loopback(code, verifier)
        sentences.add(str(refused.value))

    assert sentences == {cli_grants.EXCHANGE_REFUSED}
    assert world.events(AuditEventType.SIGN_IN_CLI_REFUSED)[0]["reason"] == "session_ended"


def test_no_token_once_the_person_was_deactivated_after_the_loopback_approval(
    world: World,
) -> None:
    kim_row = world.person("kim")
    code = _approve(world, world.signed_in(kim_row))
    kim_row.is_active = False
    world.session.commit()

    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback(code, VERIFIER)
    assert world.tokens(kim_row) == []


def test_no_token_once_the_person_was_deactivated_after_the_device_approval(
    world: World,
) -> None:
    kim_row = world.person("kim")
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.grants.approve_device(world.signed_in(kim_row), started.user_code)
    kim_row.is_active = False
    world.session.commit()

    with pytest.raises(CliGrantRefused, match="has ended"):
        world.grants.poll_device(started.device_code)
    assert world.tokens(kim_row) == []


def test_a_loopback_exchange_that_loses_the_race_gets_no_token(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kim_row = world.person("kim")
    code = _approve(world, world.signed_in(kim_row))
    claim = CliGrantService._claim

    def raced(self: CliGrantService, conditions: list, values: dict) -> CliGrant | None:
        # Another exchange of the same code commits between this one's read and its update.
        world.session.execute(update(CliGrant).values(used_at=START))
        return claim(self, conditions, values)

    monkeypatch.setattr(CliGrantService, "_claim", raced)

    with pytest.raises(CliGrantRefused):
        world.grants.exchange_loopback(code, VERIFIER)
    assert world.tokens(kim_row) == []


def test_a_replayed_code_is_audited_once(world: World) -> None:
    code = _approve(world, world.signed_in(world.person("kim")))
    world.grants.exchange_loopback(code, VERIFIER)

    for _ in range(5):
        with pytest.raises(CliGrantRefused):
            world.grants.exchange_loopback(code, VERIFIER)

    assert [e["reason"] for e in world.events(AuditEventType.SIGN_IN_CLI_REFUSED)] == ["used"]
    assert world.session.execute(select(CliGrant.refused_at)).scalar_one() is not None


def test_a_wrong_verifier_then_replays_are_audited_once(world: World) -> None:
    code = _approve(world, world.signed_in(world.person("kim")))

    for verifier in ("w" * 43, VERIFIER, VERIFIER):
        with pytest.raises(CliGrantRefused):
            world.grants.exchange_loopback(code, verifier)

    reasons = [e["reason"] for e in world.events(AuditEventType.SIGN_IN_CLI_REFUSED)]
    assert reasons == ["wrong_verifier"]


def test_the_age_of_a_device_sign_in_is_counted_by_the_server(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.clock.now = START + timedelta(seconds=150)

    assert world.grants.describe_device(kim, started.user_code).age_seconds == 150


def test_the_age_is_never_below_zero(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.clock.now = START - timedelta(seconds=30)

    assert world.grants.describe_device(kim, started.user_code).age_seconds == 0


# -- the third round's findings (#32, PR 3) ---------------------------------------------------


def test_the_device_audit_names_the_requesting_and_collecting_addresses(world: World) -> None:
    kim = world.signed_in(world.person("kim"))
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.grants.approve_device(kim, started.user_code)

    polled = world.grants.poll_device(started.device_code, peer="198.51.100.4")

    assert polled.token is not None
    approved = world.events(AuditEventType.SIGN_IN_CLI_APPROVED)[0]
    issued = world.events(AuditEventType.SIGN_IN_CLI_TOKEN_ISSUED)[0]
    assert approved["requested_from"] == "203.0.113.7"
    assert (issued["requested_from"], issued["peer"]) == ("203.0.113.7", "198.51.100.4")


def test_the_loopback_audit_names_the_collecting_address(world: World) -> None:
    code = _approve(world, world.signed_in(world.person("kim")))

    world.grants.exchange_loopback(code, VERIFIER, peer="127.0.0.1")

    issued = world.events(AuditEventType.SIGN_IN_CLI_TOKEN_ISSUED)[0]
    assert issued["peer"] == "127.0.0.1"
    assert "requested_from" not in issued
    assert "requested_from" not in world.events(AuditEventType.SIGN_IN_CLI_APPROVED)[0]


def test_a_lakana_login_token_is_refused_outside_the_token_window(world: World) -> None:
    kim_row = world.person("kim")
    kim_row.oidc_issuer, kim_row.oidc_subject = "https://idp.example.com", "kim"
    kim_row.last_provider_sign_in_at = datetime.now(UTC) - timedelta(days=1)
    world.session.commit()
    issued = world.grants.exchange_loopback(_approve(world, world.signed_in(kim_row)), VERIFIER)
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    pats = PatService(world.session, world.authz)
    assert pats.verify(*parsed).name == "kim"

    kim_row.last_provider_sign_in_at = datetime.now(UTC) - timedelta(days=31)
    world.session.commit()
    with pytest.raises(AuthenticationError, match="nobody has signed in there as you"):
        pats.verify(*parsed)

    kim_row.last_provider_sign_in_at = datetime.now(UTC)
    world.session.commit()
    assert pats.verify(*parsed).name == "kim"


def test_a_too_fast_poll_writes_nothing(world: World) -> None:
    started = world.grants.start_device("box", peer="203.0.113.7")
    world.grants.poll_device(started.device_code)
    first = world.session.execute(select(CliGrant.last_polled_at)).scalar_one()
    assert first is not None
    world.clock.now += timedelta(seconds=2)
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    engine = world.session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        with pytest.raises(CliPollTooFast):
            world.grants.poll_device(started.device_code)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert [s for s in statements if not s.lstrip().upper().startswith("SELECT")] == []
    world.session.expire_all()
    assert world.session.execute(select(CliGrant.last_polled_at)).scalar_one() == first
    # RFC 8628: the interval counts from the last accepted poll, so waiting it out works.
    world.clock.now = first + POLL_INTERVAL
    assert world.grants.poll_device(started.device_code).state == "pending"
