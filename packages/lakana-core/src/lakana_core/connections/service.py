"""Create, list, show, change and delete the connections of an environment.

`MANAGE_CONNECTION` on the environment creates, changes and deletes; `READ_ENVIRONMENT` lists
and shows. A connection has at most one bound secret (`SecretUseKind.CONNECTION`), and who may
give it which secret is split by case:

- new values (`--password-stdin`, `--values-stdin`) create a secret `<name>-connection` (or the
  next free `<name>-connection-N` when a secret holds that name), born bound to the connection,
  or set the connection's own secret again: `MANAGE_CONNECTION` is enough, because nothing that
  existed before goes anywhere new;
- naming the secret already bound to the connection keeps it: `MANAGE_CONNECTION` is enough;
- naming any other secret binds it only for a holder of `MANAGE_SECRET`
  (`bind_or_require_named_secrets`), and a secret bound elsewhere or used by the environment is
  refused to everyone;
- a change that could send a bound secret somewhere new (its address, a weaker TLS, another CA
  certificate, another way of sending it: `moves_secret`) needs `MANAGE_SECRET` too, unless the
  same change gives new values.

When a connection takes a secret, every other secret bound to it is let go (unbound), so that
"keep the bound secret" can only ever mean the one it uses. No method decrypts a secret, and no
view, message or audit event holds a secret value or the text of a CA certificate.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.connections.kinds import (
    NAME,
    ConnectionKind,
    check_secret_values,
    fingerprint,
    follow_region,
    merge_part,
    moves_secret,
    takes_secret,
    validate_connection,
)
from lakana_core.db import refuse_concurrent_change
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import shown
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    Connection,
    ConnectionTest,
    ConnectionTestOutcome,
    ConnectionTestState,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    Repository,
    Secret,
    SecretUseKind,
)
from lakana_core.models.base import utcnow
from lakana_core.pagination import Page, newest_first
from lakana_core.projects.manifest import manifest_assets, read_entry_connections
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.service import insert_secret, rotate_values
from lakana_core.secrets.uses import (
    SecretBindingRefused,
    SecretUse,
    bind_or_require_named_secrets,
    lock_row,
    record_refusal,
    release_connection_secrets,
    require_secret_management_for_url_change,
    used_by_its_environment,
)

_NAME_RULE = (
    "connection names are lower-case letters, digits and '-', starting with a letter, at most 63"
)
_BOTH = "give either --secret or --password-stdin/--values-stdin, not both"
_MAX_SECRET_NUMBER = 100
"""The highest N of a new secret's name `<connection>-connection-N`."""
TEST_STUCK_AFTER = timedelta(minutes=10)
"""How long a queued or running connection test may stay open; a request for a new test closes an
older one as failed."""
_TEST_STUCK = (
    "no worker finished this test within 10 minutes; a worker that runs Python tasks runs "
    "connection tests — see `docker compose logs worker`"
)
_CLEAR_SECRET = "secret"  # nosec B105 -- the word a caller clears, not a credential


@dataclass(frozen=True)
class ConnectionView:
    """A connection as a caller may see it: never a secret value, never a CA certificate.

    Parameters
    ----------
    name
        Its name.
    kind
        Its kind.
    address
        Its address.
    settings
        Its settings; a ``ca_certificate`` is shown as its SHA-256 fingerprints.
    secret
        The name of the secret it uses, or `None`.
    declared_by
        ``(repository, asset)`` pairs whose deployed version declares it, sorted.
    created_by
        The name of the principal who created it.
    created_at
        When it was created.
    updated_at
        When it was last changed.
    """

    name: str
    kind: ConnectionKind
    address: dict[str, Any]
    settings: dict[str, Any]
    secret: str | None
    declared_by: list[tuple[str, str]]
    created_by: str
    created_at: datetime
    updated_at: datetime


def missing_connections(
    session: Session, environment_id: uuid.UUID, names: Sequence[str], *, lock: bool
) -> list[str]:
    """Name the connections an environment does not have, of those asked for.

    A deploy calls it with `lock` so that the rows it found stay until it commits: a delete of
    one of them waits (it locks the row for update) and then sees the new deployed version.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    names
        The connection names, as declared.
    lock
        Read the found rows ``FOR SHARE`` (Postgres; nothing on SQLite) until the transaction
        ends.

    Returns
    -------
    list of str
        The names not found, in the order given.
    """
    if not names:
        return []
    query = select(Connection.name).where(
        Connection.environment_id == environment_id, Connection.name.in_(set(names))
    )
    if lock:
        query = query.with_for_update(read=True)
    found = set(session.execute(query).scalars())
    return [name for name in names if name not in found]


def _changed(part: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Name the keys of a part whose value changed, as ``part.key``.

    Parameters
    ----------
    part
        ``address`` or ``settings``.
    before
        The part before.
    after
        The part after.

    Returns
    -------
    list of str
        The changed keys, sorted.
    """
    return sorted(
        f"{part}.{key}" for key in set(before) | set(after) if before.get(key) != after.get(key)
    )


_TASK_REFUSED = "a task token cannot list or show connections; read them with your own token"


def _refuse_task(p: PrincipalRef) -> None:
    """Refuse a principal acting with a task token, bound to a run or not.

    `READ_ENVIRONMENT` is an action a task token carries, but a task must not see the hosts,
    users, secret names or declaring assets of the environment's connections; a running task
    fetches only the connections its asset declares (`lakana_core.connections.fetch`). The
    refusal names no connection.

    Parameters
    ----------
    p
        The acting principal.

    Raises
    ------
    PermissionDenied
        If `p` acts with a task token.
    """
    if p.task_scope is not None:
        raise PermissionDenied(_TASK_REFUSED)


class ConnectionService:
    """Connections of an environment, authorized and audited.

    Parameters
    ----------
    session
        Database session; mutating methods commit.
    authz
        Authorization service.
    key_provider
        Encrypts a secret created from new values. Never used to decrypt.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, key_provider: KeyProvider
    ) -> None:
        self._session = session
        self._authz = authz
        self._key_provider = key_provider

    def _environment(self, name: str) -> Environment:
        """Look up a live environment by name; the caller has authorized.

        Parameters
        ----------
        name
            The environment's name.

        Returns
        -------
        Environment
            The row.

        Raises
        ------
        NotFoundError
            If there is none.
        """
        env = self._session.execute(
            select(Environment).where(Environment.name == name, LIVE_ENVIRONMENT)
        ).scalar_one_or_none()
        if env is None:
            raise NotFoundError(f"No environment named '{shown(name)}'.")
        return env

    def _find(self, env: Environment, name: str, *, lock: bool = False) -> Connection:
        """Look up a connection of an environment; the caller has authorized.

        Parameters
        ----------
        env
            The environment.
        name
            The connection's name.
        lock
            Lock the row for update and read it afresh.

        Returns
        -------
        Connection
            The row.

        Raises
        ------
        NotFoundError
            If the environment has none of that name (a name that breaks the rule is never
            looked up).
        """
        row = None
        if isinstance(name, str) and NAME.match(name):
            row = self._session.execute(
                select(Connection).where(
                    Connection.environment_id == env.id, Connection.name == name
                )
            ).scalar_one_or_none()
            if row is not None and lock:
                row = lock_row(self._session, Connection, row.id)
        if row is None:
            raise NotFoundError(
                f"environment `{shown(env.name)}` has no connection `{shown(name)}`; "
                f"`lakana connection list --env {shown(env.name)}` lists them"
            )
        return row

    def _view(
        self,
        row: Connection,
        env: Environment,
        declarations: Mapping[str, builtins.list[tuple[str, str]]] | None = None,
    ) -> ConnectionView:
        """Turn a row into what a caller sees.

        Parameters
        ----------
        row
            The connection.
        env
            Its environment.
        declarations
            `_declarations` of the environment, when the caller read it already.

        Returns
        -------
        ConnectionView
            The view.
        """
        if declarations is None:
            declarations = self._declarations(env.id)
        settings = dict(row.settings)
        if isinstance(settings.get("ca_certificate"), str):
            settings["ca_certificate"] = fingerprint(settings["ca_certificate"])
        secret = self._used_secret(row)
        creator = self._session.get(Principal, row.created_by)
        return ConnectionView(
            name=row.name,
            kind=row.kind,
            address=dict(row.address),
            settings=settings,
            secret=secret.name if secret is not None else None,
            declared_by=list(declarations.get(row.name, [])),
            created_by=creator.name if creator is not None else "",
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def declared_by(self, environment_id: uuid.UUID, name: str) -> builtins.list[tuple[str, str]]:
        """List the assets whose deployed version declares a connection.

        Parameters
        ----------
        environment_id
            The environment.
        name
            The connection's name.

        Returns
        -------
        list of tuple of (str, str)
            ``(repository, asset)`` pairs, sorted.
        """
        return self._declarations(environment_id).get(name, [])

    def _declarations(self, environment_id: uuid.UUID) -> dict[str, builtins.list[tuple[str, str]]]:
        """Read which assets of the deployed versions declare which connections.

        Reads each repository's current project version in the environment. A stored manifest
        is untrusted: an entry that is not a Python asset declares nothing, and a Python entry
        whose `connections` is malformed declares every string it names
        (`read_entry_connections`), so its delete is refused rather than let through.

        Parameters
        ----------
        environment_id
            The environment.

        Returns
        -------
        dict of str to list of tuple of (str, str)
            Connection name to its ``(repository, asset)`` pairs, sorted.
        """
        rows = self._session.execute(
            select(Repository.name, JobVersion.manifest_json)
            .join(
                EnvironmentDeployment,
                EnvironmentDeployment.current_job_version_id == JobVersion.id,
            )
            .join(Repository, Repository.id == EnvironmentDeployment.repository_id)
            .where(EnvironmentDeployment.environment_id == environment_id)
        ).all()
        found: dict[str, set[tuple[str, str]]] = {}
        for repository, manifest in rows:
            for table, entry in manifest_assets(manifest).items():
                # Fail safe: a malformed entry declares every name it spells, so a delete is
                # refused rather than let through.
                for name in read_entry_connections(entry).names:
                    found.setdefault(name, set()).add((repository, table))
        return {name: sorted(pairs) for name, pairs in found.items()}

    def create(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        name: str,
        kind: ConnectionKind | str,
        address: Mapping[str, Any],
        settings: Mapping[str, Any],
        secret: str | None = None,
        secret_values: Mapping[str, str] | None = None,
    ) -> ConnectionView:
        """Create a connection, with a new secret or a named one.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment's name.
        name
            The connection's name.
        kind
            Its kind, a member or its REST value; any other text is a `ValidationError`, judged
            after the right.
        address
            Its address.
        settings
            Its settings.
        secret
            The name of an existing secret of the environment to use.
        secret_values
            Values for a new secret `<name>-connection` (or the next free
            `<name>-connection-N`), born bound to the connection.

        Returns
        -------
        ConnectionView
            The committed connection.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_CONNECTION` on the environment, checked before anything else.
        SecretBindingRefused
            A `PermissionDenied`: `secret` names a secret that is not bound to this connection
            and `p` may not bind it (S1); recorded as a denial.
        NotFoundError
            If the environment, or the secret `secret` names, does not exist.
        ValidationError
            For a name outside the rule, an invalid address, settings or secret keys, both
            `secret` and `secret_values`, or a secret for a kind that takes none.
        AlreadyExistsError
            If the name is taken in the environment, or new values find every name of
            `_free_secret_name` taken.
        ConflictError
            If the new secret's name was taken at the same time.
        LakanaError
            If no usable secrets master key is configured; nothing is kept.
        """
        scope = Securable.environment_scope(environment)
        self._authz.authorize(p, Action.MANAGE_CONNECTION, scope)
        env = self._environment(environment)
        if not isinstance(name, str) or not NAME.match(name):
            raise ValidationError(_NAME_RULE)
        new_address, new_settings = validate_connection(kind, address, settings)
        kind = ConnectionKind(kind)
        if secret is not None and secret_values is not None:
            raise ValidationError(_BOTH)
        if secret_values is not None:
            check_secret_values(kind, new_settings, secret_values)
        elif secret is not None and not takes_secret(kind, new_settings):
            check_secret_values(kind, new_settings, {})
        if self._exists(env, name):
            raise AlreadyExistsError(self._taken(env, name))
        row = Connection(
            id=uuid.uuid4(),
            environment_id=env.id,
            name=name,
            kind=kind,
            address=new_address,
            settings=new_settings,
            created_by=p.id,
        )
        with refuse_concurrent_change(self._session, f"connection `{shown(name)}`"):
            self._session.add(row)
            self._flush(env, name)
            try:
                if secret_values is not None:
                    row.secret_id = self._new_secret(p, env, row, dict(secret_values)).id
                elif secret is not None:
                    row.secret_id = self._named_secret(p, env, row, secret, registered=False).id
            except SecretBindingRefused as refusal:
                self._session.rollback()
                record_refusal(self._authz, p, refusal)
                raise
            except LakanaError:
                self._session.rollback()
                raise
            AuditService(self._session).record(
                p.id,
                AuditEventType.CONNECTION_CREATED,
                securable=scope,
                detail={
                    "name": name,
                    "kind": kind.value,
                    "address": new_address,
                    "secret": self._secret_name(row),
                },
            )
            self._commit(env, name)
        return self._view(row, env)

    def list(
        self, p: PrincipalRef, environment: str, *, limit: int = 100, cursor: str | None = None
    ) -> Page[ConnectionView]:
        """List an environment's connections, newest first, one page at a time.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment's name.
        limit
            Most connections returned.
        cursor
            The `next_cursor` of the previous page.

        Returns
        -------
        Page of ConnectionView
            The connections by creation time, newest first, equal times by id, and the cursor
            of the next page.

        Raises
        ------
        PermissionDenied
            Without `READ_ENVIRONMENT` on the environment, or for a task token
            (`_refuse_task`).
        NotFoundError
            If the environment does not exist.
        ValidationError
            If `limit` is below 1 or `cursor` is not one this server issued for this list.
        """
        _refuse_task(p)
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        env = self._environment(environment)
        page = newest_first(
            self._session,
            select(Connection).where(Connection.environment_id == env.id),
            kind="connections",
            at=Connection.created_at,
            row_id=Connection.id,
            at_of=lambda row: row.created_at,
            limit=limit,
            cursor=cursor,
        )
        declarations = self._declarations(env.id)
        return Page([self._view(row, env, declarations) for row in page.items], page.next_cursor)

    def get(self, p: PrincipalRef, environment: str, name: str) -> ConnectionView:
        """Show one connection.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment's name.
        name
            The connection's name.

        Returns
        -------
        ConnectionView
            The connection.

        Raises
        ------
        PermissionDenied
            Without `READ_ENVIRONMENT` on the environment, or for a task token
            (`_refuse_task`).
        NotFoundError
            If the environment or the connection does not exist.
        """
        _refuse_task(p)
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        env = self._environment(environment)
        return self._view(self._find(env, name), env)

    def update(
        self,
        p: PrincipalRef,
        environment: str,
        name: str,
        *,
        address: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        secret: str | None = None,
        secret_values: Mapping[str, str] | None = None,
        clear: Iterable[str] = (),
    ) -> ConnectionView:
        """Change a connection's address, settings or secret; its name and kind never change.

        The row is locked first. The result is validated as a whole before anything is
        decided about the secret.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment's name.
        name
            The connection's name.
        address
            Address keys to change; a key given as `None` is removed.
        settings
            Settings keys to change; a key given as `None` is removed.
        secret
            The name of a secret of the environment to use instead.
        secret_values
            New values: they set the connection's own secret again, or create
            `<name>-connection` (or the next free `<name>-connection-N`) when it has none.
        clear
            ``secret`` (stop using a secret and unbind it) and settings keys to remove.

        Returns
        -------
        ConnectionView
            The committed connection.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_CONNECTION` on the environment, checked before anything else.
        SecretBindingRefused
            A `PermissionDenied`: the change could send a bound secret somewhere new and gives
            no new values, or `secret` names a secret `p` may not bind (S1); recorded as a
            denial.
        NotFoundError
            If the environment, the connection or the named secret does not exist.
        ValidationError
            If nothing changes, `clear` names something the connection does not have, the
            result is invalid, the secret is given two ways, or the kind takes no secret.
        AlreadyExistsError
            If new values need a new secret and every name of `_free_secret_name` is taken.
        ConflictError
            If the new secret's name was taken at the same time.
        LakanaError
            If no usable secrets master key is configured; nothing is kept.
        """
        scope = Securable.environment_scope(environment)
        self._authz.authorize(p, Action.MANAGE_CONNECTION, scope)
        env = self._environment(environment)
        clear = list(clear)
        if (
            address is None
            and settings is None
            and secret is None
            and secret_values is None
            and not clear
        ):
            raise ValidationError("Nothing to change; give at least one change.")
        if secret is not None and secret_values is not None:
            raise ValidationError(_BOTH)
        with refuse_concurrent_change(self._session, f"connection `{shown(name)}`"):
            row = self._find(env, name, lock=True)
            try:
                self._change(
                    p,
                    env,
                    row,
                    address=address,
                    settings=settings,
                    secret=secret,
                    secret_values=secret_values,
                    clear=clear,
                )
            except SecretBindingRefused as refusal:
                self._session.rollback()
                record_refusal(self._authz, p, refusal)
                raise
            except LakanaError:
                self._session.rollback()
                raise
            self._commit(env, row.name)
        return self._view(row, env)

    def _change(
        self,
        p: PrincipalRef,
        env: Environment,
        row: Connection,
        *,
        address: Mapping[str, Any] | None,
        settings: Mapping[str, Any] | None,
        secret: str | None,
        secret_values: Mapping[str, str] | None,
        clear: builtins.list[str],
    ) -> None:
        """Apply an update to a locked connection and add its audit events; nothing commits.

        Parameters
        ----------
        p
            The acting principal.
        env
            The environment.
        row
            The connection, locked.
        address
            Address keys to change.
        settings
            Settings keys to change.
        secret
            The name of a secret to use instead.
        secret_values
            New values for the connection's own secret.
        clear
            ``secret`` and settings keys to remove.

        Raises
        ------
        SecretBindingRefused
            If the change needs `MANAGE_SECRET` that `p` lacks (S1).
        NotFoundError
            If the named secret does not exist.
        ValidationError
            If `clear` names something the connection does not have, or the result is invalid.
        AlreadyExistsError
            If new values need a new secret and every name it could take is taken.
        """
        name = row.name
        if row.secret_id is not None and self._used_secret(row) is None:
            row.secret_id = None
        for key in clear:
            if key != _CLEAR_SECRET and key not in row.settings:
                raise ValidationError(
                    f"connection `{shown(name)}` has nothing to clear called `{shown(key)}`; "
                    f"clear `secret` or one of its settings: {', '.join(sorted(row.settings))}"
                )
        if _CLEAR_SECRET in clear and row.secret_id is None:
            raise ValidationError(f"connection `{shown(name)}` has no secret to clear")
        if _CLEAR_SECRET in clear and (secret is not None or secret_values is not None):
            raise ValidationError(
                "give a new secret or clear the secret, not both in the same change"
            )
        old_address, old_settings = dict(row.address), dict(row.settings)
        wanted_settings = merge_part(old_settings, settings)
        for key in clear:
            wanted_settings.pop(key, None)
        new_address, new_settings = validate_connection(
            row.kind, follow_region(row.kind, old_address, address), wanted_settings
        )
        if secret_values is not None:
            check_secret_values(row.kind, new_settings, secret_values)
        before_secret = row.secret_id
        rotated = False
        row.address, row.settings = new_address, new_settings
        if _CLEAR_SECRET in clear:
            self._let_go(p, env, row, keep=None)
            row.secret_id = None
        elif secret_values is not None:
            own = self._own_secret(row)
            if own is not None:
                rotate_values(self._session, self._key_provider, p, own, dict(secret_values))
                self._let_go(p, env, row, keep=own.id)
                rotated = True
            else:
                row.secret_id = self._new_secret(p, env, row, dict(secret_values)).id
        elif secret is not None:
            own = self._own_secret(row)
            if own is None or own.name != secret:
                row.secret_id = self._named_secret(p, env, row, secret, registered=True).id
        if secret_values is None and moves_secret(
            row.kind, old_address, old_settings, new_address, new_settings
        ):
            require_secret_management_for_url_change(
                self._session, self._authz, p, SecretUse.connection(row)
            )
        if row.secret_id is not None and not takes_secret(row.kind, new_settings):
            raise ValidationError(
                f"connection `{shown(name)}` would use a secret its settings do not take; "
                "clear it in the same change (clear `secret`)"
            )
        changed = _changed("address", old_address, new_address) + _changed(
            "settings", old_settings, new_settings
        )
        if rotated or row.secret_id != before_secret:
            changed.append("secret")
        AuditService(self._session).record(
            p.id,
            AuditEventType.CONNECTION_UPDATED,
            securable=Securable.environment_scope(env.name),
            detail={
                "name": name,
                "changed": changed,
                "address": new_address,
                "secret": self._secret_name(row),
            },
        )

    def delete(self, p: PrincipalRef, environment: str, name: str) -> None:
        """Delete a connection no deployed version declares, and unbind its secrets.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment's name.
        name
            The connection's name.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_CONNECTION` on the environment.
        NotFoundError
            If the environment or the connection does not exist.
        ConflictError
            If a deployed version declares the connection; the message names the first
            ``repository: asset`` and how many more.
        """
        scope = Securable.environment_scope(environment)
        self._authz.authorize(p, Action.MANAGE_CONNECTION, scope)
        env = self._environment(environment)
        with refuse_concurrent_change(self._session, f"connection `{shown(name)}`"):
            row = self._find(env, name, lock=True)
            declared = self.declared_by(env.id, row.name)
            if declared:
                self._session.rollback()
                repository, asset = declared[0]
                more = f" and {len(declared) - 1} more" if len(declared) > 1 else ""
                raise ConflictError(
                    f"connection `{shown(name)}` is used by `{shown(repository)}: {shown(asset)}`"
                    f"{more} in `{shown(env.name)}`; deploy a version without it first."
                )
            self._let_go(p, env, row, keep=None)
            AuditService(self._session).record(
                p.id,
                AuditEventType.CONNECTION_DELETED,
                securable=scope,
                detail={"name": row.name, "kind": row.kind.value},
            )
            self._session.delete(row)
            self._session.commit()

    def request_test(self, p: PrincipalRef, environment: str, name: str) -> ConnectionTest:
        """Queue a check that a connection is reachable; a worker runs it.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment's name.
        name
            The connection's name.

        Returns
        -------
        ConnectionTest
            The queued test.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_CONNECTION` on the environment.
        NotFoundError
            If the environment or the connection does not exist.
        ConflictError
            While a test of this connection, at most `TEST_STUCK_AFTER` old, is queued or
            running. An older open test is finished as failed first.
        """
        scope = Securable.environment_scope(environment)
        self._authz.authorize(p, Action.MANAGE_CONNECTION, scope)
        env = self._environment(environment)
        with refuse_concurrent_change(self._session, f"connection `{shown(name)}`"):
            row = self._find(env, name, lock=True)
            open_test = self._session.execute(
                select(ConnectionTest).where(
                    ConnectionTest.connection_id == row.id,
                    ConnectionTest.state.in_(
                        (ConnectionTestState.QUEUED, ConnectionTestState.RUNNING)
                    ),
                )
            ).scalar_one_or_none()
            if open_test is not None:
                if utcnow() - open_test.created_at <= TEST_STUCK_AFTER:
                    state = open_test.state.value
                    self._session.rollback()
                    raise ConflictError(
                        f"a test of `{shown(name)}` is already "
                        f"{'queued' if state == 'queued' else 'running'}; see it with "
                        f"lakana connection test {shown(name)} --env {shown(env.name)} "
                        f"--id {open_test.id}"
                    )
                self._close(
                    open_test, row.name, env.name, ConnectionTestOutcome.FAILED, _TEST_STUCK
                )
            test = ConnectionTest(
                id=uuid.uuid4(),
                connection_id=row.id,
                environment_id=env.id,
                requested_by=p.id,
                state=ConnectionTestState.QUEUED,
            )
            self._session.add(test)
            AuditService(self._session).record(
                p.id,
                AuditEventType.CONNECTION_TESTED,
                securable=scope,
                detail={
                    "connection": row.name,
                    "environment": env.name,
                    "test_id": str(test.id),
                    "state": ConnectionTestState.QUEUED.value,
                },
            )
            self._session.commit()
        return test

    def test_result(
        self, p: PrincipalRef, environment: str, name: str, test_id: str
    ) -> ConnectionTest:
        """Show one test of a connection.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment's name.
        name
            The connection's name.
        test_id
            The test's id.

        Returns
        -------
        ConnectionTest
            The test.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_CONNECTION` on the environment.
        NotFoundError
            If the environment or the connection does not exist, or the connection has no such
            test (a test of another connection is not shown).
        """
        self._authz.authorize(p, Action.MANAGE_CONNECTION, Securable.environment_scope(environment))
        env = self._environment(environment)
        row = self._find(env, name)
        try:
            wanted = uuid.UUID(test_id)
        except (ValueError, AttributeError, TypeError):
            wanted = None
        test = (
            self._session.execute(
                select(ConnectionTest).where(
                    ConnectionTest.id == wanted, ConnectionTest.connection_id == row.id
                )
            ).scalar_one_or_none()
            if wanted is not None
            else None
        )
        if test is None:
            raise NotFoundError(
                f"connection `{shown(name)}` has no test `{shown(str(test_id))}`; "
                f"`lakana connection test {shown(name)} --env {shown(env.name)}` starts one"
            )
        return test

    def claim_test(
        self, owner: str, *, lease_seconds: int = 60, now: datetime
    ) -> ConnectionTest | None:
        """Claim the oldest test that is queued or whose lease ran out, under a lease.

        Parameters
        ----------
        owner
            The claiming worker's id.
        lease_seconds
            How long the claim lasts.
        now
            The time of the claim; the lease runs from it.

        Returns
        -------
        ConnectionTest or None
            The test, now running; `None` when there is none to claim.
        """
        row = self._session.scalars(
            select(ConnectionTest)
            .where(
                or_(
                    ConnectionTest.state == ConnectionTestState.QUEUED,
                    (ConnectionTest.state == ConnectionTestState.RUNNING)
                    & (ConnectionTest.lease_expires_at <= now),
                )
            )
            .order_by(ConnectionTest.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if row is None:
            self._session.rollback()
            return None
        row.state = ConnectionTestState.RUNNING
        row.lease_owner = owner
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self._session.commit()
        return row

    def finish_test(
        self,
        test_id: uuid.UUID,
        owner: str,
        lease_expires_at: datetime | None,
        outcome: str,
        message: str,
    ) -> bool:
        """Record what a test found, if the caller still holds its lease.

        Parameters
        ----------
        test_id
            The test's id.
        owner
            The worker that claimed it.
        lease_expires_at
            The lease expiry that claim set; the record must match it exactly.
        outcome
            A `ConnectionTestOutcome` value.
        message
            What was found, already redacted; cut to 1,000 characters.

        Returns
        -------
        bool
            `True` when it was recorded; `False` when the lease had changed hands (nothing
            is changed then).
        """
        row = self._session.execute(
            select(ConnectionTest).where(ConnectionTest.id == test_id).with_for_update()
        ).scalar_one_or_none()
        if (
            row is None
            or row.state != ConnectionTestState.RUNNING
            or row.lease_owner != owner
            or row.lease_expires_at != lease_expires_at
        ):
            self._session.rollback()
            return False
        env = self._session.get(Environment, row.environment_id)
        connection = self._session.get(Connection, row.connection_id)
        assert env is not None and connection is not None  # nosec B101 -- foreign keys
        self._close(row, connection.name, env.name, ConnectionTestOutcome(outcome), message)
        self._session.commit()
        return True

    def _close(
        self,
        test: ConnectionTest,
        connection: str,
        environment: str,
        outcome: ConnectionTestOutcome,
        message: str,
    ) -> None:
        """Finish a test and audit it; the caller commits.

        Parameters
        ----------
        test
            The test.
        connection
            The connection's name.
        environment
            The environment's name.
        outcome
            What it found.
        message
            The message; cut to 1,000 characters.
        """
        test.state = ConnectionTestState.DONE
        test.outcome = outcome
        test.message = message[:1000]
        test.finished_at = utcnow()
        test.lease_owner = None
        test.lease_expires_at = None
        AuditService(self._session).record(
            test.requested_by,
            AuditEventType.CONNECTION_TESTED,
            securable=Securable.environment_scope(environment),
            detail={
                "connection": connection,
                "environment": environment,
                "test_id": str(test.id),
                "state": ConnectionTestState.DONE.value,
                "outcome": outcome.value,
            },
        )

    def _exists(self, env: Environment, name: str) -> bool:
        """Say whether an environment has a connection of this name.

        Parameters
        ----------
        env
            The environment.
        name
            The name.

        Returns
        -------
        bool
            Whether it has one.
        """
        return (
            self._session.execute(
                select(Connection.id).where(
                    Connection.environment_id == env.id, Connection.name == name
                )
            ).first()
            is not None
        )

    @staticmethod
    def _taken(env: Environment, name: str) -> str:
        """Write the message for a taken connection name.

        Parameters
        ----------
        env
            The environment.
        name
            The name.

        Returns
        -------
        str
            The message, with the command that changes the existing one.
        """
        return (
            f"environment `{shown(env.name)}` already has a connection `{shown(name)}`; change it "
            f"with `lakana connection update {shown(name)} --env {shown(env.name)}`"
        )

    def _commit(self, env: Environment, name: str) -> None:
        """Commit, and turn a unique-constraint failure into a named error.

        Parameters
        ----------
        env
            The environment.
        name
            The connection's name.

        Raises
        ------
        AlreadyExistsError
            If a concurrent request created the same connection, or the same secret, first.
        ConflictError
            If the write failed for another reason. The message is fixed because an
            `IntegrityError`'s text embeds the statement and its bound parameters.
        """
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._collision(env, name, exc)

    def _flush(self, env: Environment, name: str, *, secret: str | None = None) -> None:
        """Flush, and turn a unique-constraint failure into a named error, as `_commit` does.

        A concurrent create of the same connection or secret is refused by the database at the
        first flush, not only at the commit.

        Parameters
        ----------
        env
            The environment.
        name
            The connection's name.
        secret
            The name of the secret the flush wrote, if it wrote one: a clash on it is looked
            for first (the connection exists already on an update).

        Raises
        ------
        AlreadyExistsError
            If the connection exists.
        ConflictError
            If the secret's name was taken at the same time, or the write failed for another
            reason.
        """
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._collision(env, name, exc, secret=secret)

    def _collision(
        self, env: Environment, name: str, exc: IntegrityError, *, secret: str | None = None
    ) -> NoReturn:
        """Roll back after a refused write and say which name was taken.

        Parameters
        ----------
        env
            The environment.
        name
            The connection's name.
        exc
            The database's refusal; its text, which holds the statement and its parameters,
            never reaches the caller.
        secret
            The name of the secret the write created, if any; looked for first.

        Raises
        ------
        AlreadyExistsError
            If the connection exists.
        ConflictError
            If the secret's name was taken at the same time (another try picks the next free
            name), or the write failed for another reason.
        """
        self._session.rollback()
        if secret is not None and self._secret_named(env, secret) is not None:
            raise ConflictError(
                f"secret `{shown(secret)}` was created in `{shown(env.name)}` at the same time; "
                "try again"
            ) from exc
        if self._exists(env, name):
            raise AlreadyExistsError(self._taken(env, name)) from exc
        raise ConflictError(
            f"Could not save connection `{shown(name)}`: the write failed for a reason other "
            "than a name collision. Try again."
        ) from exc

    def _secret_named(self, env: Environment, secret: str) -> Secret | None:
        """Find a secret of an environment by name, revoked or not.

        Parameters
        ----------
        env
            The environment.
        secret
            The name.

        Returns
        -------
        Secret or None
            The row.
        """
        return self._session.execute(
            select(Secret).where(Secret.environment_id == env.id, Secret.name == secret)
        ).scalar_one_or_none()

    def _free_secret_name(self, env: Environment, name: str) -> str:
        """Choose the name of a connection's new secret: the first free of its candidates.

        ``<name>-connection``, then ``<name>-connection-2`` up to ``-100``. A name any secret
        of the environment holds (bound elsewhere, unbound or revoked) is never reused, so no
        secret is overwritten; nor is a name the environment reserves for its storage or a
        webhook (`used_by_its_environment`), which no connection may take.

        Parameters
        ----------
        env
            The environment.
        name
            The connection's name.

        Returns
        -------
        str
            The name.

        Raises
        ------
        AlreadyExistsError
            If every candidate is taken; the message says what works without an administrator.
        """
        base = f"{name}-connection"
        for number in range(1, _MAX_SECRET_NUMBER + 1):
            candidate = base if number == 1 else f"{base}-{number}"
            if self._secret_named(env, candidate) is None and not used_by_its_environment(
                self._session, env, candidate
            ):
                return candidate
        raise AlreadyExistsError(
            f"secrets `{shown(base)}` to `{shown(base)}-{_MAX_SECRET_NUMBER}` all exist in "
            f"`{shown(env.name)}`; give the connection another name, or create it without new "
            "values and ask an administrator to bind a secret to it: `lakana secret bind NAME "
            f"--env {shown(env.name)} --connection {shown(name)}`"
        )

    def _secret_name(self, row: Connection) -> str | None:
        """Name the secret a connection uses.

        Parameters
        ----------
        row
            The connection.

        Returns
        -------
        str or None
            The secret's name, or `None`.
        """
        secret = self._used_secret(row)
        return secret.name if secret is not None else None

    def _used_secret(self, row: Connection) -> Secret | None:
        """Find the secret a connection uses: the one it names, if active and bound to it.

        `secret_id` and the secret's binding agree whenever the services wrote them; a row that
        names a secret bound elsewhere (or revoked) has no secret, so nothing that reads it can
        send another use's credential to this connection's address.

        Parameters
        ----------
        row
            The connection.

        Returns
        -------
        Secret or None
            The secret, or `None`.
        """
        if row.secret_id is None:
            return None
        secret = self._session.get(Secret, row.secret_id)
        if (
            secret is None
            or secret.revoked_at is not None
            or secret.use_kind is not SecretUseKind.CONNECTION
            or secret.use_target_id != row.id
        ):
            return None
        return secret

    def _own_secret(self, row: Connection) -> Secret | None:
        """Find the secret a connection uses, locked, if it is still bound to it and active.

        Parameters
        ----------
        row
            The connection, locked.

        Returns
        -------
        Secret or None
            The secret; `None` when the connection has none, or the one it names was revoked
            or bound elsewhere since (never rotate a secret that now serves something else).
        """
        if row.secret_id is None:
            return None
        secret = self._session.execute(
            select(Secret)
            .where(Secret.id == row.secret_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if (
            secret is None
            or secret.revoked_at is not None
            or secret.use_kind is not SecretUseKind.CONNECTION
            or secret.use_target_id != row.id
        ):
            return None
        return secret

    def _new_secret(
        self, p: PrincipalRef, env: Environment, row: Connection, values: dict[str, str]
    ) -> Secret:
        """Create the connection's new secret, born bound to it, and let go of any other.

        It is named `<name>-connection`, or the next free `<name>-connection-N`
        (`_free_secret_name`).

        Parameters
        ----------
        p
            The acting principal.
        env
            The environment.
        row
            The connection, locked or just added.
        values
            The values, checked for the kind.

        Returns
        -------
        Secret
            The pending secret.

        Raises
        ------
        AlreadyExistsError
            If every name the secret could take is taken.
        ConflictError
            If the chosen name was taken at the same time.
        """
        chosen = self._free_secret_name(env, row.name)
        secret = insert_secret(
            self._session,
            self._key_provider,
            p,
            env,
            name=chosen,
            values=values,
            use=SecretUse.connection(row),
        )
        self._flush(env, row.name, secret=chosen)
        self._let_go(p, env, row, keep=secret.id)
        return secret

    def _named_secret(
        self, p: PrincipalRef, env: Environment, row: Connection, name: str, *, registered: bool
    ) -> Secret:
        """Make a named secret of the environment the connection's, by the binding rules.

        Parameters
        ----------
        p
            The acting principal.
        env
            The environment.
        row
            The connection.
        name
            The secret's name.
        registered
            Whether the connection existed before this request (it does not on a create, and
            a refusal then rolls the connection back too).

        Returns
        -------
        Secret
            The secret, now bound to the connection.

        Raises
        ------
        ValidationError
            If the kind and settings take no secret.
        NotFoundError
            If the environment has no active secret of that name.
        SecretBindingRefused
            If the secret is not bound to the connection and `p` may not bind it.
        """
        if not takes_secret(row.kind, row.settings):
            check_secret_values(row.kind, row.settings, {})
        secret = self._session.execute(
            select(Secret).where(
                Secret.environment_id == env.id,
                Secret.name == name,
                Secret.revoked_at.is_(None),
            )
        ).scalar_one_or_none()
        if secret is None:
            raise NotFoundError(
                f"environment `{shown(env.name)}` has no secret `{shown(name)}`; "
                f"`lakana secret list --env {shown(env.name)}` lists them"
            )
        bind_or_require_named_secrets(
            self._session,
            self._authz,
            p,
            name=name,
            use=SecretUse.connection(row),
            registered=registered,
            environment_id=env.id,
        )
        # The binding check read the row afresh under lock; a secret revoked or moved since the
        # lookup above is not taken.
        if (
            secret.revoked_at is not None
            or secret.use_kind is not SecretUseKind.CONNECTION
            or secret.use_target_id != row.id
        ):
            raise NotFoundError(
                f"environment `{shown(env.name)}` has no secret `{shown(name)}` this connection "
                f"may use; `lakana secret list --env {shown(env.name)}` lists them"
            )
        self._let_go(p, env, row, keep=secret.id)
        return secret

    def _let_go(
        self, p: PrincipalRef, env: Environment, row: Connection, *, keep: uuid.UUID | None
    ) -> None:
        """Unbind every secret bound to the connection but `keep`, each with `SECRET_BOUND`.

        A secret let go is unbound, not revoked: its values stay, and only `MANAGE_SECRET` binds
        it again.

        Parameters
        ----------
        p
            The acting principal.
        env
            The environment.
        row
            The connection.
        keep
            The secret that stays bound, or `None` to let go of all.
        """
        release_connection_secrets(self._session, p, row.id, env.name, keep=keep)
