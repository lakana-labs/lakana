"""`describe_settings`: every setting once, in order, with no secret value."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr, create_model

from lakana_server.configuration import (
    PLAIN_SECRETS,
    ConfiguredSetting,
    describe_settings,
    setting_names,
)
from lakana_server.settings import Settings

_REPO = Path(__file__).resolve().parents[3]
_LOOKS_SECRET = re.compile(
    r"(?i)(SECRET|PASSWORD|PASSWD|KEY|TOKEN|CREDENTIAL|DSN|URL|PROXY|AUTH|COOKIE|PRIVATE"
    r"|WEBHOOK|SENTRY)"
)
REVIEWED_NOT_SECRET: dict[str, str] = {
    "LAKANA_BOOTSTRAP_PAT_KEY_ID": "the public half of the bootstrap token",
    "LAKANA_BOOTSTRAP_TOKEN_FILE": "a path, not a credential",
    "LAKANA_UI__PUBLIC_URL": "the address people open; carries no user name (validated)",
    "LAKANA_RUN__SERVER_URL": "the address workers reach the server at",
    "LAKANA_RUN__NO_PROXY": "a list of host names that bypass the proxy",
    "LAKANA_TASK_TOKENS__MAX_TTL_SECONDS": "a number of seconds",
    "LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE": "a rate, matched by AUTH in the group name",
    "LAKANA_CI_OIDC__MAX_TOKEN_AGE_SECONDS": "a number of seconds",
    "LAKANA_CI_OIDC__TOKEN_TTL_SECONDS": "a number of seconds",
    "LAKANA_SECRETS__ACCEPT_FORMAT_1": "a switch for reading the old key format, not a key",
    "LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE": "a rate",
    "LAKANA_UI__PASSKEY_OPTIONS_PER_NETWORK_PER_MINUTE": "a rate",
    "LAKANA_TRUSTED_PROXIES": "addresses of the reverse proxies, not credentials",
}


@pytest.fixture(autouse=True)
def _database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give `Settings()` the one setting it requires."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///unused.db")


def _by_name(settings: Settings) -> dict[str, ConfiguredSetting]:
    return {item.name: item for item in describe_settings(settings)}


def test_every_secret_looking_setting_is_masked_or_reviewed() -> None:
    described = _by_name(Settings())
    assert set(setting_names(Settings)) == set(described)
    looking = {name for name in setting_names(Settings) if _LOOKS_SECRET.search(name)}
    unreviewed = sorted(
        name for name in looking if not described[name].secret and name not in REVIEWED_NOT_SECRET
    )
    assert unreviewed == [], f"decide for each: mask it (SecretStr) or review it: {unreviewed}"
    stale = sorted(name for name in REVIEWED_NOT_SECRET if name not in described)
    assert stale == [], f"no longer a setting: {stale}"
    wrong = sorted(name for name in REVIEWED_NOT_SECRET if described[name].secret)
    assert wrong == [], f"masked, so not 'not secret': {wrong}"


def test_secret_str_settings_and_the_database_url_are_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "LAKANA_DATABASE__URL",
        "LAKANA_SECRETS__MASTER_KEY",
        "LAKANA_BOOTSTRAP_PAT_SECRET",
        "LAKANA_RUN__INDEX_URL",
        "LAKANA_RUN__HTTPS_PROXY",
        "LAKANA_CI_OIDC__PROXY",
    ):
        monkeypatch.setenv(name, "https://u:sentinel@h.example/x")
    described = _by_name(Settings())
    for name in (
        "LAKANA_DATABASE__URL",
        "LAKANA_SECRETS__MASTER_KEY",
        "LAKANA_BOOTSTRAP_PAT_SECRET",
        "LAKANA_RUN__INDEX_URL",
        "LAKANA_RUN__HTTPS_PROXY",
        "LAKANA_CI_OIDC__PROXY",
    ):
        assert described[name].secret, name
        assert described[name].value is None, name
        assert described[name].has_value, name
    assert "LAKANA_DATABASE__URL" in PLAIN_SECRETS
    assert "sentinel" not in repr(describe_settings(Settings()))


def test_source_says_set_for_an_explicit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKANA_PORT", "8000")
    monkeypatch.setenv("LAKANA_RUN__LEASE_SECONDS", "77")
    described = _by_name(Settings())
    assert described["LAKANA_PORT"].source == "set"
    assert described["LAKANA_PORT"].value == 8000
    assert described["LAKANA_HOST"].source == "default"
    assert described["LAKANA_RUN__LEASE_SECONDS"].source == "set"
    assert described["LAKANA_RUN__MAX_RETRIES"].source == "default"


def test_names_are_the_environment_variables_in_declaration_order() -> None:
    names = [item.name for item in describe_settings(Settings())]
    assert names[0] == "LAKANA_DATABASE__URL"
    assert "LAKANA_UI__PUBLIC_URL" in names
    assert len(names) == len(set(names))


def test_every_described_setting_is_in_the_settings_reference() -> None:
    reference = (_REPO / "docs/site/reference/settings.md").read_text()
    missing = [item.name for item in describe_settings(Settings()) if item.name not in reference]
    assert missing == []


def test_values_are_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKANA_BOOTSTRAP_TOKEN_FILE", "/run/tok")
    monkeypatch.setenv("LAKANA_REPO_ROOTS", '["/srv/a"]')
    described = _by_name(Settings())
    assert described["LAKANA_BOOTSTRAP_TOKEN_FILE"].value == "/run/tok"
    assert described["LAKANA_REPO_ROOTS"].value == ["/srv/a"]
    assert described["LAKANA_UI__PUBLIC_URL"].value is None
    assert described["LAKANA_UI__PUBLIC_URL"].has_value is False


class _Smtp(BaseModel):
    password: str = ""


class _Scalars(BaseModel):
    plain: str = ""
    token: SecretStr | None = None
    many: list[str] = []


def test_the_walk_reads_the_classes_so_an_unset_nested_model_is_seen() -> None:
    """Names come from the annotations of every nested model class, not from one instance."""

    class Root(BaseModel):
        nested: _Scalars = _Scalars()
        port: int = 1

    assert setting_names(Root) == [
        "LAKANA_NESTED__PLAIN",
        "LAKANA_NESTED__TOKEN",
        "LAKANA_NESTED__MANY",
        "LAKANA_PORT",
    ]


@pytest.mark.parametrize(
    "annotation",
    [_Smtp | None, list[_Smtp], dict[str, _Smtp], tuple[int, _Smtp], object, bytes],
    ids=[
        "optional-model",
        "list-of-models",
        "dict-of-models",
        "tuple-with-model",
        "object",
        "bytes",
    ],
)
def test_a_field_that_is_not_a_plain_model_or_a_known_scalar_is_refused(
    annotation: object,
) -> None:
    """An optional or listed model, or an unknown type, could hide a credential: refuse it."""
    root = create_model(
        "Root",
        __config__=ConfigDict(arbitrary_types_allowed=True),
        odd=(cast(Any, annotation), None),
    )

    with pytest.raises(TypeError, match="LAKANA_ODD"):
        setting_names(root)
    with pytest.raises(TypeError, match="LAKANA_ODD"):
        describe_settings(root())
