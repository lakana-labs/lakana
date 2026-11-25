"""Every step-up route the web interface sends asks for the touch before the request (#32 PR 4).

A write the server guards with `require_step_up` (`STEP_UP_ROUTES`) is sent by the interface
through `guarded` (`ui/src/api/stepUpFirst.ts`): the touch is asked first, and a person linked to
the identity provider confirms there. A write sent with a plain `api.request` reaches the touch
only through the 403 fallback, which a linked person's stale touch turns into a trip to the
provider with no retry; that is right only where asking first would be wrong.

The paths are built from helpers in the interface (`connectionPath`, `tableApiPath`, ...), so the
calls cannot be matched to routes from the source alone. `UI_CALLERS` names, for every route in
`STEP_UP_ROUTES`, the functions of `ui/src/api` that send it, or why the interface sends it not at
all: a new step-up route fails here until it is placed, and each named function must call
`guarded` (or, in `FALLBACK_ONLY`, `api.request`, with the reason). A new function of
`ui/src/api` that sends a route already placed here is caught only once it is listed in
`UI_CALLERS`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_step_up_routes import STEP_UP_ROUTES

API = Path(__file__).resolve().parents[3] / "ui" / "src" / "api"
"""The interface's API modules."""

NOT_SENT = "the interface does not send it"
"""The reason for a route only the command line, the SDK or an access file uses."""

UI_CALLERS: dict[tuple[str, str], tuple[tuple[str, str], ...] | str] = {
    ("POST", "/api/v1/sign-in/cli/approvals"): (("cliSignIn.ts", "approveCli"),),
    ("POST", "/api/v1/sign-in/cli/device/approve"): (("cliSignIn.ts", "approveDevice"),),
    ("POST", "/api/v1/principals/{name}/tokens"): (("tokens.ts", "createOwnToken"),),
    ("POST", "/api/v1/principals"): NOT_SENT,
    ("DELETE", "/api/v1/principals/{name}"): NOT_SENT,
    ("DELETE", "/api/v1/principals/{name}/oidc-link"): (("providerSession.ts", "unlinkProvider"),),
    ("POST", "/api/v1/grants"): (("grants.ts", "createGrant"),),
    ("DELETE", "/api/v1/grants/{grant_id}"): (("grants.ts", "revokeGrant"),),
    ("PUT", "/api/v1/groups/{name}/members/{principal_name}"): (("groups.ts", "addMember"),),
    ("DELETE", "/api/v1/groups/{name}/members/{principal_name}"): (("groups.ts", "removeMember"),),
    ("DELETE", "/api/v1/groups/{name}"): (("groups.ts", "deleteGroup"),),
    ("POST", "/api/v1/access/apply"): NOT_SENT,
    ("POST", "/api/v1/row-filters"): (("policies.ts", "createRowFilter"),),
    ("DELETE", "/api/v1/row-filters/{row_filter_id}"): (("policies.ts", "deleteRowFilter"),),
    ("POST", "/api/v1/column-masks"): (("policies.ts", "createColumnMask"),),
    ("DELETE", "/api/v1/column-masks/{column_mask_id}"): (("policies.ts", "deleteColumnMask"),),
    ("POST", "/api/v1/ci-trust-rules"): NOT_SENT,
    ("PATCH", "/api/v1/ci-trust-rules/{rule_id}"): NOT_SENT,
    ("POST", "/api/v1/ci-trust-rules/{rule_id}/enable"): NOT_SENT,
    ("POST", "/api/v1/ci-trust-rules/{rule_id}/disable"): NOT_SENT,
    ("DELETE", "/api/v1/ci-trust-rules/{rule_id}"): NOT_SENT,
    # One's own "sign out everywhere" (`session.ts` signOutEverywhere) sends the same route, which
    # asks no touch for oneself; only another person's sessions need one.
    ("DELETE", "/api/v1/principals/{name}/sessions"): (("account.ts", "endSessions"),),
    ("PATCH", "/api/v1/settings/sign-in"): (("signInAdmin.ts", "saveSignInSettings"),),
    ("POST", "/api/v1/invites"): (("signInAdmin.ts", "issueInvite"),),
    ("POST", "/api/v1/principals/{name}/recovery-link"): (("signInAdmin.ts", "issueRecoveryLink"),),
    ("POST", "/api/v1/principals/{name}/passkeys/options"): (("passkeys.ts", "addPasskey"),),
    ("DELETE", "/api/v1/principals/{name}/passkeys/{passkey_id}"): (
        ("account.ts", "removePasskey"),
    ),
    ("PUT", "/api/v1/environments/{env}/deployments/{repo}/run-as"): NOT_SENT,
    ("POST", "/api/v1/environments"): (("environments.ts", "createEnvironment"),),
    ("PATCH", "/api/v1/environments/{name}/storage"): (("environments.ts", "changeStorage"),),
    ("PATCH", "/api/v1/environments/{name}"): (("environments.ts", "setProtected"),),
    ("POST", "/api/v1/environments/{name}/purge-rows"): (
        ("environments.ts", "purgeEnvironmentRows"),
    ),
    # Without `purge` the same route asks no touch, and the interface sends it plainly then.
    ("DELETE", "/api/v1/environments/{name}"): (("environments.ts", "deleteEnvironment"),),
    ("POST", "/api/v1/environments/{name}/orphaned-locations/delete"): (
        ("environments.ts", "deleteOrphans"),
    ),
    ("POST", "/api/v1/environments/{environment_name}/secrets"): (("secrets.ts", "createSecret"),),
    ("PATCH", "/api/v1/environments/{environment_name}/secrets/{secret_name}"): (
        ("secrets.ts", "rotateSecret"),
    ),
    ("PUT", "/api/v1/environments/{environment_name}/secrets/{secret_name}/use"): (
        ("secrets.ts", "bindSecret"),
    ),
    ("POST", "/api/v1/environments/{environment_name}/connections"): (
        ("connections.ts", "createConnection"),
    ),
    ("PATCH", "/api/v1/environments/{environment_name}/connections/{connection_name}"): (
        ("connections.ts", "updateConnection"),
    ),
    ("POST", "/api/v1/package-indexes"): NOT_SENT,
    ("PATCH", "/api/v1/package-indexes/{name}"): NOT_SENT,
    ("POST", "/api/v1/repositories"): (("repositories.ts", "createRepository"),),
    ("PATCH", "/api/v1/repositories/{name}"): (("repositories.ts", "updateRepository"),),
    ("POST", "/api/v1/repositories/{name}/deploy-key"): (
        ("repositories.ts", "generateDeployKey"),
        ("repositories.ts", "replaceDeployKey"),
        ("repositories.ts", "makeMissingDeployKeys"),
    ),
    ("DELETE", "/api/v1/environments/{env}/namespaces/{path}"): (
        ("namespaces.ts", "deleteNamespace"),
    ),
    ("DELETE", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}"): (
        ("manage.ts", "dropTable"),
    ),
    ("POST", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/rebuild"): (
        ("manage.ts", "rebuildTable"),
    ),
    ("DELETE", "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}"): (
        ("manage.ts", "dropArtifact"),
    ),
    (
        "DELETE",
        "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/versions/{version}",
    ): (("artifacts.ts", "deleteVersion"),),
}
"""For each step-up route, the functions of `ui/src/api` that send it, or why none does."""

FALLBACK_ONLY: dict[tuple[str, str], str] = {
    ("passkeys.ts", "addPasskey"): (
        "a person adds their first passkey with no touch to give, so none is asked first; when "
        "the server asks for one, api.request's 403 fallback runs the step-up ceremony"
    ),
}
"""Functions that send a step-up route with `api.request` and its 403 fallback, with the reason."""


def _body(module: str, function: str) -> str:
    """Read one exported function's source from an API module.

    Parameters
    ----------
    module
        The file under `ui/src/api`.
    function
        The function's name.

    Returns
    -------
    str
        Its source, from its signature to the closing brace at the start of a line.
    """
    source = (API / module).read_text(encoding="utf-8")
    found = re.search(
        rf"^export (?:async )?function {re.escape(function)}\b.*?^\}}$",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert found is not None, f"{module} exports no function {function}"
    return found.group(0)


def test_every_step_up_route_is_placed() -> None:
    assert set(UI_CALLERS) == set(STEP_UP_ROUTES)


def _code(body: str) -> str:
    """Take the comments out of a function's source.

    Parameters
    ----------
    body
        The source.

    Returns
    -------
    str
        The source without `//` and `/* */` comments.
    """
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL))


GUARDED_CALL = re.compile(r"\bguarded\s*(?:<[^()]*>)?\s*\(")
"""A call of `guarded`, with or without its type argument."""


@pytest.mark.parametrize(
    ("module", "function"),
    sorted(
        {
            caller
            for callers in UI_CALLERS.values()
            if not isinstance(callers, str)
            for caller in callers
        }
    ),
)
def test_each_caller_asks_for_the_touch_first(module: str, function: str) -> None:
    code = _code(_body(module, function))
    if (module, function) in FALLBACK_ONLY:
        assert "api.request" in code and "guarded" not in code
    else:
        assert GUARDED_CALL.search(code), f"{module} {function} does not call guarded"


def test_a_comment_or_a_word_is_no_call() -> None:
    assert not GUARDED_CALL.search(
        _code("// guarded(x)\n/* guarded<T>(y) */ const guardedNot = 1;")
    )
    assert GUARDED_CALL.search(_code('return guarded<{ readonly a: string }>("POST", p);'))


def test_the_fallback_exceptions_are_callers() -> None:
    callers = {
        caller
        for callers in UI_CALLERS.values()
        if not isinstance(callers, str)
        for caller in callers
    }
    assert set(FALLBACK_ONLY) <= callers
