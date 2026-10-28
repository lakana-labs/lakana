"""The CI trust rules resource of the platform API: instance administrators only.

A rule says which CI jobs (by their OIDC ID token's claims) may deploy which repository to
which environment, as which service account. `lakana_core.ci_oidc.rules` has the rules.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.ci_oidc.rules import CiTrustRuleService, CiTrustRuleView
from lakana_server.api.platform.schemas import (
    CiTrustExplainIn,
    CiTrustExplainOut,
    CiTrustRuleCreate,
    CiTrustRuleOut,
    CiTrustRuleUpdate,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_ci_trust_rule_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/ci-trust-rules",
    tags=["ci-trust-rules"],
    responses=errors(401),
)


def _out(view: CiTrustRuleView) -> CiTrustRuleOut:
    """Build a response body.

    Parameters
    ----------
    view
        The rule.

    Returns
    -------
    CiTrustRuleOut
        The body.
    """
    return CiTrustRuleOut.model_validate(view)


@router.post("", response_model=CiTrustRuleOut, status_code=201, responses=errors(403, 404))
def create_ci_trust_rule(
    body: CiTrustRuleCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Create a CI trust rule.

    Creates an enabled rule that lets CI jobs whose ID-token claims match its conditions
    exchange them for a token that acts as a service account. Needs the `ADMIN` role on the
    instance. Answers 404 when the principal, environment or repository does not exist and
    422 when a field breaks the rules of a trust rule.

    Parameters
    ----------
    body
        The rule.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(
        service.create(
            principal,
            issuer=body.issuer,
            principal=body.principal,
            environment=body.environment,
            repository=body.repository,
            kinds=body.kinds,
            conditions=body.conditions,
            start_on_deploy_jobs=body.start_on_deploy_jobs,
        )
    )


@router.get("", response_model=list[CiTrustRuleOut], responses=errors(403))
def list_ci_trust_rules(
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> list[CiTrustRuleOut]:
    """List CI trust rules.

    Lists every rule, oldest first. Needs the `ADMIN` role on the instance.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    list of CiTrustRuleOut
        The rules.
    """
    return [_out(view) for view in service.list(principal)]


@router.post("/explain", response_model=CiTrustExplainOut, responses=errors(403))
def explain_ci_trust_rules(
    body: CiTrustExplainIn,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustExplainOut:
    """Explain which CI trust rule matches claims.

    Says which rule an exchange with the given ID-token claims would match, and why the
    others do not. Needs the `ADMIN` role on the instance. Answers 422 when no issuer is
    given and the claims have no `iss` text.

    Parameters
    ----------
    body
        The claims and the environment and repository the exchange would ask for.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustExplainOut
        The verdict and every rule's reasons.
    """
    report = service.explain(
        principal,
        claims=body.claims,
        environment=body.environment,
        repository=body.repository,
        issuer=body.issuer,
    )
    return CiTrustExplainOut.model_validate(report)


@router.get("/{rule_id}", response_model=CiTrustRuleOut, responses=errors(403, 404))
def get_ci_trust_rule(
    rule_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Show a CI trust rule.

    Needs the `ADMIN` role on the instance. Answers 404 when there is no such rule.

    Parameters
    ----------
    rule_id
        The CI trust rule's id.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(service.get(principal, rule_id))


@router.patch("/{rule_id}", response_model=CiTrustRuleOut, responses=errors(403, 404))
def update_ci_trust_rule(
    rule_id: uuid.UUID,
    body: CiTrustRuleUpdate,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Change a CI trust rule.

    Changes the rule's principal, kinds, conditions or `start_on_deploy_jobs`. The tokens issued
    under it end. Needs
    the `ADMIN` role on the instance. Answers 404 when there is no such rule or principal
    and 422 when nothing is given or a field breaks the rules of a trust rule.

    Parameters
    ----------
    rule_id
        The CI trust rule's id.
    body
        Only the fields present change.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(
        service.update(
            principal,
            rule_id,
            principal=body.principal,
            kinds=body.kinds,
            conditions=body.conditions,
            start_on_deploy_jobs=body.start_on_deploy_jobs,
        )
    )


@router.post("/{rule_id}/enable", response_model=CiTrustRuleOut, responses=errors(403, 404))
def enable_ci_trust_rule(
    rule_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Enable a CI trust rule.

    Needs the `ADMIN` role on the instance. Answers 404 when there is no such rule and 422
    when its conditions no longer meet the requirements of a new rule.

    Parameters
    ----------
    rule_id
        The CI trust rule's id.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(service.enable(principal, rule_id))


@router.post("/{rule_id}/disable", response_model=CiTrustRuleOut, responses=errors(403, 404))
def disable_ci_trust_rule(
    rule_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> CiTrustRuleOut:
    """Disable a CI trust rule.

    A disabled rule matches no exchange, and the tokens issued under it end. Needs the
    `ADMIN` role on the instance. Answers 404 when there is no such rule.

    Parameters
    ----------
    rule_id
        The CI trust rule's id.
    principal
        The authenticated caller.
    service
        The rule service.

    Returns
    -------
    CiTrustRuleOut
        The rule.
    """
    return _out(service.disable(principal, rule_id))


@router.delete("/{rule_id}", status_code=204, responses=errors(403, 404))
def delete_ci_trust_rule(
    rule_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: CiTrustRuleService = Depends(get_ci_trust_rule_service),
) -> None:
    """Delete a CI trust rule.

    The tokens issued under the rule end. Needs the `ADMIN` role on the instance. Answers
    404 when there is no such rule.

    Parameters
    ----------
    rule_id
        The CI trust rule's id.
    principal
        The authenticated caller.
    service
        The rule service.
    """
    service.delete(principal, rule_id)
