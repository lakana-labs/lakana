"""The SDK's reading of CI trust rule answers and of the CI token exchange."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._tokens import mask_token
from lakana.errors import InvalidResponse
from lakana.resources.ci_trust_rules import CiTrustRules, exchange_id_token

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_RULE = {
    "id": "00000000-0000-0000-0000-000000000001",
    "issuer": "https://token.actions.githubusercontent.com",
    "principal": "deployer",
    "environment": "prod",
    "repository": "analytics",
    "kinds": ["deploy"],
    "conditions": {"repository_id": "1", "ref": "refs/heads/main"},
    "enabled": True,
    "created_by": "admin",
    "created_at": "2026-10-20T12:00:00+00:00",
    "updated_by": None,
    "updated_at": "2026-10-20T12:00:00+00:00",
}
_TOKEN = {
    "token": "lakana_ci_abcdef_secretpart",
    "expires_at": "2026-10-20T13:00:00+00:00",
    "principal": "deployer",
    "environment": "prod",
    "repository": "analytics",
    "kinds": ["deploy"],
    "sha": "0" * 40,
    "rule_id": _RULE["id"],
}


def _client(body: Any, seen: list[httpx.Request] | None = None) -> httpx.Client:  # ruff: ignore[any-type]
    """Build an `httpx.Client` that always answers with `body`.

    Parameters
    ----------
    body
        The JSON the fake server sends, or bytes for a body that is not JSON.
    seen
        Collects each request, when given.

    Returns
    -------
    httpx.Client
        The client.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer one request.

        Parameters
        ----------
        request
            The request.

        Returns
        -------
        httpx.Response
            The answer.
        """
        if seen is not None:
            seen.append(request)
        if isinstance(body, bytes):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)


def _rules(body: Any, seen: list[httpx.Request] | None = None) -> CiTrustRules:  # ruff: ignore[any-type]
    """Build the resource over a fake server.

    Parameters
    ----------
    body
        What it answers.
    seen
        Collects each request, when given.

    Returns
    -------
    CiTrustRules
        The resource.
    """
    return CiTrustRules(HttpTransport(_CONFIG, http_client=_client(body, seen)))


def test_a_rule_is_read_and_an_unknown_field_ignored() -> None:
    rule = _rules({**_RULE, "added_later": 1}).get(str(_RULE["id"]))
    assert (rule.principal, rule.kinds, rule.updated_by) == ("deployer", ["deploy"], None)
    assert [r.id for r in _rules([_RULE]).list()] == [_RULE["id"]]


@pytest.mark.parametrize("field", ["id", "issuer", "kinds", "conditions", "created_at"])
def test_a_rule_without_a_required_field_is_an_invalid_response(field: str) -> None:
    answer = dict(_RULE)
    del answer[field]
    with pytest.raises(InvalidResponse, match="a CI trust rule"):
        _rules(answer).get("x")


@pytest.mark.parametrize("body", [b"<html>", b'"text"', b"[1]"])
def test_an_answer_that_is_not_a_rule_is_an_invalid_response(body: bytes) -> None:
    with pytest.raises(InvalidResponse):
        _rules(body).get("x")
    with pytest.raises(InvalidResponse):
        _rules(body).list()


def test_update_sends_only_the_fields_given() -> None:
    seen: list[httpx.Request] = []
    _rules(_RULE, seen).update("x", kinds=["dry_run"])
    assert json.loads(seen[0].content) == {"kinds": ["dry_run"]}


def test_explain_sends_the_issuer_only_when_given_and_reads_the_report() -> None:
    report = {
        "issuer": _RULE["issuer"],
        "environment": "prod",
        "repository": "analytics",
        "verdict": "refused: no enabled rule for this issuer, environment and repository matches",
        "matching": [],
        "rules": [{"rule_id": _RULE["id"], "matches": False, "reasons": ["the rule is disabled"]}],
    }
    seen: list[httpx.Request] = []
    explained = _rules(report, seen).explain({"iss": "x"}, environment="prod", repository="a")
    assert explained.rules[0].reasons == ["the rule is disabled"]
    assert "issuer" not in json.loads(seen[0].content)
    _rules(report, seen).explain({}, environment="prod", repository="a", issuer="github")
    assert json.loads(seen[1].content)["issuer"] == "github"
    with pytest.raises(InvalidResponse, match="the explanation"):
        _rules({**report, "rules": [{}]}).explain({}, environment="p", repository="r")


def test_the_exchange_sends_no_credential_and_hides_the_token() -> None:
    seen: list[httpx.Request] = []
    issued = exchange_id_token(
        _CONFIG.url,
        "eyJ.id.token",
        environment="prod",
        repository="analytics",
        http_client=_client(_TOKEN, seen),
    )
    assert "authorization" not in seen[0].headers
    assert json.loads(seen[0].content) == {
        "id_token": "eyJ.id.token",
        "environment": "prod",
        "repository": "analytics",
    }
    assert issued.token == _TOKEN["token"]
    assert "secretpart" not in repr(issued)
    assert mask_token(issued.token) == "lakana_ci_abcdef_***"


@pytest.mark.parametrize(
    "change",
    [{"token": "lakana_pat_abc_x"}, {"token": None}, {"expires_at": "soon"}, {"kinds": 1}],
)
def test_an_odd_exchange_answer_is_an_invalid_response(change: dict[str, object]) -> None:
    with pytest.raises(InvalidResponse, match="CI token exchange"):
        exchange_id_token(
            _CONFIG.url,
            "t",
            environment="prod",
            repository="analytics",
            http_client=_client({**_TOKEN, **change}),
        )


def test_start_on_deploy_jobs_is_read_sent_and_off_from_an_older_server() -> None:
    assert _rules(_RULE).get("x").start_on_deploy_jobs is False
    opted = _rules({**_RULE, "start_on_deploy_jobs": True}).get("x")
    assert opted.start_on_deploy_jobs is True
    seen: list[httpx.Request] = []
    _rules(_RULE, seen).update("x", start_on_deploy_jobs=False)
    assert json.loads(seen[0].content) == {"start_on_deploy_jobs": False}
    _rules(_RULE, seen).create(
        issuer="github",
        principal="deployer",
        environment="prod",
        repository="analytics",
        conditions={"repository_id": "1", "ref": "refs/heads/main"},
        start_on_deploy_jobs=True,
    )
    assert json.loads(seen[1].content)["start_on_deploy_jobs"] is True
    _rules(_RULE, seen).create(
        issuer="github",
        principal="deployer",
        environment="prod",
        repository="analytics",
        conditions={"repository_id": "1", "ref": "refs/heads/main"},
    )
    assert "start_on_deploy_jobs" not in json.loads(seen[2].content)
