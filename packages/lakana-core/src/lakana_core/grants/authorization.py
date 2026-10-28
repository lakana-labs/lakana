"""GrantAuthorization: the real AuthorizationService, backed by persisted grants."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lakana_core.artifacts.manifest import shown
from lakana_core.auth import (
    Action,
    BoundTask,
    CiScope,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.catalog.identity import resolve_environment_id, resolve_table_id
from lakana_core.errors import PermissionDenied, ValidationError
from lakana_core.grants.roles import ROLE_ACTIONS
from lakana_core.identifiers import NAMESPACE_SEPARATOR, validate_path_component
from lakana_core.models import Grant, GroupMembership

TASK_TOKEN_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.READ_ENVIRONMENT,
        Action.READ_NAMESPACE,
        Action.READ_TABLE,
        Action.SUBMIT_QUERY,
        Action.CREATE_TABLE,
        Action.WRITE_TABLE,
        Action.DROP_TABLE,
    }
)
"""The only actions a task token may ever perform.

A task token authenticates a Python task running unattended, on a schedule
or in response to an event, with no human reviewing each individual call —
unlike a PAT, which a human presents themselves. Every other action,
including the three that work only with the `ADMIN` role (`MANAGE_GRANT`,
`MANAGE_TOKEN`, `MANAGE_GROUP`), `MINT_TASK_TOKEN`, and every namespace- or environment-level
mutation (`DROP_NAMESPACE`, `MANAGE_SECRET`, ...), is refused outright,
regardless of what the token's own principal could otherwise do.
"""

BOUND_TASK_TOKEN_ACTIONS: frozenset[Action] = TASK_TOKEN_ACTIONS - {Action.DROP_TABLE}
"""What a token bound to a task attempt may ever do: a task reads and materializes,
it never drops."""

_BOUND_ENVIRONMENT_ACTIONS = frozenset(
    {Action.READ_ENVIRONMENT, Action.READ_NAMESPACE, Action.SUBMIT_QUERY}
)
_BOUND_WRITES = frozenset({Action.CREATE_TABLE, Action.WRITE_TABLE})


def _named(name: str) -> str:
    """Show a securable's name in a refusal: ASCII-escaped, cut, in single quotes.

    Parameters
    ----------
    name
        A namespace or table name from a request; of any length.

    Returns
    -------
    str
        The name in single quotes, at most 200 characters inside them.
    """
    return f"'{shown(name)}'"


def _table_parts(dotted: str) -> tuple[str, ...] | None:
    """Split a manifest-style dotted table name into ``(*namespace, table)``.

    Every string in a `BoundTask` is treated as unparsed input: a name that
    is not a namespace of at least one valid component plus a valid table
    name yields `None`, which callers must treat as matching nothing.

    Parameters
    ----------
    dotted
        A dotted ``namespace.table`` name.

    Returns
    -------
    tuple[str, ...] or None
        The components, or `None` if `dotted` is not a valid table name.
    """
    parts = tuple(dotted.split("."))
    if len(parts) < 2:
        return None
    try:
        for part in parts:
            validate_path_component(part, what="Table name component")
    except ValidationError:
        return None
    return parts


def _matches_namespace_pattern(dotted_namespace: str, pattern: str) -> bool:
    """Match a dotted namespace path against a declared asset-scope pattern.

    A pattern ending in ``.*`` covers the namespace it names and everything
    nested under it (``bronze.*`` covers ``bronze`` and ``bronze.eu``); a
    table-shaped pattern (``bronze.orders``) covers the namespace it
    navigates through (``bronze``), so a token scoped to a specific table
    can still read its containing namespace.

    Parameters
    ----------
    dotted_namespace
        The namespace being checked, in its dotted display form.
    pattern
        A declared `TaskScope.asset_scope` entry.

    Returns
    -------
    bool
        `True` if `dotted_namespace` is covered by `pattern`.

    Notes
    -----
    Matches on `NamespacePath`'s dotted *display* form, which is ambiguous
    if a namespace component itself contains a literal ``.`` (a name from before dots were refused),
    inherited here from `_matches_asset_pattern`. Accepted for the same
    reason: this only narrows an already-granted principal's access, it
    never grants beyond what `_applies_to` (which uses the unambiguous
    unit-separator form) separately allows.
    """
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return dotted_namespace == prefix or dotted_namespace.startswith(prefix + ".")
    return dotted_namespace == pattern.rsplit(".", 1)[0]


def _matches_asset_pattern(dotted_table: str, pattern: str) -> bool:
    """Match a dotted table path against a declared asset-scope pattern.

    A pattern ending in ``.*`` covers every table under that namespace
    prefix (``bronze.*`` matches ``bronze.orders``); anything else must
    match exactly.

    Parameters
    ----------
    dotted_table
        The table being checked, as ``namespace.table``.
    pattern
        A declared `TaskScope.asset_scope` entry.

    Returns
    -------
    bool
        `True` if `dotted_table` matches `pattern`.

    Notes
    -----
    Matches on `NamespacePath`'s dotted *display* form, which is ambiguous
    if a namespace component itself contains a literal ``.`` (a name from before dots were refused).
    Accepted: this narrows an already-granted principal's access, it
    never grants beyond what `GrantAuthorization._applies_to` (which uses
    the unambiguous unit-separator form) separately allows.
    """
    if pattern.endswith(".*"):
        return dotted_table.startswith(pattern[:-1])
    return dotted_table == pattern


@dataclass(frozen=True)
class Decision:
    """The authorizer's answer to one question, and what produced it.

    Parameters
    ----------
    allowed
        Whether the action is permitted.
    grants
        Every active grant that covers the action at the object or an ancestor of it, direct
        or through a group, oldest first; empty when `allowed` is false.
    reason
        Why it is refused (the text `authorize` raises), or `None` when it is permitted.
    """

    allowed: bool
    grants: tuple[Grant, ...]
    reason: str | None


class GrantAuthorization:
    """Authorization backed by persisted grants.

    Replaces `AllowAllAuthorization` at every production call site. The
    resolution algorithm is described in `docs/agent/security.md`.

    Parameters
    ----------
    session
        Database session to query grants and group memberships from.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._environment_id_cache: dict[str, uuid.UUID | None] = {}
        self._table_id_cache: dict[tuple[str, str, str], uuid.UUID | None] = {}

    def _resolve_environment_id(self, environment: str) -> uuid.UUID | None:
        """Resolve an environment name to its id, caching by name for this instance's lifetime.

        Parameters
        ----------
        environment
            The environment's name.

        Returns
        -------
        uuid.UUID or None
            The environment's id, or `None` if no environment has that
            name.
        """
        if environment not in self._environment_id_cache:
            self._environment_id_cache[environment] = resolve_environment_id(
                self._session, environment
            )
        return self._environment_id_cache[environment]

    def _resolve_table_id(
        self, environment: str, namespace_joined: str, table: str
    ) -> uuid.UUID | None:
        """Resolve a table's names to its id, caching by name tuple for this instance's lifetime.

        Parameters
        ----------
        environment
            The environment's name.
        namespace_joined
            The table's namespace, in its unit-separator-joined storage
            form.
        table
            The table's name.

        Returns
        -------
        uuid.UUID or None
            The table's id, or `None` if no table matches that combination.
        """
        key = (environment, namespace_joined, table)
        if key not in self._table_id_cache:
            self._table_id_cache[key] = resolve_table_id(
                self._session, environment, namespace_joined, table
            )
        return self._table_id_cache[key]

    def explain(self, principal: PrincipalRef, action: Action, securable: Securable) -> Decision:
        """Decide whether a grant covers the action, and name the grants that do.

        The one decision of this class: `authorize` and `permits` are implemented through it,
        so an explanation can never differ from what a route decides.

        Parameters
        ----------
        principal
            The actor the question is about. If authenticated via a task token,
            `principal.task_scope` narrows the decision before any grant is consulted.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        Decision
            Permitted with the covering grants, or refused with the reason: the task scope
            (when present) does not cover `securable`, or no grant (direct or via group
            membership) covers `action` at `securable` or an ancestor of it.
        """
        if principal.task_scope is not None:
            try:
                self._check_task_scope(principal, principal.task_scope, action, securable)
            except PermissionDenied as exc:
                return Decision(allowed=False, grants=(), reason=str(exc))
        if principal.ci_scope is not None:
            if self._ci_deploy_job_start(principal.ci_scope, action, securable):
                return Decision(allowed=True, grants=(), reason=None)
            reason = self._ci_scope_refusal(principal, principal.ci_scope, action, securable)
            if reason is not None:
                return Decision(allowed=False, grants=(), reason=reason)
        env_id = (
            self._resolve_environment_id(securable.environment)
            if securable.environment is not None
            else None
        )
        table_id: uuid.UUID | None = None
        if securable.kind == SecurableKind.TABLE:
            # `Securable.table_scope` guarantees `environment`/`namespace`/
            # `table` are all set for a TABLE-kind securable; the type
            # checker cannot see that application-level invariant.
            assert securable.environment is not None  # nosec B101
            assert securable.namespace is not None  # nosec B101
            assert securable.table is not None  # nosec B101
            table_id = self._resolve_table_id(
                securable.environment, securable.namespace.joined(), securable.table
            )
        group_ids = list(
            self._session.execute(
                select(GroupMembership.group_id).where(GroupMembership.principal_id == principal.id)
            )
            .scalars()
            .all()
        )
        subject_filter = Grant.principal_id == principal.id
        if group_ids:
            subject_filter = or_(subject_filter, Grant.group_id.in_(group_ids))
        grants = (
            self._session.execute(
                select(Grant)
                .where(Grant.revoked_at.is_(None), subject_filter)
                .order_by(Grant.granted_at, Grant.id)
            )
            .scalars()
            .all()
        )
        covering = tuple(
            grant
            for grant in grants
            if self._covers(grant, action) and self._applies_to(grant, securable, env_id, table_id)
        )
        if covering:
            return Decision(allowed=True, grants=covering, reason=None)
        return Decision(
            allowed=False, grants=(), reason=f"{principal.name!r} may not {action.value} this."
        )

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Permit the action if a grant covers it, else raise.

        Implemented through `explain`.

        Parameters
        ----------
        principal
            The actor requesting the action. If authenticated via a task
            token, `principal.task_scope` narrows this check before any
            grant is consulted.
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            If `principal`'s task scope (when present) does not cover
            `securable`, or no grant — direct or via group membership —
            covers `action` at `securable` or an ancestor of it.
        """
        decision = self.explain(principal, action, securable)
        if not decision.allowed:
            raise PermissionDenied(decision.reason)

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Implemented *through* `authorize`, so the two can never disagree —
        task-scope narrowing included.

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
            `False` exactly when `authorize` raises `PermissionDenied`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True

    @staticmethod
    def _covers(grant: Grant, action: Action) -> bool:
        """Whether `grant` covers `action`, via a raw action or a role bundle.

        Parameters
        ----------
        grant
            A candidate grant.
        action
            The requested action.

        Returns
        -------
        bool
            `True` if `grant.action == action`, or `action` is in
            `ROLE_ACTIONS[grant.role]`.
        """
        if grant.action is not None:
            return grant.action == action
        # `exactly_one_grant` (a DB CheckConstraint on `grants`) guarantees
        # `role` is set whenever `action` is not; the type checker cannot
        # see that constraint, so this narrows it explicitly. Not a
        # security-relevant assert to strip under `-O`: even if it were,
        # the next line would raise KeyError on `grant.role` (None), never
        # silently authorize.
        assert grant.role is not None  # nosec B101
        return action in ROLE_ACTIONS[grant.role]

    @staticmethod
    def _applies_to(
        grant: Grant, securable: Securable, env_id: uuid.UUID | None, table_id: uuid.UUID | None
    ) -> bool:
        """Whether `grant`'s securable is `securable` itself or an ancestor of it.

        Bound to ids, not names: a grant's names are
        display-only, so this never compares them directly against
        `securable`'s. An id that failed to resolve (`None`, from either
        `env_id`/`table_id` or the grant's own `environment_id`/`table_id`
        — the latter possible once a target's row is dropped, see
        `ON DELETE SET NULL` on both columns) never matches anything,
        rather than the two `None`s comparing equal.

        Parameters
        ----------
        grant
            A candidate grant.
        securable
            The object being acted upon.
        env_id
            `securable`'s resolved environment id, or `None` if it has no
            environment (an `INSTANCE`-kind securable) or that environment
            does not exist.
        table_id
            `securable`'s resolved table id, or `None` unless `securable`
            is `TABLE`-kind and that table exists.

        Returns
        -------
        bool
            `True` if `grant` applies, per the hierarchy
            INSTANCE ⊇ ENVIRONMENT ⊇ NAMESPACE ⊇ TABLE, with a namespace
            grant additionally covering every nested namespace beneath it.
        """
        if grant.kind == SecurableKind.INSTANCE:
            return True
        if env_id is None or grant.environment_id != env_id:
            return False
        if grant.kind == SecurableKind.ENVIRONMENT:
            return True
        if securable.namespace is None:
            return False
        candidate_ns = securable.namespace.joined()
        if grant.kind == SecurableKind.NAMESPACE:
            # `GrantService.grant`'s `_validate_securable_fields` guarantees
            # `namespace` is set for a NAMESPACE-kind grant; the type
            # checker cannot see that application-level invariant, so this
            # narrows it explicitly. Not a security-relevant assert to
            # strip under `-O`: even if it were, the next line would raise
            # TypeError concatenating None, never silently authorize.
            assert grant.namespace is not None  # nosec B101
            return candidate_ns == grant.namespace or candidate_ns.startswith(
                grant.namespace + NAMESPACE_SEPARATOR
            )
        return table_id is not None and grant.table_id == table_id

    @staticmethod
    def _ci_deploy_job_start(scope: CiScope, action: Action, securable: Securable) -> bool:
        """Say whether a CI scope may start its deploy's `on_deploy` jobs here.

        The one widening of a CI scope, and an explicit one: the trust rule opted in
        (`start_on_deploy_jobs`), and the worker is starting the jobs of the deploy the token
        queued (`starting_deploy_jobs`, which no request can set). Then `RUN` on the scope's own
        environment is allowed by the rule itself, not by the account's grants; every other
        action, environment or securable is decided as before.

        Parameters
        ----------
        scope
            The CI scope.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            Whether the rule allows it.
        """
        return (
            scope.start_on_deploy_jobs
            and scope.starting_deploy_jobs
            and action is Action.RUN
            and securable.kind == SecurableKind.ENVIRONMENT
            and securable.environment == scope.environment
        )

    @staticmethod
    def _ci_scope_refusal(
        principal: PrincipalRef, scope: CiScope, action: Action, securable: Securable
    ) -> str | None:
        """Refuse everything a CI token may not do, before any grant is consulted.

        A CI token may only `DEPLOY` on its own environment (an `ENVIRONMENT` securable): it
        reads nothing, lists nothing and changes nothing else, whatever its principal's own
        grants allow. Narrows only; the grants still decide `DEPLOY` itself. What `DEPLOY`
        then permits is decided at each `DEPLOY` check (`lakana_core.ci_oidc.scope`).

        Parameters
        ----------
        principal
            The CI-authenticated principal, for the message.
        scope
            Its scope.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        str or None
            The refusal's text, or `None` when the grants decide.
        """
        if action is not Action.DEPLOY:
            return f"{principal.name!r}'s CI token may only deploy; it may not {action.value}."
        if (
            securable.kind != SecurableKind.ENVIRONMENT
            or securable.environment != scope.environment
        ):
            return f"{principal.name!r}'s CI token may deploy only to {scope.environment!r}."
        return None

    @staticmethod
    def _check_task_scope(
        principal: PrincipalRef, scope: TaskScope, action: Action, securable: Securable
    ) -> None:
        """Refuse an action outside a task token's allow-list, environment, or declared assets.

        Narrows only — never grants anything beyond what `_applies_to`
        separately allows. A token bound to a task attempt (`scope.bound`)
        is checked by `_check_bound_scope` instead of the asset-scope
        patterns: its declared inputs come from the project version manifest, not
        from the token's strings.

        Parameters
        ----------
        principal
            The task-token-authenticated principal, for the error message.
        scope
            Its declared environment and asset-scope patterns.
        action
            The action requested. Refused outright if not in
            `TASK_TOKEN_ACTIONS`, before any other check runs — a task
            token must never reach one of the three actions that need the
            `ADMIN` role, or a namespace/environment-mutating action,
            regardless of what its principal's own grants would otherwise allow.
        securable
            The object being acted upon.

        Raises
        ------
        PermissionDenied
            If `action` is not one a task token may ever perform;
            `securable` is outside `scope`'s declared environment; for a
            NAMESPACE-kind securable, outside every declared asset pattern's
            namespace coverage; or for a TABLE-kind securable, outside every
            declared asset pattern.
        """
        if action not in TASK_TOKEN_ACTIONS:
            raise PermissionDenied(f"Task tokens may not {action.value}.")
        bound = scope.bound
        if bound is not None and action not in BOUND_TASK_TOKEN_ACTIONS:
            raise PermissionDenied(f"A task's token may not {action.value}.")
        if securable.environment != scope.environment:
            raise PermissionDenied(
                f"{principal.name!r}'s task token is scoped to {scope.environment!r}."
            )
        if bound is not None:
            GrantAuthorization._check_bound_scope(principal, bound, action, securable)
            return
        if securable.kind == SecurableKind.NAMESPACE:
            # `Securable.namespace_scope` guarantees `namespace` is set for a
            # NAMESPACE-kind securable; the type checker cannot see that
            # application-level invariant.
            assert securable.namespace is not None  # nosec B101
            dotted_namespace = str(securable.namespace)
            if not any(
                _matches_namespace_pattern(dotted_namespace, pattern)
                for pattern in scope.asset_scope
            ):
                raise PermissionDenied(
                    f"{principal.name!r}'s task token does not declare "
                    f"namespace {_named(dotted_namespace)}."
                )
            return
        if securable.kind != SecurableKind.TABLE:
            return
        dotted = f"{securable.namespace}.{securable.table}"
        if not any(_matches_asset_pattern(dotted, pattern) for pattern in scope.asset_scope):
            raise PermissionDenied(
                f"{principal.name!r}'s task token does not declare {_named(dotted)} "
                "in its asset scope."
            )

    @staticmethod
    def _check_bound_scope(
        principal: PrincipalRef, bound: BoundTask, action: Action, securable: Securable
    ) -> None:
        """Refuse what a task attempt's token may not do.

        Called after the action allow-list and the environment check, so
        `securable` is in the token's environment and `action` is one of
        `BOUND_TASK_TOKEN_ACTIONS`. Tables are compared as tuples of parts,
        never as dotted strings: a legacy component containing a dot must
        not match a deeper path that renders the same. Every name in `bound`
        is unparsed input; one that is not a valid namespace and table
        matches nothing.

        Parameters
        ----------
        principal
            The task-token-authenticated principal, for the error message.
        bound
            The attempt: its own asset and the tables it may read.
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            For anything but: reading the environment, listing its
            namespaces or submitting a query; reading a namespace that
            contains the own asset or a readable table; `READ_TABLE` on a
            readable table; `CREATE_TABLE` or `WRITE_TABLE` on the own asset.
        """
        own = _table_parts(bound.asset)
        readable = {parts for name in bound.readable if (parts := _table_parts(name))}
        if securable.kind == SecurableKind.ENVIRONMENT:
            if action not in _BOUND_ENVIRONMENT_ACTIONS:
                raise PermissionDenied(
                    f"{principal.name!r}'s task token may not {action.value} on an environment."
                )
            return
        # `Securable.namespace_scope`/`table_scope` guarantee `namespace` (and
        # `table`) below; an INSTANCE securable never gets here, its
        # environment is `None` and was refused by the caller.
        assert securable.namespace is not None  # nosec B101
        parts = tuple(securable.namespace.parts)
        if securable.kind == SecurableKind.NAMESPACE:
            known = readable if own is None else {own, *readable}
            contains = any(name[:-1][: len(parts)] == parts for name in known)
            if action is not Action.READ_NAMESPACE or not contains:
                raise PermissionDenied(
                    f"{principal.name!r}'s task token may not {action.value} "
                    f"namespace {_named(str(securable.namespace))}."
                )
            return
        assert securable.table is not None  # nosec B101
        target = (*parts, securable.table)
        dotted = ".".join(target)
        if action is Action.READ_TABLE:
            if target not in readable:
                if dotted in bound.readable:
                    raise PermissionDenied(
                        f"{principal.name!r}'s task may read only its declared inputs; "
                        f"{_named(dotted)} has a dot inside one of its names, which makes it "
                        "another table than the input of that name."
                    )
                raise PermissionDenied(
                    f"{principal.name!r}'s task may read only its declared inputs, "
                    f"not {_named(dotted)}."
                )
            return
        if action in _BOUND_WRITES:
            if target != own:
                if dotted == bound.asset:
                    raise PermissionDenied(
                        f"{principal.name!r}'s task may write only its own asset "
                        f"{_named(bound.asset)}; {_named(dotted)} has a dot inside one of its "
                        "names, which makes it another table."
                    )
                raise PermissionDenied(
                    f"{principal.name!r}'s task may write only its own asset "
                    f"{_named(bound.asset)}, not {_named(dotted)}."
                )
            return
        raise PermissionDenied(f"{principal.name!r}'s task token may not {action.value} a table.")
