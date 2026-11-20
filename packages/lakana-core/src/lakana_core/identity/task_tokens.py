"""Task-token minting, verification, and revocation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from lakana_core.audit.service import AuditService
from lakana_core.auth import (
    Action,
    AuthorizationService,
    BoundTask,
    PrincipalRef,
    RunContext,
    Securable,
    TaskScope,
)
from lakana_core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.admin import authorize_admin_only, holds_instance_admin
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.sessions import within_token_window
from lakana_core.identity.tokens import generate_token, verify_secret
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    Environment,
    JobVersion,
    Namespace,
    PersonalAccessToken,
    Principal,
    Repository,
    Run,
    RunStatus,
    Table,
    TableType,
    TaskRun,
    TaskStatus,
    TaskToken,
)
from lakana_core.pagination import Page, newest_first
from lakana_core.projects.manifest import (
    entry_keep_versions,
    entry_output,
    python_entry,
    read_entry_connections,
)

_DEFAULT_MAX_TTL_SECONDS = 86_400
"""Default cap on a minted task token's `ttl_seconds`.

Matches `lakana_core.config.TaskTokensConfig.max_ttl_seconds`'s own
default, so a caller that never wires a `Settings` instance through (tests,
direct `TaskTokenService` construction) still gets the same one-day ceiling
a production deployment does.
"""


def _is_invalid_asset_pattern(pattern: str) -> bool:
    """Whether an asset-scope pattern is malformed or too broad to mint.

    Rejects an empty or whitespace-only pattern, one with an empty
    dot-separated component (``".*"`` splits to ``["", "*"]``, ``"bronze."``
    to ``["bronze", ""]``, ``".a"`` to ``["", "a"]``), and one whose first
    component is a bare ``"*"`` (``"*"``, ``"*.x"``, ``"*.*"``) — every shape
    that is malformed or does not name one specific namespace, so it could
    never usefully scope a token's declared assets in the first place. None
    of these actually act as a wildcard that would match every namespace
    under `GrantAuthorization`'s coverage matchers
    (`_matches_asset_pattern`/`_matches_namespace_pattern`) — they are
    rejected purely because they are useless or confusing to declare, not
    because leaving them minted would silently grant broader access. A
    trailing wildcard component (``"bronze.*"``) is not rejected: it
    narrows to one declared, named namespace and everything nested under
    it, which is exactly what asset-scope patterns are for.

    Parameters
    ----------
    pattern
        A candidate `TaskScope.asset_scope` entry.

    Returns
    -------
    bool
        `True` if `pattern` must be rejected.
    """
    if not pattern.strip():
        return True
    components = pattern.split(".")
    if any(component == "" for component in components):
        return True
    return components[0] == "*"


@dataclass(frozen=True)
class MintedTaskToken:
    """A freshly minted task token, returned exactly once.

    Parameters
    ----------
    key_id
        The public lookup key.
    token
        The full `lakana_task_<key_id>_<secret>` value.
    expires_at
        When this token stops working.
    """

    key_id: str
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class TaskTokenScope:
    """A resolved task token's identity and declared scope.

    Parameters
    ----------
    principal
        The identity the task runs as, whose grants it inherits.
    environment
        The one environment this task may touch.
    asset_scope
        The declared asset name/pattern strings. Nothing in this
        sub-project enforces this list — sub-project 2's
        `AuthorizationService` is what will refuse an action outside it.
    """

    principal: PrincipalRef
    environment: str
    asset_scope: list[str]


_NOT_RUNNING = "task run {task} (claim {claim}) is not the running attempt of a Python asset"
_NO_RUN_PRINCIPAL = "The run principal no longer exists or is inactive."
_ATTEMPT_ENDED = "task_attempt_ended"


def _declared_inputs(manifest: object, asset: str) -> tuple[str, ...] | None:
    """Read a Python asset's declared input tables from a stored manifest.

    Parameters
    ----------
    manifest
        the `manifest_json` of a project version; untrusted.
    asset
        The dotted table name.

    Returns
    -------
    tuple of str or None
        The distinct input tables, sorted; `None` unless the manifest has an
        entry of kind ``python`` for `asset`.

    Raises
    ------
    ValidationError
        If that entry's `inputs` is not a mapping of strings.
    """
    entry = python_entry(manifest, asset)
    if entry is None:
        return None
    raw = entry.get("inputs")
    if not isinstance(raw, dict) or not all(isinstance(t, str) for t in raw.values()):
        raise ValidationError(f"The manifest's inputs for {asset!r} are not a mapping of names.")
    return tuple(sorted(set(raw.values())))


_MAX_VIEW_DEPTH = 10
_MAX_READABLE = 1000


def _view_references(session: Session, environment: str, names: list[str]) -> list[str]:
    """List the tables the stored definitions of the views among `names` reference.

    One query finds the views among all of `names`, however many there are.

    Parameters
    ----------
    session
        Database session.
    environment
        Environment name.
    names
        Dotted table names.

    Returns
    -------
    list of str
        The referenced dotted names, in the order of `names`; nothing for a name that is not
        a view in `environment` (a table, a missing name, a malformed name).
    """
    keys: list[tuple[str, str]] = []
    for dotted in names:
        try:
            ref = TableRef.from_dotted(environment, dotted)
        except ValidationError:
            keys.append(("", ""))
            continue
        keys.append((ref.namespace.joined(), ref.name))
    wanted = {key for key in keys if key != ("", "")}
    if not wanted:
        return []
    views = {
        (path, name): row
        for path, name, row in session.execute(
            select(Namespace.path, Table.name, Table)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(
                Environment.name == environment,
                LIVE_ENVIRONMENT,
                Table.table_type == TableType.VIEW,
                tuple_(Namespace.path, Table.name).in_(sorted(wanted)),
            )
        )
    }
    found: list[str] = []
    for key in keys:
        row = views.get(key)
        if row is None:
            continue
        definition = row.view_definition if isinstance(row.view_definition, dict) else {}
        references = definition.get("references")
        for reference in references if isinstance(references, list) else []:
            name = _current_reference_name(session, reference)
            if name is not None:
                found.append(name)
    return found


def _current_reference_name(session: Session, reference: object) -> str | None:
    """Return a stored view reference's name if its id still carries that name.

    Mirrors the query path, which refuses a view whose reference was dropped
    or renamed: such a reference makes nothing readable.

    Parameters
    ----------
    session
        Database session.
    reference
        One entry of a view definition's `references`; untrusted.

    Returns
    -------
    str or None
        The stored dotted name, or `None` if the entry is malformed, its id
        no longer exists, or the table's current name differs.
    """
    if not isinstance(reference, dict):
        return None
    stored, raw_id = reference.get("name"), reference.get("id")
    if not isinstance(stored, str) or not isinstance(raw_id, str):
        return None
    try:
        table_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    current = session.get(Table, table_id)
    namespace = session.get(Namespace, current.namespace_id) if current is not None else None
    if current is None or namespace is None:
        return None
    if f"{NamespacePath.parse(namespace.path)}.{current.name}" != stored:
        return None
    return stored


def _readable_tables(
    session: Session, environment: str, inputs: tuple[str, ...]
) -> tuple[str, ...]:
    """Add the tables behind view inputs to the declared inputs.

    Parameters
    ----------
    session
        Database session.
    environment
        Environment name.
    inputs
        The declared input tables.

    Returns
    -------
    tuple of str
        `inputs` plus every table reachable through view definitions,
        sorted; at most `_MAX_VIEW_DEPTH` levels and `_MAX_READABLE` names.
    """
    seen = set(inputs)
    frontier = list(inputs)
    for _ in range(_MAX_VIEW_DEPTH):
        found = [
            name for name in _view_references(session, environment, frontier) if name not in seen
        ]
        frontier = []
        for name in found:
            if name not in seen and len(seen) < _MAX_READABLE:
                seen.add(name)
                frontier.append(name)
        if not frontier:
            break
    return tuple(sorted(seen))


def _resolve_bound(
    session: Session,
    task_run_id: uuid.UUID | None,
    claim: int | None,
    environment: str,
    *,
    strict: bool = False,
) -> BoundTask | None:
    """Resolve the attempt a token is bound to, if it is still the running one.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        The token's `task_run_id`.
    claim
        The token's `claim`.
    environment
        The token's environment name.
    strict
        Let a malformed manifest entry raise instead of reading as "not
        the running attempt" (the minting path tells the two apart).

    Returns
    -------
    BoundTask or None
        `None` unless both values are set, the task run is `RUNNING` with
        exactly this claim counter, its run is in `environment`, and its
        run is `RUNNING` and not `cancel_requested`, and its
        project version's manifest declares the asset as a Python asset with
        well-formed inputs.

    Raises
    ------
    ValidationError
        If `strict` and the manifest entry's `inputs` is malformed.
    """
    if task_run_id is None or claim is None:
        return None
    task = session.execute(
        select(TaskRun).where(TaskRun.id == task_run_id).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if task is None or task.status is not TaskStatus.RUNNING or task.claim_count != claim:
        return None
    run = session.execute(
        select(Run).where(Run.id == task.run_id).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if run is None or run.status is not RunStatus.RUNNING or run.cancel_requested:
        return None
    env = session.get(Environment, run.environment_id)
    version = session.get(JobVersion, run.job_version_id)
    if env is None or version is None or env.name != environment:
        return None
    try:
        inputs = _declared_inputs(version.manifest_json, task.asset)
    except ValidationError:
        if strict:
            raise
        return None
    entry = python_entry(version.manifest_json, task.asset)
    if inputs is None or entry is None:
        return None
    # Read fail-safe and without logging: this runs on every request of the task. A write logs
    # an unreadable value once (`lakana_core.catalog.connection_lineage`).
    connections = read_entry_connections(entry)
    repository = session.get(Repository, version.repository_id)
    return BoundTask(
        task_run_id=task.id,
        claim=claim,
        asset=task.asset,
        inputs=inputs,
        readable=_readable_tables(session, environment, inputs),
        output=entry_output(entry),
        keep_versions=entry_keep_versions(entry),
        connections=connections.names if connections.readable else (),
        connections_readable=connections.readable,
        repository="" if repository is None else repository.name,
    )


def _minting_pat_works(session: Session, pat_id: uuid.UUID, now: datetime) -> bool:
    """Say whether the personal access token a task token was minted with still works.

    The same conditions as `lakana_core.identity.pats.PatService.verify`, read from the row at
    every authentication: so a task token stops in the same instant as its PAT, whether the PAT
    was revoked, expired, deleted or its owner deactivated, with no sweep and no window between.

    Parameters
    ----------
    session
        Database session.
    pat_id
        The PAT's row id (`TaskToken.source_pat_id`).
    now
        The time to check at.

    Returns
    -------
    bool
        `False` if the PAT is gone, revoked or expired, its owner is missing or inactive, or
        its owner is linked to the identity provider and outside the token window
        (`lakana_core.identity.sessions.within_token_window`).
    """
    pat = session.get(PersonalAccessToken, pat_id)
    if pat is None or pat.revoked_at is not None:
        return False
    if pat.expires_at is not None and pat.expires_at <= now:
        return False
    owner = session.get(Principal, pat.principal_id)
    return owner is not None and owner.is_active and within_token_window(session, pat, now)


def resolve_task_token_scope(
    session: Session, token_id: uuid.UUID, *, expected_principal_id: uuid.UUID | None = None
) -> TaskScope | None:
    """Resolve a task token's current scope by id, if it is still usable.

    The one place that decides whether a task token still works, shared by
    `TaskTokenService.verify` (the HTTP authentication path, starting from a
    presented secret) and `lakana_worker.pipeline`'s own re-check before
    running a queued query (starting from a stored
    `QueryRun.task_token_id`) — the same "is this token still good"
    question, asked from two different starting points, must never drift
    apart.

    Parameters
    ----------
    session
        Database session.
    token_id
        The task token's id.
    expected_principal_id
        If given, the token's own `principal_id` must equal this or the
        token is treated as invalid. `QueryRun.task_token_id` has no
        foreign key (migration `0011`), so nothing but this check
        stops a token row's `principal_id` from later pointing at a
        different principal than the one a run was actually submitted
        under (a direct mutation, or a future bug). `verify` has no such
        expectation to check against, so it leaves this `None`.

    Returns
    -------
    TaskScope or None
        `None` if no `TaskToken` row has this id, it is revoked, expired,
        its principal is missing or inactive, the personal access token it
        was minted with (`source_pat_id`) no longer works (`_minting_pat_works`),
        or `expected_principal_id`
        is given and does not match the row's own `principal_id`. For a
        bound token also `None` unless its task run is `RUNNING`
        with the token's claim counter in the token's environment — a
        reclaimed, finished or superseded attempt's token is invalid at once.
        Otherwise a `TaskScope` built from the row's current
        `environment`/`asset_scope`.
    """
    task_token = session.get(TaskToken, token_id)
    if task_token is None:
        return None
    if expected_principal_id is not None and task_token.principal_id != expected_principal_id:
        return None
    now = datetime.now(UTC)
    if task_token.revoked_at is not None or task_token.expires_at <= now:
        return None
    principal = session.get(Principal, task_token.principal_id)
    if principal is None or not principal.is_active:
        return None
    if task_token.source_pat_id is not None and not _minting_pat_works(
        session, task_token.source_pat_id, now
    ):
        return None
    bound: BoundTask | None = None
    if task_token.task_run_id is not None or task_token.claim is not None:
        bound = _resolve_bound(
            session, task_token.task_run_id, task_token.claim, task_token.environment
        )
        if bound is None:
            return None
    return TaskScope(
        environment=task_token.environment,
        asset_scope=list(task_token.asset_scope),
        token_id=task_token.id,
        bound=bound,
    )


@dataclass(frozen=True)
class ListedTaskToken:
    """A listed task token, with the name it signs in as and, when bound, its run.

    Parameters
    ----------
    token
        The token's row. It carries only `secret_hash`, never a retrievable secret.
    principal
        The name of the principal the token signs in as.
    run_id
        The run whose task the token was minted for; `None` for a token minted over REST.
    job
        That run's job; `None` for a REST token or an ad-hoc run.
    asset
        The asset of that task; `None` for a REST token.
    """

    token: TaskToken
    principal: str
    run_id: uuid.UUID | None
    job: str | None
    asset: str | None


class TaskTokenService:
    """Mint, verify, and revoke task tokens.

    The worker-side `mint_for_task`/`revoke_for_task` consult no authorization.

    Parameters
    ----------
    session
        Database session. Each mutating method commits internally.
    authz
        Authorization service consulted on `mint` and `revoke`. `verify`
        does not consult it, for the same reason `PatService.verify`
        doesn't: it produces the authenticated identity, before any
        authorization question can be asked.
    max_ttl_seconds
        Longest `ttl_seconds` `mint` will accept, so an unattended
        task's credential cannot outlive human review indefinitely.
        Defaults to `lakana_core.config.TaskTokensConfig`'s own default,
        one day; a server wires the operator-configured value through
        (`LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`) instead of leaving this
        default in place.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        max_ttl_seconds: int = _DEFAULT_MAX_TTL_SECONDS,
    ) -> None:
        self._session = session
        self._authz = authz
        self._max_ttl_seconds = max_ttl_seconds

    def mint(
        self,
        p: PrincipalRef,
        principal_name: str,
        *,
        environment: str,
        asset_scope: list[str],
        ttl_seconds: int,
    ) -> MintedTaskToken:
        """Mint a task token.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            The identity the token authenticates as. Must be the caller's own
            name unless the caller holds the `ADMIN` role at instance scope.
        environment
            The one environment the token is scoped to.
        asset_scope
            The declared asset name/pattern strings.
        ttl_seconds
            Seconds until the token expires, from now. May be negative in
            tests, to construct an already-expired token.

        Returns
        -------
        MintedTaskToken
            The new token, including its full value — shown here and
            nowhere else afterward. Minted with a personal access token
            (``p.credential == "pat"``), it records that token
            (`TaskToken.source_pat_id`, and its key id as ``pat_key_id`` in the
            `TASK_TOKEN_MINTED` event) and works only while that token does.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name`.
        PermissionDenied
            If the acting principal may not mint task tokens, or names another
            principal without being an instance administrator (audited).
        ValidationError
            If `ttl_seconds` exceeds `max_ttl_seconds`, or any entry in
            `asset_scope` is malformed or does not name one specific
            namespace — empty, whitespace-only, has an empty dot-separated
            component, or has ``"*"`` as its first component. See
            `_is_invalid_asset_pattern`: these shapes are rejected because
            they could never usefully scope a token, not because they would
            otherwise act as a wildcard bypass.
        ConflictError
            If a randomly generated key_id collides twice in a row
            (astronomically rare, but a server-side generation conflict,
            not an authentication failure — see the retry-once behaviour
            below).
        sqlalchemy.exc.IntegrityError
            If the commit fails for any reason other than a key_id collision
            — e.g. the acting principal's audit foreign key.
        """
        self._authz.authorize(p, Action.MINT_TASK_TOKEN, Securable.instance_scope())
        # Names identify principals here: `principals.name` is unique and nothing
        # renames a principal. A rename feature must revisit this comparison.
        if principal_name != p.name and not holds_instance_admin(self._session, p.id):
            AuditService(self._session).record(
                p.id,
                AuditEventType.AUTHORIZATION_DENIED,
                securable=Securable.instance_scope(),
                detail={
                    "action": Action.MINT_TASK_TOKEN.value,
                    "subject": principal_name,
                    "reason": "not_self",
                },
                commit=True,
            )
            raise PermissionDenied(
                f"{p.name!r} may mint task tokens only for itself; minting for another "
                "principal needs the ADMIN role on the instance."
            )
        if ttl_seconds > self._max_ttl_seconds:
            raise ValidationError(f"ttl_seconds must not exceed {self._max_ttl_seconds} seconds.")
        for pattern in asset_scope:
            if _is_invalid_asset_pattern(pattern):
                raise ValidationError(f"Invalid asset-scope pattern {pattern!r}.")
        principal = self._session.execute(
            select(Principal).where(Principal.name == principal_name)
        ).scalar_one_or_none()
        if principal is None:
            raise NotFoundError(f"No principal named {principal_name!r}.")
        source_pat_id = p.token_id if p.credential == "pat" else None
        detail: dict[str, object] = {
            "issuer": p.name,
            "subject": principal_name,
            "environment": environment,
            "asset_scope": list(asset_scope),
        }
        if source_pat_id is not None:
            pat = self._session.get(PersonalAccessToken, source_pat_id)
            detail["pat_key_id"] = pat.key_id if pat is not None else None
        return self._store(
            actor_id=p.id,
            principal_id=principal.id,
            environment=environment,
            asset_scope=asset_scope,
            ttl_seconds=ttl_seconds,
            detail=detail,
            source_pat_id=source_pat_id,
        )

    def _store(
        self,
        *,
        actor_id: uuid.UUID,
        principal_id: uuid.UUID,
        environment: str,
        asset_scope: list[str],
        ttl_seconds: int,
        detail: dict[str, object],
        task_run_id: uuid.UUID | None = None,
        claim: int | None = None,
        source_pat_id: uuid.UUID | None = None,
    ) -> MintedTaskToken:
        """Insert a token and its `TASK_TOKEN_MINTED` event, retrying one key_id collision.

        Parameters
        ----------
        actor_id
            The audit event's principal.
        principal_id
            The identity the token authenticates as.
        environment
            The one environment the token is scoped to.
        asset_scope
            The declared asset strings.
        ttl_seconds
            Seconds until the token expires.
        detail
            The audit detail, without `key_id` (added here).
        task_run_id
            The bound task run, or `None`.
        claim
            The bound claim counter, or `None`.
        source_pat_id
            The personal access token it was minted with, or `None`.

        Returns
        -------
        MintedTaskToken
            The new token, including its full value.

        Raises
        ------
        ConflictError
            If a generated key_id collides twice in a row.
        sqlalchemy.exc.IntegrityError
            If the commit fails for any other reason.
        """
        token = generate_token("lakana_task")
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        retried = False
        while True:
            try:
                self._session.add(
                    TaskToken(
                        id=uuid.uuid4(),
                        principal_id=principal_id,
                        environment=environment,
                        asset_scope=asset_scope,
                        key_id=token.key_id,
                        secret_hash=token.secret_hash,
                        issued_at=now,
                        expires_at=expires_at,
                        task_run_id=task_run_id,
                        claim=claim,
                        source_pat_id=source_pat_id,
                    )
                )
                AuditService(self._session).record(
                    actor_id,
                    AuditEventType.TASK_TOKEN_MINTED,
                    detail={**detail, "key_id": token.key_id},
                )
                self._session.commit()
            except IntegrityError as exc:
                self._session.rollback()
                if not self._key_id_taken(token.key_id):
                    raise
                if retried:
                    raise ConflictError(
                        "Generated a duplicate key_id twice in a row; giving up."
                    ) from exc
                retried = True
                token = generate_token("lakana_task")
                continue
            break
        return MintedTaskToken(key_id=token.key_id, token=token.full_token, expires_at=expires_at)

    def mint_for_task(
        self, task_run_id: uuid.UUID, claim: int, *, ttl_seconds: int
    ) -> MintedTaskToken:
        """Mint a token bound to one attempt of a Python task.

        Called by the worker, in-process, for the attempt it has just
        claimed. It does not check `MINT_TASK_TOKEN`: the worker acts as the
        platform, as it does when it runs SQL as the run principal. The
        token authenticates as the run's run-as principal, in the run's
        environment, and is valid only while the task run is `RUNNING` with
        this claim counter.

        Parameters
        ----------
        task_run_id
            The claimed task run.
        claim
            Its claim counter (`ClaimedTask.claim`).
        ttl_seconds
            Wanted lifetime; capped at `max_ttl_seconds`.

        Returns
        -------
        MintedTaskToken
            The token, shown here and nowhere else.

        Raises
        ------
        ConflictError
            If `(task_run_id, claim)` is not the running attempt of a Python
            asset (the caller has lost its claim, or the manifest does not
            declare the asset as Python).
        ValidationError
            If the manifest's entry for the asset has `inputs` that is not a
            mapping of strings (a tampered manifest, not a lost claim).
        NotFoundError
            If the run principal is gone or inactive.
        """
        task = self._session.get(TaskRun, task_run_id)
        run = self._session.get(Run, task.run_id) if task is not None else None
        env = self._session.get(Environment, run.environment_id) if run is not None else None
        bound = (
            _resolve_bound(self._session, task_run_id, claim, env.name, strict=True)
            if env is not None
            else None
        )
        if run is None or env is None or bound is None:
            raise ConflictError(_NOT_RUNNING.format(task=task_run_id, claim=claim))
        principal = (
            self._session.get(Principal, run.run_as_principal_id)
            if run.run_as_principal_id is not None
            else None
        )
        if principal is None or not principal.is_active:
            raise NotFoundError(_NO_RUN_PRINCIPAL)
        scope = sorted({*bound.inputs, bound.asset})
        return self._store(
            actor_id=principal.id,
            principal_id=principal.id,
            environment=env.name,
            asset_scope=scope,
            ttl_seconds=min(ttl_seconds, self._max_ttl_seconds),
            detail={
                "issuer": "worker",
                "subject": principal.name,
                "environment": env.name,
                "asset_scope": scope,
                "task_run_id": str(task_run_id),
                "claim": claim,
            },
            task_run_id=task_run_id,
            claim=claim,
        )

    def revoke_for_task(self, key_id: str) -> None:
        """Revoke a bound token when its attempt ends.

        Called by the worker on every path out of a Python attempt. No
        authorization check, for the reason `mint_for_task` gives.
        Idempotent; a token that is already gone (its task run was deleted)
        is not an error.

        Parameters
        ----------
        key_id
            The token's key id.

        Raises
        ------
        ValidationError
            If the token is not bound to a task: REST-minted tokens are
            revoked through `revoke`, which authorizes.
        """
        task_token = self._session.execute(
            select(TaskToken).where(TaskToken.key_id == key_id)
        ).scalar_one_or_none()
        if task_token is None or task_token.revoked_at is not None:
            return
        if task_token.task_run_id is None:
            raise ValidationError(f"Task token {key_id!r} is not bound to a task.")
        task_token.revoked_at = datetime.now(UTC)
        principal = self._session.get(Principal, task_token.principal_id)
        assert principal is not None  # nosec B101 -- FK guarantees the row still exists
        AuditService(self._session).record(
            principal.id,
            AuditEventType.TASK_TOKEN_REVOKED,
            detail={
                "issuer": "worker",
                "subject": principal.name,
                "key_id": key_id,
                "environment": task_token.environment,
                "task_run_id": str(task_token.task_run_id),
                "claim": task_token.claim,
                "reason": _ATTEMPT_ENDED,
            },
        )
        self._session.commit()

    def _key_id_taken(self, key_id: str) -> bool:
        """Whether a task token with this key_id exists, i.e. an insert failure was a collision.

        Asked after a rolled-back `IntegrityError` instead of parsing the
        driver's message or constraint name, which differ between SQLite and
        Postgres.

        Parameters
        ----------
        key_id
            The key_id the failed insert used.

        Returns
        -------
        bool
            True if another task token already holds it.
        """
        return (
            self._session.execute(select(TaskToken.id).where(TaskToken.key_id == key_id)).first()
            is not None
        )

    def revoke(self, p: PrincipalRef, key_id: str) -> None:
        """Revoke a task token before its natural expiry.

        Idempotent: revoking an already-revoked task token is a no-op — it
        leaves the existing `revoked_at` untouched and records no second
        `TASK_TOKEN_REVOKED` event.

        Parameters
        ----------
        p
            The acting principal.
        key_id
            The task token's key_id.

        Raises
        ------
        NotFoundError
            If no task token has that key_id.
        PermissionDenied
            If the acting principal may not mint task tokens, or is not an instance
            administrator and the token was not minted for itself over REST (audited; the
            same refusal for an unknown key id).
        """
        self._authz.authorize(p, Action.MINT_TASK_TOKEN, Securable.instance_scope())
        task_token = self._session.execute(
            select(TaskToken).where(TaskToken.key_id == key_id)
        ).scalar_one_or_none()
        if not holds_instance_admin(self._session, p.id) and (
            task_token is None
            or task_token.task_run_id is not None
            or task_token.principal_id != p.id
        ):
            # One refusal for "not yours", "bound to a task" and "does not exist", so
            # the response never reveals whether a key id exists.
            AuditService(self._session).record(
                p.id,
                AuditEventType.AUTHORIZATION_DENIED,
                securable=Securable.instance_scope(),
                detail={
                    "action": Action.MINT_TASK_TOKEN.value,
                    "operation": "revoke",
                    "key_id": key_id[:64],
                    "reason": "not_self",
                },
                commit=True,
            )
            raise PermissionDenied(
                f"{p.name!r} may revoke task tokens only for itself; revoking another "
                "principal's token, or one a worker minted for a task, needs the ADMIN "
                "role on the instance."
            )
        if task_token is None:
            raise NotFoundError(f"No task token {key_id!r}.")
        if task_token.revoked_at is not None:
            return
        task_token.revoked_at = datetime.now(UTC)
        principal = self._session.get(Principal, task_token.principal_id)
        assert principal is not None  # nosec B101 -- FK guarantees the row still exists
        AuditService(self._session).record(
            p.id,
            AuditEventType.TASK_TOKEN_REVOKED,
            detail={
                "actor": p.name,
                "subject": principal.name,
                "key_id": key_id,
                "environment": task_token.environment,
                "asset_scope": list(task_token.asset_scope),
            },
        )
        self._session.commit()

    def verify(self, key_id: str, secret: str) -> TaskTokenScope:
        """Verify a presented task token and resolve its identity and scope.

        Parameters
        ----------
        key_id
            The task token's public lookup key.
        secret
            The plaintext secret presented by the caller.

        Returns
        -------
        TaskTokenScope
            The principal, environment, and declared asset scope.

        Raises
        ------
        AuthenticationError
            If `key_id` is unknown, the secret does not match, or
            `resolve_task_token_scope` finds the token no longer usable —
            expired, revoked, its principal deactivated, or the personal
            access token it was minted with no longer working.
        """
        task_token = self._session.execute(
            select(TaskToken).where(TaskToken.key_id == key_id)
        ).scalar_one_or_none()
        if task_token is None or not verify_secret(secret, task_token.secret_hash):
            raise AuthenticationError("Invalid credentials.")
        scope = resolve_task_token_scope(self._session, task_token.id)
        if scope is None:
            raise AuthenticationError("This task token is no longer valid.")
        principal = self._session.get(Principal, task_token.principal_id)
        # resolve_task_token_scope already confirmed this principal exists
        # and is active; this only narrows the type for PrincipalRef below.
        assert principal is not None  # nosec B101 -- not a security boundary, see above
        return TaskTokenScope(
            principal=PrincipalRef(
                id=principal.id,
                name=principal.name,
                type=principal.type,
                language=principal.language,
                task_scope=scope,
                run_context=(
                    RunContext(task_run_id=scope.bound.task_run_id, claim=scope.bound.claim)
                    if scope.bound is not None
                    else None
                ),
            ),
            environment=scope.environment,
            asset_scope=scope.asset_scope,
        )

    def list_live(
        self,
        p: PrincipalRef,
        *,
        environment: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[ListedTaskToken]:
        """List the live task tokens, newest first, one page at a time.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Only the tokens of this environment; `None` for every environment. A name no
            token has gives an empty page, not an error.
        limit
            Most tokens returned.
        cursor
            The `next_cursor` of the previous page. Send the same `environment` with every
            page.

        Returns
        -------
        Page of ListedTaskToken
            The tokens neither revoked nor expired, nor minted with a personal access token that
            is revoked or has expired or whose owner is deactivated, by issue time, newest
            first, equal times by id, each with its principal's name and, for a token bound to
            a task, its run, job and asset, and the cursor of the next page.

        Raises
        ------
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_TOKEN` works only with it); a task or CI token is refused too.
        ValidationError
            If `limit` is below 1 or `cursor` is not one this server issued for the task-token
            list.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        now = datetime.now(UTC)
        minter = aliased(Principal)
        query = (
            select(TaskToken)
            .outerjoin(PersonalAccessToken, PersonalAccessToken.id == TaskToken.source_pat_id)
            .outerjoin(minter, minter.id == PersonalAccessToken.principal_id)
            .where(
                TaskToken.revoked_at.is_(None),
                TaskToken.expires_at > now,
                # A token whose PAT is revoked or has expired, or whose PAT's owner is
                # deactivated, no longer signs in (`_minting_pat_works`), so it is not live. A
                # token without a PAT has NULL for all three.
                PersonalAccessToken.revoked_at.is_(None),
                or_(
                    PersonalAccessToken.expires_at.is_(None),
                    PersonalAccessToken.expires_at > now,
                ),
                or_(TaskToken.source_pat_id.is_(None), minter.is_active.is_(True)),
            )
        )
        if environment is not None:
            query = query.where(TaskToken.environment == environment)
        page = newest_first(
            self._session,
            query,
            kind="task-tokens",
            at=TaskToken.issued_at,
            row_id=TaskToken.id,
            at_of=lambda row: row.issued_at,
            limit=limit,
            cursor=cursor,
        )
        details = {
            token_id: (name, run_id, job, asset)
            for token_id, name, run_id, job, asset in self._session.execute(
                select(TaskToken.id, Principal.name, Run.id, Run.job, TaskRun.asset)
                .join(Principal, Principal.id == TaskToken.principal_id)
                .outerjoin(TaskRun, TaskRun.id == TaskToken.task_run_id)
                .outerjoin(Run, Run.id == TaskRun.run_id)
                .where(TaskToken.id.in_({row.id for row in page.items}))
            )
        }
        return Page(
            [ListedTaskToken(row, *details[row.id]) for row in page.items], page.next_cursor
        )


def revoke_task_tokens_for_environment(
    session: Session, actor: PrincipalRef, environment_name: str
) -> int:
    """Soft-revoke every unrevoked task token scoped to a deleted environment.

    `TaskToken.environment` is a plain name column, not a foreign key
    — a token minted for "prod" stays usable by name alone even after
    "prod" is deleted, and would still narrow-but-not-refuse against a
    later environment re-created under the same name. Called by
    `lakana_core.catalog.service.CatalogService.delete_environment`, after
    its own authorization check and before its own commit, so the
    revocations land in the same transaction as the drop. Does not
    authorize on its own — the caller already has — matching
    `lakana_core.grants.revocation.revoke_attached`'s precedent.

    Parameters
    ----------
    session
        Database session.
    actor
        The principal performing the deletion; recorded as each
        `TASK_TOKEN_REVOKED` event's `principal_id` and as `detail.actor`.
        `TaskToken` has no `revoked_by` column (unlike `Grant`/`Secret`) —
        only `revoked_at` — so the audit event is the only place this is
        recorded.
    environment_name
        The name of the environment being deleted.

    Returns
    -------
    int
        The number of task tokens revoked.
    """
    return _revoke_where(
        session,
        actor,
        TaskToken.environment == environment_name,
        {"reason": "environment_deleted"},
    )


def _revoke_where(
    session: Session,
    actor: PrincipalRef,
    condition: ColumnElement[bool],
    reason: dict[str, object],
) -> int:
    """Soft-revoke every unrevoked task token that meets a condition, one event each.

    Parameters
    ----------
    session
        Database session; the caller commits.
    actor
        Recorded as each `TASK_TOKEN_REVOKED` event's `principal_id` and as `detail.actor`.
    condition
        Which task tokens.
    reason
        Added to each event's detail: ``reason`` and what it names.

    Returns
    -------
    int
        The number of task tokens revoked.
    """
    now = datetime.now(UTC)
    tokens = (
        session.execute(select(TaskToken).where(condition, TaskToken.revoked_at.is_(None)))
        .scalars()
        .all()
    )
    for token in tokens:
        token.revoked_at = now
        principal = session.get(Principal, token.principal_id)
        assert principal is not None  # nosec B101 -- FK guarantees the row still exists
        AuditService(session).record(
            actor.id,
            AuditEventType.TASK_TOKEN_REVOKED,
            detail={
                "actor": actor.name,
                "subject": principal.name,
                "key_id": token.key_id,
                "environment": token.environment,
                "asset_scope": list(token.asset_scope),
                **reason,
            },
        )
    return len(tokens)


def revoke_task_tokens_of_pat(
    session: Session, actor: PrincipalRef, pat: PersonalAccessToken
) -> int:
    """Soft-revoke every unrevoked task token minted with a personal access token.

    Called by `lakana_core.identity.pats.PatService.revoke` before its own commit, so the
    tokens are revoked in the same transaction as the PAT. Authentication would refuse them
    anyway (`resolve_task_token_scope` reads the PAT's row every time); this makes the rows and
    the audit log say so too. Does not authorize on its own: the caller already has.

    Parameters
    ----------
    session
        Database session.
    actor
        The principal revoking the PAT; recorded as each `TASK_TOKEN_REVOKED` event's
        `principal_id` and as `detail.actor`.
    pat
        The PAT being revoked.

    Returns
    -------
    int
        The number of task tokens revoked.
    """
    return _revoke_where(
        session,
        actor,
        TaskToken.source_pat_id == pat.id,
        {"reason": "pat_revoked", "pat_key_id": pat.key_id},
    )
