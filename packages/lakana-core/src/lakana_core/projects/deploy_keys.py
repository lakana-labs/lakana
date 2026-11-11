"""SSH deploy keys Lakana makes: one key pair per environment, kept as that environment's secret.

`DeployKeyService.generate` makes an ed25519 key pair for each environment that has none yet,
stores the private half as the environment's secret (`insert_secret`, encrypted at once, bound
to the repository and marked `generated_for` it) and keeps the public half on the same row to
show it. The private key exists in plain text only inside `generate_deploy_key` and the
encryption call; no answer, audit event or log line carries it.

Each environment's key is its own: making one for an environment never reads or changes another
environment's key, and the server never decrypts a key to copy it. A key made for a repository
is never bound to another use or rotated by hand (`lakana_core.secrets.service`); only another
call here replaces it.
"""

from __future__ import annotations

import base64
import enum
import re
import shlex
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NoReturn

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.db import refuse_concurrent_change
from lakana_core.errors import ConflictError, ValidationError
from lakana_core.identifiers import shown
from lakana_core.identity.sessions import require_step_up
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    Environment,
    Repository,
    RepositoryKind,
    Secret,
    SecretUseKind,
)
from lakana_core.projects.lookups import environment_by_name, repository_by_name
from lakana_core.projects.probes import key_fingerprint
from lakana_core.projects.repositories import RepositoryService
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.service import insert_secret, rotate_values
from lakana_core.secrets.uses import (
    SecretBindingRefused,
    SecretUse,
    plain,
    record_refusal,
    used_by_its_environment,
)

_SECRET_NAME = re.compile(r"\A[^\x00-\x20\x7f:]{1,255}\Z")
"""What a repository may name as its credential (`credential_secret`), and so a key's secret."""

REPLACED_NOTE = "The old key for {environment} stops working: replace it at your git host now."
"""What a call says for each environment whose key it replaced."""


@dataclass(frozen=True)
class DeployKeyPair:
    """A freshly made SSH key pair.

    Parameters
    ----------
    public_key
        ``ssh-ed25519 <base64> <comment>``, to add at the git host.
    private_key
        The OpenSSH PEM, unencrypted; never shown by `repr`.
    """

    public_key: str
    private_key: str = field(repr=False)


@dataclass(frozen=True)
class EnvironmentDeployKey:
    """The public half of one environment's deploy key.

    Parameters
    ----------
    environment
        The environment whose secret holds the private half.
    public_key
        ``ssh-ed25519 <base64> <comment>``.
    fingerprint
        ``SHA256:…``, as `ssh-keygen -l` prints it.
    made
        Whether this call made it; `False` for a key made earlier and listed unchanged.
    """

    environment: str
    public_key: str
    fingerprint: str
    made: bool


@dataclass(frozen=True)
class DeployKeyResult:
    """What a `DeployKeyService.generate` call made and kept.

    Parameters
    ----------
    secret
        The secret name every key is stored under; the repository's `credential_secret`.
    keys
        One per environment the call wrote or found a key in, by environment name.
    skipped
        Environments without a key where the caller may not manage secrets.
    replaced
        Environments whose existing key this call replaced.
    """

    secret: str
    keys: tuple[EnvironmentDeployKey, ...]
    skipped: tuple[str, ...]
    replaced: tuple[str, ...] = ()

    @property
    def notes(self) -> tuple[str, ...]:
        """Say, for each replaced key, that the old one stops working.

        Returns
        -------
        tuple of str
            One `REPLACED_NOTE` per replaced environment.
        """
        return tuple(REPLACED_NOTE.format(environment=plain(env)) for env in self.replaced)


def generate_deploy_key(comment: str) -> DeployKeyPair:
    """Make an ed25519 key pair.

    Parameters
    ----------
    comment
        The public key's comment, e.g. ``lakana deploy key for analytics``.

    Returns
    -------
    DeployKeyPair
        The pair; the private key as OpenSSH PEM without a passphrase.
    """
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return DeployKeyPair(public_key=f"{public} {comment}", private_key=private)


def deploy_key_secret_name(repository: str) -> str:
    """Name the secret a repository's deploy key is stored under by default.

    Parameters
    ----------
    repository
        The repository's name.

    Returns
    -------
    str
        ``<repository>-deploy-key``.
    """
    return f"{repository}-deploy-key"


def public_key_fingerprint(public_key: str) -> str:
    """Fingerprint an OpenSSH public key line.

    Parameters
    ----------
    public_key
        ``<type> <base64> [comment]``.

    Returns
    -------
    str
        ``SHA256:…`` (`key_fingerprint`).
    """
    return key_fingerprint(base64.b64decode(public_key.split()[1]))


class _Step(enum.Enum):
    """What a call does in one environment."""

    MAKE = "make"
    KEEP = "keep"
    REPLACE = "replace"


def _made_for(secret: Secret, repo: Repository) -> bool:
    """Say whether a secret is a live deploy key Lakana made for this repository.

    Parameters
    ----------
    secret
        A secret of the key's name.
    repo
        The repository.

    Returns
    -------
    bool
        Active, made for this repository's id, still bound to it and with its public half.
    """
    return (
        secret.revoked_at is None
        and secret.generated_for == repo.id
        and secret.use_kind is SecretUseKind.REPOSITORY
        and secret.use_target_id == repo.id
        and secret.public_key is not None
    )


class DeployKeyService:
    """Make a repository's deploy keys and list their public halves.

    Parameters
    ----------
    session
        Database session; `generate` commits.
    authz
        Authorization service (the server's records denials).
    key_provider
        Wraps each new secret's data key; nothing here decrypts.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, key_provider: KeyProvider
    ) -> None:
        self._session = session
        self._authz = authz
        self._key_provider = key_provider

    def generate(
        self,
        p: PrincipalRef,
        repository: str,
        *,
        secret: str | None = None,
        environments: Sequence[str] | None = None,
        replace: bool = False,
    ) -> DeployKeyResult:
        """Make a deploy key in each environment that has none yet.

        By default every live environment where `p` may manage secrets and the repository has
        no key yet gets one; one that has a key keeps it. `environments` narrows the call to
        those; with `replace` their keys are replaced. Everything is checked before the first
        write, and one commit writes every key, the repository's `credential_secret` and the
        audit event.

        Parameters
        ----------
        p
            The acting principal.
        repository
            The repository's name; an ssh repository with confirmed host keys.
        secret
            The secret name; `deploy_key_secret_name` by default.
        environments
            Only these environments; each needs `MANAGE_SECRET`.
        replace
            Replace the keys of `environments` (required with it).

        Returns
        -------
        DeployKeyResult
            The public keys, the environments skipped and those replaced.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_REPOSITORY`; for a named environment without `MANAGE_SECRET`; when
            `p` may manage secrets nowhere; or when a new secret name would leave an
            environment `p` cannot manage on its old key (each recorded as a denial).
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        NotFoundError
            If the repository or a named environment does not exist.
        ValidationError
            For a repository that is not ssh or has no host keys, a secret name a repository
            cannot name, `replace` without `environments`, no environment at all, or a new
            secret name that would leave a named-out environment on the old key.
        ConflictError
            If the repository names a secret Lakana did not make; if a secret of the name in an
            environment to write is revoked, not a key made for this repository, or the name
            the environment uses for its storage or a webhook; or if the write collides with
            a concurrent one (try again).
        """
        self._authz.authorize(p, Action.MANAGE_REPOSITORY, Securable.instance_scope())
        require_step_up(p)
        name = self._secret_name(repository, secret)
        if replace and not environments:
            raise ValidationError(
                "--replace replaces the keys of the environments you name: add --environment "
                "ENV for each key to replace (repeatable). A call without --replace only makes "
                "keys where none exists yet."
            )
        found = repository_by_name(self._session, repository)
        live = list(
            self._session.execute(
                select(Environment).where(LIVE_ENVIRONMENT).order_by(Environment.name)
            ).scalars()
        )
        if not live:
            raise ValidationError(
                "There is no environment yet, so there is nowhere to keep a deploy key. Create "
                "one first (`lakana env create`), then make the key."
            )
        named = self._named(p, environments)
        permitted = {
            env.name
            for env in live
            if self._authz.permits(p, Action.MANAGE_SECRET, Securable.environment_scope(env.name))
        }
        targets = [env for env in live if env.name in (named if named is not None else permitted)]
        if not targets:
            self._refuse(
                p,
                SecretBindingRefused(
                    "You may not manage secrets (MANAGE_SECRET) in any environment, so no deploy "
                    f"key can be made for repository '{shown(repository)}'. Ask someone with the "
                    "ADMIN role in an environment to make it: lakana repo deploy-key "
                    f"{shlex.quote(plain(repository))}",
                    Securable.instance_scope(),
                    "no_environment_to_write_deploy_key",
                ),
            )
        with refuse_concurrent_change(self._session, f"Repository '{shown(repository)}'"):
            try:
                repo = RepositoryService(self._session, self._authz).lock(found)
                return self._generate_locked(
                    p, repo, name, live, targets, permitted, named, replace
                )
            except SecretBindingRefused as refusal:
                self._refuse(p, refusal)
            except BaseException:
                self._session.rollback()
                raise

    def _refuse(self, p: PrincipalRef, refusal: SecretBindingRefused) -> NoReturn:
        """Roll back, record a refusal as a denial and raise it.

        Parameters
        ----------
        p
            The refused principal.
        refusal
            The refusal.

        Raises
        ------
        SecretBindingRefused
            Always: `refusal`.
        """
        self._session.rollback()
        record_refusal(self._authz, p, refusal)
        raise refusal

    @staticmethod
    def _secret_name(repository: str, secret: str | None) -> str:
        """Choose and check the secret name.

        Parameters
        ----------
        repository
            The repository's name.
        secret
            The name asked for, or `None` for the default.

        Returns
        -------
        str
            The name.

        Raises
        ------
        ValidationError
            If the name is not one a repository can name as its credential.
        """
        if secret is not None:
            if not _SECRET_NAME.match(secret):
                raise ValidationError(
                    "--secret must be a secret name of 1-255 characters with no spaces or ':'."
                )
            return secret
        name = deploy_key_secret_name(repository)
        if not _SECRET_NAME.match(name):
            raise ValidationError(
                f"The default secret name '{shown(name)}' is longer than 255 characters: name "
                "the secret yourself with --secret NAME."
            )
        return name

    def _named(self, p: PrincipalRef, environments: Sequence[str] | None) -> set[str] | None:
        """Check the environments a call names: each exists and `p` may manage its secrets.

        Parameters
        ----------
        p
            The acting principal.
        environments
            The names, or `None` for every environment.

        Returns
        -------
        set of str or None
            The names; `None` when none were given.

        Raises
        ------
        PermissionDenied
            For the first one `p` may not manage secrets in (recorded).
        NotFoundError
            If one does not exist.
        """
        if not environments:
            return None
        for environment in dict.fromkeys(environments):
            self._authz.authorize(p, Action.MANAGE_SECRET, Securable.environment_scope(environment))
            environment_by_name(self._session, environment)
        return set(environments)

    def _generate_locked(
        self,
        p: PrincipalRef,
        repo: Repository,
        name: str,
        live: list[Environment],
        targets: list[Environment],
        permitted: set[str],
        named: set[str] | None,
        replace: bool,
    ) -> DeployKeyResult:
        """Decide every environment's step under the locks, then write them in one commit.

        Parameters
        ----------
        p
            The acting principal.
        repo
            The repository, locked.
        name
            The secret name.
        live
            Every live environment, by name.
        targets
            The environments to write.
        permitted
            The environments where `p` may manage secrets.
        named
            The environments named, or `None`.
        replace
            Replace the keys of `targets`.

        Returns
        -------
        DeployKeyResult
            What was made and kept.
        """
        self._require_ssh(repo)
        previous = repo.credential_secret
        self._require_own_credential(repo, name, permitted)
        rows = self._lock_secrets(name)
        steps = [
            (env, rows.get(env.id), self._step(repo, env, name, rows.get(env.id), replace))
            for env in targets
        ]
        others = [env for env in live if env not in targets]
        if previous is not None and previous != name:
            self._require_every_environment(repo, name, previous, others, permitted)
        skipped = (
            ()
            if named is not None
            else tuple(
                env.name for env in others if not (env.id in rows and _made_for(rows[env.id], repo))
            )
        )
        return self._write(p, repo, name, previous, steps, skipped)

    @staticmethod
    def _require_ssh(repo: Repository) -> None:
        """Refuse a repository a deploy key cannot serve.

        Parameters
        ----------
        repo
            The repository.

        Raises
        ------
        ValidationError
            If it is not ssh, or has no confirmed host keys.
        """
        quoted = shlex.quote(plain(repo.name))
        if repo.kind is not RepositoryKind.SSH:
            raise ValidationError(
                f"Repository '{shown(repo.name)}' is {repo.kind.value}; a deploy key is an SSH "
                "key, for an ssh repository (git@host:path or ssh://…). Register the SSH URL "
                "as another repository, or give this one a token secret."
            )
        if not repo.ssh_known_hosts or not repo.ssh_known_hosts.strip():
            raise ValidationError(
                f"Repository '{shown(repo.name)}' has no confirmed host keys. Fetch and compare "
                "them first: lakana repo host-keys URL --save known_hosts, then lakana repo "
                f"update {quoted} --known-hosts-file known_hosts."
            )

    def _require_own_credential(self, repo: Repository, name: str, permitted: set[str]) -> None:
        """Refuse when the repository names a secret Lakana did not make.

        A repository may switch from one deploy key Lakana made to another name (after the old
        one was revoked); a credential a person chose is never replaced by a call here.

        Parameters
        ----------
        repo
            The repository, locked.
        name
            The secret name of this call.
        permitted
            The environments where the caller may manage secrets; only these are named.

        Raises
        ------
        ConflictError
            If `credential_secret` names another secret that is not a key made for it.
        SecretBindingRefused
            If such a secret is only in environments the caller may not manage (recorded as a
            denial by `generate`; the message names no environment).
        """
        previous = repo.credential_secret
        if previous is None or previous == name:
            return
        rows = self._session.execute(
            select(Secret.generated_for, Secret.use_kind, Secret.use_target_id, Environment.name)
            .join(Environment, Environment.id == Secret.environment_id)
            .where(Secret.name == previous)
            .order_by(Environment.name)
        ).all()
        # Only this repository's rows count: a secret of the same name that serves another
        # repository (a key made for it, or a person's) is not this repository's credential.
        own = [
            (made_for, environment)
            for made_for, use_kind, target, environment in rows
            if made_for == repo.id or (use_kind is SecretUseKind.REPOSITORY and target == repo.id)
        ]
        if own and all(made_for == repo.id for made_for, _ in own):
            return
        others = [env for made_for, env in own if made_for != repo.id]
        foreign = next((env for env in others if env in permitted), None)
        if others and foreign is None:
            raise SecretBindingRefused(
                f"Repository '{shown(repo.name)}' already names secret '{shown(previous)}' as its "
                "credential, and in an environment where you may not manage secrets that is a "
                "secret Lakana did not make. Ask someone with the ADMIN role in every "
                "environment to clear it (lakana repo update "
                f"{shlex.quote(plain(repo.name))} --clear credential_secret) and make the key.",
                Securable.environment_scope(others[0]),
                "credential_not_made_by_lakana",
            )
        cause = (
            f"in environment '{shown(foreign)}' that is a secret Lakana did not make"
            if foreign is not None
            else "Lakana made no key under that name for it"
        )
        quoted = shlex.quote(plain(repo.name))
        raise ConflictError(
            f"Repository '{shown(repo.name)}' already names secret '{shown(previous)}' as its "
            f"credential, and {cause}. A deploy key would replace it: clear it first with "
            f"lakana repo update {quoted} --clear credential_secret, then make the key with "
            f"lakana repo deploy-key {quoted}."
        )

    def _lock_secrets(self, name: str) -> dict[uuid.UUID, Secret]:
        """Lock every secret of the key's name, in every environment, revoked ones included.

        The order `require_secret_management_for_url_change` takes them in (after the
        repository's row), so a bind or a URL change of the same repository waits, and every
        decision here is made from rows as committed now.

        Parameters
        ----------
        name
            The secret name.

        Returns
        -------
        dict
            Environment id → row.
        """
        rows = self._session.execute(
            select(Secret)
            .join(Environment, Environment.id == Secret.environment_id)
            .where(Secret.name == name)
            .order_by(Environment.name)
            .with_for_update(of=Secret)
            .execution_options(populate_existing=True)
        ).scalars()
        return {row.environment_id: row for row in rows}

    def _step(
        self,
        repo: Repository,
        env: Environment,
        name: str,
        row: Secret | None,
        replace: bool,
    ) -> _Step:
        """Decide what to do in one environment to write, or refuse.

        Parameters
        ----------
        repo
            The repository.
        env
            The environment.
        name
            The secret name.
        row
            Its secret of that name, locked, or `None`.
        replace
            Replace an existing key.

        Returns
        -------
        _Step
            Make, keep or replace.

        Raises
        ------
        ConflictError
            If the name is taken there by anything but a live key made for this repository.
        """
        where = f"Environment '{shown(env.name)}'"
        other = (
            f"Use another secret name: lakana repo deploy-key {shlex.quote(plain(repo.name))} "
            "--secret OTHER-NAME (or the page's 'Use another secret name')."
        )
        if row is None:
            if used_by_its_environment(self._session, env, name):
                raise ConflictError(
                    f"{where} uses the name '{shown(name)}' for its storage or a webhook, so a "
                    f"deploy key cannot be stored under it. {other}"
                )
            return _Step.MAKE
        if row.revoked_at is not None:
            raise ConflictError(
                f"{where} has a revoked secret named '{shown(name)}', and a revoked name is "
                f"never used again. {other}"
            )
        if not _made_for(row, repo):
            raise ConflictError(
                f"{where} already has a secret named '{shown(name)}' that is not a deploy key "
                f"Lakana made for repository '{shown(repo.name)}'. It is left as it is. {other}"
            )
        return _Step.REPLACE if replace else _Step.KEEP

    def _require_every_environment(
        self,
        repo: Repository,
        name: str,
        previous: str,
        others: list[Environment],
        permitted: set[str],
    ) -> None:
        """Refuse a new secret name that leaves an environment on the old key.

        Parameters
        ----------
        repo
            The repository.
        name
            The new secret name.
        previous
            The name the repository's keys have now.
        others
            The live environments this call does not write.
        permitted
            The environments where the caller may manage secrets.

        Raises
        ------
        ValidationError
            If such an environment is one the caller may manage (it was left out by name).
        SecretBindingRefused
            If it is one the caller may not manage.
        """
        holding = {
            row.environment_id
            for row in self._session.execute(
                select(Secret).where(Secret.name == previous, Secret.revoked_at.is_(None))
            ).scalars()
            if _made_for(row, repo)
        }
        for env in others:
            if env.id not in holding:
                continue
            if env.name in permitted:
                raise ValidationError(
                    f"Switching repository '{shown(repo.name)}' to secret '{shown(name)}' would "
                    f"leave environment '{shown(env.name)}' on its old key, which deploys could "
                    "then no longer find. Leave out --environment, or name every environment "
                    "that has a key."
                )
            quoted = shlex.quote(plain(env.name))
            raise SecretBindingRefused(
                f"Making a new key under '{shown(name)}' would leave environment "
                f"'{shown(env.name)}' on the old key, and you may not manage secrets there. "
                f"Someone who manages secrets in '{shown(env.name)}' makes the key: lakana repo "
                f"deploy-key {shlex.quote(plain(repo.name))} --secret {shlex.quote(plain(name))}"
                f" (their keys for {quoted} and the others).",
                Securable.environment_scope(env.name),
                "deploy_key_leaves_an_environment_behind",
            )

    def _write(
        self,
        p: PrincipalRef,
        repo: Repository,
        name: str,
        previous: str | None,
        steps: list[tuple[Environment, Secret | None, _Step]],
        skipped: tuple[str, ...],
    ) -> DeployKeyResult:
        """Write the keys; a clash with a concurrent write of the name becomes a fixed 409.

        The clash can surface at any flush, not only at the commit: `insert_secret` queries
        before it adds, which flushes the previous environment's pending insert.

        Parameters
        ----------
        p
            The acting principal.
        repo
            The repository, locked.
        name
            The secret name.
        previous
            The repository's `credential_secret` before the call.
        steps
            Each environment to write, its locked row and its step.
        skipped
            The environments left without a key.

        Returns
        -------
        DeployKeyResult
            What was made and kept.

        Raises
        ------
        ConflictError
            If a write collides with a concurrent one; nothing is changed.
        """
        try:
            return self._write_unguarded(p, repo, name, previous, steps, skipped)
        except IntegrityError:
            self._session.rollback()
            # A fixed message and no chained cause: an IntegrityError's text embeds the
            # statement and its bound parameters, the ciphertext, nonce and wrapped key among
            # them, and a logged traceback would print it.
            raise ConflictError(
                f"Another request wrote a secret named '{shown(name)}' at the same time; "
                "nothing was changed. Look at the repository again and try again."
            ) from None

    def _write_unguarded(
        self,
        p: PrincipalRef,
        repo: Repository,
        name: str,
        previous: str | None,
        steps: list[tuple[Environment, Secret | None, _Step]],
        skipped: tuple[str, ...],
    ) -> DeployKeyResult:
        """Make and replace the keys, name the secret on the repository, audit and commit.

        `_write` turns a clash into a `ConflictError`.

        Parameters
        ----------
        p
            The acting principal.
        repo
            The repository, locked.
        name
            The secret name.
        previous
            The repository's `credential_secret` before the call.
        steps
            Each environment to write, its locked row and its step.
        skipped
            The environments left without a key.

        Returns
        -------
        DeployKeyResult
            What was made and kept.

        Raises
        ------
        IntegrityError
            If a flush or the commit collides with a concurrent write of the name.
        """
        keys: list[EnvironmentDeployKey] = []
        made: dict[str, str] = {}
        for env, row, step in steps:
            if step is _Step.KEEP:
                assert row is not None and row.public_key is not None  # nosec B101 -- _made_for
                public = row.public_key
            else:
                public = self._new_key(p, repo, env, name, row)
                made[env.name] = public_key_fingerprint(public)
            keys.append(
                EnvironmentDeployKey(
                    environment=env.name,
                    public_key=public,
                    fingerprint=public_key_fingerprint(public),
                    made=step is not _Step.KEEP,
                )
            )
        replaced = tuple(env.name for env, _, step in steps if step is _Step.REPLACE)
        result = DeployKeyResult(secret=name, keys=tuple(keys), skipped=skipped, replaced=replaced)
        if not made and previous == name:
            self._session.rollback()
            return result
        repo.credential_secret = name
        AuditService(self._session).record(
            p.id,
            AuditEventType.DEPLOY_KEY_GENERATED,
            securable=Securable.instance_scope(),
            detail={
                "repository": repo.name,
                "secret": name,
                "made": made,
                "kept": [env.name for env, _, step in steps if step is _Step.KEEP],
                "replaced": list(replaced),
                "skipped": list(skipped),
                "previous_credential_secret": previous,
            },
        )
        self._session.commit()
        return result

    def _new_key(
        self, p: PrincipalRef, repo: Repository, env: Environment, name: str, row: Secret | None
    ) -> str:
        """Make one environment's key pair and store it, as a new secret or into `row`.

        Parameters
        ----------
        p
            The acting principal.
        repo
            The repository.
        env
            The environment.
        name
            The secret name.
        row
            The key to replace, or `None` to add one.

        Returns
        -------
        str
            The new public key.
        """
        pair = generate_deploy_key(f"lakana deploy key for {repo.name}")
        values = {"private_key": pair.private_key}
        if row is None:
            insert_secret(
                self._session,
                self._key_provider,
                p,
                env,
                name=name,
                values=values,
                use=SecretUse.repository(repo),
                generated_for=repo.id,
                public_key=pair.public_key,
            )
        else:
            rotate_values(self._session, self._key_provider, p, row, values)
            row.public_key = pair.public_key
        return pair.public_key

    def deploy_keys(
        self, p: PrincipalRef, repositories: Sequence[Repository]
    ) -> dict[uuid.UUID, tuple[EnvironmentDeployKey, ...]]:
        """List the public deploy keys of repositories, where `p` may read the environment.

        A key is listed while it is live, made for the repository and named by its
        `credential_secret`: naming another secret drops the list. Not audited.

        Parameters
        ----------
        p
            The reader.
        repositories
            The repositories.

        Returns
        -------
        dict
            Repository id → its keys (each `made=False`), by environment name; an empty tuple
            for one without keys.
        """
        by_id = {repo.id: repo for repo in repositories}
        found: dict[uuid.UUID, list[EnvironmentDeployKey]] = {repo_id: [] for repo_id in by_id}
        if not by_id:
            return {}
        rows = self._session.execute(
            select(Secret, Environment.name)
            .join(Environment, Environment.id == Secret.environment_id)
            .where(Secret.generated_for.in_(list(by_id)), LIVE_ENVIRONMENT)
            .order_by(Environment.name)
        ).all()
        readable: dict[str, bool] = {}
        for secret, environment in rows:
            repo = by_id.get(secret.generated_for) if secret.generated_for is not None else None
            if repo is None or secret.name != repo.credential_secret or not _made_for(secret, repo):
                continue
            if environment not in readable:
                readable[environment] = self._authz.permits(
                    p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment)
                )
            if readable[environment] and secret.public_key is not None:
                found[repo.id].append(
                    EnvironmentDeployKey(
                        environment=environment,
                        public_key=secret.public_key,
                        fingerprint=public_key_fingerprint(secret.public_key),
                        made=False,
                    )
                )
        return {repo_id: tuple(keys) for repo_id, keys in found.items()}
