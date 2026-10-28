"""Exchange a CI ID token for a Lakana CI token, and authenticate with that token.

The exchange (`CiTokenService.exchange`), in this order:

1. The ID token is verified (`IdTokenVerifier`); a refusal keeps its `RefusalCode`.
2. Its ``jti`` is recorded once, by the primary key of `CiOidcJti`, and committed on its own:
   the ID token is used up whatever happens next, so one ID token can probe one environment
   and repository at most. Records whose ID token expired more than the clock skew ago are
   swept in the same statement's transaction.
3. The enabled rules for the token's issuer, the environment and the repository asked for are
   compared with the claims (`condition_mismatches`); none, or more than one, is a refusal.
4. The rule's principal must still be an active service account, and a rule that allows
   ``deploy`` needs a signed ``sha`` claim of a full commit id.
5. A `CiToken` is issued (``lakana_ci_<key id>_<secret>``, stored as a hash), bound to the
   rule and its revision, the principal, the environment, the repository, the kinds and the
   ``sha``, and `OIDC_TOKEN_EXCHANGED` is recorded in the same transaction.

**What a refused caller learns.** A refusal of the ID token itself (a `RefusalCode`) and a
replay are answered with their own messages: they describe the token the caller holds. Every
refusal after that (no rule, two rules, a disabled rule, an unknown environment or repository,
an inactive principal, a missing commit) is answered with one text, so the answer never tells
whether a rule, an environment or a repository exists; the reason is in the audit row, which
the server records (rate-limited) as `OIDC_EXCHANGE_REFUSED`.

Authentication (`CiTokenService.verify`, `resolve_ci_scope`) re-checks at every request that the
token has not expired, its rule still exists, is enabled and has the same revision, and its
principal is an active service account; the result is a `PrincipalRef` with a `CiScope`.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import CiScope, PrincipalRef, Securable
from lakana_core.ci_oidc.errors import IdTokenRefused
from lakana_core.ci_oidc.rules import condition_mismatches, rule_problem, valid_sha
from lakana_core.ci_oidc.verification import (
    CLOCK_SKEW_SECONDS,
    MAX_TOKEN_BYTES,
    IdTokenVerifier,
)
from lakana_core.errors import AuthenticationError, ConflictError, PermissionDenied, ValidationError
from lakana_core.identity.tokens import generate_token, verify_secret
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    CiOidcJti,
    CiToken,
    CiTrustRule,
    DeployRun,
    Environment,
    Principal,
    PrincipalType,
    Repository,
)
from lakana_core.models.base import utcnow

TOKEN_PREFIX = "lakana_ci"  # nosec B105 -- the wire prefix of a credential, not a credential
"""The wire prefix of a CI token: ``lakana_ci_<key id>_<secret>``."""

DEFAULT_TTL_SECONDS = 3600
_MAX_NAME = 255

REPLAYED_MESSAGE = (
    "This ID token was exchanged before; each ID token works once. Request a new one in the CI "
    "job for each exchange."
)
EXPIRED_MESSAGE = "This CI token has expired; exchange a new ID token in the CI job."
INVALID_MESSAGE = (
    "This CI token is no longer valid: its CI trust rule was changed, disabled or deleted, or "
    "its principal was deactivated. Exchange a new ID token in the CI job."
)
RUN_TOKEN_INVALID_MESSAGE = (  # nosec B105 -- a message, not a credential
    "The CI token that queued this deploy is no longer valid: its CI trust rule was changed, "
    "disabled or deleted, or its principal was deactivated. Queue it again from the CI job."
)


class ExchangeRefusal(enum.StrEnum):
    """Why an exchange was refused after the ID token verified; the values are stable."""

    REPLAYED = "replayed"
    NO_RULE = "no_matching_rule"
    AMBIGUOUS = "ambiguous_rules"
    PRINCIPAL_INACTIVE = "principal_inactive"
    COMMIT_MISSING = "commit_missing"


class CiExchangeRefused(Exception):
    """An exchange was refused.

    Parameters
    ----------
    code
        A `RefusalCode` or an `ExchangeRefusal` value, for the audit row.
    message
        What the caller is told.
    authenticated
        `False` when the ID token itself is not accepted (refused or replayed; an HTTP 401),
        `True` when it verified and no rule accepts it (an HTTP 403).
    issuer
        The issuer, when it is a configured one; never a text the caller chose.
    detail
        Further fields for the audit row.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        authenticated: bool,
        issuer: str | None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.authenticated = authenticated
        self.issuer = issuer
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class IssuedCiToken:
    """A freshly issued CI token, returned once.

    Parameters
    ----------
    token
        The full ``lakana_ci_…`` value; never retrievable again.
    key_id
        Its public key id.
    expires_at
        When it stops working.
    principal
        The service account it acts as.
    environment
        The one environment.
    repository
        The one repository.
    kinds
        ``deploy`` and/or ``dry_run``.
    sha
        The signed commit a deploy is pinned to, or `None`.
    rule_id
        The rule that accepted the ID token.
    """

    token: str
    key_id: str
    expires_at: datetime
    principal: str
    environment: str
    repository: str
    kinds: list[str] = field(default_factory=list)
    sha: str | None = None
    rule_id: uuid.UUID | None = None


def jti_digest(issuer: str, jti: str) -> str:
    """Key an ID token's id by its issuer.

    Parameters
    ----------
    issuer
        The configured issuer URL (it never contains a line break).
    jti
        The ``jti`` claim.

    Returns
    -------
    str
        The SHA-256 of ``issuer``, a line break and ``jti``, in hexadecimal.
    """
    return hashlib.sha256(f"{issuer}\n{jti}".encode()).hexdigest()


def _claimed_issuer(token: object, issuers: frozenset[str]) -> str | None:
    """Read the unverified ``iss`` of a refused token, when it is a configured issuer.

    For the audit row of a refusal only: an issuer that is not configured is a text the caller
    chose, and is never recorded.

    Parameters
    ----------
    token
        The token as sent.
    issuers
        The configured issuers.

    Returns
    -------
    str or None
        The issuer, or `None`.
    """
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES or token.count(".") != 2:
        return None
    body = token.split(".")[1]
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, binascii.Error, RecursionError):
        return None
    issuer = payload.get("iss") if isinstance(payload, dict) else None
    return issuer if isinstance(issuer, str) and issuer in issuers else None


def _check_name(value: object, what: str) -> str:
    """Check an environment or repository name of an exchange request.

    Parameters
    ----------
    value
        The name.
    what
        ``environment`` or ``repository``.

    Returns
    -------
    str
        The name.

    Raises
    ------
    ValidationError
        If it is not a text of 1 to 255 characters.
    """
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_NAME:
        raise ValidationError(f"The {what} must be a name of 1 to 255 characters.")
    return value


def _live_environment(session: Session, environment_id: uuid.UUID) -> Environment | None:
    """Read an environment by id, unless it is deleted.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The id.

    Returns
    -------
    Environment or None
        The row.
    """
    return session.execute(
        select(Environment).where(Environment.id == environment_id, LIVE_ENVIRONMENT)
    ).scalar_one_or_none()


def _active_service_account(session: Session, principal_id: uuid.UUID) -> Principal | None:
    """Read a principal, fresh from the database, when it is an active service account.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The id.

    Returns
    -------
    Principal or None
        The row.
    """
    row = session.execute(
        select(Principal)
        .where(Principal.id == principal_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None or not row.is_active or row.type is not PrincipalType.SERVICE_ACCOUNT:
        return None
    return row


def resolve_ci_scope(
    session: Session, token: CiToken, *, now: datetime, require_unexpired: bool
) -> CiScope | None:
    """Decide whether a CI token is still valid, and what it is bound to.

    Read fresh from the database every time, so a revocation takes effect at the next request.

    Parameters
    ----------
    session
        Database session.
    token
        The token's row.
    now
        The time of the check.
    require_unexpired
        Whether an expired token is invalid: at authentication it is; for the worker's re-check
        of a run the token already queued, it is not (the queue may outlast the token).

    Returns
    -------
    CiScope or None
        The scope; `None` when the token has expired (if that counts), its rule is gone,
        disabled, changed since the token was issued or bound to another principal,
        environment or repository, its principal is not an active service account, or its
        environment is deleted.
    """
    if require_unexpired and token.expires_at <= now:
        return None
    if token.rule_id is None:
        return None
    rule = session.execute(
        select(CiTrustRule)
        .where(CiTrustRule.id == token.rule_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        rule is None
        or not rule.enabled
        or rule.revision != token.rule_revision
        or rule.principal_id != token.principal_id
        or rule.environment_id != token.environment_id
        or rule.repository_id != token.repository_id
    ):
        return None
    if _active_service_account(session, token.principal_id) is None:
        return None
    environment = _live_environment(session, token.environment_id)
    repository = session.get(Repository, token.repository_id)
    if environment is None or repository is None:
        return None
    return CiScope(
        token_id=token.id,
        rule_id=rule.id,
        environment=environment.name,
        environment_id=environment.id,
        repository=repository.name,
        repository_id=repository.id,
        kinds=frozenset(token.kinds) & frozenset(rule.kinds),
        sha=token.sha,
        used=token.used_at is not None,
        expires_at=token.expires_at,
        start_on_deploy_jobs=rule.start_on_deploy_jobs,
    )


def ci_scope_for_run(session: Session, run: DeployRun) -> CiScope | None:
    """Resolve the CI token that queued a deploy run, for the worker's re-check.

    Parameters
    ----------
    session
        Database session.
    run
        The run.

    Returns
    -------
    CiScope or None
        `None` for a run no CI token queued; otherwise the token's current scope.

    Raises
    ------
    PermissionDenied
        If the run names a CI token that is missing, no longer valid (an expiry since the run
        was queued does not count) or bound to another principal than the run's.
    """
    if run.ci_token_id is None:
        return None
    token = session.execute(
        select(CiToken)
        .where(CiToken.id == run.ci_token_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    scope = (
        None
        if token is None or token.principal_id != run.principal_id
        else resolve_ci_scope(session, token, now=utcnow(), require_unexpired=False)
    )
    if scope is None:
        raise PermissionDenied(RUN_TOKEN_INVALID_MESSAGE)
    return scope


class CiTokenService:
    """Issue CI tokens for verified ID tokens, and verify CI tokens.

    Neither method is authorized: the exchange is how a CI job without a credential gets one,
    and verification is what produces an authenticated principal.

    Parameters
    ----------
    session
        Database session; both methods commit.
    ttl_seconds
        How long an issued token lives (`LAKANA_CI_OIDC__TOKEN_TTL_SECONDS`).
    """

    def __init__(self, session: Session, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._session = session
        self._ttl = timedelta(seconds=ttl_seconds)

    def _refused(
        self,
        code: ExchangeRefusal,
        issuer: str,
        environment: str,
        repository: str,
        **detail: object,
    ) -> CiExchangeRefused:
        """Build the one refusal of an ID token that verified but that no rule accepts.

        Parameters
        ----------
        code
            Why, for the audit row only.
        issuer
            The verified issuer.
        environment
            The environment asked for.
        repository
            The repository asked for.
        **detail
            Further fields for the audit row.

        Returns
        -------
        CiExchangeRefused
            The refusal, with the same message whatever the code.
        """
        return CiExchangeRefused(
            code.value,
            f"No enabled CI trust rule accepts this ID token for environment {environment!r} "
            f"and repository {repository!r}. An administrator finds the reason in the audit log "
            "(lakana audit list --event-type OIDC_EXCHANGE_REFUSED) and can test the token's "
            "claims with lakana ci-trust explain.",
            authenticated=True,
            issuer=issuer,
            detail={"environment": environment, "repository": repository, **detail},
        )

    def _use_jti(self, issuer: str, jti: str, expires_at: datetime, now: datetime) -> str:
        """Record an ID token's id once, committed on its own, sweeping the expired ones.

        Parameters
        ----------
        issuer
            The verified issuer.
        jti
            The verified ``jti``.
        expires_at
            When the ID token expires.
        now
            The time of the exchange.

        Returns
        -------
        str
            The id's digest (`jti_digest`).

        Raises
        ------
        CiExchangeRefused
            `REPLAYED`, if the id was recorded before.
        """
        digest = jti_digest(issuer, jti)
        self._session.execute(
            delete(CiOidcJti).where(
                CiOidcJti.expires_at < now - timedelta(seconds=CLOCK_SKEW_SECONDS)
            )
        )
        self._session.add(CiOidcJti(jti_sha256=digest, expires_at=expires_at))
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            raise CiExchangeRefused(
                ExchangeRefusal.REPLAYED.value,
                REPLAYED_MESSAGE,
                authenticated=False,
                issuer=issuer,
                detail={"jti_sha256": digest},
            ) from None
        return digest

    def exchange(
        self,
        verifier: IdTokenVerifier,
        id_token: str,
        *,
        environment: str,
        repository: str,
        now: datetime | None = None,
    ) -> IssuedCiToken:
        """Exchange a CI ID token for a CI token, in the order of this module's docstring.

        Parameters
        ----------
        verifier
            The server's ID token verifier.
        id_token
            The ID token as the CI provider issued it.
        environment
            The environment the CI job deploys to.
        repository
            The registered repository it deploys.
        now
            The time of the exchange; the clock when `None`.

        Returns
        -------
        IssuedCiToken
            The token, shown once.

        Raises
        ------
        ValidationError
            If `environment` or `repository` is not a name of 1 to 255 characters.
        CiExchangeRefused
            With the `RefusalCode` of the verifier, `REPLAYED`, or one of the other
            `ExchangeRefusal` codes behind one message.
        ConflictError
            If a freshly generated key id collides twice in a row.
        """
        now = now or utcnow()
        _check_name(environment, "environment")
        _check_name(repository, "repository")
        try:
            verified = verifier.verify_id_token(id_token, now=now)
        except IdTokenRefused as exc:
            raise CiExchangeRefused(
                exc.code.value,
                exc.message,
                authenticated=False,
                issuer=_claimed_issuer(id_token, verifier.issuers),
            ) from None
        issuer, claims = verified.issuer, verified.claims
        digest = self._use_jti(issuer, verified.jti, verified.expires_at, now)
        env = self._session.execute(
            select(Environment).where(Environment.name == environment, LIVE_ENVIRONMENT)
        ).scalar_one_or_none()
        repo = self._session.execute(
            select(Repository).where(Repository.name == repository)
        ).scalar_one_or_none()
        refusal = {"issuer": issuer, "environment": environment, "repository": repository}
        if env is None or repo is None:
            raise self._refused(ExchangeRefusal.NO_RULE, **refusal, jti_sha256=digest)
        candidates = self._session.execute(
            select(CiTrustRule)
            .where(
                CiTrustRule.issuer == issuer,
                CiTrustRule.environment_id == env.id,
                CiTrustRule.repository_id == repo.id,
                CiTrustRule.enabled.is_(True),
            )
            .order_by(CiTrustRule.created_at, CiTrustRule.id)
        ).scalars()
        # A rule the current requirements refuse (one written by an earlier build) never matches.
        matching = [
            rule
            for rule in candidates
            if rule_problem(rule.issuer, rule.conditions) is None
            and not condition_mismatches(rule.conditions, claims)
        ]
        if not matching:
            raise self._refused(ExchangeRefusal.NO_RULE, **refusal, jti_sha256=digest)
        if len(matching) > 1:
            raise self._refused(
                ExchangeRefusal.AMBIGUOUS,
                **refusal,
                jti_sha256=digest,
                rule_ids=sorted(str(rule.id) for rule in matching),
            )
        (rule,) = matching
        account = _active_service_account(self._session, rule.principal_id)
        if account is None:
            raise self._refused(
                ExchangeRefusal.PRINCIPAL_INACTIVE,
                **refusal,
                jti_sha256=digest,
                rule_id=str(rule.id),
            )
        sha = valid_sha(claims.get("sha"))
        if "deploy" in rule.kinds and sha is None:
            raise self._refused(
                ExchangeRefusal.COMMIT_MISSING, **refusal, jti_sha256=digest, rule_id=str(rule.id)
            )
        return self._issue(rule, account, env, repo, sha, digest, now)

    def _issue(
        self,
        rule: CiTrustRule,
        account: Principal,
        env: Environment,
        repo: Repository,
        sha: str | None,
        digest: str,
        now: datetime,
    ) -> IssuedCiToken:
        """Store a new CI token and its audit row in one transaction.

        Parameters
        ----------
        rule
            The rule that accepted the ID token.
        account
            Its service account.
        env
            The environment.
        repo
            The repository.
        sha
            The signed commit, or `None`.
        digest
            The ID token's id digest.
        now
            The time of the exchange.

        Returns
        -------
        IssuedCiToken
            The token.

        Raises
        ------
        ConflictError
            If a generated key id collides twice in a row.
        """
        expires_at = now + self._ttl
        kinds = sorted(rule.kinds)
        retried = False
        while True:
            generated = generate_token(TOKEN_PREFIX)
            self._session.add(
                CiToken(
                    id=uuid.uuid4(),
                    key_id=generated.key_id,
                    secret_hash=generated.secret_hash,
                    rule_id=rule.id,
                    rule_revision=rule.revision,
                    principal_id=account.id,
                    environment_id=env.id,
                    repository_id=repo.id,
                    kinds=kinds,
                    sha=sha,
                    jti_sha256=digest,
                    issued_at=now,
                    expires_at=expires_at,
                )
            )
            AuditService(self._session).record(
                account.id,
                AuditEventType.OIDC_TOKEN_EXCHANGED,
                securable=Securable.environment_scope(env.name),
                detail={
                    "rule_id": str(rule.id),
                    "principal": account.name,
                    "environment": env.name,
                    "repository": repo.name,
                    "sha": sha,
                    "kinds": kinds,
                    "jti_sha256": digest,
                    "key_id": generated.key_id,
                    "expires_at": expires_at.isoformat(),
                },
            )
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                if retried:
                    raise ConflictError(
                        "Generated a duplicate key id twice in a row; exchange a new ID token."
                    ) from None
                retried = True
                continue
            return IssuedCiToken(
                token=generated.full_token,
                key_id=generated.key_id,
                expires_at=expires_at,
                principal=account.name,
                environment=env.name,
                repository=repo.name,
                kinds=kinds,
                sha=sha,
                rule_id=rule.id,
            )

    def verify(self, key_id: str, secret: str, *, now: datetime | None = None) -> PrincipalRef:
        """Verify a presented CI token and resolve its principal and scope.

        Parameters
        ----------
        key_id
            The token's key id.
        secret
            The secret presented.
        now
            The time of the check; the clock when `None`.

        Returns
        -------
        PrincipalRef
            The service account, with its `CiScope`.

        Raises
        ------
        AuthenticationError
            If the key id is unknown or the secret wrong, the token expired, or
            `resolve_ci_scope` finds it no longer valid.
        """
        now = now or utcnow()
        token = self._session.execute(
            select(CiToken).where(CiToken.key_id == key_id)
        ).scalar_one_or_none()
        if token is None or not verify_secret(secret, token.secret_hash):
            raise AuthenticationError("Invalid credentials.")
        if token.expires_at <= now:
            raise AuthenticationError(EXPIRED_MESSAGE)
        scope = resolve_ci_scope(self._session, token, now=now, require_unexpired=True)
        if scope is None:
            raise AuthenticationError(INVALID_MESSAGE)
        principal = self._session.get(Principal, token.principal_id)
        # resolve_ci_scope has just read this row as an active service account.
        assert principal is not None  # nosec B101 -- narrows the type only
        return PrincipalRef(
            id=principal.id, name=principal.name, type=principal.type, ci_scope=scope
        )
