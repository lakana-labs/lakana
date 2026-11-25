"""The settings and the client the identity-provider how-tos show are ones the server accepts.

"Sign in through Keycloak" and "Sign in through Microsoft Entra ID" show `.env` lines, the lines
that pass them to the Compose bundle's server, and a Keycloak client to import. The pages sync
those blocks from `docs/examples/oidc-*`. This test fails when a line names a setting that
`OidcSignInConfig` does not have or a value it refuses, when a setting of the `.env` lines is not
passed to the server, when a variable the snippet passes does not exist in the bundle's
`compose.yaml` service, or when the client's redirect URI, flow or PKCE method differs from what
the server sends, or from the client of the realm that
`packages/lakana-server/tests/test_oidc_keycloak_integration.py` signs in through.
"""

from __future__ import annotations

import json
import os
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
REALM = ROOT / "packages/lakana-server/tests/keycloak/lakana-realm.json"
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


def test_the_how_to_s_client_is_the_tested_realm_s() -> None:
    shown = json.loads((EXAMPLES / "oidc-keycloak-client.json").read_text())
    realm = json.loads(REALM.read_text())
    [tested] = [c for c in realm["clients"] if c["clientId"] == shown["clientId"]]
    # Every setting is the same, but the address: the test's Lakana is at its own.
    for key, value in shown.items():
        if key == "rootUrl":
            continue
        if key == "redirectUris":
            assert [u.removeprefix(tested["rootUrl"]) for u in tested[key]] == [
                u.removeprefix(shown["rootUrl"]) for u in value
            ]
            continue
        assert tested[key] == value, key
    assert tested["redirectUris"] == [tested["rootUrl"] + CALLBACK_PATH]


def _compose_environment(env: dict[str, str]) -> dict[str, str]:
    """Name what the bundle's `server` gets with the snippets' lines and an `.env`.

    Compose replaces `${NAME}` with the `.env` value, or with an empty string when `.env` has
    no line for it.

    Parameters
    ----------
    env
        The `LAKANA_OIDC__` lines of `.env`.

    Returns
    -------
    dict of str to str
        Each variable of both snippets with the value the server sees.
    """
    passed: dict[str, str] = {}
    for name in COMPOSE_SNIPPETS:
        for key, value in _passed(name).items():
            match = _INTERPOLATED.match(str(value))
            assert match is not None, (key, value)
            passed[key] = env.get(match.group(1), "")
    return passed


def _server_starts(monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]) -> bool:
    """Say whether the server's settings accept an environment.

    Parameters
    ----------
    monkeypatch
        Sets the variables for this test only.
    environment
        The `LAKANA_OIDC__` variables the server gets; absent ones are unset.

    Returns
    -------
    bool
        Whether `lakana_server.settings.Settings` validates.
    """
    from pydantic import ValidationError

    from lakana_server.settings import Settings

    for key in [k for k in os.environ if k.startswith("LAKANA_")]:
        monkeypatch.delenv(key)
    base = {
        "LAKANA_DATABASE__URL": "sqlite:///lakana.db",
        "LAKANA_UI__ENABLED": "true",
        "LAKANA_UI__PUBLIC_URL": "https://lakana.example.com",
    }
    for key, value in {**base, **environment}.items():
        monkeypatch.setenv(key, value)
    try:
        Settings(_env_file=None)
    except ValidationError:
        return False
    return True


def test_the_documented_lines_start_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    keycloak = _env("oidc-keycloak.env")
    entra = _env("oidc-entra-id.env")
    # Keycloak with every line (the internal ones too), and Entra ID with the four lines of
    # `oidc-compose.yaml` only: the page has no internal lines for it.
    assert _server_starts(monkeypatch, _compose_environment(keycloak))
    four = set(_passed("oidc-compose.yaml"))
    assert _server_starts(
        monkeypatch, {k: v for k, v in _compose_environment(entra).items() if k in four}
    )


@pytest.mark.parametrize("left_out", ["LAKANA_OIDC__DISPLAY_NAME", "LAKANA_OIDC__ISSUER"])
def test_a_setting_left_out_of_env_only_stops_the_server(
    monkeypatch: pytest.MonkeyPatch, left_out: str
) -> None:
    env = {k: v for k, v in _env("oidc-keycloak.env").items() if k != left_out}
    passed = _compose_environment(env)
    # The line still in compose.yaml passes an empty value, which the server refuses ...
    assert passed[left_out] == ""
    assert not _server_starts(monkeypatch, passed)
    # ... and with its line removed from compose.yaml too, the server starts.
    if left_out == "LAKANA_OIDC__ISSUER":
        passed = {}  # turning single sign-on off removes every LAKANA_OIDC__ line
    else:
        passed = {k: v for k, v in passed.items() if k != left_out}
    assert _server_starts(monkeypatch, passed)


def test_only_an_empty_allow_hosts_starts_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from lakana_core.oidc.config import OidcSignInConfig

    full = _compose_environment(_env("oidc-keycloak.env"))
    accepted = set()
    for field in OidcSignInConfig.model_fields:
        key = f"{PREFIX}{field.upper()}"
        if _server_starts(monkeypatch, {**full, key: ""}):
            accepted.add(key)
    # The pages name the one exception.
    assert accepted == {f"{PREFIX}ALLOW_HOSTS"}


def test_the_pages_say_to_leave_a_setting_out_of_both_files() -> None:
    rule = (
        "the server does not start with an empty `LAKANA_OIDC__` setting, except "
        "`LAKANA_OIDC__ALLOW_HOSTS`, where empty allows no internal host. To leave a setting "
        "out, remove its line from both files."
    )
    for page in PAGES:
        assert rule in " ".join((HOW_TO / page).read_text().split()), page
    guide = " ".join((HOW_TO / "bring-your-team-onto-single-sign-on.md").read_text().split())
    assert "means removing every `LAKANA_OIDC__` line from the `server` service's" in guide
    assert (
        "the server does not start with any empty `LAKANA_OIDC__` setting but "
        "`LAKANA_OIDC__ALLOW_HOSTS`"
    ) in guide


UI = ROOT / "ui/src"
_ROW = re.compile(r"^\| `([a-z_]+)` \| (.+?) \| ", re.MULTILINE)
"""A row of the Keycloak how-to's refusal table: the reason, then what the person reads."""


def _ui_text(file: str, key: str, name: str = "Keycloak") -> str:
    """Read a string of the interface, with the provider's name filled in.

    Parameters
    ----------
    file
        The locale file under `ui/src`.
    key
        The dotted key.
    name
        The provider's name, for ``<name/>`` and ``{{provider}}``.

    Returns
    -------
    str
        The string as the interface shows it.
    """
    value: object = json.loads((UI / file).read_text())
    for part in key.split("."):
        assert isinstance(value, dict), (file, key)
        value = value[part]
    assert isinstance(value, str), (file, key)
    return value.replace("<name/>", name).replace("{{provider}}", name)


def _page(name: str) -> str:
    """Read a how-to with its line breaks as spaces.

    Parameters
    ----------
    name
        The file under `docs/site/how-to`.

    Returns
    -------
    str
        The text.
    """
    return " ".join((HOW_TO / name).read_text().split())


def test_the_refusal_table_quotes_the_interface() -> None:
    refused = json.loads((UI / "sign-in/oidc.en.json").read_text())["refused"]
    reasons = {k for k in refused if k not in ("title", "confirmTitle", "tryAgain")}
    rows = dict(_ROW.findall((HOW_TO / "sign-in-with-keycloak.md").read_text()))
    rows.pop("reason")  # the table's head
    assert set(rows) == reasons
    for reason, sentence in rows.items():
        assert sentence == refused[reason], reason


QUOTED = [
    ("sign-in-with-keycloak.md", "settings/configuration.en.json", "secretSet", "Keycloak"),
    ("sign-in-with-keycloak.md", "settings/signIn.en.json", "provider.title", "Keycloak"),
    ("sign-in-with-keycloak.md", "settings/signIn.en.json", "provider.name", "Keycloak"),
    ("sign-in-with-keycloak.md", "settings/signIn.en.json", "provider.issuer", "Keycloak"),
    ("sign-in-with-keycloak.md", "settings/signIn.en.json", "provider.linked", "Keycloak"),
    ("sign-in-with-keycloak.md", "settings/signIn.en.json", "provider.unlinked", "Keycloak"),
    ("sign-in-with-keycloak.md", "settings/signIn.en.json", "window.label", "Keycloak"),
    ("sign-in-with-keycloak.md", "settings/signIn.en.json", "provider.oneLeft", "Keycloak"),
    ("sign-in-with-keycloak.md", "i18n/en.json", "signIn.oidc.provider", "Keycloak"),
    ("sign-in-with-keycloak.md", "i18n/en.json", "signIn.oidc.passkey", "Keycloak"),
    ("sign-in-with-keycloak.md", "i18n/en.json", "signIn.oidc.passkeyHint", "Keycloak"),
    ("sign-in-with-entra-id.md", "sign-in/oidc.en.json", "refused.auth_too_old", "Microsoft"),
    ("sign-in-with-entra-id.md", "sign-in/oidc.en.json", "refused.not_linked", "Microsoft"),
    ("sign-in-with-entra-id.md", "sign-in/oidc.en.json", "section.link", "Microsoft"),
    ("sign-in-with-entra-id.md", "i18n/en.json", "signIn.oidc.provider", "Microsoft"),
    (
        "bring-your-team-onto-single-sign-on.md",
        "i18n/en.json",
        "notices.confirmed.step_up",
        "Keycloak",
    ),
    (
        "bring-your-team-onto-single-sign-on.md",
        "sign-in/oidc.en.json",
        "section.unlinked",
        "Keycloak",
    ),
    ("bring-your-team-onto-single-sign-on.md", "sign-in/oidc.en.json", "section.link", "Keycloak"),
    (
        "bring-your-team-onto-single-sign-on.md",
        "sign-in/oidc.en.json",
        "section.linkTitle",
        "Keycloak",
    ),
    (
        "bring-your-team-onto-single-sign-on.md",
        "sign-in/oidc.en.json",
        "section.linked",
        "Keycloak",
    ),
    (
        "bring-your-team-onto-single-sign-on.md",
        "sign-in/oidc.en.json",
        "refused.not_linked",
        "Keycloak",
    ),
    (
        "bring-your-team-onto-single-sign-on.md",
        "access/access.en.json",
        "provider.title",
        "Keycloak",
    ),
    ("bring-your-team-onto-single-sign-on.md", "i18n/en.json", "link.withProvider", "Keycloak"),
    ("bring-your-team-onto-single-sign-on.md", "i18n/en.json", "link.providerOnly", "Keycloak"),
]
"""Strings of the interface that a how-to quotes: the page, the locale file, the key, the name."""


@pytest.mark.parametrize(("page", "file", "key", "name"), QUOTED)
def test_a_quoted_interface_string_is_the_interface_s(
    page: str, file: str, key: str, name: str
) -> None:
    assert _ui_text(file, key, name) in _page(page)


def test_the_other_quoted_sentences_are_the_code_s() -> None:
    from lakana_core.identity.sessions import TOKEN_WINDOW_MESSAGE

    guide = _page("bring-your-team-onto-single-sign-on.md")
    assert TOKEN_WINDOW_MESSAGE.format(days=30) in guide
    notice = _ui_text("i18n/en.json", "notices.oidc_unlinked.other")
    assert notice.split("{{date}}")[0].replace("{{subject}}", "kim").rstrip() in guide
    warning = _ui_text("settings/signIn.en.json", "provider.warningWord")
    oneleft = _ui_text("settings/signIn.en.json", "provider.oneLeft")
    assert f"{warning} {oneleft}" in _page("sign-in-with-keycloak.md")


@pytest.mark.parametrize(
    "page",
    [
        "sign-in-with-keycloak.md",
        "bring-your-team-onto-single-sign-on.md",
        "../explanation/upgrades.md",
    ],
)
def test_the_advice_for_tokens_with_sso_off_says_what_an_unlink_does(page: str) -> None:
    """Review of #814: an unlink revokes the tokens and deletes the passkeys, it keeps none.

    With single sign-on off a stale link's tokens stop at the window and only an unlink frees
    them; the page must say that it takes the passkeys and tokens, that a recovery link and new
    tokens follow, and how a sole administrator, who cannot unlink themselves, gets there.
    """
    text = " ".join((HOW_TO / page).read_text().split())
    assert "keep using tokens" not in text
    assert r"deletes their passkeys and revokes their tokens" in text, page
    assert "recovery link" in text and "which have no window" in text, page
    assert "lakana principal unlink-provider NAME" in text and "bootstrap token" in text, page
