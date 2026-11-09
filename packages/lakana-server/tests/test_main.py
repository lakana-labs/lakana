"""Tests for the `lakana-server` console script."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import sqlalchemy

import lakana_server.ui
from lakana_server import __main__ as server_main
from lakana_server.log_config import uvicorn_log_config


def test_the_console_scripts_are_registered() -> None:
    """`lakana-server` resolves to its `main`, `lakana-worker` to `command.cli` (`healthcheck`)."""
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}

    assert scripts["lakana-server"] == "lakana_server.__main__:main"
    assert scripts["lakana-worker"] == "lakana_worker.command:cli"


def test_migrate_upgrades_the_configured_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lakana-server migrate` applies the migrations to LAKANA_DATABASE__URL."""
    url = f"sqlite:///{tmp_path}/lakana.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", url)

    server_main.main(["migrate"])

    # Dispose the engine this test creates, or it
    # leaks a connection (a ResourceWarning under `-W error`).
    engine = sqlalchemy.create_engine(url)
    try:
        assert "environments" in sqlalchemy.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_serve_runs_uvicorn_on_the_configured_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no subcommand it serves `create_app()` on Settings.host/port."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_PORT", "8765")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kw: calls.append(kw))

    server_main.main([])

    [call] = calls
    assert {k: v for k, v in call.items() if k != "log_config"} == {
        "host": "127.0.0.1",
        "port": 8765,
        "log_level": "info",
        "access_log": True,
        "proxy_headers": False,
        "forwarded_allow_ips": "",
    }
    assert call["log_config"] == uvicorn_log_config()


@pytest.mark.parametrize(("given", "expected"), [(None, True), ("false", False), ("true", True)])
def test_uvicorn_access_log_follows_the_log_access_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str | None, expected: bool
) -> None:
    """`LAKANA_LOG__ACCESS=false` turns uvicorn's per-request access log off."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delenv("LAKANA_LOG__ACCESS", raising=False)
    if given is not None:
        monkeypatch.setenv("LAKANA_LOG__ACCESS", given)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kw: calls.append(kw))

    server_main.main([])

    assert calls[0]["access_log"] is expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [(None, "info"), ("DEBUG", "debug"), ("warning", "warning"), ("Error", "error")],
)
def test_uvicorn_gets_the_configured_log_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str | None, expected: str
) -> None:
    """`LAKANA_LOG__LEVEL` is uvicorn's level too, in the lower case it takes."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delenv("LAKANA_LOG__LEVEL", raising=False)
    if given is not None:
        monkeypatch.setenv("LAKANA_LOG__LEVEL", given)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kw: calls.append(kw))

    server_main.main([])

    assert calls[0]["log_level"] == expected


def test_a_bad_log_level_stops_the_server_naming_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal comes before anything is served, and says what to write."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_LOG__LEVEL", "loud")
    with pytest.raises(Exception, match=r"LAKANA_LOG__LEVEL is 'loud'.*DEBUG, INFO, WARNING or"):
        server_main.main([])


def test_an_unknown_subcommand_is_a_usage_error() -> None:
    """Argparse rejects anything but `migrate` with exit code 2."""
    with pytest.raises(SystemExit) as caught:
        server_main.main(["nope"])

    assert caught.value.code == 2


def test_an_interface_that_is_not_built_stops_the_server_with_its_message_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No traceback: the cause and the next steps on stderr, exit code 1, nothing served."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_UI__ENABLED", "true")
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", tmp_path / "ui_dist")
    calls: list[object] = []
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kw: calls.append(app))

    with pytest.raises(SystemExit) as caught:
        server_main.main([])

    assert caught.value.code == 1
    assert calls == []
    err = capsys.readouterr().err
    assert err.startswith("lakana-server: LAKANA_UI__ENABLED is true, but this installation")
    assert "LAKANA_UI__ENABLED=false" in err
    assert "Traceback" not in err


def test_forwarded_headers_are_off_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uvicorn's own default trusts `X-Forwarded-*` from 127.0.0.1; Lakana trusts no one."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delenv("LAKANA_TRUSTED_PROXIES", raising=False)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kw: calls.append(kw))

    server_main.main([])

    assert calls[0]["proxy_headers"] is False
    assert calls[0]["forwarded_allow_ips"] == ""


def test_trusted_proxies_turn_forwarded_headers_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_TRUSTED_PROXIES` names the only peers whose forwarded headers count."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_TRUSTED_PROXIES", '["10.0.0.2"]')
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kw: calls.append(kw))

    server_main.main([])

    assert calls[0]["proxy_headers"] is True
    assert calls[0]["forwarded_allow_ips"] == "10.0.0.2"
