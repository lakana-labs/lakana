"""Register, list, update and delete package indexes.

Only `MANAGE_PACKAGE_INDEX` registers an index and names its credential (binding a secret
needs `MANAGE_SECRET`, as the paragraph below says).
The `ADMIN` role carries the action, and it can be granted on its own. A
project's lock may only name a registered index's URL (a wheel's own URL in
the lock may still point at another host, which receives no credential).
A manager of indexes never binds a secret (`bind_or_require_named_secrets`): the secret they
name must already be bound to the index. Binding is for someone who may manage secrets, with
`lakana secret bind` or by naming an unbound secret. The URL of an index with a bound secret
changes only for a principal who has that right.
A build sends a secret's `password` only to the index it is bound to.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Mapping

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import is_secret_name, shown, validate_path_component
from lakana_core.identity.sessions import require_step_up
from lakana_core.models import AuditEventType, PackageIndex
from lakana_core.projects.lookups import live_environment_names
from lakana_core.projects.package_sources import RegisteredIndex, normalize_index_url
from lakana_core.secrets.uses import (
    SecretBindingRefused,
    SecretUse,
    bind_or_require_named_secrets,
    lock_row,
    record_refusal,
    require_secret_management_for_url_change,
)

UPDATABLE_FIELDS = frozenset({"url", "credential_secret"})
"""What `PATCH /package-indexes/{name}` may change. The name is fixed."""

_MAX_NAME = 255


def registered_indexes(session: Session) -> tuple[RegisteredIndex, ...]:
    """List every registered index as plain values, without authorizing.

    For the worker, which reads the registry in-process because it is the
    platform; the REST routes go through `PackageIndexService`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of RegisteredIndex
        Sorted by name.
    """
    rows = session.execute(select(PackageIndex).order_by(PackageIndex.name)).scalars()
    return tuple(RegisteredIndex(row.name, row.url, row.credential_secret, row.id) for row in rows)


def _validate_name(name: str) -> None:
    """Check a package index name, as a name that could be registered.

    Parameters
    ----------
    name
        The name.

    Raises
    ------
    ValidationError
        If the name is empty, has a ``/``, a ``.``, a NUL byte or the unit separator, or is
        longer than 255 characters.
    """
    validate_path_component(name, what="Package index name")
    if len(name) > _MAX_NAME:
        raise ValidationError(f"Package index name is longer than {_MAX_NAME} characters.")


def _validate_secret_name(secret: str | None) -> None:
    """Check the name of the secret that holds an index's credential.

    Parameters
    ----------
    secret
        The name, or `None` for an index that asks for no credential.

    Raises
    ------
    ValidationError
        If the name is not text, is empty, is longer than 255 characters or
        contains a space, a control character or ``:``.
    """
    if secret is not None and not is_secret_name(secret):
        raise ValidationError("credential_secret must be a secret name with no spaces.")


class PackageIndexService:
    """CRUD for `package_indexes`, authorized and audited.

    Parameters
    ----------
    session
        Database session; mutating methods commit.
    authz
        Authorization service.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def _find(self, name: str) -> PackageIndex:
        """Look an index up by name, without authorizing; the caller has authorized.

        The name is checked as `create` checks it, so that a name that could never be
        registered (a NUL byte, which the database refuses) is a `ValidationError` and the
        database is not asked.

        Parameters
        ----------
        name
            The index's name.

        Returns
        -------
        PackageIndex
            The row.

        Raises
        ------
        ValidationError
            If `name` could never be registered.
        NotFoundError
            If none has that name.
        """
        _validate_name(name)
        row = self._session.execute(
            select(PackageIndex).where(PackageIndex.name == name)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"No package index named '{shown(name)}'.")
        return row

    def _lock(self, row: PackageIndex) -> PackageIndex:
        """Lock an index's row and read it afresh, before anything about it is decided.

        A bind, a naming and a URL change of one index take this lock first, so they run one
        after the other, and the one that waits sees what the other committed.

        Parameters
        ----------
        row
            The index found by name.

        Returns
        -------
        PackageIndex
            The row as committed now.

        Raises
        ------
        NotFoundError
            If another request deleted it since it was found.
        """
        locked = lock_row(self._session, PackageIndex, row.id)
        if locked is None:
            self._session.rollback()
            raise NotFoundError(f"No package index named '{shown(row.name)}'.")
        return locked

    def _clash(self, name: str, url: str, *, except_id: uuid.UUID | None = None) -> str | None:
        """Find a registered index that has this name or this URL.

        Parameters
        ----------
        name
            The name to register.
        url
            The URL to register, in stored form.
        except_id
            The row being updated, which does not clash with itself.

        Returns
        -------
        str or None
            The `AlreadyExistsError` text: the name's when the name is taken,
            otherwise the URL's, which names the index that has it. `None`
            when both are free.
        """
        rows = self._session.execute(
            select(PackageIndex).where(or_(PackageIndex.name == name, PackageIndex.url == url))
        ).scalars()
        clashing = [row for row in rows if row.id != except_id]
        for row in clashing:
            if row.name == name:
                return f"A package index named {name!r} already exists."
        for row in clashing:
            return (
                f"The package index {row.name!r} already has this URL. Use that index, or "
                f"remove it first (`lakana index remove {shlex.quote(row.name)}`)."
            )
        return None

    def _authorize_read(self, p: PrincipalRef) -> None:
        """Permit reading indexes with READ_ENVIRONMENT anywhere, or MANAGE_PACKAGE_INDEX.

        Parameters
        ----------
        p
            The acting principal.

        Raises
        ------
        PermissionDenied
            If `p` holds neither (raised by an instance-scope check, so the
            denial is audited like any other), or if `p` is a task token: a
            task has no use for the registry, whatever its principal may read.
        """
        if p.task_scope is not None:
            # `authorize` records the denial; the `raise` covers an allow-all service.
            self._authz.authorize(p, Action.MANAGE_PACKAGE_INDEX, Securable.instance_scope())
            raise PermissionDenied("A task's token may not read the package index registry.")
        if self._authz.permits(p, Action.MANAGE_PACKAGE_INDEX, Securable.instance_scope()):
            return
        for name in live_environment_names(self._session):
            if self._authz.permits(p, Action.READ_ENVIRONMENT, Securable.environment_scope(name)):
                return
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.instance_scope())

    def create(
        self,
        p: PrincipalRef,
        *,
        name: str,
        url: str,
        credential_secret: str | None = None,
    ) -> PackageIndex:
        """Register a package index.

        Parameters
        ----------
        p
            The acting principal.
        name
            Unique name; no dots, at most 255 characters.
        url
            The index URL; stored in the form `normalize_index_url` returns.
            TestPyPI's is accepted only without `credential_secret`.
        credential_secret
            Name of the secret that holds the credential, resolved per
            environment when a project's environment is built; `None` for an
            index that asks for none.

        Returns
        -------
        PackageIndex
            The committed row.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_PACKAGE_INDEX`.
        SecretBindingRefused
            A `PermissionDenied`: `credential_secret` names a secret that is not bound to this
            index (unbound, bound elsewhere, or used by its environment).
        ValidationError
            If the name, the URL or the secret name is invalid.
        AlreadyExistsError
            If the name or the URL is taken, including by a concurrent create.
        ConflictError
            If the commit fails for a reason other than a collision.
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        self._authz.authorize(p, Action.MANAGE_PACKAGE_INDEX, Securable.instance_scope())
        require_step_up(p)
        _validate_name(name)
        stored = normalize_index_url(url, credential=credential_secret is not None)
        _validate_secret_name(credential_secret)
        clash = self._clash(name, stored)
        if clash is not None:
            raise AlreadyExistsError(clash)
        row = PackageIndex(
            id=uuid.uuid4(),
            name=name,
            url=stored,
            credential_secret=credential_secret,
            created_by=p.id,
        )
        if credential_secret is not None:
            try:
                bind_or_require_named_secrets(
                    self._session,
                    self._authz,
                    p,
                    name=credential_secret,
                    use=SecretUse.package_index(row),
                    registered=False,
                )
            except SecretBindingRefused as refusal:
                self._session.rollback()
                record_refusal(self._authz, p, refusal)
                raise
        AuditService(self._session).record(
            p.id,
            AuditEventType.PACKAGE_INDEX_CREATED,
            securable=Securable.instance_scope(),
            detail={"name": name, "url": stored, "credential_secret": credential_secret},
        )
        self._session.add(row)
        self._commit(row, "register")
        return row

    def _commit(self, row: PackageIndex, verb: str) -> None:
        """Commit, and turn a unique-constraint failure into a named error.

        Parameters
        ----------
        row
            The index about to be committed, with its pending name and URL.
        verb
            ``register`` or ``update``, for the message.

        Raises
        ------
        AlreadyExistsError
            If, after the rollback, the name or the URL turns out to be taken
            (the loser of a race between two servers).
        NotFoundError
            If an update finds its row gone: another request deleted the index
            between the lookup and the commit.
        ConflictError
            If the commit failed for another reason. The message is fixed
            because an `IntegrityError`'s text embeds the statement and its
            bound parameters.
        """
        name, row_id, url = row.name, row.id, row.url
        try:
            self._session.commit()
        except StaleDataError:
            self._session.rollback()
            raise NotFoundError(f"No package index named '{shown(name)}'.") from None
        except IntegrityError as exc:
            self._session.rollback()
            clash = self._clash(name, url, except_id=row_id)
            if clash is not None:
                raise AlreadyExistsError(clash) from exc
            raise ConflictError(
                f"Could not {verb} package index {name!r}: the write failed for a reason "
                "other than a name or URL collision."
            ) from exc

    def list_indexes(self, p: PrincipalRef) -> list[PackageIndex]:
        """List every registered index, by name.

        Parameters
        ----------
        p
            The acting principal.

        Returns
        -------
        list of PackageIndex
            Sorted by name.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT on any environment or MANAGE_PACKAGE_INDEX.
        """
        self._authorize_read(p)
        return list(
            self._session.execute(select(PackageIndex).order_by(PackageIndex.name)).scalars()
        )

    def get(self, p: PrincipalRef, name: str) -> PackageIndex:
        """Show one index.

        Parameters
        ----------
        p
            The acting principal.
        name
            The index's name.

        Returns
        -------
        PackageIndex
            The row.

        Raises
        ------
        PermissionDenied
            As for `list_indexes`, checked before the lookup.
        ValidationError
            If `name` could never be registered.
        NotFoundError
            If no index has that name.
        """
        self._authorize_read(p)
        return self._find(name)

    def update(self, p: PrincipalRef, name: str, changes: Mapping[str, str | None]) -> PackageIndex:
        """Change an index's URL or its credential secret.

        The row is locked first. Everything is validated before it changes. A refusal over a
        secret is rolled back and recorded as a denial. The URL and the credential the row
        would have afterwards are checked together (`normalize_index_url`), whichever of the two
        changes.

        Parameters
        ----------
        p
            The acting principal.
        name
            The index's name.
        changes
            Field → new value; `None` clears ``credential_secret``. Only
            `UPDATABLE_FIELDS`.

        Returns
        -------
        PackageIndex
            The committed row.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_PACKAGE_INDEX`.
        SecretBindingRefused
            A `PermissionDenied`: the URL changes while a secret is bound to the index and `p`
            may not manage secrets in its environment, or the new `credential_secret` names a
            secret that is not bound to this index.
        NotFoundError
            If no index has that name, including when a concurrent request
            deletes it before this commit.
        ValidationError
            If `changes` is empty, names a field that cannot be changed,
            clears the URL, gives a value that is not text, or the result is
            invalid.
        AlreadyExistsError
            If the new URL belongs to another index, including by a concurrent
            change.
        ConflictError
            If the commit fails for a reason other than a collision.
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        self._authz.authorize(p, Action.MANAGE_PACKAGE_INDEX, Securable.instance_scope())
        require_step_up(p)
        row = self._find(name)
        if not changes:
            raise ValidationError("Nothing to change; name at least one field.")
        fixed = set(changes) - UPDATABLE_FIELDS
        if fixed:
            raise ValidationError(
                f"{', '.join(sorted(shown(key) for key in fixed))} cannot be changed."
            )
        if "url" in changes and changes["url"] is None:
            raise ValidationError("url cannot be cleared.")
        row = self._lock(row)
        new_secret = changes.get("credential_secret", row.credential_secret)
        new_url = changes.get("url", row.url)
        assert new_url is not None  # nosec B101 -- the stored URL is never null; a null change was refused
        new_url = normalize_index_url(new_url, credential=new_secret is not None)
        _validate_secret_name(new_secret)
        clash = self._clash(name, new_url, except_id=row.id)
        if clash is not None:
            raise AlreadyExistsError(clash)
        use = SecretUse.package_index(row, new_url)
        try:
            if new_url != row.url:
                require_secret_management_for_url_change(self._session, self._authz, p, use)
            if new_secret is not None and new_secret != row.credential_secret:
                bind_or_require_named_secrets(
                    self._session, self._authz, p, name=new_secret, use=use, registered=True
                )
        except SecretBindingRefused as refusal:
            self._session.rollback()
            record_refusal(self._authz, p, refusal)
            raise
        previous = {key: getattr(row, key) for key in changes}
        row.url = new_url
        row.credential_secret = new_secret
        AuditService(self._session).record(
            p.id,
            AuditEventType.PACKAGE_INDEX_UPDATED,
            securable=Securable.instance_scope(),
            detail={
                "name": name,
                "changed": sorted(changes),
                "url": row.url,
                "credential_secret": row.credential_secret,
                "previous": previous,
            },
        )
        self._commit(row, "update")
        return row

    def delete(self, p: PrincipalRef, name: str) -> None:
        """Delete an index.

        Checks nothing about deployments: a deployed project whose lock names
        the index fails its next execution with a message that says so.

        Parameters
        ----------
        p
            The acting principal.
        name
            The index's name.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_PACKAGE_INDEX`.
        ValidationError
            If `name` could never be registered.
        NotFoundError
            If no index has that name.
        """
        self._authz.authorize(p, Action.MANAGE_PACKAGE_INDEX, Securable.instance_scope())
        row = self._find(name)
        AuditService(self._session).record(
            p.id,
            AuditEventType.PACKAGE_INDEX_DELETED,
            securable=Securable.instance_scope(),
            detail={"name": name, "url": row.url},
        )
        self._session.delete(row)
        self._session.commit()
