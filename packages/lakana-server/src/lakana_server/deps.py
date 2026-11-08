"""FastAPI dependencies shared by both routers."""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime

from fastapi import Depends, Header, Query, Request
from sqlalchemy.orm import Session

from lakana_core.access.service import AccessService
from lakana_core.artifacts.service import ArtifactService
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.service import AuditService
from lakana_core.auth import AuthorizationService, PrincipalRef
from lakana_core.catalog.current_fields import current_fields
from lakana_core.catalog.glossary_read import GlossaryService
from lakana_core.catalog.graph_service import GraphService
from lakana_core.catalog.lineage_graph import LineageService
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.search import CatalogSearch
from lakana_core.catalog.service import CatalogService
from lakana_core.ci_oidc.exchange import TOKEN_PREFIX as CI_TOKEN_PREFIX
from lakana_core.ci_oidc.exchange import CiTokenService
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.config import ViewAuditConfig
from lakana_core.connections.service import ConnectionService
from lakana_core.errors import AuthenticationError, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.sessions import SessionService, utc_now
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.identity.tokens import parse_token
from lakana_core.jobs.service import JobService
from lakana_core.models import AuditEventType
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.package_indexes import PackageIndexService
from lakana_core.projects.repositories import RepositoryService
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.service import RunService
from lakana_core.secrets.keys import EnvKeyProvider, KeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.statement_limit import check_statement_length
from lakana_core.storage.resolver import StorageResolver
from lakana_server.browser_session import BACKGROUND_HEADER, CSRF_HEADER, read_session_cookie
from lakana_server.errors import CsrfRefused, TokenOnlyRoute
from lakana_server.sign_in_audit import Credential

PRINCIPAL_STATE_KEY = "_lakana_authenticated_principal"
"""The private name on `request.state` under which `authenticate_before_body` keeps the principal.

Not `principal`: nothing else in the request's life sets this name, and `get_principal` accepts
only a `PrincipalRef` under it."""

_AUTH_HEADER_PATTERN = re.compile(r"^(?i:Bearer)\s+(\S+)$")
"""Matches the `Authorization` header's outer shape only.

Case-insensitive on the `Bearer` scheme via the inline `(?i:...)` group — a
bare `re.IGNORECASE` flag would also loosen the captured token itself,
which stays case-sensitive since it is not a registered scheme token.
Which credential kind the captured token actually is (PAT, task token or CI
token) is `parse_token`'s job, not this pattern's.
"""

TOKEN_ONLY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("POST", "/api/v1/task-tokens"), ("DELETE", "/api/v1/task-tokens/{key_id}")}
)
"""Operations (method, route template) that refuse the session cookie: they are for code only.

Minting a task token from a browser would put a token in the interface, which never holds one."""

TOKEN_ONLY_MESSAGE = (  # nosec B105 -- a refusal's text, not a credential
    "Task tokens are for code, not a browser: create one with `lakana` and your own token."
)

CSRF_MESSAGE = (
    "This change came without the session's CSRF token (X-Lakana-CSRF), or with another "
    "session's. Reload the page and try again."
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_SIGN_IN_OR_TOKEN = (  # nosec B105 -- a refusal's text, not a credential
    "Not signed in. In the web interface: Sign in again. From code: send a token "
    "(Authorization: Bearer lakana_pat_<keyid>_<secret>)."
)
"""The 401 detail of a request without any credential while the web interface is on."""

_SHAPES = (
    "Authorization header must be 'Bearer lakana_pat_<keyid>_<secret>', "
    "'Bearer lakana_task_<keyid>_<secret>' or 'Bearer lakana_ci_<keyid>_<secret>'."
)


def get_session(request: Request) -> Iterator[Session]:
    """Provide a database session for the duration of one request.

    Parameters
    ----------
    request
        The current request, used only to reach `app.state.engine`, which
        `create_app`'s lifespan constructs once at startup.

    Yields
    ------
    Session
        A session against the app's engine, closed when the request ends.
    """
    with Session(request.app.state.engine) as session:
        yield session


def get_grant_authorization(
    request: Request, session: Session = Depends(get_session)
) -> AuthorizationService:
    """Build the authorization service for the current request.

    Parameters
    ----------
    request
        The current request, used to reach the process-wide `denial_audit_limiter`.
    session
        The current request's database session.

    Returns
    -------
    AuthorizationService
        `GrantAuthorization` wrapped in `AuditingAuthorization`, so every
        `PermissionDenied` any dependent service's `authorize()` call
        raises is recorded as an `AUTHORIZATION_DENIED` audit event before
        propagating, unchanged, to the caller.
    """
    return AuditingAuthorization(
        GrantAuthorization(session), session, request.app.state.denial_audit_limiter
    )


def get_pat_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> PatService:
    """Build a PatService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.

    Returns
    -------
    PatService
        A service using real authorization.
    """
    return PatService(session, authz)


def get_task_token_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> TaskTokenService:
    """Build a TaskTokenService for the current request.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.settings` for the
        operator-configured `max_ttl_seconds`.
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.

    Returns
    -------
    TaskTokenService
        A service using real authorization, capping minted tokens at
        `Settings.task_tokens.max_ttl_seconds` (`LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`).
    """
    return TaskTokenService(
        session, authz, max_ttl_seconds=request.app.state.settings.task_tokens.max_ttl_seconds
    )


def get_clock(request: Request) -> Callable[[], datetime]:
    """Read the clock the sign-in services of this app use.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.clock` (set at start-up; a test may
        replace it).

    Returns
    -------
    Callable
        The clock; `lakana_core.identity.sessions.utc_now` when the app has none.
    """
    return getattr(request.app.state, "clock", utc_now)


def get_session_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> SessionService:
    """Build a SessionService for the current request.

    Parameters
    ----------
    request
        The current request, used to reach the app's clock.
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.

    Returns
    -------
    SessionService
        A service using real authorization and the app's clock.
    """
    return SessionService(session, authz, clock=get_clock(request))


def get_principal(
    request: Request,
    authorization: str | None = Header(
        None,
        description=(
            "The credential: `Bearer <token>` with a personal access token, task token or CI "
            "token. Without it, the web interface's session cookie counts instead."
        ),
    ),
    session: Session = Depends(get_session),
    pats: PatService = Depends(get_pat_service),
    task_tokens: TaskTokenService = Depends(get_task_token_service),
) -> PrincipalRef:
    """Resolve the acting principal from the Authorization header or the session cookie.

    Verifies a real credential: a PAT (`lakana_pat_<key_id>_<secret>`), a
    task token (`lakana_task_<key_id>_<secret>`) or a CI token
    (`lakana_ci_<key_id>_<secret>`, `CiTokenService.verify`, which re-checks its
    rule and principal every time), dispatched by prefix, or, with no `Authorization` header
    at all, the browser session cookie (`lakana_server.browser_session.read_session_cookie`:
    below `/api/v1/` only, never with the interface off). Any `Authorization` header, valid or
    not, wins: the cookie is then not read, and no CSRF token is asked for. A cookie request is
    refused on `TOKEN_ONLY_ROUTES` (403 `TokenOnlyRoute`) and, for any method but GET, HEAD and
    OPTIONS, without the session's CSRF token in `X-Lakana-CSRF` (403 `CsrfRefused`); the
    session's row is then read (`SessionService.verify`), so an ended session fails at once
    with `SessionEndedError` (401 with `reason`). The returned principal carries its
    credential kind (`PrincipalRef.credential`). This
    replaces M1's shape-only check — a well-formed header with the wrong
    secret is now rejected, not accepted. Records `AUTHENTICATION_SUCCEEDED`
    for a successful attempt when `app.state.sign_in_audit_limiter`
    (`lakana_server.sign_in_audit.SignInAuditLimiter`) admits it: once per
    credential per `LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES`, with the count of
    successes left out since the previous row as `detail.suppressed` when it
    is nonzero. A success it does not admit writes nothing and commits
    nothing. A failed attempt that names a credential makes that
    credential's next success recorded.

    A failed attempt is recorded as `AUTHENTICATION_FAILED` only if
    `app.state.auth_audit_limiter` (`lakana_server.auth_rate_limit.
    AuthAuditLimiter`) admits it for the requesting client
    (`request.client.host`, or `"unknown"` if unavailable; `X-Forwarded-For`
    counts only from `LAKANA_TRUSTED_PROXIES`, so behind an unlisted proxy the
    limit applies per proxy address — see the threat model). This
    bounds how many rows a client retrying a bad credential can cause to be
    written; the response is still 401 either way. The recorded
    `key_id` is truncated to 64 characters, and the count of attempts
    suppressed since that client's last recorded failure is attached as
    `detail.suppressed` whenever it is nonzero.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.auth_audit_limiter`,
        `app.state.sign_in_audit_limiter` and the requesting client's address.
    authorization
        The raw `Authorization` header value, or `None` if absent.
    session
        The current request's database session, used to record the
        authentication event.
    pats
        Verifies PAT-shaped tokens.
    task_tokens
        Verifies task-token-shaped tokens.

    Returns
    -------
    PrincipalRef
        The authenticated principal.

    Raises
    ------
    AuthenticationError
        If the header is missing (and no session cookie counts), does not match a credential
        shape, or the credential fails verification (unknown, wrong secret, expired, revoked,
        ended, or its principal is deactivated).
    TokenOnlyRoute
        If a session cookie is presented on a route of `TOKEN_ONLY_ROUTES`.
    CsrfRefused
        If a state-changing cookie request lacks its session's CSRF token.
    """
    already = getattr(request.state, PRINCIPAL_STATE_KEY, None)
    if isinstance(already, PrincipalRef):
        return already
    audit = AuditService(session)
    if authorization is None:
        cookie = read_session_cookie(request)
        if cookie is not None:
            return _session_principal(request, cookie, session, audit)
    key_id: str | None = None
    credential: Credential | None = None
    try:
        match = _AUTH_HEADER_PATTERN.match(authorization or "")
        if match is None:
            if authorization is None and request.app.state.settings.ui.enabled:
                raise AuthenticationError(_SIGN_IN_OR_TOKEN)
            raise AuthenticationError(_SHAPES)
        raw_token = match.group(1)
        if (parsed := parse_token(raw_token, "lakana_pat")) is not None:
            key_id, secret = parsed
            credential = ("pat", key_id)
            principal = pats.verify(key_id, secret)
            _record_sign_in(request, audit, credential, principal.id)
            return principal
        if (parsed := parse_token(raw_token, "lakana_task")) is not None:
            key_id, secret = parsed
            credential = ("task", key_id)
            scope = task_tokens.verify(key_id, secret)
            _record_sign_in(request, audit, credential, scope.principal.id)
            return dataclasses.replace(scope.principal, credential="task")
        if (parsed := parse_token(raw_token, CI_TOKEN_PREFIX)) is not None:
            key_id, secret = parsed
            credential = ("ci", key_id)
            principal = CiTokenService(session).verify(key_id, secret)
            _record_sign_in(request, audit, credential, principal.id)
            return dataclasses.replace(principal, credential="ci")
        raise AuthenticationError(_SHAPES)
    except AuthenticationError:
        _record_failure(request, audit, credential, {"key_id": (key_id or "")[:64]})
        raise


def _session_principal(
    request: Request, cookie: str, session: Session, audit: AuditService
) -> PrincipalRef:
    """Authenticate a request by its session cookie.

    Refuses the token-only routes, then a state-changing request without the session's CSRF
    token, then verifies the session's row (read on every request, so an ended session fails
    at once). Only a request the person started extends the idle timeout: one marked
    `X-Lakana-Background: 1` does not.

    Parameters
    ----------
    request
        The current request.
    cookie
        The session cookie's value.
    session
        The current request's database session.
    audit
        Writes the authentication events.

    Returns
    -------
    PrincipalRef
        The session's principal, with ``credential="session"``.

    Raises
    ------
    TokenOnlyRoute
        On a route of `TOKEN_ONLY_ROUTES` (403); audited as a failed sign-in with
        ``reason: token_only_route``.
    CsrfRefused
        If a state-changing request lacks the session's CSRF token (403); audited with
        ``reason: csrf``.
    AuthenticationError
        If the cookie names no session or a wrong secret; `SessionEndedError` if the session
        has ended. Audited as a failed token is.
    """
    key_id = cookie.split("_", 1)[0][:64]
    credential: Credential = ("session", key_id)
    detail: dict[str, str | int] = {"key_id": key_id, "credential": "session"}
    route = request.scope.get("route")
    if (request.method, getattr(route, "path", None)) in TOKEN_ONLY_ROUTES:
        _record_failure(request, audit, credential, {**detail, "reason": "token_only_route"})
        raise TokenOnlyRoute(TOKEN_ONLY_MESSAGE)
    sessions = SessionService(
        session, get_grant_authorization(request, session), clock=get_clock(request)
    )
    if request.method not in _SAFE_METHODS and not sessions.check_csrf(
        cookie, request.headers.get(CSRF_HEADER) or ""
    ):
        _record_failure(request, audit, credential, {**detail, "reason": "csrf"})
        raise CsrfRefused(CSRF_MESSAGE)
    try:
        principal = sessions.verify(cookie, extend=request.headers.get(BACKGROUND_HEADER) != "1")
    except AuthenticationError:
        _record_failure(request, audit, credential, detail)
        raise
    _record_sign_in(request, audit, credential, principal.id, kind="session")
    return principal


def _record_failure(
    request: Request,
    audit: AuditService,
    credential: Credential | None,
    detail: dict[str, str | int],
) -> None:
    """Record a failed authentication, if the per-address limit admits it.

    Parameters
    ----------
    request
        The current request, used to reach both limiters and the client's address.
    audit
        Writes the row, with its own commit.
    credential
        The credential the attempt named, if any: its next success is then recorded.
    detail
        The row's detail; the count of attempts left out is added as ``suppressed``.
    """
    if credential is not None:
        request.app.state.sign_in_audit_limiter.forget(credential)
    client = request.client.host if request.client else "unknown"
    admitted, suppressed = request.app.state.auth_audit_limiter.admit(client)
    if admitted:
        if suppressed > 0:
            detail["suppressed"] = suppressed
        audit.record(None, AuditEventType.AUTHENTICATION_FAILED, detail=detail, commit=True)


def _record_sign_in(
    request: Request,
    audit: AuditService,
    credential: Credential,
    principal_id: uuid.UUID,
    *,
    kind: str | None = None,
) -> None:
    """Record a successful authentication, if the sign-in window admits it.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.sign_in_audit_limiter`.
    audit
        Writes the row, with its own commit.
    credential
        The verified token's kind and key id.
    principal_id
        The principal the token authenticated.
    kind
        Written to the row as ``credential`` when given (``session``); a token's row has
        only its key id, as before.

    Raises
    ------
    Exception
        Whatever writing the row raised; the limiter is told first, so that the next success of
        the credential is recorded and counts this one.
    """
    limiter = request.app.state.sign_in_audit_limiter
    admitted, suppressed = limiter.admit(credential, principal_id)
    if not admitted:
        return
    detail: dict[str, str | int] = {"key_id": credential[1]}
    if kind is not None:
        detail["credential"] = kind
    if suppressed > 0:
        detail["suppressed"] = suppressed
    try:
        audit.record(
            principal_id, AuditEventType.AUTHENTICATION_SUCCEEDED, detail=detail, commit=True
        )
    except BaseException:
        limiter.not_recorded(credential, suppressed)
        raise


def authenticate_before_body(request: Request) -> None:
    """Authenticate a request whose JSON body has not been read yet, and keep the result.

    FastAPI reads and decodes a JSON body before it runs any dependency, so without this a
    caller with no credential still makes the server read and decode what it sent.
    This runs `get_principal` with the services it needs, and `get_principal` then returns the
    principal stored here instead of verifying the credential, and auditing it, a second time.

    Parameters
    ----------
    request
        The request.

    Raises
    ------
    AuthenticationError
        If the credential is missing or fails verification; audited as `get_principal` does.
    """
    with Session(request.app.state.engine) as session:
        authz = get_grant_authorization(request, session)
        setattr(
            request.state,
            PRINCIPAL_STATE_KEY,
            get_principal(
                request,
                request.headers.get("authorization"),
                session,
                get_pat_service(session, authz),
                get_task_token_service(request, session, authz),
            ),
        )


def check_statement(request: Request, text: str, what: str = "The SQL") -> None:
    """Refuse SQL text over `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS` before anything parses it.

    Parameters
    ----------
    request
        The request, used to reach the settings.
    text
        The SQL or expression the caller submitted.
    what
        What the text is, for the message.

    Raises
    ------
    ValidationError
        If `text` is longer than the limit; mapped to 422.
    """
    check_statement_length(text, request.app.state.settings.query_limits.max_statement_chars, what)


def with_break_glass(principal: PrincipalRef, reason: str | None) -> PrincipalRef:
    """Attach a break-glass reason to the request's principal.

    Parameters
    ----------
    principal
        The authenticated caller.
    reason
        The reason from the body or the query string, or `None`.

    Returns
    -------
    PrincipalRef
        `principal` unchanged without a reason; otherwise a copy carrying it,
        which `ProtectedWriteGuard` validates, authorizes and audits.
    """
    if reason is None:
        return principal
    return dataclasses.replace(principal, break_glass_reason=reason)


def get_write_principal(
    principal: PrincipalRef = Depends(get_principal),
    break_glass_reason: str | None = Query(
        default=None,
        max_length=500,
        description=(
            "Why the protection of a protected environment is broken, up to 500 characters; "
            "recorded in the audit log. Only a protected environment needs it."
        ),
    ),
) -> PrincipalRef:
    """Authenticate a write endpoint without a JSON body, honouring `?break_glass_reason=`.

    Parameters
    ----------
    principal
        The authenticated caller.
    break_glass_reason
        A break-glass reason (required to break glass on a protected environment).

    Returns
    -------
    PrincipalRef
        The principal, with the reason attached when given.
    """
    return with_break_glass(principal, break_glass_reason)


def get_managed_write_principal(
    principal: PrincipalRef = Depends(get_principal),
    override_managed: bool = Query(
        default=False,
        description=(
            "Change access in a scope an access file manages anyway, in an emergency. Needs the "
            "ADMIN role on the instance and `override_reason`; audited, and shown as drift until "
            "the next apply."
        ),
    ),
    override_reason: str | None = Query(
        default=None,
        max_length=500,
        description="Why, 10 to 500 characters on one line; with `override_managed=true`.",
    ),
) -> PrincipalRef:
    """Authenticate a route that changes access, honouring the managed-scope override (#46).

    Parameters
    ----------
    principal
        The authenticated caller.
    override_managed
        Whether the caller overrides the scope's access file.
    override_reason
        The override's reason.

    Returns
    -------
    PrincipalRef
        The principal, carrying the reason when overriding; the service's guard checks it.

    Raises
    ------
    ValidationError
        If one of the two parameters comes without the other (422).
    """
    if override_managed and override_reason is None:
        raise ValidationError(
            "override_managed=true needs override_reason (`--override-managed` needs `--reason`)"
        )
    if override_reason is not None and not override_managed:
        raise ValidationError(
            "override_reason goes with override_managed=true "
            "(`--reason` goes with `--override-managed`)"
        )
    if override_reason is None:
        return principal
    return dataclasses.replace(principal, managed_override_reason=override_reason)


def get_key_provider(request: Request) -> KeyProvider:
    """Build a KeyProvider for the current request.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.settings`.

    Returns
    -------
    KeyProvider
        Backed by whatever master key is configured — `None` if none is,
        which is a valid state; it only becomes an error if a secret
        operation is actually attempted with it.
    """
    secrets = request.app.state.settings.secrets
    return EnvKeyProvider(secrets.master_key, accept_format_1=secrets.accept_format_1)


def get_undo_days(request: Request) -> int:
    """Read how many days a dropped table stays restorable.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.settings`.

    Returns
    -------
    int
        `LAKANA_TABLES__UNDO_DAYS`.
    """
    return request.app.state.settings.tables.undo_days


def get_expiry_warning_days(request: Request) -> int:
    """Read how many days ahead a table's expiry is shown as soon.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.settings`.

    Returns
    -------
    int
        `LAKANA_TABLES__EXPIRY_WARNING_DAYS`.
    """
    return request.app.state.settings.tables.expiry_warning_days


def get_main_language(request: Request) -> str:
    """Read the instance's main language.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.settings`.

    Returns
    -------
    str
        `LAKANA_MAIN_LANGUAGE`.
    """
    return request.app.state.settings.main_language


def get_view_audit(request: Request) -> ViewAuditConfig:
    """Read whether and how often a view of other people's activity is audited.

    Parameters
    ----------
    request
        The current request, used to reach `app.state.settings`.

    Returns
    -------
    ViewAuditConfig
        `LAKANA_VIEW_AUDIT__*`.
    """
    return request.app.state.settings.view_audit


def get_catalog(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    key_provider: KeyProvider = Depends(get_key_provider),
    undo_days: int = Depends(get_undo_days),
    expiry_warning_days: int = Depends(get_expiry_warning_days),
    view_audit: ViewAuditConfig = Depends(get_view_audit),
    main_language: str = Depends(get_main_language),
) -> CatalogService:
    """Build a CatalogService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    key_provider
        Decrypts a secret-backed environment's storage credentials.
    undo_days
        How long a table dropped in this request stays restorable.
    expiry_warning_days
        How many days ahead a table's expiry is described as soon.
    view_audit
        Whether and how often seeing the readers of a table is audited.
    main_language
        The language of the texts of a table whose project declares none.

    Returns
    -------
    CatalogService
        A catalog service using real authorization.
    """
    return CatalogService(
        session,
        StorageResolver(session, key_provider),
        authz,
        undo_days=undo_days,
        expiry_warning_days=expiry_warning_days,
        view_audit=view_audit,
        main_language=main_language,
    )


def get_catalog_search(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    main_language: str = Depends(get_main_language),
) -> CatalogSearch:
    """Build a CatalogSearch for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    main_language
        `LAKANA_MAIN_LANGUAGE`, the language of header texts whose project declares none.

    Returns
    -------
    CatalogSearch
        A search using real authorization.
    """
    return CatalogSearch(session, authz, main_language=main_language)


def get_glossary_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    main_language: str = Depends(get_main_language),
) -> GlossaryService:
    """Build a GlossaryService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    main_language
        `LAKANA_MAIN_LANGUAGE`, for a term whose project declares none.

    Returns
    -------
    GlossaryService
        The glossary reader.
    """
    return GlossaryService(session, authz, main_language=main_language)


def get_graph_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    main_language: str = Depends(get_main_language),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> GraphService:
    """Build a GraphService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    main_language
        `LAKANA_MAIN_LANGUAGE`, for labels and term names without a translation.
    key_provider
        Decrypts a secret-backed environment's storage credentials, to read a table's current
        fields so a column mask is linked to its column's current name.

    Returns
    -------
    GraphService
        The metadata graph reader.
    """
    storage = StorageResolver(session, key_provider)
    return GraphService(
        session,
        authz,
        main_language=main_language,
        fields_of=lambda table_id, seconds: current_fields(
            session, storage, table_id, timeout=seconds
        ),
    )


def get_artifact_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> ArtifactService:
    """Build the artifact service for the current request.

    Parameters
    ----------
    request
        The current request, for the `LAKANA_ARTIFACTS__*` settings.
    session
        The request's session.
    authz
        The request's authorization service.
    key_provider
        Decrypts a secret-backed environment's storage credentials.

    Returns
    -------
    ArtifactService
        The service.
    """
    return ArtifactService(
        session,
        StorageResolver(session, key_provider),
        authz,
        request.app.state.settings.artifacts,
    )


def get_principal_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> PrincipalService:
    """Build a PrincipalService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.

    Returns
    -------
    PrincipalService
        A service using real authorization.
    """
    return PrincipalService(session, authz)


def get_group_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> GroupService:
    """Build a GroupService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.

    Returns
    -------
    GroupService
        A service using real authorization.
    """
    return GroupService(session, authz)


def get_query_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> QueryService:
    """Build a QueryService for the current request.

    Parameters
    ----------
    request
        The current request, for `app.state.settings`: the run lease decides which workers a
        cancel counts as alive, and the limit per user is the one the wait reasons judge by.
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.

    Returns
    -------
    QueryService
        A query service using real authorization, the same carried-forward
        implementation `get_catalog` uses, with the operator's run lease and limit per user.
    """
    settings = request.app.state.settings
    return QueryService(
        session,
        authz,
        lease_seconds=settings.run.lease_seconds,
        running_per_principal=settings.query_limits.running_per_principal,
        view_audit=settings.view_audit,
    )


def get_grant_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> GrantService:
    """Build a GrantService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.

    Returns
    -------
    GrantService
        A service using real authorization.
    """
    return GrantService(session, authz)


def get_row_filter_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    catalog: CatalogService = Depends(get_catalog),
) -> RowFilterService:
    """Build a RowFilterService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    catalog
        The catalog service, from which the filter service reads a table's columns.

    Returns
    -------
    RowFilterService
        A service using real authorization that checks every column a filter reads exists.
    """
    return RowFilterService(session, authz, catalog)


def get_access_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    catalog: CatalogService = Depends(get_catalog),
) -> AccessService:
    """Build an AccessService for the current request.

    Parameters
    ----------
    request
        The current request, for `LAKANA_CI_OIDC__ISSUERS`.
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    catalog
        The catalog service, from which the access service reads a table's current columns.

    Returns
    -------
    AccessService
        A service using real authorization.
    """
    return AccessService(
        session, authz, catalog, issuers=request.app.state.settings.ci_oidc.issuers
    )


def get_column_mask_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    catalog: CatalogService = Depends(get_catalog),
) -> ColumnMaskService:
    """Build a ColumnMaskService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    catalog
        The catalog service, from which the mask service reads a table's columns.

    Returns
    -------
    ColumnMaskService
        A service using real authorization that checks every masked column exists.
    """
    return ColumnMaskService(session, authz, catalog)


def get_secret_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> SecretService:
    """Build a SecretService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    key_provider
        Wraps a freshly generated data key for each new/rotated secret.

    Returns
    -------
    SecretService
        A service using real authorization.
    """
    return SecretService(session, authz, key_provider)


def get_connection_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> ConnectionService:
    """Build a ConnectionService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, grant-backed authorization service.
    key_provider
        Encrypts a secret created from new values; never used to decrypt.

    Returns
    -------
    ConnectionService
        A service using real authorization.
    """
    return ConnectionService(session, authz, key_provider)


def get_audit_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    view_audit: ViewAuditConfig = Depends(get_view_audit),
) -> AuditService:
    """Build an AuditService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, auditing-wrapped authorization service.
    view_audit
        Whether and how often a read of other people's events is audited.

    Returns
    -------
    AuditService
        A service that can both record and, once authorized, read audit
        events.
    """
    return AuditService(session, authz, view_audit=view_audit)


def get_lineage_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> LineageService:
    """Build a LineageService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, auditing-wrapped authorization service.

    Returns
    -------
    LineageService
        A lineage service using real authorization.
    """
    return LineageService(session, authz)


def get_repository_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> RepositoryService:
    """Build a RepositoryService for the current request.

    Parameters
    ----------
    request
        The current request, for `app.state.settings.repo_roots`.
    session
        The current request's database session.
    authz
        The real, auditing-wrapped authorization service.

    Returns
    -------
    RepositoryService
        A service confined to the configured repo roots.
    """
    return RepositoryService(session, authz, repo_roots=request.app.state.settings.repo_roots)


def get_package_index_service(
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> PackageIndexService:
    """Build a PackageIndexService for the current request.

    Parameters
    ----------
    session
        The current request's database session.
    authz
        The real, auditing-wrapped authorization service.

    Returns
    -------
    PackageIndexService
        A service using real authorization.
    """
    return PackageIndexService(session, authz)


def get_ci_trust_rule_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> CiTrustRuleService:
    """Build a CiTrustRuleService for the current request.

    Parameters
    ----------
    request
        The current request, for `LAKANA_CI_OIDC__ISSUERS`.
    session
        The current request's database session.
    authz
        The real, auditing-wrapped authorization service.

    Returns
    -------
    CiTrustRuleService
        A service that accepts rules for the configured issuers only.
    """
    return CiTrustRuleService(session, authz, issuers=request.app.state.settings.ci_oidc.issuers)


def get_deploy_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> DeployService:
    """Build a DeployService for the current request.

    Parameters
    ----------
    request
        The current request, for `app.state.settings`: the run lease decides which workers a
        queued deploy's wait reason counts as alive.
    session
        The current request's database session.
    authz
        The real, auditing-wrapped authorization service.

    Returns
    -------
    DeployService
        A service using real authorization, with the operator's run lease.
    """
    return DeployService(session, authz, lease_seconds=request.app.state.settings.run.lease_seconds)


def get_run_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> RunService:
    """Build a RunService for the current request.

    Parameters
    ----------
    request
        The current request, for `app.state.settings`.
    session
        The current request's database session.
    authz
        The real, auditing-wrapped authorization service.
    key_provider
        Decrypts a secret-backed environment's storage credentials (task logs).

    Returns
    -------
    RunService
        A service with the operator's run caps.
    """
    settings = request.app.state.settings
    return RunService(
        session,
        authz,
        key_provider=key_provider,
        caps=RunCaps.from_config(settings.query_limits, settings.run),
        lease_seconds=settings.run.lease_seconds,
    )


def get_job_service(
    request: Request,
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> JobService:
    """Build a JobService for the current request.

    Parameters
    ----------
    request
        The current request, for `app.state.settings`.
    session
        The current request's database session.
    authz
        The real, auditing-wrapped authorization service.

    Returns
    -------
    JobService
        A service with the operator's run caps.
    """
    settings = request.app.state.settings
    return JobService(
        session,
        authz,
        caps=RunCaps.from_config(settings.query_limits, settings.run),
        lease_seconds=settings.run.lease_seconds,
    )
