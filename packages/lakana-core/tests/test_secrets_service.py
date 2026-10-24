"""Tests for SecretService."""

import base64
import uuid

import pytest
from core_test_helpers import LEGACY_SECRET, LEGACY_VALUES
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.errors import (
    AlreadyExistsError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    Principal,
    PrincipalType,
    Secret,
)
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService


def _key_provider() -> EnvKeyProvider:
    """Build a KeyProvider backed by a fixed, valid master key.

    Returns
    -------
    EnvKeyProvider
        A provider ready to wrap and unwrap data keys.
    """
    return EnvKeyProvider(base64.b64encode(b"0" * 32).decode())


def _add_principal(session: Session, name: str) -> PrincipalRef:
    """Insert a principal and return a PrincipalRef for it.

    Parameters
    ----------
    session
        Database session to insert into.
    name
        The principal's name.

    Returns
    -------
    PrincipalRef
        A reference to the inserted principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _add_environment(session: Session, name: str) -> Environment:
    """Insert and return a bare environment.

    Parameters
    ----------
    session
        Database session to insert into.
    name
        The environment's name.

    Returns
    -------
    Environment
        The inserted environment row.
    """
    env = Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///tmp/{name}")
    session.add(env)
    session.commit()
    return env


def test_create_encrypts_the_values_and_returns_only_metadata(session: Session) -> None:
    """The returned Secret's own fields never carry plaintext or hint at it."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())

    created = service.create(
        admin, environment="dev", name="s3-creds", values={"access_key_id": "AKIA123"}
    )

    assert created.name == "s3-creds"
    assert created.ciphertext != b""
    assert b"AKIA123" not in created.ciphertext


def test_create_raises_not_found_for_an_unknown_environment(session: Session) -> None:
    """Creating a secret for an environment that doesn't exist is a NotFoundError."""
    admin = _add_principal(session, "admin")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())

    with pytest.raises(NotFoundError):
        service.create(admin, environment="nope", name="s3-creds", values={"k": "v"})


def test_create_then_list_for_environment(session: Session) -> None:
    """A created secret shows up when listing that environment's secrets."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())

    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})
    listed = service.list_for_environment(admin, "dev")

    assert [s.id for s in listed] == [created.id]


def test_list_for_environment_excludes_revoked_secrets(session: Session) -> None:
    """A revoked secret does not appear in the active list."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})

    service.revoke(admin, created.id)

    assert service.list_for_environment(admin, "dev") == []


def test_rotate_replaces_the_ciphertext_and_the_wrapped_data_key(session: Session) -> None:
    """Rotating a secret re-encrypts under a fresh data key, same id and name."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    provider = _key_provider()
    service = SecretService(session, AllowAllAuthorization(), provider)
    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "old"})
    created_id = created.id
    original_wrapped_data_key = created.wrapped_data_key
    # Snapshot before rotating: `session.get()` inside `rotate()` returns the
    # same identity-mapped object as `created` (same PK, same session), so
    # `rotate()`'s in-place mutation would otherwise be visible through
    # `created` too, making a post-rotation comparison against `created`
    # trivially self-referential rather than a before/after check.

    rotated = service.rotate(admin, created_id, values={"k": "new"})

    assert rotated.id == created_id
    assert rotated.name == "s3-creds"
    assert rotated.wrapped_data_key != original_wrapped_data_key
    assert decrypt_secret_row(rotated, provider, environment_name="dev") == {"k": "new"}


def test_rotate_raises_not_found_for_an_unknown_id(session: Session) -> None:
    """Rotating a secret id that doesn't exist is a NotFoundError."""
    admin = _add_principal(session, "admin")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())

    with pytest.raises(NotFoundError):
        service.rotate(admin, uuid.uuid4(), values={"k": "v"})


def test_revoke_sets_revoked_at_and_revoked_by(session: Session) -> None:
    """Revoking a secret sets its revoked_at/revoked_by, not deleting the row."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})

    service.revoke(admin, created.id)

    row = session.get(Secret, created.id)
    assert row is not None
    assert row.revoked_at is not None
    assert row.revoked_by == admin.id


def test_revoke_raises_not_found_for_an_unknown_id(session: Session) -> None:
    """Revoking a secret id that doesn't exist is a NotFoundError."""
    admin = _add_principal(session, "admin")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())

    with pytest.raises(NotFoundError):
        service.revoke(admin, uuid.uuid4())


def test_revoke_secret_twice_is_idempotent(session: Session) -> None:
    """Revoking an already-revoked secret is a no-op.

    Matches PATs, task tokens, row filters, and column masks: no second
    event, and the original `revoked_at`/`revoked_by` untouched.
    """
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})
    service.revoke(admin, created.id)
    row = session.get(Secret, created.id)
    assert row is not None
    first_revoked_at = row.revoked_at
    first_revoked_by = row.revoked_by

    service.revoke(admin, created.id)

    session.refresh(row)
    assert row.revoked_at == first_revoked_at
    assert row.revoked_by == first_revoked_by
    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_REVOKED)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


def test_rotate_refuses_a_revoked_secret(session: Session) -> None:
    """Rotating a revoked secret is refused.

    Reactivating a revoked secret's value through `rotate` would silently
    undo the revocation without ever recording a new `SECRET_CREATED` or
    un-revoking event.
    """
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})
    service.revoke(admin, created.id)

    with pytest.raises(ValidationError):
        service.rotate(admin, created.id, values={"k": "new"})


def test_create_raises_already_exists_for_a_duplicate_name(session: Session) -> None:
    """A second create with the same (environment, name) raises AlreadyExistsError.

    Not a raw `sqlalchemy.exc.IntegrityError` -- that would leak a SQL
    statement and its bound parameters to a caller once this is wired
    into a REST endpoint.
    """
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})

    with pytest.raises(AlreadyExistsError):
        service.create(admin, environment="dev", name="s3-creds", values={"k": "v2"})


def test_rotate_and_revoke_authorize_against_the_secrets_own_environment(
    session: Session,
) -> None:
    """rotate/revoke authorize MANAGE_SECRET scoped to the secret's own environment.

    Not the instance, and not any other environment -- the one the secret
    actually belongs to, looked up from its own row.
    """

    class _RecordAll:
        def __init__(self) -> None:
            self.seen: list[tuple[Action, Securable]] = []

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            self.seen.append((action, securable))

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    _add_environment(session, "prod")
    creator = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = creator.create(admin, environment="dev", name="s3-creds", values={"k": "v"})

    authz = _RecordAll()
    service = SecretService(session, authz, _key_provider())

    service.rotate(admin, created.id, values={"k": "new"})
    assert authz.seen == [(Action.MANAGE_SECRET, Securable.environment_scope("dev"))]

    authz.seen.clear()
    service.revoke(admin, created.id)
    assert authz.seen == [(Action.MANAGE_SECRET, Securable.environment_scope("dev"))]


def test_rotate_and_revoke_leave_the_secret_unchanged_when_authorization_denies(
    session: Session,
) -> None:
    """A PermissionDenied from authz propagates, and the row is untouched.

    Both `rotate` and `revoke` look up the row (to learn its environment)
    before authorizing; this confirms that lookup never leaks into a
    mutation when the authorization check itself then denies -- the one
    security property this task exists to establish for these two methods.
    """

    class _DenyAll:
        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            raise PermissionDenied(f"denied: {action} on {securable}")

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    creator = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = creator.create(admin, environment="dev", name="s3-creds", values={"k": "v"})
    original_ciphertext = created.ciphertext
    original_wrapped_data_key = created.wrapped_data_key

    service = SecretService(session, _DenyAll(), _key_provider())

    with pytest.raises(PermissionDenied):
        service.rotate(admin, created.id, values={"k": "new"})

    unchanged = session.get(Secret, created.id)
    assert unchanged is not None
    assert unchanged.ciphertext == original_ciphertext
    assert unchanged.wrapped_data_key == original_wrapped_data_key

    with pytest.raises(PermissionDenied):
        service.revoke(admin, created.id)

    still_unchanged = session.get(Secret, created.id)
    assert still_unchanged is not None
    assert still_unchanged.revoked_at is None


def test_create_authorizes_against_the_named_environment(session: Session) -> None:
    """Create authorizes MANAGE_SECRET scoped to the given environment, not instance-wide."""

    class _RecordAll:
        def __init__(self) -> None:
            self.seen: list[tuple[Action, Securable]] = []

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            self.seen.append((action, securable))

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    authz = _RecordAll()
    service = SecretService(session, authz, _key_provider())

    service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})

    assert authz.seen == [(Action.MANAGE_SECRET, Securable.environment_scope("dev"))]


def test_create_records_a_secret_created_audit_event(session: Session) -> None:
    """Creating a secret writes a matching AuditEvent, never the plaintext value."""
    from lakana_core.models import AuditEvent, AuditEventType

    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())

    service.create(admin, environment="dev", name="s3-creds", values={"k": "SUPERSECRET"})

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.SECRET_CREATED).all()
    assert len(events) == 1
    assert events[0].securable_environment == "dev"
    assert events[0].detail == {"name": "s3-creds", "use": None}
    assert "SUPERSECRET" not in str(events[0].detail)


def test_rotate_records_a_secret_rotated_audit_event(session: Session) -> None:
    """Rotating a secret writes a matching AuditEvent, never the new plaintext value."""
    from lakana_core.models import AuditEvent, AuditEventType

    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "old"})

    service.rotate(admin, created.id, values={"k": "NEWSECRETVALUE"})

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.SECRET_ROTATED).all()
    assert len(events) == 1
    assert events[0].detail == {"name": "s3-creds"}
    assert "NEWSECRETVALUE" not in str(events[0].detail)


def test_a_rotate_that_fails_to_encrypt_records_no_audit_event(session: Session) -> None:
    """A rotate that fails during encryption must not leave a pending audit event.

    `record()` is called only after `encrypt_secret_values` succeeds -- if it
    ran first, a failure here (no usable master key, the same failure mode
    `test_secrets_keys.py` exercises directly) would leave a `SECRET_ROTATED`
    `AuditEvent` added to the session with nothing to roll it back, ready to
    be committed later by an unrelated action sharing the same session and
    recording an audit event for a rotation that never happened.
    """
    from lakana_core.models import AuditEvent, AuditEventType

    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    creator = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = creator.create(admin, environment="dev", name="s3-creds", values={"k": "old"})

    no_master_key_service = SecretService(session, AllowAllAuthorization(), EnvKeyProvider(None))
    with pytest.raises(LakanaError, match="master key"):
        no_master_key_service.rotate(admin, created.id, values={"k": "new"})

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.SECRET_ROTATED).all()
    assert events == []


def test_revoke_records_a_secret_revoked_audit_event(session: Session) -> None:
    """Revoking a secret writes a matching AuditEvent."""
    from lakana_core.models import AuditEvent, AuditEventType

    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})

    service.revoke(admin, created.id)

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.SECRET_REVOKED).all()
    assert len(events) == 1
    assert events[0].detail == {"name": "s3-creds"}


def test_a_duplicate_secret_name_rolls_back_the_audit_event_too(session: Session) -> None:
    """The same-transaction guarantee: a failed create leaves no orphaned audit row.

    Reuses the real, already-existing IntegrityError path (a duplicate
    (environment, name)) rather than a synthetic failure -- this is the one
    call site in this plan with a genuine, naturally-occurring rollback
    scenario to test against.
    """
    from lakana_core.errors import AlreadyExistsError
    from lakana_core.models import AuditEvent, AuditEventType

    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})

    with pytest.raises(AlreadyExistsError):
        service.create(admin, environment="dev", name="s3-creds", values={"k": "v2"})

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.SECRET_CREATED).all()
    assert (
        len(events) == 1
    )  # only the first, successful create -- the second's audit row rolled back too


def test_create_and_rotate_record_the_key_that_wrapped_the_data_key(session: Session) -> None:
    """Each written row names the provider's key; a rotation writes it again."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    provider = _key_provider()
    service = SecretService(session, AllowAllAuthorization(), provider)

    created = service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})
    assert created.key_id == provider.key_id
    assert created.format_version == 2

    created.key_id = "env:stale"
    rotated = service.rotate(admin, created.id, values={"k": "w"})
    assert rotated.key_id == provider.key_id


def _legacy_row(session: Session, row: Secret) -> None:
    """Overwrite `row` with a secret as the previous release stored it.

    Parameters
    ----------
    session
        The session holding `row`.
    row
        The row to overwrite: format 1, bare `env` key id, and the bytes of
        `origin/main`'s code (see `core_test_helpers.LEGACY_SECRET`).
    """
    row.ciphertext = LEGACY_SECRET.ciphertext
    row.nonce = LEGACY_SECRET.nonce
    row.wrapped_data_key = LEGACY_SECRET.wrapped_data_key
    row.format_version = 1
    row.key_id = "env"
    session.commit()


def test_a_format_1_secret_is_read_only_when_accepted_and_becomes_format_2_when_set_again(
    session: Session,
) -> None:
    """Legacy rows need the setting; a rotation (setting it again) needs none and upgrades them."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    provider = _key_provider()
    service = SecretService(session, AllowAllAuthorization(), provider)
    row = service.create(admin, environment="dev", name="s3-creds", values={"k": "v"})
    _legacy_row(session, row)

    listed = service.list_for_environment(admin, "dev")
    assert [(s.format_version, s.key_id) for s in listed] == [(1, "env")]
    with pytest.raises(LakanaError, match="stored in the old format"):
        decrypt_secret_row(row, provider, environment_name="dev")
    assert (
        decrypt_secret_row(row, provider, environment_name="dev", accept_format_1=True)
        == LEGACY_VALUES
    )

    rotated = service.rotate(admin, row.id, values={"k": "new"})

    assert (rotated.format_version, rotated.key_id) == (2, provider.key_id)
    assert decrypt_secret_row(rotated, provider, environment_name="dev") == {"k": "new"}


def test_a_secret_copied_onto_another_row_is_refused_end_to_end(session: Session) -> None:
    """Copying one stored secret's triple onto another row (same or other environment) fails."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    _add_environment(session, "prod")
    provider = _key_provider()
    service = SecretService(session, AllowAllAuthorization(), provider)
    source = service.create(admin, environment="dev", name="a", values={"k": "a"})
    same_env = service.create(admin, environment="dev", name="b", values={"k": "b"})
    other_env = service.create(admin, environment="prod", name="a", values={"k": "c"})

    for target, env_name in ((same_env, "dev"), (other_env, "prod")):
        target.ciphertext = source.ciphertext
        target.nonce = source.nonce
        target.wrapped_data_key = source.wrapped_data_key
        session.commit()
        with pytest.raises(LakanaError, match="could not be decrypted"):
            decrypt_secret_row(target, provider, environment_name=env_name)
    assert decrypt_secret_row(source, provider, environment_name="dev") == {"k": "a"}


def test_a_format_2_row_marked_format_1_is_refused_end_to_end(session: Session) -> None:
    """An attacker who sets `format_version` back to 1 gets no unbound read."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    provider = _key_provider()
    row = SecretService(session, AllowAllAuthorization(), provider).create(
        admin, environment="dev", name="a", values={"k": "a"}
    )
    row.format_version = 1
    session.commit()

    with pytest.raises(LakanaError, match="stored in the old format"):
        decrypt_secret_row(row, provider, environment_name="dev")
    with pytest.raises(LakanaError, match="could not be decrypted"):
        decrypt_secret_row(row, provider, environment_name="dev", accept_format_1=True)


def test_a_format_1_triple_pasted_onto_a_format_2_row_is_refused_end_to_end(
    session: Session,
) -> None:
    """Database write access and no master key: the paste reads nothing unless the setting is on.

    With the setting on it does read: the documented residual of accepting old-format secrets.
    """
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    provider = _key_provider()
    row = SecretService(session, AllowAllAuthorization(), provider).create(
        admin, environment="dev", name="a", values={"k": "a"}
    )
    for key_id in (row.key_id, "env", "env:ffffffffffffffff"):
        row.ciphertext = LEGACY_SECRET.ciphertext
        row.nonce = LEGACY_SECRET.nonce
        row.wrapped_data_key = LEGACY_SECRET.wrapped_data_key
        row.format_version = 1
        row.key_id = key_id
        session.commit()

        with pytest.raises(LakanaError, match="stored in the old format"):
            decrypt_secret_row(row, provider, environment_name="dev")
        assert (
            decrypt_secret_row(row, provider, environment_name="dev", accept_format_1=True)
            == LEGACY_VALUES
        )


def test_a_value_that_is_not_text_is_refused_at_create_and_rotate_naming_only_its_key(
    session: Session,
) -> None:
    """A lone surrogate cannot be written as UTF-8: no task could ever be shown it masked."""
    admin = _add_principal(session, "admin")
    _add_environment(session, "dev")
    service = SecretService(session, AllowAllAuthorization(), _key_provider())
    with pytest.raises(ValidationError) as refused:
        service.create(admin, environment="dev", name="bad", values={"password": "x\ud800yz"})
    assert str(refused.value) == (
        "the value of `password` is not valid text (it cannot be written as UTF-8); "
        "give it again as plain text"
    )
    assert session.execute(select(Secret).where(Secret.name == "bad")).first() is None
    created = service.create(admin, environment="dev", name="good", values={"password": "ok12"})
    with pytest.raises(ValidationError) as rotated:
        service.rotate(admin, created.id, values={"token": "\udfff"})
    assert "`token`" in str(rotated.value)
    assert "\udfff" not in str(rotated.value)
