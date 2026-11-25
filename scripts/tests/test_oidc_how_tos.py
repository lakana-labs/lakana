"""The settings and the client the identity-provider how-tos show are ones the server accepts.

"Sign in through Keycloak" and "Sign in through Microsoft Entra ID" show `.env` lines, the lines
that pass them to the Compose bundle's server, and a Keycloak client to import. The pages sync
those blocks from `docs/examples/oidc-*`. This test fails when a line names a setting that
`OidcSignInConfig` does not have or a value it refuses, when a setting of the `.env` lines is not
passed to the server, when a variable the snippet passes does not exist in the bundle's
`compose.yaml` service, or when the client's redirect URI, flow or PKCE method differs from what
the server sends.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from lakana_core.oidc.config import OidcSignInConfig
from lakana_server.oidc_flow import CALLBACK_PATH

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "docs/examples"
HOW_TO = ROOT / "docs/site/how-to"
COMPOSE = ROOT / "packages/lakana/src/lakana/cli/deploy/compose.yaml"
PREFIX = "LAKANA_OIDC__"
ENV_FILES = ("oidc-keycloak.env", "oidc-entra-id.env")
COMPOSE_SNIPPETS = ("oidc-compose.yaml", "oidc-compose-internal.yaml")
PAGES = ("sign-in-with-keycloak.md", "sign-in-with-entra-id.md")

_INTERPOLATED = re.compile(r"^\$\{([A-Z0-9_]+)\}$")
"""A Compose value that is exactly one variable of `.env`: `${NAME}`."""


def _env(name: str) -> dict[str, str]:
    """Read an example `.env` file, comments and blank lines left out.

    Parameters
    ----------
    name
        The file under `docs/examples`.

    Returns
    -------
    dict of str to str
        Each variable with its value.
    """
    pairs: dict[str, str] = {}
    for line in (EXAMPLES / name).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        pairs[key] = value
    return pairs


def _passed(name: str) -> dict[str, str]:
    """Read the `environment` of the server in a Compose snippet.

    Parameters
    ----------
    name
        The file under `docs/examples`.

    Returns
    -------
    dict of str to str
        Each variable the server gets, with its value as written.
    """
    snippet = yaml.safe_load((EXAMPLES / name).read_text())
    return dict(snippet["services"]["server"]["environment"])


@pytest.mark.parametrize("name", ENV_FILES)
def test_the_env_lines_are_settings_the_server_accepts(name: str) -> None:
    pairs = _env(name)
    assert pairs, name
    assert all(key.startswith(PREFIX) for key in pairs), sorted(pairs)
    fields = {key.removeprefix(PREFIX).lower(): value for key, value in pairs.items()}
    assert set(fields) <= set(OidcSignInConfig.model_fields), sorted(fields)
    config = OidcSignInConfig.model_validate(fields)
    assert config.enabled
    assert config.client_id
    assert config.client_secret is not None


def test_the_entra_issuer_names_the_tenant_and_v2() -> None:
    issuer = _env("oidc-entra-id.env")[f"{PREFIX}ISSUER"]
    assert re.fullmatch(
        r"https://login\.microsoftonline\.com/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}/v2\.0",
        issuer,
    ), issuer


def test_every_env_line_is_passed_to_the_server() -> None:
    shown = {key for name in ENV_FILES for key in _env(name)}
    passed: dict[str, str] = {}
    for name in COMPOSE_SNIPPETS:
        passed |= _passed(name)
    assert shown == set(passed), (sorted(shown), sorted(passed))
    for key, value in passed.items():
        match = _INTERPOLATED.match(str(value))
        assert match is not None and match.group(1) == key, (key, value)


def test_the_snippets_fit_the_bundle_s_server_service() -> None:
    bundle = yaml.safe_load(COMPOSE.read_text())
    server = bundle["services"]["server"]
    snippet = yaml.safe_load((EXAMPLES / "oidc-compose-internal.yaml").read_text())
    volumes = snippet["services"]["server"]["volumes"]
    # The snippet repeats the server's own volume as unchanged and adds the CA file read-only.
    assert volumes[: len(server["volumes"])] == server["volumes"], volumes
    added = volumes[len(server["volumes"]) :]
    assert added == ["./certs/corp-ca.pem:/certs/corp-ca.pem:ro"], added
    ca = _env("oidc-keycloak.env")[f"{PREFIX}CA_BUNDLE"]
    assert added[0].split(":")[1] == ca
    # No setting of the bundle's server is named again by the snippets.
    for name in COMPOSE_SNIPPETS:
        assert not set(_passed(name)) & set(server["environment"]), name


def test_the_keycloak_client_matches_what_the_server_sends() -> None:
    client = json.loads((EXAMPLES / "oidc-keycloak-client.json").read_text())
    assert client["clientId"] == _env("oidc-keycloak.env")[f"{PREFIX}CLIENT_ID"]
    assert client["protocol"] == "openid-connect"
    assert client["publicClient"] is False
    assert client["clientAuthenticatorType"] == "client-secret"
    assert client["standardFlowEnabled"] is True
    for other in ("implicitFlowEnabled", "directAccessGrantsEnabled", "serviceAccountsEnabled"):
        assert client[other] is False, other
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == [client["rootUrl"] + CALLBACK_PATH]


@pytest.mark.parametrize("page", PAGES)
def test_each_how_to_names_the_redirect_uri(page: str) -> None:
    text = (HOW_TO / page).read_text()
    assert f"https://lakana.example.com{CALLBACK_PATH}" in text, page
