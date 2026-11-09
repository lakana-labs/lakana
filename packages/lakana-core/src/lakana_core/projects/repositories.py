"""Register, list, update and delete git repositories.

Only `MANAGE_REPOSITORY` sets a URL and names a credential (binding a secret needs
`MANAGE_SECRET`, as the paragraph below says); deployers name a
repository and nothing else, so the worker fetches only from hosts an admin
chose. That grant is the SSRF trust boundary (private addresses are allowed:
self-hosted GitLab is usually internal).

A manager of repositories never binds a secret (`bind_or_require_named_secrets`): the secret
they name must already be bound to the repository. Binding is for someone who may manage
secrets, with `lakana secret bind` or by naming an unbound secret. The URL of a repository with
a bound secret changes only for a principal who has that right.
A deploy sends a secret's `token` only to the repository it is bound to.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from lakana_core.identifiers import validate_path_component
from lakana_core.identity.sessions import require_step_up
from lakana_core.models import (
    AuditEventType,
    Environment,
    EnvironmentDeployment,
    Repository,
    RepositoryKind,
)
from lakana_core.projects.git_input import resolve_local_path, validate_ref, validate_repo_url
from lakana_core.projects.lookups import live_environment_names
from lakana_core.secrets.uses import (
    SecretBindingRefused,
    SecretUse,
    bind_or_require_named_secrets,
    lock_row,
    record_refusal,
    require_secret_management_for_url_change,
)

UPDATABLE_FIELDS = frozenset(
    {"url", "credential_secret", "credential_username", "ssh_known_hosts", "default_ref"}
)
"""What `PATCH /repositories/{name}` may change. Name and kind are fixed."""

_TOKEN = re.compile(r"\A[^\x00-\x20\x7f:]{1,255}\Z")
_MAX_KNOWN_HOSTS = 64 * 1024


def _validate(repo: Repository, repo_roots: Sequence[str]) -> None:
    """Check a repository row's fields against each other and its kind.

    Parameters
    ----------
    repo
        The row, new or updated, not yet committed.
    repo_roots
        `LAKANA_REPO_ROOTS`.

    Raises
    ------
    ValidationError
        On the first rule broken.
    """
    validate_repo_url(repo.kind, repo.url)
    if repo.kind is RepositoryKind.LOCAL:
        resolve_local_path(repo.url, repo_roots)
        if repo.credential_secret is not None or repo.credential_username is not None:
            raise ValidationError("A local repository takes no credential.")
    if repo.kind is RepositoryKind.SSH:
        if not repo.ssh_known_hosts or not repo.ssh_known_hosts.strip():
            raise ValidationError("ssh_known_hosts is required for an ssh repository.")
    elif repo.ssh_known_hosts is not None:
        raise ValidationError("ssh_known_hosts is only for ssh repositories.")
    if repo.ssh_known_hosts is not None and (
        len(repo.ssh_known_hosts) > _MAX_KNOWN_HOSTS or "\x00" in repo.ssh_known_hosts
    ):
        raise ValidationError("ssh_known_hosts is too large or contains a NUL byte.")
    if repo.credential_username is not None:
        if repo.kind is not RepositoryKind.HTTPS:
            raise ValidationError("credential_username is only for https repositories.")
        if not _TOKEN.match(repo.credential_username):
            raise ValidationError(
                "credential_username must be 1-255 characters with no ':' or spaces."
            )
    if repo.credential_secret is not None and not _TOKEN.match(repo.credential_secret):
        raise ValidationError("credential_secret must be a secret name with no spaces.")
    if repo.default_ref is not None:
        validate_ref(repo.default_ref)


class RepositoryService:
    """CRUD for `repositories`, authorized and audited.

    Parameters
    ----------
    session
        Database session; mutating methods commit.
    authz
        Authorization service.
    repo_roots
        `LAKANA_REPO_ROOTS`, confining local repositories.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, *, repo_roots: Sequence[str] = ()
    ) -> None:
        self._session = session
        self._authz = authz
        self._repo_roots = tuple(repo_roots)

    def _find(self, name: str) -> Repository:
        """Look a repository up by name, without authorizing.

        Parameters
        ----------
        name
            The repository's name.

        Returns
        -------
        Repository
            The row.

        Raises
        ------
        NotFoundError
            If none has that name.
        """
        repo = self._session.execute(
            select(Repository).where(Repository.name == name)
        ).scalar_one_or_none()
        if repo is None:
            raise NotFoundError(f"No repository named {name!r}.")
        return repo

    def _authorize_read(self, p: PrincipalRef) -> None:
        """Permit reading repositories with READ_ENVIRONMENT anywhere, or MANAGE_REPOSITORY.

        Parameters
        ----------
        p
            The acting principal.

        Raises
        ------
        PermissionDenied
            If `p` holds neither (raised by an instance-scope check, so the
            denial is audited like any other).
        """
        if self._authz.permits(p, Action.MANAGE_REPOSITORY, Securable.instance_scope()):
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
        kind: RepositoryKind,
        url: str,
        credential_secret: str | None = None,
        credential_username: str | None = None,
        ssh_known_hosts: str | None = None,
        default_ref: str | None = None,
    ) -> Repository:
        """Register a repository.

        Parameters
        ----------
        p
            The acting principal.
        name
            Unique name; no dots.
        kind
            ``local``, ``https`` or ``ssh``.
        url
            The URL or, for ``local``, an absolute path under a repo root.
        credential_secret
            Secret name resolved per environment at deploy time.
        credential_username
            HTTPS username; `None` means ``oauth2``.
        ssh_known_hosts
            ``known_hosts`` lines; required for ``ssh``.
        default_ref
            Ref deployed when a deploy names none.

        Returns
        -------
        Repository
            The committed row.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_REPOSITORY`.
        SecretBindingRefused
            A `PermissionDenied`: `credential_secret` names a secret that is not bound to this
            repository (unbound, bound elsewhere, or used by its environment).
        ValidationError
            If a field or the kind is invalid.
        AlreadyExistsError
            If the name is taken, including by a concurrent create.
        ConflictError
            If the commit fails for a reason other than a name collision.
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        self._authz.authorize(p, Action.MANAGE_REPOSITORY, Securable.instance_scope())
        require_step_up(p)
        validate_path_component(name, what="Repository name")
        try:
            kind = RepositoryKind(kind)
        except ValueError as exc:
            raise ValidationError(
                f"kind must be one of {', '.join(k.value for k in RepositoryKind)}; got {kind!r}."
            ) from exc
        repo = Repository(
            id=uuid.uuid4(),
            name=name,
            kind=kind,
            url=url,
            credential_secret=credential_secret,
            credential_username=credential_username,
            ssh_known_hosts=ssh_known_hosts,
            default_ref=validate_ref(default_ref) if default_ref is not None else None,
            created_by=p.id,
        )
        _validate(repo, self._repo_roots)
        if self._session.execute(select(Repository.id).where(Repository.name == name)).first():
            raise AlreadyExistsError(f"A repository named {name!r} already exists.")
        if credential_secret is not None:
            try:
                bind_or_require_named_secrets(
                    self._session,
                    self._authz,
                    p,
                    name=credential_secret,
                    use=SecretUse.repository(repo),
                    registered=False,
                )
            except SecretBindingRefused as refusal:
                self._session.rollback()
                record_refusal(self._authz, p, refusal)
                raise
        AuditService(self._session).record(
            p.id,
            AuditEventType.REPOSITORY_CREATED,
            securable=Securable.instance_scope(),
            detail={
                "name": name,
                "kind": kind.value,
                "url": url,
                "credential_secret": credential_secret,
            },
        )
        self._session.add(repo)
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            # The loser of a create/create race on two servers reaches here;
            # the message is fixed because an IntegrityError's text embeds the
            # statement and its bound parameters.
            if self._session.execute(select(Repository.id).where(Repository.name == name)).first():
                raise AlreadyExistsError(f"A repository named {name!r} already exists.") from exc
            raise ConflictError(
                f"Could not register repository {name!r}: the write failed for a reason "
                "other than a name collision."
            ) from exc
        return repo

    def _lock(self, repo: Repository) -> Repository:
        """Lock a repository's row and read it afresh, before anything about it is decided.

        A bind, a naming and a URL change of one repository take this lock first, so they run
        one after the other, and the one that waits sees what the other committed.

        Parameters
        ----------
        repo
            The repository found by name.

        Returns
        -------
        Repository
            The row as committed now.

        Raises
        ------
        NotFoundError
            If another request deleted it since it was found.
        """
        locked = lock_row(self._session, Repository, repo.id)
        if locked is None:
            self._session.rollback()
            raise NotFoundError(f"No repository named {repo.name!r}.")
        return locked

    def list_repositories(self, p: PrincipalRef) -> list[Repository]:
        """List every repository, by name.

        Parameters
        ----------
        p
            The acting principal.

        Returns
        -------
        list of Repository
            Sorted by name.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT on any environment or MANAGE_REPOSITORY.
        """
        self._authorize_read(p)
        return list(self._session.execute(select(Repository).order_by(Repository.name)).scalars())

    def get(self, p: PrincipalRef, name: str) -> Repository:
        """Show one repository.

        Parameters
        ----------
        p
            The acting principal.
        name
            The repository's name.

        Returns
        -------
        Repository
            The row.

        Raises
        ------
        PermissionDenied
            As for `list`, checked before the lookup.
        NotFoundError
            If no repository has that name.
        """
        self._authorize_read(p)
        return self._find(name)

    def update(self, p: PrincipalRef, name: str, changes: Mapping[str, str | None]) -> Repository:
        """Change a repository's URL, credential, username, known hosts or default ref.

        Parameters
        ----------
        p
            The acting principal.
        name
            The repository's name.
        changes
            Field → new value; `None` clears a nullable field. Only
            `UPDATABLE_FIELDS`.

        Returns
        -------
        Repository
            The committed row.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_REPOSITORY`.
        SecretBindingRefused
            A `PermissionDenied`: the URL changes while a secret is bound to the repository and
            `p` may not manage secrets in its environment, or the new `credential_secret` names
            a secret that is not bound to this repository.
        NotFoundError
            If no repository has that name, including when a concurrent request deletes it
            before this commit.
        ValidationError
            If `changes` is empty, a field cannot be changed or the result is invalid.
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        self._authz.authorize(p, Action.MANAGE_REPOSITORY, Securable.instance_scope())
        require_step_up(p)
        repo = self._find(name)
        if not changes:
            raise ValidationError("Nothing to change; name at least one field.")
        fixed = set(changes) - UPDATABLE_FIELDS
        if fixed:
            raise ValidationError(f"{', '.join(sorted(fixed))} cannot be changed.")
        if "url" in changes and changes["url"] is None:
            raise ValidationError("url cannot be cleared.")
        repo = self._lock(repo)
        previous_url, previous_secret = repo.url, repo.credential_secret
        try:
            for field, value in changes.items():
                setattr(repo, field, value)
            if repo.default_ref is not None:
                repo.default_ref = validate_ref(repo.default_ref)
            _validate(repo, self._repo_roots)
            # Built after the changes, so that it carries the URL the request leaves.
            use = SecretUse.repository(repo)
            if repo.url != previous_url:
                require_secret_management_for_url_change(self._session, self._authz, p, use)
            if repo.credential_secret is not None and repo.credential_secret != previous_secret:
                bind_or_require_named_secrets(
                    self._session,
                    self._authz,
                    p,
                    name=repo.credential_secret,
                    use=use,
                    registered=True,
                )
        except SecretBindingRefused as refusal:
            self._session.rollback()
            record_refusal(self._authz, p, refusal)
            raise
        except ValidationError:
            self._session.rollback()
            raise
        except StaleDataError:
            # Local mode (SQLite) takes no row lock, so a concurrent delete shows up as the
            # autoflush of the changed row finding it gone.
            self._session.rollback()
            raise NotFoundError(f"No repository named {name!r}.") from None
        AuditService(self._session).record(
            p.id,
            AuditEventType.REPOSITORY_UPDATED,
            securable=Securable.instance_scope(),
            detail={
                "name": name,
                "changed": sorted(changes),
                "credential_secret": repo.credential_secret,
            },
        )
        try:
            self._session.commit()
        except StaleDataError:
            self._session.rollback()
            raise NotFoundError(f"No repository named {name!r}.") from None
        return repo

    def delete(self, p: PrincipalRef, name: str) -> None:
        """Delete a repository and its deploy history.

        Parameters
        ----------
        p
            The acting principal.
        name
            The repository's name.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_REPOSITORY`.
        NotFoundError
            If no repository has that name.
        ConflictError
            While any environment has a current deployment from it; undeploy it
            there first.
        """
        self._authz.authorize(p, Action.MANAGE_REPOSITORY, Securable.instance_scope())
        repo = self._find(name)
        deployed = sorted(
            self._session.execute(
                select(Environment.name)
                .join(EnvironmentDeployment, EnvironmentDeployment.environment_id == Environment.id)
                .where(EnvironmentDeployment.repository_id == repo.id)
            ).scalars()
        )
        if deployed:
            raise ConflictError(
                f"Repository {name!r} is deployed in {', '.join(deployed)}. Undeploy it first: "
                + "; ".join(f"lakana undeploy {name} --env {env}" for env in deployed)
                + "."
            )
        AuditService(self._session).record(
            p.id,
            AuditEventType.REPOSITORY_DELETED,
            securable=Securable.instance_scope(),
            detail={"name": name},
        )
        self._session.delete(repo)
        self._session.commit()
