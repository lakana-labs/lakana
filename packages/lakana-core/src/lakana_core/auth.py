"""The authorization seam.

M1 ships an allow-all implementation deliberately. The call sites exist from
this milestone so that M3 replaces an implementation rather than threading
permission checks through code that never had them.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from lakana_core.identifiers import NamespacePath

if TYPE_CHECKING:
    from lakana_core.models import PrincipalType

# PrincipalType is imported for the type checker only: lakana_core.models imports
# SecurableKind/Action from this module (via models.grant and grants.roles), so a runtime import
# here made `import lakana_core.grants.roles` fail as the first import of a process. It is used
# below only as an annotation, which `from __future__ import annotations` keeps a string.


@dataclass(frozen=True)
class BoundTask:
    """The task attempt a bound task token was minted for.

    Resolved from the database on every authentication, never from the
    token's own `asset_scope` strings.

    Parameters
    ----------
    task_run_id
        The task run.
    claim
        The task's claim counter when the token was minted; the token is
        valid only while the task run still has it.
    asset
        The task's own asset, the only table it may create or write.
    inputs
        The asset's declared inputs from the project version manifest, sorted.
    readable
        The only tables it may read: `inputs`, plus the tables behind every
        input that is a view (a view is read with the reader's own grants
        on its base tables).
    output
        ``table`` or ``artifact``, from the project version manifest. A task of a
        ``table`` asset may not add an artifact version, and one of an
        ``artifact`` asset may not write a table; the lockdown guard
        (`ProtectedWriteGuard`) enforces both from the manifest.
    keep_versions
        How many live versions an artifact asset keeps; `None` keeps all.
        Applied by the server at commit.
    connections
        The connections the asset declares in the project version manifest, in declared
        order (`lakana_core.projects.manifest.read_entry_connections`); empty when it declares
        none or when the stored value cannot be read. A write by this task records one lineage
        edge per connection, derived here and never from what the task sends.
    connections_readable
        Whether the manifest's `connections` value could be read; `False` for a damaged entry,
        which then declares none.
    repository
        The name of the repository the run's project version was deployed from; empty when
        that row is gone.
    """

    task_run_id: uuid.UUID
    claim: int
    asset: str
    inputs: tuple[str, ...]
    readable: tuple[str, ...]
    output: str = "table"
    keep_versions: int | None = None
    connections: tuple[str, ...] = ()
    connections_readable: bool = True
    repository: str = ""


@dataclass(frozen=True)
class TaskScope:
    """The environment and declared assets a task-token principal is restricted to.

    Carried on `PrincipalRef.task_scope` so every `authorize()` call made
    with that principal — not just the one at authentication time — can see
    and enforce it, however many service methods a single request passes
    through.

    Parameters
    ----------
    environment
        The one environment this task may touch.
    asset_scope
        Declared asset name/pattern strings (e.g. ``"bronze.*"``).
    token_id
        The minted task token's own id. Carried so a query run submitted
        under this scope can be stamped with it (`QueryRun.task_token_id`)
        and later re-checked for validity — revoked, expired, or deleted —
        independently of the principal it authenticates as.
    bound
        Set for a token the worker minted for one task attempt; `None` for a
        REST-minted token. A bound token is narrowed further than
        `asset_scope` (see `GrantAuthorization._check_task_scope`).
    """

    environment: str
    asset_scope: list[str]
    token_id: uuid.UUID
    bound: BoundTask | None = None


@dataclass(frozen=True)
class CiScope:
    """What a CI token (`lakana_ci_…`) is bound to; carried on `PrincipalRef.ci_scope`.

    Resolved from the database at every authentication (`lakana_core.ci_oidc.exchange`), never
    from the token's text. It narrows, never widens: `GrantAuthorization` allows a CI-scoped
    principal nothing but `DEPLOY` on `environment`, and only where the principal's own grants
    allow it too; `DeployService` allows one submission of `repository`, of a kind in `kinds`,
    pinned to `sha` for a deploy, and afterwards only reading that run.

    Parameters
    ----------
    token_id
        The CI token's own id (`CiToken.id`), recorded on the run it submits.
    rule_id
        The trust rule the token was issued under.
    environment
        The one environment's name.
    environment_id
        Its id.
    repository
        The one repository's name.
    repository_id
        Its id.
    kinds
        ``deploy`` and/or ``dry_run``.
    sha
        The signed commit (the ID token's ``sha`` claim), the only one a deploy may deploy;
        `None` when the ID token carried none (then only a dry run is possible).
    used
        Whether the token has already submitted its one run.
    expires_at
        When the token stops working; `None` only where a scope is built without a token row.
    start_on_deploy_jobs
        The rule's opt-in (`CiTrustRule.start_on_deploy_jobs`): a deploy under it may start the
        deployed project's jobs with ``on_deploy: true``.
    starting_deploy_jobs
        Set only by the worker while it starts those jobs after the deploy
        (`lakana_core.jobs.on_deploy`), never from a request. With the opt-in, it lets the
        scope `RUN` in its own environment; nothing else changes.
    """

    token_id: uuid.UUID
    rule_id: uuid.UUID
    environment: str
    environment_id: uuid.UUID
    repository: str
    repository_id: uuid.UUID
    kinds: frozenset[str]
    sha: str | None
    used: bool = False
    expires_at: datetime | None = None
    start_on_deploy_jobs: bool = False
    starting_deploy_jobs: bool = False


@dataclass(frozen=True)
class RunContext:
    """The task run a write is executed for (the claim form).

    Two forms, exactly one of which is set:

    - `query_run_id`: set only by the worker (`lakana_worker.pipeline.run_one`)
      from `QueryRun.task_run_id`, for the write run of a SQL task.
    - `claim`: set at authentication when the presented task token is bound
      to a task attempt, by the worker for a queued query submitted with
      one, and by the worker for the rollback after a failed check of the
      task it holds (`lakana_worker.tasks`). Never taken from a
      request body or parameter.

    The protected-environment lockdown re-reads the task run before
    trusting either. A context with both or neither covers nothing.

    Parameters
    ----------
    task_run_id
        The running task run.
    query_run_id
        The query run executing it; must still be the task's current
        `query_run_id`, so a superseded attempt holds no authority.
    claim
        The task's claim counter when the token was minted; must still be
        the task's `claim_count`.
    """

    task_run_id: uuid.UUID
    query_run_id: uuid.UUID | None = None
    claim: int | None = None


@dataclass(frozen=True)
class WriteFence:
    """The standing a worker's table write needs: its query run, still running under it.

    Parameters
    ----------
    query_run_id
        The query run the worker executes.
    worker_id
        The worker's id as written on that run when it claimed it; `None` for a run
        claimed by a worker that sent none.
    """

    query_run_id: uuid.UUID
    worker_id: str | None


@dataclass(frozen=True)
class PrincipalRef:
    """The actor on whose behalf an operation runs.

    Parameters
    ----------
    id
        The principal's stable identifier.
    name
        Human-readable name, used in error messages and the audit log.
    type
        Whether this is a human user or a service account.
    task_scope
        Set when this principal was authenticated via a task token —
        `None` for a PAT-authenticated principal. An `AuthorizationService`
        must narrow every check to this scope before consulting any grant.
    run_context
        Set by the worker for a write it executes inside a task run, and at
        authentication for a token bound to a task attempt; `None`
        otherwise.
    break_glass_reason
        The reason of a break-glass write, set per request by the write
        endpoints; `None` otherwise. Authorized and audited by
        `ProtectedWriteGuard`.
    write_fence
        Set only by the worker (`lakana_worker.pipeline._run_one`) for the run it executes;
        never taken from a request. The guard then refuses a table write unless that run is
        still `RUNNING` under that worker; `None` otherwise.
    ci_scope
        Set when this principal was authenticated with a CI token, and by the worker for the
        re-check of a deploy such a token submitted; `None` otherwise. An
        `AuthorizationService` must narrow every check to it before consulting any grant.
    """

    id: uuid.UUID
    name: str
    type: PrincipalType
    task_scope: TaskScope | None = None
    run_context: RunContext | None = None
    break_glass_reason: str | None = None
    write_fence: WriteFence | None = None
    ci_scope: CiScope | None = None


class SecurableKind(enum.StrEnum):
    """The kind of object an action applies to."""

    INSTANCE = "INSTANCE"
    ENVIRONMENT = "ENVIRONMENT"
    NAMESPACE = "NAMESPACE"
    TABLE = "TABLE"


class Action(enum.StrEnum):
    """An operation that authorization can permit or refuse.

    Kept deliberately small. A long list of privileges means that nobody can
    answer "who can read this table?" without tooling.
    """

    MANAGE_ENVIRONMENT = "MANAGE_ENVIRONMENT"
    READ_ENVIRONMENT = "READ_ENVIRONMENT"
    CREATE_NAMESPACE = "CREATE_NAMESPACE"
    READ_NAMESPACE = "READ_NAMESPACE"
    DROP_NAMESPACE = "DROP_NAMESPACE"
    MANAGE_NAMESPACE = "MANAGE_NAMESPACE"
    """Mutate an existing namespace's properties.

    Distinct from `CREATE_NAMESPACE` on purpose: a principal allowed to create
    new namespaces must not thereby be allowed to rewrite the properties of
    namespaces someone else created. Distinct from `DROP_NAMESPACE` because
    editing a label must not require destructive rights.
    """
    CREATE_TABLE = "CREATE_TABLE"
    READ_TABLE = "READ_TABLE"
    WRITE_TABLE = "WRITE_TABLE"
    DROP_TABLE = "DROP_TABLE"
    MAINTAIN_TABLE = "MAINTAIN_TABLE"
    """Expire a table's old snapshots and delete its orphaned files.

    Table-scoped and grantable at table, namespace or environment level like
    the other table actions. Deliberately separate from `WRITE_TABLE`:
    maintenance deletes files and history, which a principal allowed to
    append rows must not thereby be allowed to do. Only the built-in admin
    role carries it, and it is not a task-token action (`TASK_TOKEN_ACTIONS`).
    """
    SUBMIT_QUERY = "SUBMIT_QUERY"
    """Submit SQL to be executed against an environment.

    Environment-scoped, not table-scoped: a query can reference many tables,
    known only once sqlglot has parsed the submitted SQL, so this authorizes
    the act of submitting, not any specific table. Per-table `READ_TABLE`
    authorization happens later, in the worker, once the referenced tables
    are known.
    """
    MANAGE_PRINCIPAL = "MANAGE_PRINCIPAL"
    """Create, list, or deactivate a principal. Instance-scoped."""
    MANAGE_GROUP = "MANAGE_GROUP"
    """Create, delete, or manage the membership of a group. Instance-scoped."""
    MANAGE_TOKEN = "MANAGE_TOKEN"  # nosec B105 -- an Action enum label, not a credential
    """Issue, list, or revoke a PAT for a principal. Instance-scoped."""
    MINT_TASK_TOKEN = "MINT_TASK_TOKEN"  # nosec B105 -- an Action enum label, not a credential
    """Mint a task token on behalf of a principal. Instance-scoped."""
    MANAGE_GRANT = "MANAGE_GRANT"  # nosec B105 -- an Action enum label, not a credential
    """Create, list, or revoke a grant. Instance-scoped."""
    MANAGE_POLICY = "MANAGE_POLICY"
    """Create, list, or revoke a row filter or column mask. Instance-scoped."""
    MANAGE_SECRET = "MANAGE_SECRET"  # nosec B105 -- an Action enum label, not a credential
    """Create, list, rotate, or revoke a secret, or reference one by name in
    an environment's storage config. Environment-scoped."""
    MANAGE_CONNECTION = "MANAGE_CONNECTION"
    """Create, change, delete or test a connection of an environment. Environment-scoped;
    reading connections is `READ_ENVIRONMENT`. Not a task-token action: a task fetches a
    connection by its bound token's own binding, never by a grant."""
    READ_AUDIT_LOG = "READ_AUDIT_LOG"
    """Read the audit log. Instance-scoped — a security-review/compliance
    function reserved for organization-wide admins, not delegated
    per-environment the way `MANAGE_ENVIRONMENT` is."""
    READ_QUERY_TEXT = "READ_QUERY_TEXT"
    """See query text and column values in other principals' audit events.

    The fields of `lakana_core.audit.content.CONTENT_FIELDS`: a query's SQL text and an
    incremental write's watermark.

    Instance-scoped, like the audit log it applies to, and granted as a single action only:
    no role carries it, `ADMIN` included. Without it a reader of the audit log sees each
    content field of another principal's event hidden (query text as its SHA-256 and length,
    a watermark with nothing derived from it; `lakana_core.audit.content`); a reader's own
    events are always shown in full. It opens nothing by itself: reading the log still needs
    `READ_AUDIT_LOG`. Not a task-token action.
    """
    DEPLOY = "DEPLOY"
    """Deploy, dry-run or roll back a repository in an environment.

    Environment-scoped, not per repository: a principal holding it on `prod`
    can deploy any registered repository there. Dry runs need it too — they
    fetch with the environment's credential. "Only CI deploys to prod" is
    this grant held only by the CI service principal, and therefore no human
    holding `ADMIN` (which carries every action but `READ_QUERY_TEXT`) on the instance or on
    prod.
    Admin-only by role and not a task-token action.
    """
    MANAGE_REPOSITORY = "MANAGE_REPOSITORY"
    """Register, update or delete a git repository. Instance-scoped.

    The trust boundary for repository URLs: deployers pick a name, never a
    URL, so the worker only ever fetches from hosts an admin chose.
    """
    MANAGE_PACKAGE_INDEX = "MANAGE_PACKAGE_INDEX"
    """Register, update or delete a package index. Instance-scoped.

    The trust boundary for where a project's packages come from with a
    credential: a project names an index by URL, and only a registered URL
    is accepted. Admin-only by role and not a task-token action.
    """
    RUN = "RUN"
    """Start, cancel and resume runs of the deployed project versions in an environment.

    Environment-scoped. A run writes as the deployment's bound run principal
    in a protected environment, so holding `RUN` lets a principal trigger
    deployed code, never choose what it does. Admin-only by role and not a
    task-token action.
    """


@dataclass(frozen=True)
class Securable:
    """The object an action applies to.

    Parameters
    ----------
    kind
        Which tier of the hierarchy this refers to.
    environment
        The environment containing the object, or ``None`` for an
        instance-scoped securable, which spans every environment.
    namespace
        The namespace, for namespace- and table-scoped securables.
    table
        The table name, for table-scoped securables.
    """

    kind: SecurableKind
    environment: str | None
    namespace: NamespacePath | None = None
    table: str | None = None

    @classmethod
    def instance_scope(cls) -> Securable:
        """Refer to the entire deployment, across every environment.

        Returns
        -------
        Securable
            An instance-scoped securable. Its `environment` is ``None``,
            since no single environment applies.
        """
        return cls(kind=SecurableKind.INSTANCE, environment=None)

    @classmethod
    def environment_scope(cls, environment: str) -> Securable:
        """Refer to an entire environment.

        Parameters
        ----------
        environment
            Environment name.

        Returns
        -------
        Securable
            An environment-scoped securable.
        """
        return cls(kind=SecurableKind.ENVIRONMENT, environment=environment)

    @classmethod
    def namespace_scope(cls, environment: str, namespace: NamespacePath) -> Securable:
        """Refer to a namespace.

        Parameters
        ----------
        environment
            Environment name.
        namespace
            The namespace.

        Returns
        -------
        Securable
            A namespace-scoped securable.
        """
        return cls(kind=SecurableKind.NAMESPACE, environment=environment, namespace=namespace)

    @classmethod
    def table_scope(cls, environment: str, namespace: NamespacePath, table: str) -> Securable:
        """Refer to a table.

        Parameters
        ----------
        environment
            Environment name.
        namespace
            The namespace containing the table.
        table
            The table's name.

        Returns
        -------
        Securable
            A table-scoped securable.
        """
        return cls(
            kind=SecurableKind.TABLE,
            environment=environment,
            namespace=namespace,
            table=table,
        )


class AuthorizationService(Protocol):
    """Decides whether a principal may perform an action."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Permit the action, or refuse it.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            If the principal may not perform the action.
        """
        ...

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer the same question as `authorize`, without raising or side effects.

        For visibility filtering (listing only what a caller may see), where
        a "no" is not a denied action. Implementations must return `False`
        exactly when `authorize` would raise `PermissionDenied` for the same
        arguments, and must not record the answer anywhere.

        Parameters
        ----------
        principal
            The actor the question is about.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Whether `authorize` would permit this.
        """
        ...


class AllowAllAuthorization:
    """Permits every action.

    This is M1's implementation. M3 replaces it with grants on hierarchical
    securables; the call sites do not change.
    """

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Permit the action unconditionally.

        Parameters
        ----------
        principal
            The actor requesting the action. Ignored.
        action
            The action requested. Ignored.
        securable
            The object acted upon. Ignored.
        """

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Permit unconditionally.

        Parameters
        ----------
        principal
            Ignored.
        action
            Ignored.
        securable
            Ignored.

        Returns
        -------
        bool
            Always `True`.
        """
        return True
