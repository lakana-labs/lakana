"""Tests for lakana-server's runtime settings."""

from pathlib import Path

import pydantic
import pytest

from lakana_core.config import ArtifactsConfig, DatabaseConfig, SecretsConfig
from lakana_server.settings import Settings


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
