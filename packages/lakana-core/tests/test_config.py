"""Tests for configuration models."""

import logging
import math
import os
from pathlib import Path
from unittest import mock

import pydantic
import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from lakana_core.config import (
    ArtifactsConfig,
    DatabaseConfig,
    GitConfig,
    LogConfig,
    QueryLimitsConfig,
    RunConfig,
    Settings,
    StorageConfig,
    WebhookConfig,
    WorkerConfig,
    parse_task_memory,
    warn_about_tokens_in_url_paths,
)
from lakana_core.identifiers import shown


def test_storage_config_defaults_to_no_options() -> None:
    """A storage config with no options defaults to an empty mapping."""
    config = StorageConfig(scheme="file", root="file:///var/lakana/dev")
    assert config.options == {}


def test_storage_config_rejects_unknown_scheme() -> None:
    """Only the supported storage schemes are accepted."""
    with pytest.raises(PydanticValidationError):
        StorageConfig.model_validate({"scheme": "ftp", "root": "ftp://example"})


def test_database_config_defaults_echo_to_false() -> None:
    """A database config defaults to not logging emitted SQL."""
    config = DatabaseConfig(url="sqlite:///./lakana.db")
    assert config.echo is False


def test_settings_reads_database_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings resolves the nested database URL from LAKANA_DATABASE__URL."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///./lakana.db")
    settings = Settings()
    assert settings.database.url == "sqlite:///./lakana.db"
    assert settings.bootstrap_admin == "admin"


def test_settings_requires_database_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings refuses to build without a database URL configured."""
    monkeypatch.delenv("LAKANA_DATABASE__URL", raising=False)
    monkeypatch.delenv("LAKANA_DATABASE__ECHO", raising=False)
    with pytest.raises(PydanticValidationError):
        Settings()


def test_secrets_master_key_defaults_to_none() -> None:
    """With no env var set, master_key is None, not an error."""
    with mock.patch.dict(os.environ, {"LAKANA_DATABASE__URL": "sqlite://"}, clear=True):
        settings = Settings()
    assert settings.secrets.master_key is None


def test_secrets_master_key_reads_from_the_nested_env_var() -> None:
    """LAKANA_SECRETS__MASTER_KEY populates settings.secrets.master_key."""
    env = {"LAKANA_DATABASE__URL": "sqlite://", "LAKANA_SECRETS__MASTER_KEY": "abc123"}
    with mock.patch.dict(os.environ, env, clear=True):
        settings = Settings()
    assert settings.secrets.master_key == "abc123"


def test_accept_format_1_is_off_by_default() -> None:
    """Old-format secrets are refused unless an operator turns the setting on."""
    with mock.patch.dict(os.environ, {"LAKANA_DATABASE__URL": "sqlite://"}, clear=True):
        settings = Settings()
    assert settings.secrets.accept_format_1 is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("false", False), ("False", False), ("0", False)],
)
def test_accept_format_1_reads_from_the_nested_env_var(text: str, expected: bool) -> None:
    """LAKANA_SECRETS__ACCEPT_FORMAT_1 populates settings.secrets.accept_format_1."""
    env = {"LAKANA_DATABASE__URL": "sqlite://", "LAKANA_SECRETS__ACCEPT_FORMAT_1": text}
    with mock.patch.dict(os.environ, env, clear=True):
        settings = Settings()
    assert settings.secrets.accept_format_1 is expected


@pytest.mark.parametrize("text", ["", "yes", "on", "2", "tru", "maybe"])
def test_accept_format_1_refuses_anything_but_true_or_false(text: str) -> None:
    """A typo is a start-up error naming the setting and the accepted words, never the value."""
    env = {"LAKANA_DATABASE__URL": "sqlite://", "LAKANA_SECRETS__ACCEPT_FORMAT_1": text}
    with mock.patch.dict(os.environ, env, clear=True), pytest.raises(PydanticValidationError) as e:
        Settings()
    message = str(e.value)
    assert "accept_format_1" in message and "must be true or false" in message
    assert "LAKANA_SECRETS__ACCEPT_FORMAT_1=true" in message


def test_accept_format_1_refuses_a_non_text_value() -> None:
    """A value that is neither a bool nor a string is refused the same way."""
    from lakana_core.config import SecretsConfig

    with pytest.raises(PydanticValidationError, match="must be true or false"):
        SecretsConfig(accept_format_1=2)


def test_secrets_config_repr_shows_the_format_1_setting() -> None:
    """The repr says whether old-format secrets are read."""
    from lakana_core.config import SecretsConfig

    assert "accept_format_1=True" in repr(SecretsConfig(accept_format_1=True))
    assert "accept_format_1=False" in repr(SecretsConfig())


def test_query_limits_default_and_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults apply, and `LAKANA_QUERY_LIMITS__*` overrides them."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().query_limits == QueryLimitsConfig()
    assert QueryLimitsConfig().max_result_rows == 1_000_000
    assert QueryLimitsConfig().max_result_bytes == 256 * 1024 * 1024
    assert QueryLimitsConfig().memory_limit == "1GB"
    assert QueryLimitsConfig().timeout_seconds == 900
    assert QueryLimitsConfig().kill_grace_seconds == 10
    assert QueryLimitsConfig().threads == 4
    assert QueryLimitsConfig().max_temp_directory_size == "10GB"

    monkeypatch.setenv("LAKANA_QUERY_LIMITS__MAX_RESULT_ROWS", "10")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__MEMORY_LIMIT", "512MB")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS", "2.5")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__THREADS", "2")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__MAX_TEMP_DIRECTORY_SIZE", "1GB")
    limits = Settings().query_limits
    assert limits.max_result_rows == 10
    assert limits.memory_limit == "512MB"
    assert limits.timeout_seconds == pytest.approx(2.5)
    assert limits.kill_grace_seconds == pytest.approx(2.5)
    assert limits.threads == 2
    assert limits.max_temp_directory_size == "1GB"


def test_a_kill_grace_of_zero_is_allowed_and_a_negative_one_is_refused() -> None:
    assert QueryLimitsConfig(kill_grace_seconds=0).kill_grace_seconds == 0
    with pytest.raises(pydantic.ValidationError):
        QueryLimitsConfig(kill_grace_seconds=-1)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_a_kill_grace_must_be_finite(value: float) -> None:
    """An infinite grace would make the time limit soft again without a word."""
    with pytest.raises(pydantic.ValidationError):
        QueryLimitsConfig(kill_grace_seconds=value)


@pytest.mark.parametrize(
    "field", ["max_result_rows", "max_result_bytes", "timeout_seconds", "threads"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_caps_are_rejected(field: str, value: int) -> None:
    """A zero or negative cap fails at load, not as a failure on every query."""
    with pytest.raises(pydantic.ValidationError):
        QueryLimitsConfig.model_validate({field: value})


def test_storage_config_repr_redacts_option_values() -> None:
    """`repr`/`str` never print an option's value, secret-looking or not."""
    config = StorageConfig(
        scheme="s3",
        root="s3://bucket/prefix",
        options={
            "s3.secret-access-key": "SUPER_SECRET_VALUE",
            "s3.endpoint": "http://s3.example:9000",
        },
    )

    text = repr(config)
    as_str = str(config)

    for rendered in (text, as_str):
        assert "SUPER_SECRET_VALUE" not in rendered
        assert "http://s3.example:9000" not in rendered
        assert "s3.secret-access-key" in rendered
        assert "s3.endpoint" in rendered
        assert "s3://bucket/prefix" in rendered

    # Behaviour is unchanged: the real values are still reachable.
    assert config.options["s3.secret-access-key"] == "SUPER_SECRET_VALUE"
    assert config.model_dump()["options"]["s3.secret-access-key"] == "SUPER_SECRET_VALUE"


def test_database_config_repr_redacts_the_password() -> None:
    """`repr`/`str` show the URL with its password hidden, not omitted."""
    config = DatabaseConfig(url="postgresql+pg8000://lakana_user:hunter2@db.internal:5432/lakana")

    text = repr(config)
    as_str = str(config)

    for rendered in (text, as_str):
        assert "hunter2" not in rendered
        assert "lakana_user" in rendered
        assert "db.internal" in rendered

    # Behaviour is unchanged: the real URL is still reachable.
    assert config.url == "postgresql+pg8000://lakana_user:hunter2@db.internal:5432/lakana"


def test_database_config_repr_fully_redacts_an_unparseable_url() -> None:
    """A URL sqlalchemy can't parse is redacted wholesale, not echoed."""
    config = DatabaseConfig(url="not a sqlalchemy url at all; password=hunter2")

    assert "hunter2" not in repr(config)
    assert "hunter2" not in str(config)
    assert "***" in repr(config)


def test_secrets_config_repr_shows_asterisks_when_set() -> None:
    """A set `master_key` never appears in `repr`/`str`."""
    from lakana_core.config import SecretsConfig

    config = SecretsConfig(master_key="dGhpc2lzYXNlY3JldGtleQ==")

    assert "dGhpc2lzYXNlY3JldGtleQ==" not in repr(config)
    assert "dGhpc2lzYXNlY3JldGtleQ==" not in str(config)
    assert "***" in repr(config)
    assert config.master_key == "dGhpc2lzYXNlY3JldGtleQ=="


def test_secrets_config_repr_shows_none_when_unset() -> None:
    """An unset `master_key` still reprs as `None`, not `***`."""
    from lakana_core.config import SecretsConfig

    config = SecretsConfig()

    assert "None" in repr(config)
    assert "***" not in repr(config)


def test_settings_repr_redacts_nested_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings`'s own repr inherits redaction from its nested models."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "postgresql+pg8000://user:hunter2@db/lakana")
    monkeypatch.setenv("LAKANA_SECRETS__MASTER_KEY", "dGhpc2lzYXNlY3JldGtleQ==")

    settings = Settings()

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)
    assert "dGhpc2lzYXNlY3JldGtleQ==" not in repr(settings)
    assert "dGhpc2lzYXNlY3JldGtleQ==" not in str(settings)


def test_settings_validation_error_hides_a_bad_master_key_value() -> None:
    """A `ValidationError` from a bad `master_key` input does not echo it."""
    with mock.patch.dict(os.environ, {}, clear=True):
        try:
            Settings(database=DatabaseConfig(url="sqlite://"), secrets={"master_key": object()})
        except PydanticValidationError as exc:
            assert "at 0x" not in str(exc)
        else:
            pytest.fail("expected a ValidationError for a non-string master_key")


def test_database_config_validation_error_hides_a_bad_url_value() -> None:
    """A `ValidationError` from a bad `url` input does not echo it."""
    with mock.patch.dict(os.environ, {}, clear=True):
        try:
            Settings(database={"url": object()})
        except PydanticValidationError as exc:
            assert "at 0x" not in str(exc)
        else:
            pytest.fail("expected a ValidationError for a non-string database url")


def test_max_write_rows_defaults_to_one_hundred_million(monkeypatch: pytest.MonkeyPatch) -> None:
    """`max_write_rows` defaults to 100 million and is operator-configurable."""
    from lakana_core.config import QueryLimitsConfig, Settings

    assert QueryLimitsConfig().max_write_rows == 100_000_000
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__MAX_WRITE_ROWS", "5")
    assert Settings().query_limits.max_write_rows == 5


def test_repo_roots_default_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local repositories are refused unless an operator opts in."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.delenv("LAKANA_REPO_ROOTS", raising=False)
    assert Settings().repo_roots == []


def test_repo_roots_parse_from_json_and_must_be_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LAKANA_REPO_ROOTS` is a JSON list of absolute paths."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_REPO_ROOTS", '["/srv/repos", "/home/me"]')
    assert Settings().repo_roots == ["/srv/repos", "/home/me"]
    monkeypatch.setenv("LAKANA_REPO_ROOTS", '["relative"]')
    with pytest.raises(pydantic.ValidationError, match="absolute"):
        Settings()


def test_run_config_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LAKANA_RUN__MAX_RETRIES` defaults to 5 and must not be negative."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().run.max_retries == 5
    monkeypatch.setenv("LAKANA_RUN__MAX_RETRIES", "1")
    assert Settings().run.max_retries == 1
    with pytest.raises(pydantic.ValidationError):
        RunConfig(max_retries=-1)


def test_the_history_open_versions_cap_defaults_to_a_million_and_may_be_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().run.max_history_open_versions == 1_000_000
    monkeypatch.setenv("LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS", "3000000")
    assert Settings().run.max_history_open_versions == 3_000_000
    with pytest.raises(pydantic.ValidationError):
        RunConfig(max_history_open_versions=0)


def test_run_config_scheduling_defaults() -> None:
    """Interval 1 s, lease 60 s, renewal 20 s, drain grace 20 s; non-positive values refused."""
    config = RunConfig()
    assert (
        config.scheduler_interval_seconds,
        config.lease_seconds,
        config.renew_seconds,
        config.drain_grace_seconds,
    ) == (1.0, 60, 20, 20.0)
    for field in ("scheduler_interval_seconds", "lease_seconds", "renew_seconds"):
        with pytest.raises(pydantic.ValidationError):
            RunConfig.model_validate({field: 0})
    with pytest.raises(pydantic.ValidationError):
        RunConfig(lease_seconds=20, renew_seconds=20)


def test_git_config_defaults_to_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy, bypass list or CA bundle unless the operator sets one."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    git = Settings().git
    assert (git.https_proxy, git.no_proxy, git.ca_bundle) == (None, None, None)


def test_git_config_reads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`LAKANA_GIT__*` populate the nested model."""
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_GIT__HTTPS_PROXY", "https://proxy.corp:3128")
    monkeypatch.setenv("LAKANA_GIT__NO_PROXY", "gitlab.corp,.internal")
    monkeypatch.setenv("LAKANA_GIT__CA_BUNDLE", str(bundle))
    git = Settings().git
    assert git.https_proxy is not None
    assert git.https_proxy.get_secret_value() == "https://proxy.corp:3128"
    assert git.no_proxy == "gitlab.corp,.internal"
    assert git.ca_bundle == bundle


@pytest.mark.parametrize(
    "proxy",
    [
        "proxy.corp:3128",
        "socks5://proxy.corp:1080",
        "ftp://proxy.corp",
        "http://",
        "http://:3128",
        "http://proxy.corp:notaport",
        "http://proxy .corp",
        "http://proxy.corp\nx",
        "http://proxy.corp/#frag",
        "http://svc@proxy.corp:3128",
        "http://svc:@proxy.corp:3128",
        "http://:pw@proxy.corp:3128",
        "http://svc:p@ss@proxy.corp:3128",
        "http://svc:p/ss@proxy.corp:3128",
        "http://svc:p%zzss@proxy.corp:3128",
        "http://svc:p:ss@proxy.corp:3128",
        "",
    ],
)
def test_git_config_refuses_a_proxy_that_is_not_an_http_url(proxy: str) -> None:
    """The proxy must be an `http://` or `https://` URL with a host."""
    with pytest.raises(PydanticValidationError, match="https_proxy"):
        GitConfig(https_proxy=proxy)


def test_git_config_errors_never_echo_the_proxy_password() -> None:
    """A rejected value, which may carry a password, is not shown."""
    with pytest.raises(PydanticValidationError) as raised:
        GitConfig(https_proxy="socks5://svc:hunter2@proxy.corp")
    assert "hunter2" not in str(raised.value)


def test_git_config_hides_the_proxy_password_everywhere() -> None:
    """`repr`, `str` and `model_dump` never carry the proxy URL."""
    config = GitConfig(https_proxy="http://svc:hunter2@proxy.corp:3128")
    for text in (repr(config), str(config), repr(config.model_dump())):
        assert "hunter2" not in text
        assert "svc" not in text
    assert "None" in repr(GitConfig())


@pytest.mark.parametrize("no_proxy", ["a.corp\nb", "a\x00b", ""])
def test_git_config_refuses_control_characters_in_no_proxy(no_proxy: str) -> None:
    """The bypass list is one environment-variable line."""
    with pytest.raises(PydanticValidationError, match="no_proxy"):
        GitConfig(no_proxy=no_proxy)


def test_git_config_ca_bundle_is_checked_for_format_only(tmp_path: Path) -> None:
    """Absolute single-line paths pass with no filesystem access; the worker checks the file."""
    missing = tmp_path / "missing.pem"
    assert GitConfig(ca_bundle=missing).ca_bundle == missing
    for bad in ("relative/ca.pem", "ca.pem", "", "/etc/ca\n.pem"):
        with pytest.raises(PydanticValidationError, match="ca_bundle"):
            GitConfig(ca_bundle=bad)  # type: ignore[arg-type]


def test_settings_load_with_an_unreadable_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server and `migrate` load `Settings` without the worker's CA mount."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_GIT__CA_BUNDLE", "/certs/not-mounted-here.pem")
    assert str(Settings().git.ca_bundle) == "/certs/not-mounted-here.pem"


def test_git_config_accepts_explicit_none() -> None:
    """Passing `None` explicitly is the same as leaving a setting unset."""
    config = GitConfig(https_proxy=None, no_proxy=None, ca_bundle=None)
    assert (config.https_proxy, config.no_proxy, config.ca_bundle) == (None, None, None)


def test_python_asset_settings_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_RUN__SERVER_URL", "http://server:8000/")
    monkeypatch.setenv("LAKANA_RUN__VENV_CACHE", str(tmp_path / "venvs"))
    monkeypatch.setenv("LAKANA_RUN__VENV_KEEP", "3")
    monkeypatch.setenv("LAKANA_RUN__VENV_BUILD_TIMEOUT", "90")
    monkeypatch.setenv("LAKANA_RUN__VENV_MAX_BYTES", "1048576")
    monkeypatch.setenv("LAKANA_RUN__KILL_GRACE", "2.5")
    monkeypatch.setenv("LAKANA_RUN__INDEX_URL", "https://svc:hunter2@pypi.corp/simple")
    monkeypatch.setenv("LAKANA_RUN__HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("LAKANA_RUN__NO_PROXY", "pypi.corp,.internal")
    monkeypatch.setenv("LAKANA_RUN__CA_BUNDLE", "/certs/ca.pem")
    run = Settings().run
    assert run.server_url == "http://server:8000"
    assert run.venv_cache == tmp_path / "venvs"
    assert run.venv_keep == 3
    assert math.isclose(run.venv_build_timeout, 90.0)
    assert run.venv_max_bytes == 1048576
    assert math.isclose(run.kill_grace, 2.5)
    assert run.index_url is not None
    assert run.index_url.get_secret_value() == "https://svc:hunter2@pypi.corp/simple"
    assert run.https_proxy is not None
    assert run.https_proxy.get_secret_value() == "http://proxy.corp:3128"
    assert run.no_proxy == "pypi.corp,.internal"
    assert run.ca_bundle == Path("/certs/ca.pem")
    assert "hunter2" not in repr(run)


def test_python_asset_settings_defaults() -> None:
    run = RunConfig()
    assert run.server_url is None
    assert run.venv_cache is None
    assert run.venv_keep == 10
    assert math.isclose(run.venv_build_timeout, 600.0)
    assert run.venv_max_bytes == 5 * 1024**3
    assert math.isclose(run.kill_grace, 10.0)
    assert (run.index_url, run.https_proxy, run.no_proxy, run.ca_bundle) == (None,) * 4


@pytest.mark.parametrize(
    "url",
    [
        "server:8000",
        "ftp://server",
        "http://",
        "http://user:hunter2@server:8000",
        "http://server:8000/?x=1",
        "http://server:8000/#frag",
        "http://ser ver",
        "http://server:port",
        "http://server\n",
    ],
)
def test_a_bad_server_url_is_refused_without_echoing_it(url: str) -> None:
    with pytest.raises(pydantic.ValidationError) as caught:
        RunConfig(server_url=url)
    assert "hunter2" not in str(caught.value)
    assert "LAKANA_RUN__SERVER_URL must be" in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("venv_cache", "relative/venvs"),
        ("venv_cache", "/var/cache\n/x"),
        ("venv_keep", 0),
        ("venv_build_timeout", 0),
        ("venv_max_bytes", 0),
        ("kill_grace", -1),
        ("sweep_interval", -1),
        ("index_url", "socks5://svc:hunter2@pypi.corp"),
        ("index_url", "https://svc@pypi.corp/simple"),
        ("https_proxy", "http://svc:hunter2@"),
        ("no_proxy", ""),
        ("no_proxy", "a\nb"),
        ("ca_bundle", "certs/ca.pem"),
    ],
)
def test_bad_python_asset_settings_are_refused_without_echoing_them(
    field: str, value: object
) -> None:
    with pytest.raises(pydantic.ValidationError) as caught:
        RunConfig.model_validate({field: value})
    assert "hunter2" not in str(caught.value)


def test_python_asset_settings_accept_an_explicit_none() -> None:
    run = RunConfig(
        server_url=None, venv_cache=None, index_url=None, https_proxy=None, no_proxy=None
    )
    assert run == RunConfig()


def test_an_index_url_error_names_the_index_not_a_proxy() -> None:
    for bad in ("socks5://svc:hunter2@pypi.corp", "https://svc@pypi.corp/simple"):
        with pytest.raises(pydantic.ValidationError) as caught:
            RunConfig.model_validate({"index_url": bad})
        text = str(caught.value)
        assert "proxy" not in text
        assert "hunter2" not in text
        assert "svc" not in text
    with pytest.raises(pydantic.ValidationError, match="index URL must include a password"):
        RunConfig.model_validate({"index_url": "https://svc@pypi.corp/simple"})
    with pytest.raises(pydantic.ValidationError, match=r"pypi\.corp/simple"):
        RunConfig.model_validate({"index_url": "ftp://pypi.corp"})
    with pytest.raises(pydantic.ValidationError, match="proxy URL must include a password"):
        RunConfig.model_validate({"https_proxy": "http://svc@proxy.corp"})


def test_task_token_settings_are_core_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker reads the TTL cap for the tokens it mints for Python tasks."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.delenv("LAKANA_TASK_TOKENS__MAX_TTL_SECONDS", raising=False)
    assert Settings().task_tokens.max_ttl_seconds == 86_400
    monkeypatch.setenv("LAKANA_TASK_TOKENS__MAX_TTL_SECONDS", "120")
    assert Settings().task_tokens.max_ttl_seconds == 120


@pytest.mark.parametrize("suffix", ["?token=hunter2", "?", "/simple?a=b&key=hunter2"])
def test_a_query_string_on_an_index_or_proxy_url_is_refused_without_echoing_it(
    suffix: str,
) -> None:
    """A credential in a query string is not redacted by the log redactor, so refuse it."""
    for build in (
        lambda url: RunConfig.model_validate({"index_url": url}),
        lambda url: RunConfig.model_validate({"https_proxy": url}),
        lambda url: GitConfig(https_proxy=url),
    ):
        with pytest.raises(pydantic.ValidationError) as caught:
            build(f"https://pypi.corp{suffix}")
        text = str(caught.value)
        assert "hunter2" not in text
        assert "query string" in text
        assert "user:token@host" in text


def test_the_attempt_sweep_interval_defaults_to_an_hour_and_zero_disables_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LAKANA_RUN__SWEEP_INTERVAL`."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.delenv("LAKANA_RUN__SWEEP_INTERVAL", raising=False)
    assert math.isclose(Settings().run.sweep_interval, 3600.0)
    monkeypatch.setenv("LAKANA_RUN__SWEEP_INTERVAL", "0")
    assert math.isclose(Settings().run.sweep_interval, 0.0)


def test_artifact_limits_have_defaults_and_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().artifacts == ArtifactsConfig(
        max_file_bytes=2147483648, max_bytes=10737418240, max_files=10000, upload_ttl_seconds=3600
    )
    monkeypatch.setenv("LAKANA_ARTIFACTS__MAX_FILE_BYTES", "5")
    monkeypatch.setenv("LAKANA_ARTIFACTS__MAX_BYTES", "6")
    monkeypatch.setenv("LAKANA_ARTIFACTS__MAX_FILES", "7")
    monkeypatch.setenv("LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS", "8")
    assert Settings().artifacts == ArtifactsConfig(
        max_file_bytes=5, max_bytes=6, max_files=7, upload_ttl_seconds=8
    )


@pytest.mark.parametrize("name", ["MAX_FILE_BYTES", "MAX_BYTES", "MAX_FILES", "UPLOAD_TTL_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_artifact_limits_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv(f"LAKANA_ARTIFACTS__{name}", value)
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_the_worker_memory_limit_is_unset_by_default_and_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset means "derive it" (the worker decides at start); the variable overrides."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.delenv("LAKANA_WORKER__MEMORY_LIMIT", raising=False)
    assert Settings().worker.memory_limit is None
    monkeypatch.setenv("LAKANA_WORKER__MEMORY_LIMIT", "2g")
    assert Settings().worker.memory_limit == "2g"


@pytest.mark.parametrize("value", ["lots", "100MB", "9" * 400 + "GB"])
def test_a_bad_worker_memory_limit_is_refused_when_settings_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The worker never starts with a limit it cannot read."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.setenv("LAKANA_WORKER__MEMORY_LIMIT", value)
    with pytest.raises(PydanticValidationError, match="LAKANA_WORKER__MEMORY_LIMIT"):
        Settings()


def test_an_explicitly_unset_worker_memory_limit_is_accepted() -> None:
    """`None` (derive it at start) passes the validator, which only reads a given text."""
    from lakana_core.config import WorkerConfig

    assert WorkerConfig(memory_limit=None).memory_limit is None


_TOKEN = "Tk3nValue9xQ2mZ7pLw4"


_TOKEN_PATHS = [
    f"/{_TOKEN}/simple/",
    f"/simple/{_TOKEN}",
    "/0123456789abcdef0123456789abcdef/org/repo/simple/",
    "/a1b2c3d4e5f6a7b8/simple",
    f"/{_TOKEN}",
    # Real index URLs the check cannot tell from a token: a project id with digits, a repository
    # named with capitals.
    "/projects/my-project-123456/repos/Team-Python-Mirror/simple/",
]


@pytest.mark.parametrize("path", _TOKEN_PATHS)
def test_a_token_like_path_of_an_index_or_proxy_url_is_accepted_and_warned_about(
    path: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The settings load (a project id is not a secret); the warning is a separate step."""
    for build in (
        lambda url: RunConfig.model_validate({"index_url": url}),
        lambda url: RunConfig.model_validate({"https_proxy": url}),
        lambda url: GitConfig(https_proxy=url),
    ):
        build(f"https://pypi.corp{path}")
    urls = {
        "LAKANA_RUN__INDEX_URL": SecretStr(f"https://pypi.corp{path}"),
        "LAKANA_GIT__HTTPS_PROXY": SecretStr("https://proxy.corp:3128"),
        "LAKANA_RUN__HTTPS_PROXY": None,
    }
    logger = logging.getLogger("test_token_path")
    with caplog.at_level("WARNING", logger="test_token_path"):
        warn_about_tokens_in_url_paths(logger, urls)
    [record] = caplog.records
    text = record.getMessage()
    assert "LAKANA_RUN__INDEX_URL" in text
    assert "not redacted" in text
    assert "user:token@host" in text
    assert "nothing needs doing" in text
    assert text.rstrip().endswith(".")
    for part in path.strip("/").split("/"):
        assert len(part) < 16 or part not in text
    assert "pypi.corp" not in text
    assert "proxy.corp" not in text


def test_an_ordinary_url_is_not_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    """No setting, or a URL with no token-like segment, logs nothing."""
    urls = {
        "LAKANA_RUN__INDEX_URL": SecretStr("https://pypi.corp/repository/pypi-all/simple/"),
        "LAKANA_GIT__HTTPS_PROXY": None,
    }
    logger = logging.getLogger("test_token_path")
    with caplog.at_level("WARNING", logger="test_token_path"):
        warn_about_tokens_in_url_paths(logger, urls)
    assert caplog.records == []


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/simple",
        "/simple/",
        "/api/v4/projects/12345678/packages/pypi/simple",
        "/repository/pypi-all/simple/",
        "/internal/api/pypi/company-internal-packages/simple",
        "/root/pypi/+simple/",
        "/a1b2c3d4e5f6a7b",
    ],
)
def test_an_ordinary_index_path_is_still_accepted(path: str) -> None:
    """A long word, a number or a short name is not taken for a token."""
    run = RunConfig.model_validate({"index_url": f"https://pypi.corp{path}"})
    assert run.index_url is not None


@pytest.mark.parametrize(
    "url", ["http://server:", "http://server:/path", "https://[::1]:", "http://user:pw@server:"]
)
def test_a_server_url_with_an_empty_port_is_refused(url: str) -> None:
    """`urlsplit` reads "host:" as no port; a colon with nothing after it is a typo."""
    with pytest.raises(
        pydantic.ValidationError,
        match=r"LAKANA_RUN__SERVER_URL has a colon after the host and no port.*http://server:8000",
    ):
        RunConfig(server_url=url)


@pytest.mark.parametrize(
    "url",
    ["http://proxy.corp:", "http://svc:hunter2@proxy.corp:", "https://pypi.corp:/simple"],
)
def test_a_proxy_or_index_url_with_an_empty_port_is_refused_without_echoing_it(url: str) -> None:
    """The same typo in a proxy or an index URL."""
    for build in (
        lambda value: RunConfig.model_validate({"index_url": value}),
        lambda value: RunConfig.model_validate({"https_proxy": value}),
        lambda value: GitConfig(https_proxy=value),
    ):
        with pytest.raises(pydantic.ValidationError) as caught:
            build(url)
        assert "hunter2" not in str(caught.value)
        assert "the port after the colon is missing" in str(caught.value)
        assert "or drop the colon" in str(caught.value)


def test_a_url_with_a_port_is_still_accepted() -> None:
    """The check refuses only the empty port."""
    run = RunConfig.model_validate(
        {"server_url": "http://server:8000", "https_proxy": "http://proxy.corp:3128"}
    )
    assert run.server_url == "http://server:8000"


@pytest.mark.parametrize("value", [",", " ", ", ,", ",,,"])
def test_a_proxy_bypass_list_without_a_host_is_refused(value: str) -> None:
    """A list that names no host bypasses nothing and is a typo."""
    with pytest.raises(pydantic.ValidationError, match="comma-separated host list"):
        RunConfig.model_validate({"no_proxy": value})
    with pytest.raises(pydantic.ValidationError, match="comma-separated host list"):
        GitConfig(no_proxy=value)


@pytest.mark.parametrize("value", ["pypi.corp", "pypi.corp, .internal", "a,,b", "*"])
def test_a_proxy_bypass_list_with_a_host_is_accepted(value: str) -> None:
    """Spaces, empty entries beside a host and `*` are still accepted."""
    assert RunConfig.model_validate({"no_proxy": value}).no_proxy == value


def test_the_secret_settings_never_show_their_value() -> None:
    """The index and proxy URLs carry passwords; no rendering of the settings prints them."""
    run = RunConfig.model_validate(
        {
            "index_url": "https://svc:hunter2@pypi.corp/simple",
            "https_proxy": "http://svc:hunter2@proxy.corp:3128",
        }
    )
    for text in (
        repr(run),
        str(run),
        run.model_dump_json(),
        str(run.model_dump()),
        repr(run.index_url),
        str(run.https_proxy),
    ):
        assert "hunter2" not in text
    assert run.index_url is not None
    assert run.index_url.get_secret_value() == "https://svc:hunter2@pypi.corp/simple"


def test_the_secret_settings_stay_hidden_in_the_whole_settings_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same, reached through `Settings` and the environment."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_RUN__INDEX_URL", "https://svc:hunter2@pypi.corp/simple")
    settings = Settings()
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in settings.model_dump_json()


def test_the_log_level_is_info_by_default_and_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LAKANA_LOG__LEVEL` is one setting for the server and the worker."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.delenv("LAKANA_LOG__LEVEL", raising=False)
    assert Settings().log.level == "INFO"
    monkeypatch.setenv("LAKANA_LOG__LEVEL", "warning")
    assert Settings().log.level == "WARNING"


def test_the_access_log_is_on_by_default_and_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LAKANA_LOG__ACCESS=false` turns off the server's per-request line."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.delenv("LAKANA_LOG__ACCESS", raising=False)
    assert Settings().log.access is True
    monkeypatch.setenv("LAKANA_LOG__ACCESS", "false")
    assert Settings().log.access is False


@pytest.mark.parametrize("value", ["DEBUG", "info", "Warning", "eRRor"])
def test_the_log_level_is_accepted_in_any_letter_case(value: str) -> None:
    """The four names are accepted whatever their case, and kept in upper case."""
    assert LogConfig(level=value).level == value.upper()


@pytest.mark.parametrize("value", ["verbose", "10", "CRITICAL", "", "in fo", "x\x1b[31m"])
def test_a_bad_log_level_is_refused_naming_the_setting_the_value_and_the_choices(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A process never starts with a level it does not know, and the message says what to write."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.setenv("LAKANA_LOG__LEVEL", value)
    with pytest.raises(PydanticValidationError) as caught:
        Settings()
    text = str(caught.value)
    assert "LAKANA_LOG__LEVEL" in text
    assert "DEBUG, INFO, WARNING or ERROR" in text
    assert "\x1b" not in text  # a raw control character never reaches the message
    if value == "verbose":
        assert "'verbose'" in text


def test_a_worker_takes_all_by_default_and_the_setting_is_read_in_any_letter_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LAKANA_WORKER__TAKES` defaults to `all` and is stored in lower case."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.delenv("LAKANA_WORKER__TAKES", raising=False)
    assert Settings().worker.takes == "all"
    monkeypatch.setenv("LAKANA_WORKER__TAKES", " Interactive ")
    assert Settings().worker.takes == "interactive"
    assert WorkerConfig(takes="PIPELINE").takes == "pipeline"


def test_a_worker_delivers_notifications_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LAKANA_WORKER__NOTIFICATIONS` defaults to on and `false` turns the delivery loop off."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.delenv("LAKANA_WORKER__NOTIFICATIONS", raising=False)
    assert Settings().worker.notifications is True
    monkeypatch.setenv("LAKANA_WORKER__NOTIFICATIONS", "false")
    assert Settings().worker.notifications is False


@pytest.mark.parametrize("value", ["both", "", "interactive,pipeline", "x\x1b[31m"])
def test_a_bad_takes_is_refused_when_settings_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A worker never starts with a `takes` it does not know; the message says what to write."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.setenv("LAKANA_WORKER__TAKES", value)
    with pytest.raises(PydanticValidationError) as caught:
        Settings()
    text = str(caught.value)
    assert "\x1b" not in text  # a raw control character never reaches the message
    assert (
        f"LAKANA_WORKER__TAKES must be all, interactive or pipeline (got '{shown(value)}')." in text
    )


def test_the_cap_is_auto_by_default_and_reads_a_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` is `auto`, in any letter case, or a number."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.delenv("LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL", raising=False)
    assert Settings().query_limits.running_per_principal == "auto"
    for text, expected in (("auto", "auto"), (" AUTO ", "auto"), ("3", 3), (" 12 ", 12)):
        monkeypatch.setenv("LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL", text)
        assert Settings().query_limits.running_per_principal == expected
    assert QueryLimitsConfig(running_per_principal=3).running_per_principal == 3
    assert QueryLimitsConfig(running_per_principal="Auto").running_per_principal == "auto"


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "off", "", "true", "٣", "1e2", "000"])
def test_a_bad_cap_is_refused_when_settings_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The process does not start with a cap it does not understand; the message says why."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL", value)
    with pytest.raises(PydanticValidationError) as caught:
        Settings()
    assert (
        f"LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL must be auto or a whole number of 1 or more "
        f"(got '{shown(value)}'). auto reserves one worker for other users once three or more "
        "workers take interactive work."
    ) in str(caught.value)


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, None])
def test_a_bad_cap_given_in_code_is_refused_too(value: object) -> None:
    """The same rule holds for a value that is not text: a bool is not a number here."""
    with pytest.raises(PydanticValidationError, match="RUNNING_PER_PRINCIPAL must be auto"):
        QueryLimitsConfig(running_per_principal=value)


def test_a_cap_with_very_many_digits_is_read_as_no_limit_and_does_not_crash() -> None:
    """Python refuses `int()` of a few thousand digits; the setting reads it as a huge number."""
    assert QueryLimitsConfig(running_per_principal="9" * 5000).running_per_principal == 999999999


def test_webhook_config_defaults_to_nothing_set() -> None:
    """No allow-list, proxy or CA bundle unless the operator sets one."""
    config = WebhookConfig()
    assert (config.allowed_hosts(), config.proxy, config.ca_bundle) == ((), None, None)
    assert WebhookConfig(allow_hosts=" A.Test. , *.B.test").allowed_hosts() == (
        "a.test",
        "*.b.test",
    )


@pytest.mark.parametrize(
    "proxy", ["leaky.test:9", "socks5://leaky.test", "http://u:p@leaky.test/?q=1"]
)
def test_webhook_proxy_is_validated_without_quoting_it(proxy: str) -> None:
    """The proxy follows the git proxy's rules; an error never shows the value."""
    with pytest.raises(PydanticValidationError) as info:
        WebhookConfig(proxy=proxy)  # type: ignore[arg-type]
    assert proxy not in str(info.value)


def test_webhook_ca_bundle_is_checked_for_format_only(tmp_path: Path) -> None:
    """Absolute single-line paths pass with no filesystem access; the worker checks the file."""
    missing = tmp_path / "missing.pem"
    assert WebhookConfig(ca_bundle=missing).ca_bundle == missing
    with pytest.raises(PydanticValidationError, match="ca_bundle"):
        WebhookConfig(ca_bundle="relative.pem")  # type: ignore[arg-type]


def test_webhook_proxy_is_hidden_from_repr() -> None:
    """A proxy URL may carry a password."""
    assert "s3cr3t" not in repr(WebhookConfig(proxy="http://u:s3cr3t@proxy.corp:3128"))


def test_webhook_config_accepts_an_explicit_none_for_every_setting() -> None:
    """An explicit `None` (a setting passed but unset) skips the validators and means unset."""
    config = WebhookConfig(allow_hosts=None, proxy=None, ca_bundle=None)
    assert (config.allowed_hosts(), config.proxy, config.ca_bundle) == ((), None, None)


@pytest.mark.parametrize("entry", ["*.0.0.1", "10.0.0.5", "chat.corp.test,1"])
def test_webhook_allow_hosts_refuses_a_numeric_last_label(entry: str) -> None:
    """An IP address cannot be allowed; the message says to use its name."""
    with pytest.raises(PydanticValidationError, match="use its host name"):
        WebhookConfig(allow_hosts=entry)


def test_table_reads_keep_90_days_and_show_30_by_default() -> None:
    from lakana_core.config import TableReadsConfig

    config = TableReadsConfig()
    assert (config.keep_days, config.default_days) == (90, 30)


def test_table_reads_settings_read_their_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.setenv("LAKANA_TABLE_READS__KEEP_DAYS", "365")
    monkeypatch.setenv("LAKANA_TABLE_READS__DEFAULT_DAYS", "7")
    settings = Settings()
    assert (settings.table_reads.keep_days, settings.table_reads.default_days) == (365, 7)


@pytest.mark.parametrize("name", ["keep_days", "default_days"])
def test_table_reads_days_must_be_at_least_one(name: str) -> None:
    from lakana_core.config import TableReadsConfig

    with pytest.raises(PydanticValidationError):
        TableReadsConfig(**{name: 0})


def test_the_default_window_cannot_exceed_the_kept_period() -> None:
    from lakana_core.config import TableReadsConfig

    with pytest.raises(PydanticValidationError, match="LAKANA_TABLE_READS__DEFAULT_DAYS"):
        TableReadsConfig(keep_days=10, default_days=11)
    assert TableReadsConfig(keep_days=10, default_days=10).default_days == 10


def test_abandoned_claims_wait_24_hours_by_default() -> None:
    from lakana_core.config import TablesConfig

    assert TablesConfig().abandoned_grace_hours == 24


def test_the_grace_period_reads_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_TABLES__ABANDONED_GRACE_HOURS", "72")
    assert Settings().tables.abandoned_grace_hours == 72


@pytest.mark.parametrize("hours", [0, -1, 8761])
def test_the_grace_period_is_between_one_hour_and_one_year(hours: int) -> None:
    from lakana_core.config import TablesConfig

    with pytest.raises(PydanticValidationError, match="abandoned_grace_hours"):
        TablesConfig(abandoned_grace_hours=hours)


def test_a_dropped_table_can_be_restored_for_seven_days_by_default() -> None:
    from lakana_core.catalog.tombstones import DEFAULT_UNDO_DAYS
    from lakana_core.config import TableUndoConfig

    assert TableUndoConfig().undo_days == DEFAULT_UNDO_DAYS == 7


@pytest.mark.parametrize("days", [-1, 366])
def test_the_undo_period_is_between_zero_days_and_one_year(days: int) -> None:
    from lakana_core.config import TableUndoConfig

    with pytest.raises(PydanticValidationError, match="undo_days"):
        TableUndoConfig(undo_days=days)


def test_the_worker_reads_the_undo_period_and_a_cap_on_expiries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lakana_core.catalog.retention import DEFAULT_MAX_EXPIRIES
    from lakana_core.catalog.tombstones import DEFAULT_UNDO_DAYS

    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    tables = Settings().tables
    assert (tables.undo_days, tables.max_expiries_per_sweep) == (
        DEFAULT_UNDO_DAYS,
        DEFAULT_MAX_EXPIRIES,
    )
    monkeypatch.setenv("LAKANA_TABLES__UNDO_DAYS", "3")
    monkeypatch.setenv("LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP", "10")
    tables = Settings().tables
    assert (tables.undo_days, tables.max_expiries_per_sweep) == (3, 10)


@pytest.mark.parametrize("count", [0, 10001])
def test_the_cap_on_expiries_is_between_one_and_ten_thousand(count: int) -> None:
    from lakana_core.config import TablesConfig

    with pytest.raises(PydanticValidationError, match="max_expiries_per_sweep"):
        TablesConfig(max_expiries_per_sweep=count)


def test_the_server_warns_of_an_expiry_seven_days_ahead_by_default() -> None:
    from lakana_core.catalog.retention import DEFAULT_EXPIRY_WARNING_DAYS
    from lakana_core.config import TableUndoConfig

    assert TableUndoConfig().expiry_warning_days == DEFAULT_EXPIRY_WARNING_DAYS == 7


@pytest.mark.parametrize("days", [-1, 3651])
def test_the_expiry_warning_is_between_zero_and_3650_days(days: int) -> None:
    from lakana_core.config import TableUndoConfig

    with pytest.raises(PydanticValidationError, match="expiry_warning_days"):
        TableUndoConfig(expiry_warning_days=days)


def test_the_package_cache_is_trimmed_above_ten_gibibytes_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES` is 10 GiB unless set; 0 turns the trim off."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.delenv("LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES", raising=False)
    monkeypatch.delenv("LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS", raising=False)
    worker = Settings().worker
    assert (worker.package_cache_max_bytes, worker.package_cache_min_age_hours) == (
        10 * 1024**3,
        24.0,
    )
    monkeypatch.setenv("LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES", "0")
    monkeypatch.setenv("LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS", "0")
    worker = Settings().worker
    assert (worker.package_cache_max_bytes, worker.package_cache_min_age_hours) == (0, 0.0)


@pytest.mark.parametrize(
    "variable",
    ["LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES", "LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS"],
)
def test_a_negative_package_cache_setting_is_refused(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.setenv(variable, "-1")
    with pytest.raises(PydanticValidationError, match=variable.split("__")[1].lower()):
        Settings()


def test_the_runner_settings_default_to_the_local_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local stays the default; docker reads its socket and the task limits."""
    assert RunConfig().runner == "local"
    assert RunConfig().launcher_socket == Path("/run/lakana-launcher/launcher.sock")
    assert (RunConfig().task_memory, RunConfig().task_cpus, RunConfig().task_pids) == (
        "2GB",
        1.0,
        256,
    )
    assert parse_task_memory("2GB") == 2 * 1024**3
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_RUN__RUNNER", "Docker")
    monkeypatch.setenv("LAKANA_RUN__TASK_MEMORY", "512MB")
    assert (Settings().run.runner, Settings().run.task_memory) == ("docker", "512MB")


@pytest.mark.parametrize(
    ("text", "size"),
    [("64MB", 64 * 1024**2), ("1.5g", int(1.5 * 1024**3)), (" 1GiB ", 1024**3), ("1tb", 1024**4)],
)
def test_a_task_memory_reads_binary_units(text: str, size: int) -> None:
    """Units are binary, as Docker's are.

    Parameters
    ----------
    text
        The setting.
    size
        Bytes.
    """
    assert parse_task_memory(text) == size


@pytest.mark.parametrize("text", ["63MB", "0", "2048TB", "1" * 40 + "MB", "4 apples", ""])
def test_a_task_memory_out_of_bounds_is_refused_with_the_setting_named(text: str) -> None:
    """Below 64 MB, above 1 PiB or not a size: one message that says what to write.

    Parameters
    ----------
    text
        The setting.
    """
    with pytest.raises(ValueError, match="LAKANA_RUN__TASK_MEMORY must be a size with a unit"):
        parse_task_memory(text)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"runner": "podman"}, "LAKANA_RUN__RUNNER must be local or docker"),
        ({"runner": 3}, "LAKANA_RUN__RUNNER must be local or docker"),
        ({"task_memory": "32MB"}, "LAKANA_RUN__TASK_MEMORY must be a size with a unit"),
        ({"task_memory": "lots"}, "LAKANA_RUN__TASK_MEMORY must be a size with a unit"),
        ({"launcher_socket": "launcher.sock"}, "absolute"),
        ({"runner": "docker", "https_proxy": "http://proxy:3128"}, "LAKANA_EGRESS__UPSTREAM_PROXY"),
        ({"task_pids": 4}, "task_pids"),
        ({"task_cpus": 0}, "task_cpus"),
        ({"task_cpus": float("inf")}, "task_cpus"),
    ],
)
def test_a_runner_setting_that_cannot_work_is_refused_at_start(
    values: dict[str, object], message: str
) -> None:
    """Each message names the setting and what to write instead.

    Parameters
    ----------
    values
        The settings.
    message
        Part of the error.
    """
    with pytest.raises(pydantic.ValidationError, match=message):
        RunConfig(**values)  # ty: ignore[invalid-argument-type]


def test_a_private_ca_is_accepted_with_either_runner() -> None:
    """Build and task containers trust LAKANA_RUN__CA_BUNDLE; the old refusal is gone."""
    bundle = Path("/etc/ssl/corp.pem")
    assert RunConfig(runner="docker", ca_bundle=bundle).ca_bundle == bundle
    assert RunConfig(runner="local", ca_bundle=bundle).ca_bundle == bundle
    assert RunConfig(runner="local", https_proxy=SecretStr("http://proxy:3128")).https_proxy


def test_an_http_index_with_the_docker_runner_is_refused_with_the_way_out() -> None:
    """Builds reach indexes only over https through the egress proxy: the message says so."""
    with pytest.raises(pydantic.ValidationError) as caught:
        RunConfig(runner="docker", index_url=SecretStr("http://u:secret@mirror.corp:8080/simple"))
    text = str(caught.value)
    assert "LAKANA_RUN__INDEX_URL" in text
    assert "https" in text
    assert "egress" in text
    assert "LAKANA_RUN__RUNNER=local" in text
    assert "secret" not in text
    assert "mirror.corp" not in text
    assert RunConfig(runner="docker", index_url=SecretStr("https://mirror.corp/simple")).index_url
    assert RunConfig(runner="local", index_url=SecretStr("http://mirror.corp/simple")).index_url
    with pytest.raises(pydantic.ValidationError, match="LAKANA_RUN__INDEX_URL"):
        RunConfig(runner="docker", index_url=SecretStr("HTTP://mirror.corp/simple"))


def test_run_ingestion_roots_read_from_settings_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LAKANA_RUN__INGESTION_ROOTS` is a JSON object of name to directory; empty by default."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().run.ingestion_roots == {}
    assert Settings(run={"ingestion_roots": {"shared": "/srv/in"}}).run.ingestion_roots == {  # type: ignore[arg-type]
        "shared": "/srv/in"
    }
    monkeypatch.setenv("LAKANA_RUN__INGESTION_ROOTS", '{"shared": "/srv/in"}')
    assert Settings().run.ingestion_roots == {"shared": "/srv/in"}
