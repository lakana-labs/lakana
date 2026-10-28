"""CI trust rules: which CI jobs may deploy which repository to which environment.

A rule names an issuer (one of `LAKANA_CI_OIDC__ISSUERS`), an active service account, an
environment, a registered repository, the kinds of run it allows (``deploy`` and/or
``dry_run``; never a rollback or an undeploy) and claim conditions that must all match the ID
token exactly: the same claim, a text, the same characters. Every rule must pin the provider's
numeric repository or project id (``repository_id`` for GitHub, ``project_id`` for GitLab),
which a rename or a transfer of the repository cannot take over, and a ``ref`` or an
``environment`` condition.

Only an instance administrator manages rules (`MANAGE_TOKEN` through `authorize_admin_only`):
a rule lets a CI job act as a service account, which is issuing that account a token.
`condition_mismatches` is the one matching function of the exchange and of `explain`.
"""

from __future__ import annotations

import builtins
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.ci_oidc.config import GITHUB_ISSUER, PRESETS
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.grants.admin import authorize_admin_only
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    CiTrustRule,
    Environment,
    Principal,
    PrincipalType,
    Repository,
)
from lakana_core.models.base import utcnow
from lakana_core.projects.lookups import environment_by_name, repository_by_name

ALLOWED_KINDS = ("deploy", "dry_run")
"""What a rule may allow. A rollback and an undeploy need a person's token."""

RESERVED_CLAIMS = frozenset({"iss", "aud", "exp", "iat", "nbf", "jti"})
"""Claims the verifier already checks, or that differ for every token; never a condition."""

MAX_CONDITIONS = 16
MAX_VALUE_LENGTH = 255
_CLAIM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DIGITS = re.compile(r"^[0-9]{1,20}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_REF_CONDITIONS = ("ref", "environment")


def trigger_claims(issuer: str) -> tuple[str, ...]:
    """Name the claims of which a rule must pin one, to fix how the CI run was started.

    ``ref`` alone does not say that: a GitHub ``pull_request_target`` run, which a fork's pull
    request starts, gets an ID token with the base branch as its ``ref``. On GitLab a ``ref`` pin
    alone also matches the branch while it is unprotected, and pipelines of every source
    (schedules, triggers, the web, the API), so a rule must say which runs it trusts. (A fork's
    merge request pipeline carries the fork's ``project_id``, which the id pin already refuses.)
    An ``environment`` condition counts on both: deploying to it is what its protection rules
    decide.

    Parameters
    ----------
    issuer
        A configured issuer URL.

    Returns
    -------
    tuple of str
        ``event_name`` or ``environment`` for GitHub Actions; ``ref_protected``,
        ``pipeline_source`` or ``environment`` for GitLab.
    """
    if issuer == GITHUB_ISSUER:
        return ("event_name", "environment")
    return ("ref_protected", "pipeline_source", "environment")


def _trigger_message(issuer: str) -> str:
    """Say which trigger condition a rule needs, with the option that adds it.

    Parameters
    ----------
    issuer
        A configured issuer URL.

    Returns
    -------
    str
        The sentence.
    """
    if issuer == GITHUB_ISSUER:
        return (
            "A GitHub rule must also pin how the run was started: add --claim event_name=push "
            "(or the event that deploys), or pin a protected GitHub environment with --claim "
            "environment=production. A ref alone also matches a pull_request_target run that "
            "a fork's pull request starts."
        )
    return (
        "A GitLab rule must also pin how the pipeline was started: add --claim "
        "ref_protected=true or --claim pipeline_source=push, or pin a protected environment with "
        "--claim environment=production. A ref alone also matches the branch while it is "
        "unprotected, and pipelines of every source (schedules, triggers, the web, the API)."
    )


def id_claim(issuer: str) -> str:
    """Name the claim that carries an issuer's numeric repository or project id.

    Parameters
    ----------
    issuer
        A configured issuer URL.

    Returns
    -------
    str
        ``repository_id`` for GitHub Actions; ``project_id`` for GitLab (GitLab.com, or a
        self-hosted GitLab: the only other issuers the settings accept).
    """
    return "repository_id" if issuer == GITHUB_ISSUER else "project_id"


def condition_mismatches(
    conditions: Mapping[str, str], claims: Mapping[str, object]
) -> list[tuple[str, str]]:
    """Compare a rule's conditions with an ID token's claims, exactly.

    A claim matches only when it is a text with exactly the condition's characters: no case
    folding, no trimming, no number for a text, no list that contains the text.

    Parameters
    ----------
    conditions
        Claim name to required text.
    claims
        The claims (verified ones at an exchange; pasted ones in `explain`).

    Returns
    -------
    list of tuple of (str, str)
        ``(claim, problem)`` for each condition that fails, sorted by claim, with the problem
        ``missing``, ``not_text`` or ``differs``; empty when the rule's conditions all match.
    """
    found: list[tuple[str, str]] = []
    for name in sorted(conditions):
        if name not in claims:
            found.append((name, "missing"))
            continue
        actual = claims[name]
        if not isinstance(actual, str):
            found.append((name, "not_text"))
        elif actual != conditions[name]:
            found.append((name, "differs"))
    return found


def _resolve_issuer(value: str, issuers: Sequence[str]) -> str:
    """Resolve an issuer as given (a preset or a URL) to one of the configured issuers.

    Parameters
    ----------
    value
        ``github``, ``gitlab`` (any letter case) or an issuer URL.
    issuers
        `LAKANA_CI_OIDC__ISSUERS`, resolved.

    Returns
    -------
    str
        The configured issuer URL.

    Raises
    ------
    ValidationError
        If no issuer is configured, or `value` is not one of them.
    """
    if not issuers:
        raise ValidationError(
            "CI OIDC is off on this server: set LAKANA_CI_OIDC__ISSUERS (and "
            "LAKANA_CI_OIDC__AUDIENCE) and restart it, then create the rule."
        )
    resolved = PRESETS.get(value.lower(), value)
    if resolved not in issuers:
        raise ValidationError(
            f"The issuer must be one of LAKANA_CI_OIDC__ISSUERS ({', '.join(issuers)}), or "
            "github or gitlab when that one is configured; ask the operator to add it."
        )
    return resolved


def _validate_kinds(kinds: Sequence[str]) -> list[str]:
    """Check the kinds a rule allows.

    Parameters
    ----------
    kinds
        As given.

    Returns
    -------
    list of str
        Sorted, without repeats.

    Raises
    ------
    ValidationError
        If none is given, or one is not ``deploy`` or ``dry_run``.
    """
    if not kinds:
        raise ValidationError("A rule allows at least one kind: deploy, dry_run or both.")
    for kind in kinds:
        if kind not in ALLOWED_KINDS:
            shown = kind if isinstance(kind, str) else type(kind).__name__
            raise ValidationError(
                f"A rule allows only deploy and dry_run, not {shown[:40]!r}: a rollback or an "
                "undeploy through CI is never allowed; a person does it with their own token."
            )
    return sorted(set(kinds))


def _validate_conditions(issuer: str, conditions: Mapping[str, object]) -> dict[str, str]:
    """Check a rule's claim conditions.

    Parameters
    ----------
    issuer
        The rule's issuer URL, which decides the numeric id claim.
    conditions
        Claim name to required text, as given.

    Returns
    -------
    dict of str to str
        The conditions.

    Raises
    ------
    ValidationError
        If a claim name or value is malformed or reserved, there are more than
        `MAX_CONDITIONS`, the numeric id claim is missing or not digits, or neither ``ref`` nor
        ``environment`` is pinned, or no claim of `trigger_claims` is.
    """
    if not isinstance(conditions, Mapping):
        raise ValidationError("The conditions must be an object of claim names to texts.")
    if len(conditions) > MAX_CONDITIONS:
        raise ValidationError(f"A rule has at most {MAX_CONDITIONS} claim conditions.")
    for name, value in conditions.items():
        if not isinstance(name, str) or not _CLAIM_NAME.match(name):
            raise ValidationError(
                "A claim name is lower-case letters, digits and underscores, starting with a "
                "letter, at most 64 characters, as the CI provider writes it (for example ref)."
            )
        if name in RESERVED_CLAIMS:
            raise ValidationError(
                f"The claim {name} is checked by the server for every ID token, or differs for "
                "each one; it cannot be a condition."
            )
        if not isinstance(value, str):
            raise ValidationError(
                f"The condition on {name} must be a text, exactly as the ID token carries it "
                '(GitHub and GitLab write ids as texts too, such as "123456").'
            )
        if not value:
            raise ValidationError(f"The condition on {name} is empty.")
        if len(value) > MAX_VALUE_LENGTH:
            raise ValidationError(f"The condition on {name} is longer than 255 characters.")
        if _CONTROL.search(value):
            raise ValidationError(f"The condition on {name} contains a control character.")
    numeric = id_claim(issuer)
    value = conditions.get(numeric)
    if value is None:
        raise ValidationError(
            f"A rule must pin the numeric id of the repository with a {numeric} condition (the "
            "repository's own number, which a rename or a transfer cannot take over)."
        )
    if not isinstance(value, str) or not _DIGITS.match(value):
        raise ValidationError(f"The {numeric} condition must be the id's digits only.")
    if not any(name in conditions for name in _REF_CONDITIONS):
        raise ValidationError(
            "A rule must also pin a ref or environment condition (for example "
            "ref=refs/heads/main on GitHub, ref=main on GitLab, or environment=prod), so that "
            "not every branch of the repository may deploy."
        )
    if not any(name in conditions for name in trigger_claims(issuer)):
        raise ValidationError(_trigger_message(issuer))
    return dict(conditions)  # ty: ignore[invalid-return-type]


def rule_problem(issuer: str, conditions: Mapping[str, object]) -> str | None:
    """Say why a stored rule's conditions would not be accepted today, if they would not.

    Parameters
    ----------
    issuer
        The rule's issuer.
    conditions
        Its conditions.

    Returns
    -------
    str or None
        The refusal `create` would give; `None` for a rule that is allowed.
    """
    try:
        _validate_conditions(issuer, conditions)
    except ValidationError as exc:
        return str(exc)
    return None


@dataclass(frozen=True)
class CiTrustRuleView:
    """A rule as the API shows it, with names for its ids.

    Parameters
    ----------
    id
        The rule's id.
    issuer
        The issuer URL.
    principal
        The service account's name.
    environment
        The environment's name.
    repository
        The repository's name.
    kinds
        ``deploy`` and/or ``dry_run``.
    conditions
        Claim name to required text.
    enabled
        Whether it accepts ID tokens.
    start_on_deploy_jobs
        Whether a deploy accepted under it starts the project's jobs with ``on_deploy: true``.
    created_by
        Who created it; `None` once that principal is deleted.
    created_at
        When.
    updated_by
        Who changed it last; `None` until it changes.
    updated_at
        When it last changed.
    """

    id: uuid.UUID
    issuer: str
    principal: str
    environment: str
    repository: str
    kinds: list[str]
    conditions: dict[str, str]
    enabled: bool
    created_by: str | None
    created_at: datetime
    updated_by: str | None
    updated_at: datetime
    start_on_deploy_jobs: bool = False


@dataclass(frozen=True)
class RuleExplanation:
    """Why one rule does or does not accept a set of claims.

    Parameters
    ----------
    rule_id
        The rule.
    matches
        Whether it accepts them.
    reasons
        Why not, one sentence each; empty when it matches.
    """

    rule_id: uuid.UUID
    matches: bool
    reasons: list[str]


@dataclass(frozen=True)
class ExplainReport:
    """What an exchange with these claims would do.

    Parameters
    ----------
    issuer
        The issuer the claims were compared for.
    environment
        The environment asked for.
    repository
        The repository asked for.
    verdict
        One sentence: which rule accepts the claims, or why the exchange is refused.
    matching
        The ids of the rules that match.
    rules
        Every rule, with its reasons.
    """

    issuer: str
    environment: str
    repository: str
    verdict: str
    matching: list[uuid.UUID]
    rules: list[RuleExplanation]


def describe_mismatch(name: str, problem: str, claims: Mapping[str, object], wanted: str) -> str:
    """Write one mismatch for an administrator who pasted the claims.

    Parameters
    ----------
    name
        The claim.
    problem
        ``missing``, ``not_text`` or ``differs``.
    claims
        The pasted claims.
    wanted
        The rule's text.

    Returns
    -------
    str
        The sentence.
    """
    if problem == "missing":
        return f"claim {name} is missing; the rule needs {wanted!r}"
    if problem == "not_text":
        return f"claim {name} is not a text; the rule needs the text {wanted!r}"
    actual = str(claims[name])[:MAX_VALUE_LENGTH]
    return f"claim {name} is {actual!r}; the rule needs {wanted!r}"


def valid_sha(value: object) -> str | None:
    """Return a signed ``sha`` claim when it is a full lower-case commit id.

    Parameters
    ----------
    value
        The claim, or `None`.

    Returns
    -------
    str or None
        The id, or `None` when the claim is missing or not 40 lower-case hexadecimal digits.
    """
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    return None


class CiTrustRuleService:
    """Create, change, list and explain CI trust rules; instance administrators only.

    Parameters
    ----------
    session
        Database session; mutating methods commit.
    authz
        Authorization service.
    issuers
        `LAKANA_CI_OIDC__ISSUERS`, resolved; a rule's issuer must be one of them.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, *, issuers: Sequence[str]
    ) -> None:
        self._session = session
        self._authz = authz
        self._issuers = list(issuers)

    def _service_account(self, name: str) -> Principal:
        """Look up the active service account a rule acts as.

        Parameters
        ----------
        name
            Its name.

        Returns
        -------
        Principal
            The row.

        Raises
        ------
        NotFoundError
            If no principal has the name.
        ValidationError
            If it is a user, or not active.
        """
        row = self._session.execute(
            select(Principal).where(Principal.name == name)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"No principal named {name!r}.")
        if row.type is not PrincipalType.SERVICE_ACCOUNT:
            raise ValidationError(
                f"A CI trust rule acts as a service account; {name!r} is a user. Create one "
                "with lakana principal create NAME --type service-account and grant it DEPLOY "
                "on the environment."
            )
        if not row.is_active:
            raise ValidationError(f"The principal {name!r} is not active.")
        return row

    def _rule(self, rule_id: uuid.UUID) -> CiTrustRule:
        """Look a rule up by id.

        Parameters
        ----------
        rule_id
            The id.

        Returns
        -------
        CiTrustRule
            The row.

        Raises
        ------
        NotFoundError
            If there is none.
        """
        row = self._session.get(CiTrustRule, rule_id)
        if row is None:
            raise NotFoundError(f"No CI trust rule {rule_id}.")
        return row

    def _name(self, model: type[Principal | Environment | Repository], row_id: object) -> str:
        """Read the name of a principal, environment or repository by id.

        Parameters
        ----------
        model
            The model.
        row_id
            The id, or `None`.

        Returns
        -------
        str
            The name; empty when the row is gone.
        """
        if row_id is None:
            return ""
        row = self._session.get(model, row_id)
        return "" if row is None else row.name

    def _view(self, row: CiTrustRule) -> CiTrustRuleView:
        """Build the API view of a rule.

        Parameters
        ----------
        row
            The rule.

        Returns
        -------
        CiTrustRuleView
            With names.
        """
        return CiTrustRuleView(
            id=row.id,
            issuer=row.issuer,
            principal=self._name(Principal, row.principal_id),
            environment=self._name(Environment, row.environment_id),
            repository=self._name(Repository, row.repository_id),
            kinds=list(row.kinds),
            conditions=dict(row.conditions),
            enabled=row.enabled,
            created_by=self._name(Principal, row.created_by) or None,
            created_at=row.created_at,
            updated_by=self._name(Principal, row.updated_by) or None,
            updated_at=row.updated_at,
            start_on_deploy_jobs=row.start_on_deploy_jobs,
        )

    def _audit(
        self, p: PrincipalRef, kind: AuditEventType, view: CiTrustRuleView, **extra: object
    ) -> None:
        """Record a rule's change, in the caller's transaction.

        Parameters
        ----------
        p
            The administrator.
        kind
            The event type.
        view
            The rule after the change (before it, for a deletion).
        **extra
            Further fields.
        """
        AuditService(self._session).record(
            p.id,
            kind,
            securable=Securable.environment_scope(view.environment),
            detail={
                "rule_id": str(view.id),
                "issuer": view.issuer,
                "principal": view.principal,
                "environment": view.environment,
                "repository": view.repository,
                "kinds": view.kinds,
                "conditions": view.conditions,
                "start_on_deploy_jobs": view.start_on_deploy_jobs,
                **extra,
            },
        )

    def create(
        self,
        p: PrincipalRef,
        *,
        issuer: str,
        principal: str,
        environment: str,
        repository: str,
        kinds: Sequence[str],
        conditions: Mapping[str, str],
        start_on_deploy_jobs: bool = False,
    ) -> CiTrustRuleView:
        """Create an enabled rule.

        Parameters
        ----------
        p
            The acting administrator.
        issuer
            ``github``, ``gitlab`` or a configured issuer URL.
        principal
            The active service account the CI job acts as.
        environment
            The environment it may deploy to.
        repository
            The registered repository it may deploy.
        kinds
            ``deploy`` and/or ``dry_run``.
        conditions
            Claim name to the exact text the claim must have.
        start_on_deploy_jobs
            Let a deploy accepted under the rule start the project's jobs with
            ``on_deploy: true`` in the environment, as the CI identity; off by default.

        Returns
        -------
        CiTrustRuleView
            The rule.

        Raises
        ------
        PermissionDenied
            Without the `ADMIN` role on the instance (checked first).
        NotFoundError
            If the principal, environment or repository does not exist.
        ValidationError
            If a field breaks the rules of this module's docstring.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        resolved = _resolve_issuer(issuer, self._issuers)
        allowed = _validate_kinds(kinds)
        pinned = _validate_conditions(resolved, conditions)
        account = self._service_account(principal)
        env = environment_by_name(self._session, environment)
        repo = repository_by_name(self._session, repository)
        now = utcnow()
        row = CiTrustRule(
            id=uuid.uuid4(),
            issuer=resolved,
            principal_id=account.id,
            environment_id=env.id,
            repository_id=repo.id,
            kinds=allowed,
            conditions=pinned,
            enabled=True,
            start_on_deploy_jobs=start_on_deploy_jobs,
            revision=1,
            created_by=p.id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        view = self._view(row)
        self._audit(p, AuditEventType.CI_TRUST_RULE_CREATED, view)
        self._session.commit()
        return view

    def list(self, p: PrincipalRef) -> builtins.list[CiTrustRuleView]:
        """List every rule, oldest first.

        Parameters
        ----------
        p
            The acting administrator.

        Returns
        -------
        list of CiTrustRuleView
            The rules.

        Raises
        ------
        PermissionDenied
            Without the `ADMIN` role on the instance.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        rows = self._session.execute(
            select(CiTrustRule).order_by(CiTrustRule.created_at, CiTrustRule.id)
        ).scalars()
        return [self._view(row) for row in rows]

    def get(self, p: PrincipalRef, rule_id: uuid.UUID) -> CiTrustRuleView:
        """Show one rule.

        Parameters
        ----------
        p
            The acting administrator.
        rule_id
            The rule to show.

        Returns
        -------
        CiTrustRuleView
            The rule.

        Raises
        ------
        PermissionDenied
            Without the `ADMIN` role on the instance (checked first).
        NotFoundError
            If there is no such rule.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        return self._view(self._rule(rule_id))

    def update(
        self,
        p: PrincipalRef,
        rule_id: uuid.UUID,
        *,
        principal: str | None = None,
        kinds: Sequence[str] | None = None,
        conditions: Mapping[str, str] | None = None,
        start_on_deploy_jobs: bool | None = None,
    ) -> CiTrustRuleView:
        """Change a rule's principal, kinds, conditions or opt-in; every token issued under it ends.

        The issuer, environment and repository never change: create another rule instead.

        Parameters
        ----------
        p
            The acting administrator.
        rule_id
            The rule.
        principal
            A new service account.
        kinds
            New kinds.
        conditions
            New conditions, replacing all of the old ones.
        start_on_deploy_jobs
            Whether a deploy under the rule starts the project's ``on_deploy`` jobs.

        Returns
        -------
        CiTrustRuleView
            The rule.

        Raises
        ------
        PermissionDenied
            Without the `ADMIN` role on the instance (checked first).
        NotFoundError
            If there is no such rule or principal.
        ValidationError
            If nothing is given, or a field breaks the rules of `create`.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        row = self._rule(rule_id)
        if (
            principal is None
            and kinds is None
            and conditions is None
            and start_on_deploy_jobs is None
        ):
            raise ValidationError(
                "Nothing to change: give a principal, kinds, conditions or start_on_deploy_jobs."
            )
        if start_on_deploy_jobs is not None:
            row.start_on_deploy_jobs = start_on_deploy_jobs
        if kinds is not None:
            row.kinds = _validate_kinds(kinds)
        row.conditions = _validate_conditions(
            row.issuer, row.conditions if conditions is None else conditions
        )
        if principal is not None:
            row.principal_id = self._service_account(principal).id
        row.revision += 1
        row.updated_by = p.id
        row.updated_at = utcnow()
        self._session.flush()
        view = self._view(row)
        self._audit(p, AuditEventType.CI_TRUST_RULE_UPDATED, view)
        self._session.commit()
        return view

    def _set_enabled(self, p: PrincipalRef, rule_id: uuid.UUID, enabled: bool) -> CiTrustRuleView:
        """Enable or disable a rule; doing it again changes nothing and records nothing.

        Parameters
        ----------
        p
            The acting administrator, already authorized.
        rule_id
            The rule.
        enabled
            The new state.

        Returns
        -------
        CiTrustRuleView
            The rule.

        Raises
        ------
        NotFoundError
            If there is no such rule.
        """
        row = self._rule(rule_id)
        if enabled:
            _validate_conditions(row.issuer, row.conditions)
        if row.enabled is not enabled:
            row.enabled = enabled
            row.revision += 1
            row.updated_by = p.id
            row.updated_at = utcnow()
            self._session.flush()
            kind = (
                AuditEventType.CI_TRUST_RULE_ENABLED
                if enabled
                else AuditEventType.CI_TRUST_RULE_DISABLED
            )
            self._audit(p, kind, self._view(row))
            self._session.commit()
        return self._view(row)

    def enable(self, p: PrincipalRef, rule_id: uuid.UUID) -> CiTrustRuleView:
        """Enable a rule. Tokens issued before it was disabled stay dead.

        Parameters
        ----------
        p
            The acting administrator.
        rule_id
            The rule to enable.

        Returns
        -------
        CiTrustRuleView
            The rule.

        Raises
        ------
        PermissionDenied
            Without the `ADMIN` role on the instance (checked first).
        NotFoundError
            If there is no such rule.
        ValidationError
            If its conditions do not meet the requirements of `create` (a rule written by an
            earlier build).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        return self._set_enabled(p, rule_id, True)

    def disable(self, p: PrincipalRef, rule_id: uuid.UUID) -> CiTrustRuleView:
        """Disable a rule: no exchange matches it, and every token issued under it ends.

        Parameters
        ----------
        p
            The acting administrator.
        rule_id
            The rule to disable.

        Returns
        -------
        CiTrustRuleView
            The rule.

        Raises
        ------
        PermissionDenied
            Without the `ADMIN` role on the instance (checked first).
        NotFoundError
            If there is no such rule.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        return self._set_enabled(p, rule_id, False)

    def delete(self, p: PrincipalRef, rule_id: uuid.UUID) -> None:
        """Delete a rule; every token issued under it ends.

        Parameters
        ----------
        p
            The acting administrator.
        rule_id
            The rule to delete.

        Raises
        ------
        PermissionDenied
            Without the `ADMIN` role on the instance (checked first).
        NotFoundError
            If there is no such rule.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        row = self._rule(rule_id)
        view = self._view(row)
        self._session.delete(row)
        self._audit(p, AuditEventType.CI_TRUST_RULE_DELETED, view)
        self._session.commit()

    def explain(
        self,
        p: PrincipalRef,
        *,
        claims: Mapping[str, object],
        environment: str,
        repository: str,
        issuer: str | None = None,
    ) -> ExplainReport:
        """Say which rule an exchange with these claims would match, and why not the others.

        Needs no ID token: the claims are compared as given, nothing is verified, nothing is
        recorded, and no token is issued.

        Parameters
        ----------
        p
            The acting administrator.
        claims
            The ID token's claims, as decoded (for example from a CI job's debug output).
        environment
            The environment the exchange would ask for.
        repository
            The repository it would ask for.
        issuer
            The issuer; the ``iss`` claim when `None`.

        Returns
        -------
        ExplainReport
            The verdict and every rule's reasons.

        Raises
        ------
        PermissionDenied
            Without the `ADMIN` role on the instance (checked first).
        ValidationError
            If no issuer is given and the claims have no ``iss`` text.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        if issuer is None:
            claimed = claims.get("iss")
            if not isinstance(claimed, str):
                raise ValidationError("Give the issuer, or claims with an iss claim.")
            issuer = claimed
        issuer = PRESETS.get(issuer.lower(), issuer)
        results: builtins.list[RuleExplanation] = []
        matching: builtins.list[CiTrustRule] = []
        rows = self._session.execute(
            select(CiTrustRule).order_by(CiTrustRule.created_at, CiTrustRule.id)
        ).scalars()
        for row in rows:
            view = self._view(row)
            reasons: builtins.list[str] = []
            if row.issuer != issuer:
                reasons.append(f"the rule is for issuer {row.issuer!r}, not {issuer!r}")
            if view.environment != environment or not self._live(row.environment_id):
                reasons.append(
                    f"the rule is for environment {view.environment!r}, not {environment!r}"
                )
            if view.repository != repository:
                reasons.append(
                    f"the rule is for repository {view.repository!r}, not {repository!r}"
                )
            if not row.enabled:
                reasons.append("the rule is disabled")
            problem = rule_problem(row.issuer, row.conditions)
            if problem is not None:
                reasons.append(f"the rule is not allowed and never matches: {problem}")
            reasons.extend(
                describe_mismatch(name, problem, claims, row.conditions[name])
                for name, problem in condition_mismatches(row.conditions, claims)
            )
            if not reasons:
                matching.append(row)
            results.append(RuleExplanation(rule_id=row.id, matches=not reasons, reasons=reasons))
        return ExplainReport(
            issuer=issuer,
            environment=environment,
            repository=repository,
            verdict=self._verdict(matching, claims),
            matching=[row.id for row in matching],
            rules=results,
        )

    def _live(self, environment_id: uuid.UUID) -> bool:
        """Say whether an environment exists and is not deleted.

        Parameters
        ----------
        environment_id
            The environment.

        Returns
        -------
        bool
            Whether an exchange could name it.
        """
        return (
            self._session.execute(
                select(Environment.id).where(Environment.id == environment_id, LIVE_ENVIRONMENT)
            ).first()
            is not None
        )

    def _verdict(self, matching: Sequence[CiTrustRule], claims: Mapping[str, object]) -> str:
        """Say what the exchange would do, as the exchange decides it.

        Parameters
        ----------
        matching
            The rules that match.
        claims
            The claims.

        Returns
        -------
        str
            One sentence.
        """
        if not matching:
            return "refused: no enabled rule for this issuer, environment and repository matches"
        if len(matching) > 1:
            return (
                f"refused: {len(matching)} rules match, which is ambiguous; disable or narrow all "
                "but one"
            )
        (rule,) = matching
        account = self._session.get(Principal, rule.principal_id)
        if (
            account is None
            or not account.is_active
            or account.type is not PrincipalType.SERVICE_ACCOUNT
        ):
            name = "" if account is None else account.name
            return f"refused: the rule's principal {name!r} is not active"
        if "deploy" in rule.kinds and valid_sha(claims.get("sha")) is None:
            return (
                "refused: the ID token has no sha claim of a full commit id, which a rule that "
                "allows deploy needs"
            )
        return f"accepted by rule {rule.id}"
