"""Secret creation, listing, rotation, binding, and revocation.

Every method here authorizes `Action.MANAGE_SECRET` before doing anything
else it can (`create`/`list_for_environment`, which already know the
target environment from their own parameters) or as soon as it can
(`rotate`/`revoke`, which must look up the row first to learn its
environment). This service never decrypts a secret's value — see
`lakana_core.secrets.crypto` for why, and `lakana_core.storage.resolver` and
`lakana_worker.deploy` and `lakana_worker.index_credentials` for the places decryption
actually happens.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.connections.endpoints import endpoint_hosts
from lakana_core.db import refuse_concurrent_change
from lakana_core.errors import AlreadyExistsError, ConflictError, NotFoundError, ValidationError
from lakana_core.identifiers import shown
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    Connection,
    Environment,
    Secret,
    SecretUseKind,
)
from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
from lakana_core.secrets.forms import require_text
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import (
    SecretUse,
    adopt_connection_secret,
    detach_from_connection,
    lock_connections,
    lock_entry,
    own_secret_steps,
    record_binding,
    require_connection_takes,
    resolve_use,
    target_row,
    used_by_its_environment,
)


def insert_secret(
    session: Session,
    key_provider: KeyProvider,
    p: PrincipalRef,
    environment: Environment,
    *,
    name: str,
    values: dict[str, str],
    use: SecretUse | None,
) -> Secret:
    """Encrypt and add a new secret, born bound to `use`; no authorization, no commit.

    The caller has authorized, holds the lock of the entry `use` names (if any), and commits.
    Adds `SECRET_CREATED`, and `SECRET_BOUND` when `use` is given.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Wraps the secret's fresh data key.
    p
        The acting principal, recorded as the creator.
    environment
        The secret's environment.
    name
        The secret's name, unique in the environment (the database refuses a second one when
        the caller commits).
    values
        The plaintext values.
    use
        What the secret is for; `None` leaves it unbound.

    Returns
    -------
    Secret
        The pending row.

    Raises
    ------
    ValidationError
        If `use` is an index, repository or connection and the environment itself uses a
        secret of this name, for storage or webhooks.
        If a value is not valid text (`lakana_core.secrets.forms.require_text`).
    LakanaError
        If no usable secrets master key is configured.
    """
    if (
        use is not None
        and use.kind is not SecretUseKind.ENVIRONMENT
        and used_by_its_environment(session, environment, name)
    ):
        raise ValidationError(
            f"Environment '{shown(environment.name)}' uses the name '{shown(name)}' for its "
            f"storage or a webhook, so a secret of that name cannot be created for "
            f"{use.wanted()}. Create it with --own-env, or give the {_noun(use)} a secret under "
            "another name."
        )
    require_text(values)
    secret_id = uuid.uuid4()
    encrypted = encrypt_secret_values(
        values, key_provider, SecretBinding(secret_id, environment.id, name)
    )
    secret = Secret(
        id=secret_id,
        environment_id=environment.id,
        name=name,
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        wrapped_data_key=encrypted.wrapped_data_key,
        format_version=encrypted.format_version,
        key_id=encrypted.key_id,
        created_by=p.id,
        created_at=datetime.now(UTC),
    )
    scope = Securable.environment_scope(environment.name)
    AuditService(session).record(
        p.id,
        AuditEventType.SECRET_CREATED,
        securable=scope,
        detail={"name": name, "use": use.kind.value if use is not None else None},
    )
    if use is not None:
        record_binding(session, p, secret, scope, use)
    session.add(secret)
    return secret


def rotate_values(
    session: Session,
    key_provider: KeyProvider,
    p: PrincipalRef,
    secret: Secret,
    values: dict[str, str],
) -> None:
    """Encrypt new values into a secret under a fresh data key; no authorization, no commit.

    The caller has authorized and commits. Adds `SECRET_ROTATED`.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Wraps the fresh data key.
    p
        The acting principal.
    secret
        The secret, active.
    values
        The new plaintext values.

    Raises
    ------
    ValidationError
        If a value is not valid text (`lakana_core.secrets.forms.require_text`).
    LakanaError
        If no usable secrets master key is configured.
    """
    require_text(values)
    env = session.get(Environment, secret.environment_id)
    assert env is not None  # nosec B101 -- environment_id is a non-nullable FK with ON DELETE CASCADE
    encrypted = encrypt_secret_values(
        values, key_provider, SecretBinding(secret.id, secret.environment_id, secret.name)
    )
    AuditService(session).record(
        p.id,
        AuditEventType.SECRET_ROTATED,
        securable=Securable.environment_scope(env.name),
        detail={"name": secret.name},
    )
    secret.ciphertext = encrypted.ciphertext
    secret.nonce = encrypted.nonce
    secret.wrapped_data_key = encrypted.wrapped_data_key
    secret.format_version = encrypted.format_version
    secret.key_id = encrypted.key_id


def _noun(use: SecretUse) -> str:
    """Name the kind of entry a use is, for a message.

    Parameters
    ----------
    use
        An index, repository or connection use.

    Returns
    -------
    str
        ``connection`` for a connection, else ``index or repository``.
    """
    return "connection" if use.kind is SecretUseKind.CONNECTION else "index or repository"


class SecretService:
    """Create, list, rotate, and revoke secrets.

    Parameters
    ----------
    session
        Database session. Each mutating method commits internally.
    authz
        Authorization service consulted before every operation.
    key_provider
        Wraps each secret's freshly generated data key. Never used to
        decrypt anything here — only `encrypt_secret_values` is called
        from this class.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, key_provider: KeyProvider
    ) -> None:
        self._session = session
        self._authz = authz
        self._key_provider = key_provider

    def _get_environment(self, name: str) -> Environment:
        """Look up an environment by name.

        Parameters
        ----------
        name
            Environment name.

        Returns
        -------
        Environment
            The matching row.

        Raises
        ------
        NotFoundError
            If no environment has that name.
        """
        env = self._session.execute(
            select(Environment).where(Environment.name == name, LIVE_ENVIRONMENT)
        ).scalar_one_or_none()
        if env is None:
            raise NotFoundError(f"No environment named {name!r}.")
        return env

    def _get_secret(self, secret_id: uuid.UUID) -> Secret:
        """Look up a secret by id, without authorizing.

        Parameters
        ----------
        secret_id
            The secret's id.

        Returns
        -------
        Secret
            The matching row.

        Raises
        ------
        NotFoundError
            If no secret has that id.
        """
        secret = self._session.get(Secret, secret_id)
        if secret is None:
            raise NotFoundError(f"No secret {secret_id}.")
        return secret

    def create(
        self,
        p: PrincipalRef,
        *,
        environment: str,
        name: str,
        values: dict[str, str],
        use: SecretUse | None = None,
    ) -> Secret:
        """Create a secret.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment this secret is scoped to.
        name
            The secret's name, unique within `environment`.
        values
            The plaintext key-value bag to encrypt and store.
        use
            What the secret is for; `None` leaves it unbound until `bind`. Naming it from an
            index or repository binds it only for someone who may manage secrets. A
            connection's secret becomes the one the connection uses.

        Returns
        -------
        Secret
            The created row. Its `ciphertext`/`nonce`/`wrapped_data_key`
            fields are the encrypted form; nothing about `values` is
            otherwise retrievable from the return value.

        Raises
        ------
        NotFoundError
            If no environment named `environment` exists.
        AlreadyExistsError
            If a secret named `name` already exists in `environment`.
        LakanaError
            If no usable secrets master key is configured.
        PermissionDenied
            If the acting principal may not manage secrets in this
            environment.
        ValidationError
            If `use` is an index, repository or connection and the environment itself uses a
            secret of this name, for storage or webhooks, or `use` is a connection of another
            environment.
        """
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(environment))
        env = self._get_environment(environment)
        with refuse_concurrent_change(self._session, f"Secret '{shown(name)}'"):
            if use is not None:
                use = lock_entry(self._session, use, env)
                require_connection_takes(self._session, use, name, values)
            secret = insert_secret(
                self._session, self._key_provider, p, env, name=name, values=values, use=use
            )
            self._commit_created(secret, use, p, env, name)
        return secret

    def _commit_created(
        self, secret: Secret, use: SecretUse | None, p: PrincipalRef, env: Environment, name: str
    ) -> None:
        """Commit a new secret; a connection's becomes the one it uses.

        Parameters
        ----------
        secret
            The pending secret.
        use
            Its use.
        p
            The acting principal.
        env
            Its environment.
        name
            Its name.

        Raises
        ------
        AlreadyExistsError
            If a secret of that name exists in the environment.
        """
        environment = env.name
        try:
            if use is not None and use.kind is SecretUseKind.CONNECTION:
                self._session.flush()
                adopt_connection_secret(self._session, p, secret, env.name)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            # A fixed message, not `str(exc)`: SQLAlchemy's `IntegrityError`
            # string embeds the failed statement and its bound parameters,
            # which must never reach a caller.
            raise AlreadyExistsError(
                f"A secret named {name!r} already exists in environment {environment!r}."
            ) from exc

    def bind(self, p: PrincipalRef, secret_id: uuid.UUID, use: SecretUse) -> Secret:
        """Bind a secret to a use, whatever it was bound to before.

        Bound to a connection, the secret becomes the one the connection uses, and the
        connection's other secrets are unbound, in the same transaction.

        Parameters
        ----------
        p
            The acting principal.
        secret_id
            The secret's id.
        use
            The use (`resolve_use` builds it from names).

        Returns
        -------
        Secret
            The updated row.

        Raises
        ------
        NotFoundError
            If no secret has that id.
        ValidationError
            If the secret has been revoked, is the environment's storage secret and `use` is
            an index, a repository or a connection, or `use` is a connection of another
            environment.
        PermissionDenied
            If the acting principal may not manage secrets in the secret's environment.
        """
        secret = self._get_secret(secret_id)
        env = self._session.get(Environment, secret.environment_id)
        assert env is not None  # nosec B101 -- environment_id is a non-nullable FK with ON DELETE CASCADE
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(env.name))
        with refuse_concurrent_change(self._session, f"Secret '{shown(secret.name)}'"):
            self._bind_locked(p, secret, env, use)
        return secret

    def _bind_locked(
        self, p: PrincipalRef, secret: Secret, env: Environment, use: SecretUse
    ) -> None:
        """Take the locks of a bind, check it and commit it.

        The connections first (the one the secret leaves and the one it goes to, by id), then
        the entry, then the secret: the order every naming and URL change takes too.

        Parameters
        ----------
        p
            The acting principal.
        secret
            The secret, read without a lock.
        env
            Its environment.
        use
            The new use.

        Raises
        ------
        ValidationError
            If the secret has been revoked, is the storage secret and `use` is not the
            environment, or `use` is a connection that takes no secret.
        ConflictError
            If the secret was bound elsewhere while the locks were taken; try again.
        """
        previous = secret.use_target_id if secret.use_kind is SecretUseKind.CONNECTION else None
        target = use.target_id if use.kind is SecretUseKind.CONNECTION else None
        lock_connections(self._session, (previous, target))
        use = lock_entry(self._session, use, env)
        self._session.refresh(secret, with_for_update=True)
        if secret.use_kind is SecretUseKind.CONNECTION and secret.use_target_id not in (
            previous,
            target,
        ):
            self._session.rollback()
            raise ConflictError(
                f"Secret '{shown(secret.name)}' changed at the same time; try again."
            )
        if secret.revoked_at is not None:
            raise ValidationError(f"Secret {secret.id} has been revoked and cannot be bound.")
        if use.kind is not SecretUseKind.ENVIRONMENT and env.storage_secret_ref == secret.name:
            raise ValidationError(
                f"Secret '{shown(secret.name)}' is the storage secret of environment "
                f"'{shown(env.name)}', so binding it to {use.wanted()} would stop its storage. "
                f"Give {use.wanted()} a secret of its own: {own_secret_steps(env.name, use)}."
            )
        require_connection_takes(self._session, use, secret.name, None)
        scope = Securable.environment_scope(env.name)
        if secret.use_target_id != target:
            detach_from_connection(self._session, secret, previous)
        record_binding(self._session, p, secret, scope, use)
        adopt_connection_secret(self._session, p, secret, env.name)
        self._session.commit()

    def use_from_names(
        self, p: PrincipalRef, environment: str, kind: SecretUseKind, target: str | None
    ) -> SecretUse:
        """Turn a use as a caller names it into a `SecretUse`, after checking the right.

        The right is checked first, so a caller who may not manage secrets learns nothing
        about which indexes, repositories or connections exist. A connection is looked up in
        `environment`, the secret's own.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment the secret is scoped to.
        kind
            The kind of use.
        target
            The index's, repository's or connection's name; `None` for the environment.

        Returns
        -------
        SecretUse
            The use, with the target's id.

        Raises
        ------
        PermissionDenied
            If `p` may not manage secrets in `environment`.
        NotFoundError
            If the environment does not exist, no index or repository has that name, or the
            environment has no such connection.
        ValidationError
            If the environment use is given a name, or an index or repository use none.
        """
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(environment))
        environment_id = (
            self._get_environment(environment).id if kind is SecretUseKind.CONNECTION else None
        )
        return resolve_use(self._session, kind, target, environment_id=environment_id)

    def use_target_name(self, secret: Secret) -> str | None:
        """Name the index, repository or connection a secret is bound to, for a listing.

        Parameters
        ----------
        secret
            The row.

        Returns
        -------
        str or None
            The name; `None` for an unbound secret, the environment use, or a target that was
            removed.
        """
        if secret.use_kind is None:
            return None
        row = target_row(self._session, secret.use_kind, secret.use_target_id)
        return row.name if row is not None else None

    def use_target_url(self, secret: Secret) -> str | None:
        """Give where the index, repository or connection a secret is bound to sends it.

        Parameters
        ----------
        secret
            The row.

        Returns
        -------
        str or None
            The URL of an index or repository; for a connection, which has no URL, every
            ``host:port`` it sends the secret to, joined by ``, `` (an oauth2 token URL's host
            included); never a secret. `None` for an unbound secret, the environment use, or a
            target that was removed.
        """
        if secret.use_kind is None:
            return None
        row = target_row(self._session, secret.use_kind, secret.use_target_id)
        if isinstance(row, Connection):
            return endpoint_hosts(row.kind, row.address, row.settings)
        return row.url if row is not None else None

    def list_for_environment(self, p: PrincipalRef, environment: str) -> list[Secret]:
        """List an environment's active secrets.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment whose secrets to list.

        Returns
        -------
        list of Secret
            Every active secret in `environment`. Each row's encrypted
            fields are present (they are the row), but nothing decrypts
            them here or anywhere downstream of this method.

        Raises
        ------
        NotFoundError
            If no environment has that name.
        PermissionDenied
            If the acting principal may not manage secrets in this
            environment.
        """
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(environment))
        env = self._get_environment(environment)
        return list(
            self._session.execute(
                select(Secret).where(Secret.environment_id == env.id, Secret.revoked_at.is_(None))
            )
            .scalars()
            .all()
        )

    def rotate(self, p: PrincipalRef, secret_id: uuid.UUID, *, values: dict[str, str]) -> Secret:
        """Re-encrypt a secret's value under a freshly generated data key.

        The secret is written in the current stored format, whatever format
        it was in: this is how a secret stored in an older format is
        upgraded.

        The secret's id and name are unchanged, so anything referencing it
        by name (an environment's `storage_secret_ref`) picks up the new
        value on next use, with no reference update needed anywhere.

        Parameters
        ----------
        p
            The acting principal.
        secret_id
            The secret's id.
        values
            The new plaintext key-value bag.

        Returns
        -------
        Secret
            The updated row.

        Raises
        ------
        NotFoundError
            If no secret has that id.
        ValidationError
            If the secret has been revoked.
        LakanaError
            If no usable secrets master key is configured.
        PermissionDenied
            If the acting principal may not manage secrets in this
            secret's environment.
        """
        secret = self._get_secret(secret_id)
        env = self._session.get(Environment, secret.environment_id)
        assert env is not None  # nosec B101 -- environment_id is a non-nullable FK with ON DELETE CASCADE
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(env.name))
        if secret.revoked_at is not None:
            raise ValidationError(f"Secret {secret_id} has been revoked and cannot be rotated.")
        rotate_values(self._session, self._key_provider, p, secret, values)
        self._session.commit()
        return secret

    def revoke(self, p: PrincipalRef, secret_id: uuid.UUID) -> None:
        """Revoke a secret.

        Idempotent: revoking an already-revoked secret is a no-op — it
        leaves the existing `revoked_at`/`revoked_by` untouched and records
        no second `SECRET_REVOKED` event, matching PATs, task tokens, row
        filters, and column masks.

        Parameters
        ----------
        p
            The acting principal.
        secret_id
            The secret's id.

        Raises
        ------
        NotFoundError
            If no secret has that id.
        PermissionDenied
            If the acting principal may not manage secrets in this
            secret's environment.
        """
        secret = self._get_secret(secret_id)
        env = self._session.get(Environment, secret.environment_id)
        assert env is not None  # nosec B101 -- environment_id is a non-nullable FK with ON DELETE CASCADE
        self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(env.name))
        if secret.revoked_at is not None:
            return
        AuditService(self._session).record(
            p.id,
            AuditEventType.SECRET_REVOKED,
            securable=Securable.environment_scope(env.name),
            detail={"name": secret.name},
        )
        secret.revoked_at = datetime.now(UTC)
        secret.revoked_by = p.id
        self._session.commit()
