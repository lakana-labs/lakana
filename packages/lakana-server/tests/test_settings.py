"""Tests for lakana-server's runtime settings."""

from pathlib import Path

import pydantic
import pytest

from lakana_core.config import ArtifactsConfig, DatabaseConfig, HealthConfig, SecretsConfig
from lakana_server.settings import PublicOrigin, Settings, UiConfig

DB = DatabaseConfig(url="sqlite://")
"""A database for settings that are only validated, never connected."""


def test_settings_repr_redacts_nested_secrets() -> None:
    """`Settings`'s repr inherits redaction from its nested config models."""
    settings = Settings(
        database=DatabaseConfig(url="postgresql+pg8000://user:hunter2@db.internal/lakana"),
        secrets=SecretsConfig(master_key="dGhpc2lzYXNlY3JldGtleQ=="),
    )

    text = repr(settings)
    as_str = str(settings)

    for rendered in (text, as_str):
        assert "hunter2" not in rendered
        assert "dGhpc2lzYXNlY3JldGtleQ==" not in rendered
        assert "db.internal" in rendered

    # Behaviour is unchanged: the real values are still reachable.
    assert settings.database.url == "postgresql+pg8000://user:hunter2@db.internal/lakana"
    assert settings.secrets.master_key == "dGhpc2lzYXNlY3JldGtleQ=="


def test_repo_roots_must_be_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The server validates `LAKANA_REPO_ROOTS` like the worker does."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    monkeypatch.setenv("LAKANA_REPO_ROOTS", '["/srv/repos"]')
    assert Settings().repo_roots == ["/srv/repos"]
    monkeypatch.setenv("LAKANA_REPO_ROOTS", '["relative"]')
    with pytest.raises(pydantic.ValidationError, match="absolute"):
        Settings()


def test_repo_roots_default_to_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With `LAKANA_REPO_ROOTS` unset no local repository can be registered or deployed."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    monkeypatch.delenv("LAKANA_REPO_ROOTS", raising=False)
    assert Settings().repo_roots == []


def test_run_caps_settings_are_read_from_the_shared_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The server reads the same LAKANA_QUERY_LIMITS__/LAKANA_RUN__ variables as the worker."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("LAKANA_RUN__MAX_RETRIES", "1")
    settings = Settings()
    assert (settings.query_limits.timeout_seconds, settings.run.max_retries) == (120, 1)


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


def test_table_reads_settings_have_defaults_and_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lakana_core.config import TableReadsConfig

    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().table_reads == TableReadsConfig(keep_days=90, default_days=30)
    monkeypatch.setenv("LAKANA_TABLE_READS__KEEP_DAYS", "400")
    monkeypatch.setenv("LAKANA_TABLE_READS__DEFAULT_DAYS", "14")
    assert Settings().table_reads == TableReadsConfig(keep_days=400, default_days=14)


def test_the_undo_period_of_a_drop_is_seven_days_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().tables.undo_days == 7
    monkeypatch.setenv("LAKANA_TABLES__UNDO_DAYS", "0")
    assert Settings().tables.undo_days == 0
    monkeypatch.setenv("LAKANA_TABLES__UNDO_DAYS", "366")
    with pytest.raises(pydantic.ValidationError, match="undo_days"):
        Settings()


def test_the_view_audit_is_on_with_an_hour_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lakana_core.config import ViewAuditConfig

    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().view_audit == ViewAuditConfig(enabled=True, window_minutes=60)
    monkeypatch.setenv("LAKANA_VIEW_AUDIT__ENABLED", "false")
    monkeypatch.setenv("LAKANA_VIEW_AUDIT__WINDOW_MINUTES", "1440")
    assert Settings().view_audit == ViewAuditConfig(enabled=False, window_minutes=1440)
    monkeypatch.setenv("LAKANA_VIEW_AUDIT__WINDOW_MINUTES", "1441")
    with pytest.raises(pydantic.ValidationError, match="window_minutes"):
        Settings()


def test_the_audit_stores_full_query_text_by_default_and_refuses_an_unknown_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lakana_server.settings import ServerAuditConfig

    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.delenv("LAKANA_AUDIT__QUERY_TEXT", raising=False)
    assert Settings().audit == ServerAuditConfig(query_text="full")
    monkeypatch.setenv("LAKANA_AUDIT__QUERY_TEXT", "OFF")
    assert Settings().audit.query_text == "off"
    monkeypatch.setenv("LAKANA_AUDIT__QUERY_TEXT", "none")
    with pytest.raises(pydantic.ValidationError, match="must be full, hash or off"):
        Settings()


def test_a_sign_in_is_recorded_once_per_quarter_hour_and_zero_records_every_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.delenv("LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES", raising=False)
    assert Settings().audit.sign_in_window_minutes == 15
    monkeypatch.setenv("LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES", "0")
    assert Settings().audit.sign_in_window_minutes == 0
    monkeypatch.setenv("LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES", "1440")
    assert Settings().audit.sign_in_window_minutes == 1440
    for wrong in ("-1", "1441"):
        monkeypatch.setenv("LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES", wrong)
        with pytest.raises(pydantic.ValidationError, match="sign_in_window_minutes"):
            Settings()


def test_ci_oidc_is_off_by_default_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`LAKANA_CI_OIDC__*` is the server's; empty issuers turn it off."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    assert not Settings().ci_oidc.enabled
    monkeypatch.setenv("LAKANA_CI_OIDC__ISSUERS", "github,gitlab")
    monkeypatch.setenv("LAKANA_CI_OIDC__AUDIENCE", "https://lakana.example.com")
    config = Settings().ci_oidc
    assert config.issuers == ["https://token.actions.githubusercontent.com", "https://gitlab.com"]
    assert config.audience == "https://lakana.example.com"


def test_the_server_does_not_start_with_issuers_and_no_audience(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The message names the missing setting and what to set it to."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    monkeypatch.setenv("LAKANA_CI_OIDC__ISSUERS", "github")
    with pytest.raises(pydantic.ValidationError, match="LAKANA_CI_OIDC__AUDIENCE is required"):
        Settings()


def test_a_main_language_that_is_not_a_tag_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`LAKANA_MAIN_LANGUAGE` defaults to `en`, takes a tag and refuses anything else."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    assert Settings().main_language == "en"
    monkeypatch.setenv("LAKANA_MAIN_LANGUAGE", "de-CH")
    assert Settings().main_language == "de-CH"
    monkeypatch.setenv("LAKANA_MAIN_LANGUAGE", "English")
    with pytest.raises(pydantic.ValidationError, match="is not a language tag Lakana takes"):
        Settings()


def test_health_settings_have_defaults_and_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server's settings carry the health section with its defaults and environment.

    Parameters
    ----------
    monkeypatch
        Sets the environment.
    """
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    assert Settings().health == HealthConfig(retention_days=90)
    monkeypatch.setenv("LAKANA_HEALTH__RETENTION_DAYS", "30")
    assert Settings().health == HealthConfig(retention_days=30)


def test_ui_enabled_reads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`LAKANA_UI__ENABLED=true` turns the web interface on; it is off without it."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    monkeypatch.delenv("LAKANA_UI__ENABLED", raising=False)
    assert Settings().ui.enabled is False
    monkeypatch.setenv("LAKANA_UI__ENABLED", "true")
    assert Settings().ui.enabled is True


def test_ui_enabled_refuses_a_word_that_is_not_a_boolean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value that is neither true nor false stops the start, naming the setting."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    monkeypatch.setenv("LAKANA_UI__ENABLED", "maybe")
    with pytest.raises(pydantic.ValidationError, match=r"ui\.enabled"):
        Settings()


def test_public_url_defaults_to_localhost_with_the_port() -> None:
    """Unset, the public address is `http://localhost:<LAKANA_PORT>`."""
    s = Settings(database=DB, port=8123)
    assert s.public_origin() == PublicOrigin(
        scheme="http", host="localhost", port=8123, origin="http://localhost:8123", loopback=True
    )


@pytest.mark.parametrize(
    ("url", "origin", "port"),
    [
        ("https://lakana.example.com", "https://lakana.example.com", 443),
        ("https://Lakana.Example.com:8443/", "https://lakana.example.com:8443", 8443),
        ("http://localhost:80", "http://localhost", 80),
    ],
)
def test_public_origin_omits_the_default_port_and_lowers_the_name(
    url: str, origin: str, port: int
) -> None:
    """The origin is what a browser sends in `Origin`: lower case, no default port."""
    s = Settings(database=DB, ui=UiConfig(enabled=True, public_url=url))
    assert s.public_origin().origin == origin
    assert s.public_origin().port == port


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "https://10.0.0.5",
        "http://[::1]:8000",
        "https://127.1",
        "https://0x7f.0.0.1",
        "https://lakana.example.42",
    ],
)
def test_public_url_refuses_an_ip_address(url: str) -> None:
    """Passkeys need a name: an address a browser reads as IPv4 or IPv6 is refused."""
    with pytest.raises(
        pydantic.ValidationError, match=r"LAKANA_UI__PUBLIC_URL.*host name.*localhost"
    ):
        Settings(database=DB, ui=UiConfig(public_url=url))


def test_public_url_refuses_plain_http_off_loopback_with_the_interface_on() -> None:
    """A team server needs HTTPS; plain http is only for `localhost`."""
    with pytest.raises(pydantic.ValidationError, match="https://"):
        Settings(database=DB, ui=UiConfig(enabled=True, public_url="http://lakana.example.com"))


@pytest.mark.parametrize(
    "url",
    [
        "https://x.example/lakana",
        "https://x.example/?a=1",
        "https://x.example/#top",
        "https://user:pw@x.example",
        "ftp://x.example",
        "x.example",
        "https://x.example.",
        "https://bücher.example",
        "https://x.example:99999",
        "https://",
    ],
)
def test_public_url_refuses_what_is_not_a_bare_origin(url: str) -> None:
    """A path, query, fragment, user, other scheme, trailing dot or bad port names the setting."""
    with pytest.raises(pydantic.ValidationError, match="LAKANA_UI__PUBLIC_URL"):
        Settings(database=DB, ui=UiConfig(public_url=url))


def test_allowed_host_headers_for_localhost() -> None:
    """Locally: `localhost` and both loopback addresses, each with the port."""
    assert Settings(database=DB, port=8000).allowed_host_headers() == {
        "localhost:8000",
        "127.0.0.1:8000",
        "[::1]:8000",
    }


def test_allowed_host_headers_for_a_team_name() -> None:
    """Behind HTTPS on the default port, the browser sends the bare name."""
    s = Settings(database=DB, ui=UiConfig(public_url="https://lakana.example.com"))
    assert s.allowed_host_headers() == {"lakana.example.com"}


def test_allowed_hosts_add_names_in_lower_case() -> None:
    """`LAKANA_UI__ALLOWED_HOSTS` adds `host[:port]` entries to the public one."""
    s = Settings(
        database=DB,
        ui=UiConfig(
            public_url="https://lakana.example.com", allowed_hosts=["Lakana.internal:8443"]
        ),
    )
    assert s.allowed_host_headers() == {"lakana.example.com", "lakana.internal:8443"}


@pytest.mark.parametrize(
    "entry",
    [
        "*",
        "",
        "https://x.example",
        "x.example/a",
        "x.example:",
        "x.example:abc",
        "a b",
        "10.0.0.5:8000",
        "[::1]",
        "127.1:8000",
    ],
)
def test_allowed_hosts_refuse_what_is_not_a_host_and_port(entry: str) -> None:
    """No wildcard, scheme, path, empty port or IP address: each entry is a name, exactly."""
    with pytest.raises(pydantic.ValidationError, match="LAKANA_UI__ALLOWED_HOSTS"):
        UiConfig(allowed_hosts=[entry])


@pytest.mark.parametrize(
    ("host", "public_url"),
    [("0.0.0.0", None), ("127.0.0.1", "https://lakana.example.com"), ("lakana", None)],
)
def test_local_sign_in_needs_a_loopback_bind_and_localhost(
    host: str, public_url: str | None
) -> None:
    """Local sign-in is for one person's own machine only."""
    with pytest.raises(
        pydantic.ValidationError, match=r"LAKANA_UI__LOCAL_SIGN_IN.*lakana server start --local"
    ):
        Settings(
            database=DB,
            host=host,
            ui=UiConfig(enabled=True, local_sign_in=True, public_url=public_url),
        )


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_local_sign_in_is_accepted_on_a_loopback_bind(host: str) -> None:
    """On a loopback address with the default public address it is accepted."""
    s = Settings(database=DB, host=host, ui=UiConfig(enabled=True, local_sign_in=True))
    assert s.ui.local_sign_in is True


def test_local_sign_in_is_off_by_default() -> None:
    """Only `lakana server start --local` sets it."""
    assert Settings(database=DB).ui.local_sign_in is False


def test_the_interface_settings_read_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`LAKANA_UI__PUBLIC_URL`, `LAKANA_UI__ALLOWED_HOSTS`, `LAKANA_TRUSTED_PROXIES`."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/p.db")
    monkeypatch.setenv("LAKANA_UI__PUBLIC_URL", "https://lakana.example.com")
    monkeypatch.setenv("LAKANA_UI__ALLOWED_HOSTS", '["lakana.internal:8000"]')
    monkeypatch.setenv("LAKANA_TRUSTED_PROXIES", '["10.0.0.2", "10.1.0.0/16"]')
    s = Settings()
    assert s.public_origin().origin == "https://lakana.example.com"
    assert "lakana.internal:8000" in s.allowed_host_headers()
    assert s.trusted_proxies == ["10.0.0.2", "10.1.0.0/16"]


def test_trusted_proxies_default_to_none() -> None:
    """Unset, no forwarded header is trusted."""
    assert Settings(database=DB).trusted_proxies == []


@pytest.mark.parametrize(
    "entry", ["*", "proxy.internal", "10.0.0.300", "", "10.1.0.5/16", "0.0.0.0/0", "::/0"]
)
def test_trusted_proxies_refuse_what_is_not_an_address(entry: str) -> None:
    """Only IP addresses and networks: a wildcard would trust every client's own header."""
    with pytest.raises(pydantic.ValidationError, match="LAKANA_TRUSTED_PROXIES"):
        Settings(database=DB, trusted_proxies=[entry])
