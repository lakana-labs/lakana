"""CI trust rules (`Client.ci_trust_rules`) and the exchange of a CI ID token (`exchange_id_token`).

A CI trust rule says which CI jobs, by their OIDC ID token's claims, may deploy which repository
to which environment, as which service account. Only an instance administrator manages rules.
`exchange_id_token` needs no Lakana credential: it trades the CI job's ID token for a
``lakana_ci_…`` token that may queue one deploy (of the job's own commit) or dry run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import httpx

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import CiToken, CiTrustExplanation, CiTrustRule, CiTrustRuleExplanation
from lakana._routes import ROUTES
from lakana.errors import InvalidResponse
from lakana.resources.artifacts import _ODD_ANSWER, _array, _object


def _rule_from_json(data: object) -> CiTrustRule:
    """Build a `CiTrustRule` from a `CiTrustRuleOut` body; unknown fields are ignored.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    CiTrustRule
        The rule.

    Raises
    ------
    InvalidResponse
        If `data` is not a rule.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; caught just below
        return CiTrustRule(
            id=data["id"],
            issuer=data["issuer"],
            principal=data["principal"],
            environment=data["environment"],
            repository=data["repository"],
            kinds=list(data["kinds"]),
            conditions=dict(data["conditions"]),
            enabled=bool(data["enabled"]),
            created_by=data.get("created_by"),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_by=data.get("updated_by"),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            start_on_deploy_jobs=data.get("start_on_deploy_jobs") is True,
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a CI trust rule")) from None


def _explanation_from_json(data: object) -> CiTrustExplanation:
    """Build a `CiTrustExplanation` from a `CiTrustExplainOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    CiTrustExplanation
        The explanation.

    Raises
    ------
    InvalidResponse
        If `data` is not an explanation.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; caught just below
        return CiTrustExplanation(
            issuer=data["issuer"],
            environment=data["environment"],
            repository=data["repository"],
            verdict=data["verdict"],
            matching=list(data["matching"]),
            rules=[
                CiTrustRuleExplanation(
                    rule_id=item["rule_id"],
                    matches=bool(item["matches"]),
                    reasons=list(item["reasons"]),
                )
                for item in data["rules"]
            ],
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="the explanation")) from None


def _token_from_json(data: object) -> CiToken:
    """Build a `CiToken` from a `CiTokenOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    CiToken
        The token.

    Raises
    ------
    InvalidResponse
        If `data` is not a CI token.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; caught just below
        token = data["token"]
        assert isinstance(token, str) and token.startswith("lakana_ci_")  # nosec B101
        return CiToken(
            token=token,
            expires_at=datetime.fromisoformat(data["expires_at"]),
            principal=data["principal"],
            environment=data["environment"],
            repository=data["repository"],
            kinds=list(data["kinds"]),
            sha=data.get("sha"),
            rule_id=data["rule_id"],
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="the CI token exchange")) from None


def exchange_id_token(
    url: str,
    id_token: str,
    *,
    environment: str,
    repository: str,
    timeout: float = 30.0,
    verify: bool | str = True,
    http_client: httpx.Client | None = None,
) -> CiToken:
    """Exchange a CI job's OIDC ID token for a Lakana CI token; no Lakana credential needed.

    Parameters
    ----------
    url
        The Lakana server's base URL.
    id_token
        The ID token the CI provider issued for the job, for the server's audience.
    environment
        The environment the job deploys to.
    repository
        The registered Lakana repository it deploys.
    timeout
        Request timeout in seconds.
    verify
        TLS verification: `True`, `False` or a CA bundle path.
    http_client
        An existing `httpx.Client` to send through, for example in tests.

    Returns
    -------
    CiToken
        The token; it never appears in a `repr`.

    Raises
    ------
    lakana.errors.AuthenticationError
        If the ID token is refused or was exchanged before (the message says why).
    lakana.errors.PermissionDenied
        If no single enabled CI trust rule accepts it for this environment and repository.
    """
    # No token: the exchange takes none, and the request is sent unauthenticated.
    config = ResolvedConfig(url=url, token="")  # nosec B106 -- an empty token, never sent
    transport = HttpTransport(config, timeout=timeout, verify=verify, http_client=http_client)
    try:
        response = transport.request(
            ROUTES["ci_oidc.exchange"],
            json={"id_token": id_token, "environment": environment, "repository": repository},
            authenticated=False,
        )
    finally:
        transport.close()
    return _token_from_json(_object(response, "the CI token exchange"))


_RuleList = list[CiTrustRule]
"""`list[CiTrustRule]` resolved before `CiTrustRules.list` shadows the builtin."""


class CiTrustRules:
    """The CI trust rules resource of the platform API; instance administrators only.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        *,
        issuer: str,
        principal: str,
        environment: str,
        repository: str,
        conditions: Mapping[str, str],
        kinds: Sequence[str] = ("deploy",),
        start_on_deploy_jobs: bool = False,
    ) -> CiTrustRule:
        """Create an enabled CI trust rule.

        Parameters
        ----------
        issuer
            ``github``, ``gitlab`` or a configured issuer URL.
        principal
            The active service account the CI job acts as; it needs its own `DEPLOY` grant.
        environment
            The environment it may deploy to.
        repository
            The registered repository it may deploy.
        conditions
            Claim name to exact text; must pin ``repository_id`` (GitHub) or ``project_id``
            (GitLab), and ``ref`` or ``environment``.
        kinds
            ``deploy`` and/or ``dry_run``.
        start_on_deploy_jobs
            Let a deploy under the rule start the project's `on_deploy` jobs; sent only when
            true, so a server that predates the setting accepts the request.

        Returns
        -------
        CiTrustRule
            The rule.

        Raises
        ------
        lakana.errors.ValidationError
            If a field breaks the rules (the message says which).
        """
        body: dict[str, Any] = {
            "issuer": issuer,
            "principal": principal,
            "environment": environment,
            "repository": repository,
            "kinds": list(kinds),
            "conditions": dict(conditions),
        }
        if start_on_deploy_jobs:
            body["start_on_deploy_jobs"] = True
        response = self._transport.request(ROUTES["ci_trust_rules.create"], json=body)
        return _rule_from_json(_object(response, "a CI trust rule"))

    def list(self) -> _RuleList:
        """List every CI trust rule, oldest first.

        Returns
        -------
        list of CiTrustRule
            The rules.
        """
        response = self._transport.request(ROUTES["ci_trust_rules.list"])
        return [_rule_from_json(item) for item in _array(response, "the CI trust rules")]

    def get(self, rule_id: str) -> CiTrustRule:
        """Show one CI trust rule.

        Parameters
        ----------
        rule_id
            The rule's id.

        Returns
        -------
        CiTrustRule
            The rule.

        Raises
        ------
        lakana.errors.NotFound
            If there is no such rule.
        """
        response = self._transport.request(
            ROUTES["ci_trust_rules.get"], path_params={"rule_id": rule_id}
        )
        return _rule_from_json(_object(response, "a CI trust rule"))

    def update(
        self,
        rule_id: str,
        *,
        principal: str | None = None,
        kinds: Sequence[str] | None = None,
        conditions: Mapping[str, str] | None = None,
        start_on_deploy_jobs: bool | None = None,
    ) -> CiTrustRule:
        """Change a rule's principal, kinds, conditions or opt-in; the tokens issued under it end.

        Parameters
        ----------
        rule_id
            The rule's id.
        principal
            A new service account.
        kinds
            New kinds.
        conditions
            New conditions, replacing all of the old ones.
        start_on_deploy_jobs
            Whether a deploy under the rule starts the project's `on_deploy` jobs.

        Returns
        -------
        CiTrustRule
            The rule.
        """
        changes: dict[str, Any] = {}
        if start_on_deploy_jobs is not None:
            changes["start_on_deploy_jobs"] = start_on_deploy_jobs
        if principal is not None:
            changes["principal"] = principal
        if kinds is not None:
            changes["kinds"] = list(kinds)
        if conditions is not None:
            changes["conditions"] = dict(conditions)
        response = self._transport.request(
            ROUTES["ci_trust_rules.update"], path_params={"rule_id": rule_id}, json=changes
        )
        return _rule_from_json(_object(response, "a CI trust rule"))

    def enable(self, rule_id: str) -> CiTrustRule:
        """Enable a rule.

        Parameters
        ----------
        rule_id
            The rule's id.

        Returns
        -------
        CiTrustRule
            The rule.
        """
        response = self._transport.request(
            ROUTES["ci_trust_rules.enable"], path_params={"rule_id": rule_id}
        )
        return _rule_from_json(_object(response, "a CI trust rule"))

    def disable(self, rule_id: str) -> CiTrustRule:
        """Disable a rule: it matches no exchange, and the tokens issued under it end.

        Parameters
        ----------
        rule_id
            The rule's id.

        Returns
        -------
        CiTrustRule
            The rule.
        """
        response = self._transport.request(
            ROUTES["ci_trust_rules.disable"], path_params={"rule_id": rule_id}
        )
        return _rule_from_json(_object(response, "a CI trust rule"))

    def delete(self, rule_id: str) -> None:
        """Delete a rule; the tokens issued under it end.

        Parameters
        ----------
        rule_id
            The rule's id.
        """
        self._transport.request(ROUTES["ci_trust_rules.delete"], path_params={"rule_id": rule_id})

    def explain(
        self,
        claims: Mapping[str, Any],
        *,
        environment: str,
        repository: str,
        issuer: str | None = None,
    ) -> CiTrustExplanation:
        """Say which rule an exchange with these ID token claims would match, and why not others.

        Needs no ID token: paste the claims a CI job printed.

        Parameters
        ----------
        claims
            The ID token's claims, decoded.
        environment
            The environment the exchange would ask for.
        repository
            The repository it would ask for.
        issuer
            The issuer; the ``iss`` claim when `None`.

        Returns
        -------
        CiTrustExplanation
            The verdict and every rule's reasons.
        """
        body: dict[str, Any] = {
            "claims": dict(claims),
            "environment": environment,
            "repository": repository,
        }
        if issuer is not None:
            body["issuer"] = issuer
        response = self._transport.request(ROUTES["ci_trust_rules.explain"], json=body)
        return _explanation_from_json(_object(response, "the explanation"))
