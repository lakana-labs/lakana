"""A world for the CI trust rule, exchange and enforcement tests: no network, local keys.

`ci_world` creates the environments ``dev`` and ``prod``, the repositories ``analytics`` and
``other``, an administrator, and the service account ``deployer`` that holds `DEPLOY` on
``prod``. `sign` makes a GitHub Actions ID token with a local key that `verifier` trusts
through a fake fetcher.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import jwt
from ci_oidc_test_helpers import AUDIENCE, ISSUER, FakeFetcher, jwk, rsa_key
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    CiScope,
    PrincipalRef,
    SecurableKind,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.ci_oidc.exchange import CiTokenService, IssuedCiToken
from lakana_core.ci_oidc.jwks import JwksCache
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.ci_oidc.verification import IdTokenVerifier
from lakana_core.config import StorageConfig
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.tokens import parse_token
from lakana_core.models import Principal, PrincipalType, Repository, RepositoryKind
from lakana_core.storage.resolver import StorageResolver

NOW = datetime.now(UTC).replace(microsecond=0)
"""The tests' clock: real time, because a submission checks a token's expiry by the clock."""
T = int(NOW.timestamp())
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


@dataclass(frozen=True)
class CiWorld:
    """What `ci_world` made.

    Parameters
    ----------
    admin
        An administrator (the `ADMIN` role on the instance).
    deployer
        The service account rules act as; holds `DEPLOY` on ``prod``.
    """

    admin: PrincipalRef
    deployer: PrincipalRef


def principal(
    session: Session, name: str, kind: PrincipalType = PrincipalType.SERVICE_ACCOUNT
) -> PrincipalRef:
    """Persist an active principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    kind
        Its type.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=kind)
    session.add(Principal(id=ref.id, type=kind, name=name, is_active=True))
    session.commit()
    return ref


def ci_world(session: Session, tmp_path: Path) -> CiWorld:
    """Create two environments, two repositories, an administrator and a deployer.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root of the environments.

    Returns
    -------
    CiWorld
        The two principals.
    """
    admin = principal(session, "admin", PrincipalType.USER)
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    for name in ("dev", "prod"):
        catalog.create_environment(
            admin,
            name=name,
            storage=StorageConfig(scheme="file", root=f"file://{tmp_path / name}", options={}),
        )
    for name in ("analytics", "other"):
        session.add(
            Repository(
                id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}"
            )
        )
    session.commit()
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    deployer = principal(session, "deployer")
    grants.grant(
        admin,
        principal="deployer",
        action=Action.DEPLOY,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )
    return CiWorld(admin=admin, deployer=deployer)


def claims(**changes: object) -> dict[str, object]:
    """Return the claims of a GitHub Actions ID token for ``org/repo`` on ``main``.

    Parameters
    ----------
    **changes
        Claims to set; `None` removes one.

    Returns
    -------
    dict[str, object]
        The claims.
    """
    base: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "repo:org/repo:environment:prod",
        "jti": str(uuid.uuid4()),
        "iat": T - 30,
        "nbf": T - 30,
        "exp": T + 270,
        "repository": "org/repo",
        "repository_id": "123456",
        "ref": "refs/heads/main",
        "event_name": "push",
        "environment": "prod",
        "sha": SHA,
    }
    base.update(changes)
    return {name: value for name, value in base.items() if value is not None}


def sign(payload: dict[str, object] | None = None) -> str:
    """Sign an ID token with the key the fake issuer publishes.

    Parameters
    ----------
    payload
        The claims; `claims()` by default.

    Returns
    -------
    str
        The compact token.
    """
    return jwt.encode(
        payload or claims(), rsa_key("main"), algorithm="RS256", headers={"kid": "k1"}
    )


def config(**changes: object) -> CiOidcConfig:
    """Return CI OIDC settings for the test issuer.

    Parameters
    ----------
    **changes
        Further fields.

    Returns
    -------
    CiOidcConfig
        The settings.
    """
    return CiOidcConfig.model_validate({"issuers": ["github"], "audience": AUDIENCE, **changes})


def verifier(settings: CiOidcConfig | None = None) -> IdTokenVerifier:
    """Build a verifier that reads the test issuer's keys from a fake fetcher.

    Parameters
    ----------
    settings
        The settings; `config()` by default.

    Returns
    -------
    IdTokenVerifier
        The verifier.
    """
    settings = settings or config()
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("main"), "k1", alg="RS256"))
    return IdTokenVerifier(settings, JwksCache(settings, fetcher=fetcher))


def rules(session: Session) -> CiTrustRuleService:
    """Build the rule service with real authorization and the test issuer configured.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CiTrustRuleService
        The service.
    """
    return CiTrustRuleService(session, GrantAuthorization(session), issuers=[ISSUER])


def add_rule(
    session: Session,
    world: CiWorld,
    *,
    environment: str = "prod",
    repository: str = "analytics",
    kinds: tuple[str, ...] = ("deploy", "dry_run"),
    conditions: dict[str, str] | None = None,
    start_on_deploy_jobs: bool = False,
) -> uuid.UUID:
    """Create a rule for ``deployer`` that matches `claims()`.

    Parameters
    ----------
    session
        Database session.
    world
        The world.
    environment
        The rule's environment.
    repository
        The rule's repository.
    kinds
        What it allows.
    conditions
        Its claim conditions; ``repository_id`` and ``ref`` of `claims()` by default.
    start_on_deploy_jobs
        Whether its deploys start the project's `on_deploy` jobs.

    Returns
    -------
    uuid.UUID
        The rule's id.
    """
    view = rules(session).create(
        world.admin,
        issuer="github",
        principal="deployer",
        environment=environment,
        repository=repository,
        kinds=list(kinds),
        conditions=conditions
        or {"repository_id": "123456", "ref": "refs/heads/main", "event_name": "push"},
        start_on_deploy_jobs=start_on_deploy_jobs,
    )
    return view.id


def exchange(
    session: Session,
    *,
    payload: dict[str, object] | None = None,
    environment: str = "prod",
    repository: str = "analytics",
    now: datetime = NOW,
) -> IssuedCiToken:
    """Exchange an ID token for a CI token.

    Parameters
    ----------
    session
        Database session.
    payload
        The ID token's claims; `claims()` by default.
    environment
        The environment asked for.
    repository
        The repository asked for.
    now
        The time of the exchange.

    Returns
    -------
    IssuedCiToken
        The token.
    """
    return CiTokenService(session).exchange(
        verifier(), sign(payload), environment=environment, repository=repository, now=now
    )


def authenticate(session: Session, issued: IssuedCiToken, now: datetime = NOW) -> PrincipalRef:
    """Authenticate with a CI token, as the server does.

    Parameters
    ----------
    session
        Database session.
    issued
        The token.
    now
        The time of the authentication.

    Returns
    -------
    PrincipalRef
        The principal, with its `ci_scope`.
    """
    parsed = parse_token(issued.token, "lakana_ci")
    assert parsed is not None
    return CiTokenService(session).verify(*parsed, now=now)


def scope_of(p: PrincipalRef) -> CiScope:
    """Return a principal's CI scope.

    Parameters
    ----------
    p
        A CI-authenticated principal.

    Returns
    -------
    CiScope
        Its scope.
    """
    assert p.ci_scope is not None
    return p.ci_scope
