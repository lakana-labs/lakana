"""Tests for bootstrap principal creation and admin lockout recovery."""

import logging
import os
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import lakana_core.bootstrap as bootstrap_module
from lakana_core.audit.service import AuditService
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.bootstrap import (
    admin_exists,
    bootstrap_if_needed,
    ensure_bootstrap_admin_grant,
    ensure_bootstrap_principal,
)
from lakana_core.errors import ConflictError, ValidationError
from lakana_core.grants.admin import holds_instance_admin
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.sessions import SessionService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    BrowserSession,
    Grant,
    Passkey,
    PersonalAccessToken,
    Principal,
    PrincipalType,
    SignInLink,
)


def test_the_principal_is_created_on_first_call(session: Session) -> None:
    """A fresh database gains exactly one principal."""
    ref = ensure_bootstrap_principal(session, "admin")

    rows = session.execute(select(Principal)).scalars().all()
    assert [r.name for r in rows] == ["admin"]
    assert ref.name == "admin"
    assert ref.type is PrincipalType.USER


def test_first_boot_records_principal_created_exactly_once(session: Session) -> None:
    """Creating the bootstrap principal records `PRINCIPAL_CREATED`, attributed to itself.

    No other principal exists yet at first boot, so the new principal is
    its own actor.
    """
    ref = ensure_bootstrap_principal(session, "admin")

    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PRINCIPAL_CREATED)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].principal_id == ref.id
    assert events[0].detail == {"principal": "admin", "type": "USER"}


def test_a_restart_records_no_second_principal_created(session: Session) -> None:
    """The idempotent second call finds the existing row and records nothing new."""
    ensure_bootstrap_principal(session, "admin")

    ensure_bootstrap_principal(session, "admin")

    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PRINCIPAL_CREATED)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


def test_the_second_call_returns_the_same_principal(session: Session) -> None:
    """Called on every start, so it must be idempotent."""
    first = ensure_bootstrap_principal(session, "admin")
    second = ensure_bootstrap_principal(session, "admin")

    assert first.id == second.id
    assert len(session.execute(select(Principal)).scalars().all()) == 1


def test_a_different_name_creates_a_second_principal(session: Session) -> None:
    """The name is configuration, not a constant."""
    ensure_bootstrap_principal(session, "admin")
    other = ensure_bootstrap_principal(session, "operator")

    names = sorted(r.name for r in session.execute(select(Principal)).scalars().all())
    assert names == ["admin", "operator"]
    assert other.name == "operator"


def test_a_lost_race_on_first_start_returns_the_winners_principal(session: Session) -> None:
    """Two servers racing to bootstrap must not crash the loser.

    "Two servers starting at once against one database" is the ordinary
    Docker Compose topology, not an exotic race. A real concurrent writer
    cannot be exercised against a single in-process session, so the race is
    simulated by patching the pre-check to miss the row that is, in fact,
    already there (as `test_create_table_race_on_the_unique_constraint_raises_already_exists`
    does for `create_table`), forcing the insert itself to hit the real
    `Principal.name` unique constraint.
    """
    ensure_bootstrap_principal(session, "admin")
    winner = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()

    with mock.patch.object(bootstrap_module, "_lookup_principal", side_effect=[None, winner]):
        result = ensure_bootstrap_principal(session, "admin")

    assert result.id == winner.id
    assert len(session.execute(select(Principal)).scalars().all()) == 1


def test_an_integrity_error_that_is_not_a_name_collision_is_not_misreported(
    session: Session,
) -> None:
    """An `IntegrityError` that is not a name collision must not be swallowed.

    If the post-failure re-select also comes back empty, the failure is not
    the race this function exists to tolerate, and must not silently return
    nothing useful or crash on an `AttributeError`.
    """
    with (
        mock.patch.object(bootstrap_module, "_lookup_principal", return_value=None),
        mock.patch.object(Session, "flush", side_effect=IntegrityError("INSERT", {}, Exception())),
        pytest.raises(ConflictError),
    ):
        ensure_bootstrap_principal(session, "admin")


def test_ensure_bootstrap_admin_grant_creates_one(session: Session) -> None:
    """The bootstrap principal gets an admin grant at instance scope."""
    principal = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, principal, AllowAllAuthorization())

    grants = (
        session.execute(select(Grant).where(Grant.principal_id == principal.id)).scalars().all()
    )
    assert len(grants) == 1
    assert grants[0].role == Role.ADMIN
    assert grants[0].kind == SecurableKind.INSTANCE


def test_ensure_bootstrap_admin_grant_is_idempotent(session: Session) -> None:
    """Calling it twice does not create a second grant."""
    principal = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, principal, AllowAllAuthorization())
    ensure_bootstrap_admin_grant(session, principal, AllowAllAuthorization())

    grants = (
        session.execute(select(Grant).where(Grant.principal_id == principal.id)).scalars().all()
    )
    assert len(grants) == 1


def test_admin_exists_is_false_on_a_fresh_database(session: Session) -> None:
    """No principal, no grant: no admin exists yet."""
    assert admin_exists(session) is False


def test_admin_exists_is_true_for_a_direct_active_grant(session: Session) -> None:
    """A principal with a direct, unrevoked instance ADMIN grant counts."""
    admin = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())

    assert admin_exists(session) is True


def test_admin_exists_is_false_when_the_only_admin_grant_is_revoked(session: Session) -> None:
    """A revoked grant does not count, even though the row still exists.

    Revoked directly on the row, bypassing `GrantService.revoke` — which
    would itself refuse to leave zero active admins. This simulates the
    lockout scenario `admin_exists`/`bootstrap_if_needed` exist to recover
    from: an admin grant gone by some path other than that guarded
    service call (a direct database edit, a bug, a restore from backup).
    """
    admin = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
    [grant] = session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
    grant.revoked_at = datetime.now(UTC)
    session.commit()

    assert admin_exists(session) is False


def test_admin_exists_is_false_for_an_inactive_principal(session: Session) -> None:
    """A deactivated principal's still-unrevoked grant does not count."""
    admin = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
    row = session.get(Principal, admin.id)
    assert row is not None
    row.is_active = False
    session.commit()

    assert admin_exists(session) is False


def test_admin_via_group_counts_as_existing_admin(session: Session) -> None:
    """An instance ADMIN grant on a group, held by an active member, counts too."""
    root = ensure_bootstrap_principal(session, "root")
    principals = PrincipalService(session, AllowAllAuthorization())
    groups = GroupService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    principals.create_principal(root, name="alice", type=PrincipalType.USER)
    groups.create_group(root, name="admins")
    groups.add_member(root, "admins", "alice")
    grants.grant(root, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    assert admin_exists(session) is True


def test_bootstrap_if_needed_does_nothing_when_an_admin_already_exists(session: Session) -> None:
    """The fix for the lockout exploit: an existing admin blocks every bootstrap action."""
    carol = ensure_bootstrap_principal(session, "carol")
    ensure_bootstrap_admin_grant(session, carol, AllowAllAuthorization())

    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token is None
    assert (
        session.execute(select(Principal).where(Principal.name == "admin")).scalar_one_or_none()
        is None
    )


def test_bootstrap_if_needed_creates_grants_and_mints_when_no_admin_exists(
    session: Session,
) -> None:
    """The recovery path: principal, grant, and PAT all come into being."""
    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token == "lakana_pat_testkey_testsecret"
    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    grants = session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
    assert any(
        g.role == Role.ADMIN and g.kind == SecurableKind.INSTANCE and g.revoked_at is None
        for g in grants
    )


def test_bootstrap_pat_is_marked(session: Session) -> None:
    """The PAT `bootstrap_if_needed` mints is the operator's: `bootstrap` is true."""
    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    pat = session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.key_id == "testkey")
    ).scalar_one()
    assert pat.bootstrap is True


def test_bootstrap_if_needed_reactivates_an_inactive_principal(session: Session) -> None:
    """A deactivated bootstrap principal is reactivated as part of recovery."""
    admin = ensure_bootstrap_principal(session, "admin")
    row = session.get(Principal, admin.id)
    assert row is not None
    row.is_active = False
    session.commit()

    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    session.refresh(row)
    assert row.is_active is True


def test_reactivating_the_bootstrap_principal_removes_passkeys_sessions_and_links(
    session: Session,
) -> None:
    """Reactivation ends the account's sessions, removes its passkeys and withdraws its links."""
    admin = ensure_bootstrap_principal(session, "admin")
    now = datetime.now(UTC)
    session.add(
        Passkey(
            id=uuid.uuid4(),
            principal_id=admin.id,
            credential_id="cred",
            public_key=b"k",
            sign_count=0,
            rp_id="localhost",
            name="key",
            backup_eligible=False,
            created_at=now,
            created_user_agent="u",
        )
    )
    link_id = uuid.uuid4()
    session.add(
        SignInLink(
            id=link_id,
            public_id="pub",
            secret_hash="0" * 64,
            kind="recovery",
            principal_id=admin.id,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    session.commit()
    started = SessionService(session, AllowAllAuthorization()).start(
        admin.id, via="passkey", replacing=None, peer="p", user_agent="u", touched=True
    )
    row = session.get(Principal, admin.id)
    assert row is not None
    row.is_active = False
    session.commit()

    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert session.execute(select(Passkey)).all() == []
    ended = session.execute(
        select(BrowserSession).where(BrowserSession.key_id == started.key_id)
    ).scalar_one()
    assert ended.end_reason == "reactivated"
    link = session.get(SignInLink, link_id)
    assert link is not None and link.withdrawn_at is not None


def test_bootstrap_if_needed_regrants_after_the_only_admin_grant_was_revoked(
    session: Session,
) -> None:
    """A revoked-but-only admin grant is treated as no admin, and re-granted."""
    admin = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
    [grant] = session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
    grant.revoked_at = datetime.now(UTC)
    session.commit()

    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    active_grants = (
        session.execute(
            select(Grant).where(Grant.principal_id == admin.id, Grant.revoked_at.is_(None))
        )
        .scalars()
        .all()
    )
    assert any(g.role == Role.ADMIN and g.kind == SecurableKind.INSTANCE for g in active_grants)


def test_bootstrap_if_needed_records_bootstrap_admin_restored(session: Session) -> None:
    """The recovery path is audited, attributed to the bootstrap principal's own id."""
    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    events = (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.BOOTSTRAP_ADMIN_RESTORED
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].principal_id == admin.id


def test_bootstrap_if_needed_mints_a_random_pat_when_no_fixed_credential_is_given(
    session: Session, tmp_path: Path
) -> None:
    """Production configuration (key_id/secret both None) still gets a usable PAT."""
    token = bootstrap_if_needed(session, "admin", token_file=tmp_path / "token")

    assert token is not None
    assert token.startswith("lakana_pat_")


def test_bootstrap_if_needed_raises_if_the_principal_vanishes_immediately(
    session: Session,
) -> None:
    """Fix round 1: the production `assert` here is now an explicit raise.

    Should be unreachable in practice — `ensure_bootstrap_principal` just
    created or found this exact row — but needs its own test to stay
    covered now that it is a real `raise` rather than an `assert`.
    """
    with (
        mock.patch.object(Session, "get", return_value=None),
        pytest.raises(ConflictError, match="vanished"),
    ):
        bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")


def test_bootstrap_if_needed_refuses_when_the_configured_key_id_is_taken(
    session: Session, tmp_path: Path
) -> None:
    """A later recovery reusing the first boot's fixed key id fails; it mints nothing.

    Revoking a PAT never frees its `key_id` in the unique index, so the supplied pair cannot be
    issued again. Before #208 a random token was minted and logged instead; now start-up fails
    with a message that names the key id and the next step, and no file is written.
    """
    first_token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")
    assert first_token == "lakana_pat_testkey_testsecret"
    [grant] = session.execute(select(Grant)).scalars().all()
    grant.revoked_at = datetime.now(UTC)
    session.commit()
    token_file = tmp_path / "token"

    with pytest.raises(ConflictError, match=r"testkey.*LAKANA_BOOTSTRAP_PAT_KEY_ID"):
        bootstrap_if_needed(
            session, "admin", key_id="testkey", secret="testsecret", token_file=token_file
        )
    session.rollback()

    assert not token_file.exists()
    assert admin_exists(session) is False
    assert len(session.execute(select(PersonalAccessToken)).scalars().all()) == 1


def test_a_minted_token_is_written_to_a_private_file(session: Session, tmp_path: Path) -> None:
    """The file holds the token and nothing else, readable by the owner only (mode 0600)."""
    token_file = tmp_path / "private" / "bootstrap-token"

    token = bootstrap_if_needed(session, "admin", token_file=token_file)

    assert token is not None and token.startswith("lakana_pat_")
    assert token_file.read_text() == token + "\n"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_the_token_file_is_0600_whatever_the_umask(session: Session, tmp_path: Path) -> None:
    """A permissive umask cannot widen the file's mode."""
    token_file = tmp_path / "token"
    old = os.umask(0)
    try:
        bootstrap_if_needed(session, "admin", token_file=token_file)
    finally:
        os.umask(old)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_minting_without_a_token_file_fails_before_changing_anything(session: Session) -> None:
    """Neither a supplied key id nor a file: refuse, naming the setting, with no state changed."""
    with pytest.raises(ValidationError, match="LAKANA_BOOTSTRAP_TOKEN_FILE"):
        bootstrap_if_needed(session, "admin")

    assert session.execute(select(Principal)).scalars().all() == []


def test_an_existing_token_file_is_refused_and_left_alone(session: Session, tmp_path: Path) -> None:
    """A file already at the path is never overwritten; the message says to remove it after use."""
    token_file = tmp_path / "token"
    token_file.write_text("older secret\n")

    with pytest.raises(ConflictError, match="remove it after use"):
        bootstrap_if_needed(session, "admin", token_file=token_file)
    session.rollback()

    assert token_file.read_text() == "older secret\n"
    assert admin_exists(session) is False
    assert session.execute(select(PersonalAccessToken)).scalars().all() == []


def test_a_symlink_at_the_token_path_is_refused(session: Session, tmp_path: Path) -> None:
    """A symlink (dangling or not) is never followed: its target stays untouched."""
    target = tmp_path / "victim"
    link = tmp_path / "token"
    link.symlink_to(target)

    with pytest.raises(ConflictError, match="remove it after use"):
        bootstrap_if_needed(session, "admin", token_file=link)
    session.rollback()

    assert not target.exists()
    assert link.is_symlink()


def test_a_token_file_that_cannot_be_written_is_a_clear_error(
    session: Session, tmp_path: Path
) -> None:
    """Any other write failure names the path and the cause, and nothing is committed."""
    blocker = tmp_path / "file"
    blocker.write_text("x")

    with pytest.raises(ConflictError, match="Could not write"):
        bootstrap_if_needed(session, "admin", token_file=blocker / "token")
    session.rollback()

    assert admin_exists(session) is False


@pytest.mark.parametrize(
    ("key_id", "secret"),
    [("testkey", None), (None, "testsecret")],
    ids=["key-id-only", "secret-only"],
)
def test_half_a_supplied_credential_is_refused_before_anything_changes(
    session: Session, tmp_path: Path, key_id: str | None, secret: str | None
) -> None:
    """A key id without its secret (or the reverse) would silently lose the minted token."""
    with pytest.raises(
        ValidationError, match=r"LAKANA_BOOTSTRAP_PAT_KEY_ID.*LAKANA_BOOTSTRAP_PAT_SECRET"
    ):
        bootstrap_if_needed(
            session, "admin", key_id=key_id, secret=secret, token_file=tmp_path / "token"
        )

    assert session.execute(select(Principal)).scalars().all() == []
    assert not (tmp_path / "token").exists()


def test_a_directory_the_server_cannot_write_to_is_a_clear_error(
    session: Session, tmp_path: Path
) -> None:
    """A read-only directory names the path and the cause."""
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(ConflictError, match="Could not write"):
            bootstrap_if_needed(session, "admin", token_file=locked / "token")
    finally:
        locked.chmod(0o700)


def test_a_failed_commit_with_a_supplied_credential_leaves_files_alone(
    session: Session, tmp_path: Path
) -> None:
    """With nothing written, a failing commit has no file to remove and still raises."""
    real_commit = Session.commit
    calls: list[int] = []

    def _second_commit_raises(self: Session) -> None:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("boom")
        real_commit(self)

    with (
        mock.patch.object(Session, "commit", _second_commit_raises),
        pytest.raises(RuntimeError, match="boom"),
    ):
        bootstrap_if_needed(
            session, "admin", key_id="testkey", secret="testsecret", token_file=tmp_path / "t"
        )

    assert not (tmp_path / "t").exists()


def test_a_failed_write_leaves_no_half_written_token_file(session: Session, tmp_path: Path) -> None:
    """If the content cannot be flushed to disk, the empty file is removed and the error named."""
    token_file = tmp_path / "token"

    with (
        mock.patch("lakana_core.bootstrap.os.fsync", side_effect=OSError(28, "No space left")),
        pytest.raises(ConflictError, match="Could not write"),
    ):
        bootstrap_if_needed(session, "admin", token_file=token_file)

    assert not token_file.exists()
    assert admin_exists(session) is False


def test_the_token_file_is_removed_when_the_commit_fails(session: Session, tmp_path: Path) -> None:
    """A file for a token the database never stored would mislead the operator; it is removed."""
    token_file = tmp_path / "token"
    real_commit = Session.commit
    calls: list[int] = []

    def _commit_or_raise(self: Session) -> None:
        calls.append(1)
        if token_file.exists():
            raise RuntimeError("boom")
        real_commit(self)

    with (
        mock.patch.object(Session, "commit", _commit_or_raise),
        pytest.raises(RuntimeError, match="boom"),
    ):
        bootstrap_if_needed(session, "admin", token_file=token_file)

    assert not token_file.exists()


def test_a_supplied_credential_writes_no_file(session: Session, tmp_path: Path) -> None:
    """The caller already knows the token, so nothing is written even when a path is given."""
    token_file = tmp_path / "token"

    token = bootstrap_if_needed(
        session, "admin", key_id="testkey", secret="testsecret", token_file=token_file
    )

    assert token == "lakana_pat_testkey_testsecret"
    assert not token_file.exists()


def test_bootstrap_logs_no_token_at_any_level(
    session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No log record, at any level, from any logger, holds a `lakana_pat_` string."""
    with caplog.at_level(logging.DEBUG):
        bootstrap_if_needed(session, "admin", token_file=tmp_path / "token")

    assert "lakana_pat_" not in caplog.text
    assert not [r for r in caplog.records if "lakana_pat_" in r.getMessage()]


def test_bootstrap_if_needed_revokes_the_principals_earlier_pats(
    session: Session, tmp_path: Path
) -> None:
    """Recovery revokes every unrevoked PAT the principal already had.

    So the freshly minted, freshly logged PAT is the only working
    credential afterward — an operator relying on the newly logged secret
    can trust that no older, unlogged credential for this principal still
    works.
    """
    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")
    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    [first_pat] = (
        session.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.principal_id == admin.id)
        )
        .scalars()
        .all()
    )
    assert first_pat.revoked_at is None

    [grant] = session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
    grant.revoked_at = datetime.now(UTC)
    session.commit()

    bootstrap_if_needed(session, "admin", token_file=tmp_path / "token")

    session.refresh(first_pat)
    assert first_pat.revoked_at is not None


def test_bootstrap_if_needed_rolls_back_cleanly_if_pat_minting_fails(session: Session) -> None:
    """A failure while minting the PAT leaves no committed grant to block a retry.

    Once, the admin grant committed on its own, before the
    PAT was minted — so a PAT-minting failure left a committed admin grant
    with no PAT and no audit event, and, because that principal was then
    an active admin, `admin_exists` would be `True` on the next restart,
    so nothing would ever retry the recovery. Grant, PAT, and audit event
    now share one commit, so a failure here leaves none of them behind.
    """
    with (
        mock.patch.object(PatService, "issue", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")
    session.rollback()

    assert admin_exists(session) is False
    assert session.execute(select(Grant)).scalars().all() == []
    assert session.execute(select(PersonalAccessToken)).scalars().all() == []
    assert (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.BOOTSTRAP_ADMIN_RESTORED
            )
        )
        .scalars()
        .all()
        == []
    )

    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token == "lakana_pat_testkey_testsecret"
    assert admin_exists(session) is True


def test_bootstrap_if_needed_rolls_back_a_real_issue_commit_false_pat_too(
    session: Session,
) -> None:
    """A failure *after* `PatService.issue(..., commit=False)` has really run still rolls back.

    The test above patches `PatService.issue` away
    entirely, so it never exercises `issue`'s own `commit=False` branch
    (`pats.py`'s `if not commit: break`) actually adding a
    `PersonalAccessToken` row and a `PAT_ISSUED` audit event to the
    session. This test lets that branch run for real, and fails only at
    the very last step — recording `BOOTSTRAP_ADMIN_RESTORED` — to prove
    bootstrap's single-commit guarantee still holds when the failure
    happens *after* `issue`'s own session-mutating code, not just when
    `issue` itself is prevented from running.
    """
    real_record = AuditService.record

    def _record_or_raise(
        self: AuditService,
        principal_id: uuid.UUID | None,
        event_type: AuditEventType,
        *,
        securable: Securable | None = None,
        detail: dict | None = None,
        commit: bool = False,
    ) -> AuditEvent:
        if event_type == AuditEventType.BOOTSTRAP_ADMIN_RESTORED:
            raise RuntimeError("boom")
        return real_record(
            self, principal_id, event_type, securable=securable, detail=detail, commit=commit
        )

    with (
        mock.patch.object(AuditService, "record", _record_or_raise),
        pytest.raises(RuntimeError, match="boom"),
    ):
        bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")
    session.rollback()

    assert admin_exists(session) is False
    assert session.execute(select(Grant)).scalars().all() == []
    assert session.execute(select(PersonalAccessToken)).scalars().all() == []
    # `PRINCIPAL_CREATED` survives: `ensure_bootstrap_principal` commits it
    # in its own, separate transaction, before this attempt's later
    # single-commit (grant + PAT + BOOTSTRAP_ADMIN_RESTORED) ever begins —
    # so it is not part of what this rollback undoes.
    surviving = session.execute(select(AuditEvent)).scalars().all()
    assert [e.event_type for e in surviving] == [AuditEventType.PRINCIPAL_CREATED]

    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token == "lakana_pat_testkey_testsecret"
    assert admin_exists(session) is True


def test_the_bootstrap_still_creates_the_first_administrator(
    session: Session, tmp_path: Path
) -> None:
    """The bootstrap principal grants itself ADMIN before it holds it."""
    token = bootstrap_if_needed(session, "admin", token_file=tmp_path / "token")
    assert token is not None and token.startswith("lakana_pat_")
    row = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    assert holds_instance_admin(session, row.id)
    admin = PrincipalRef(id=row.id, name=row.name, type=row.type)
    real = GrantAuthorization(session)
    GrantService(session, real).grant(
        admin, principal="admin", role=Role.VIEWER, kind=SecurableKind.INSTANCE
    )
    assert len(PatService(session, real).list(admin, "admin")) == 1
