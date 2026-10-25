"""`lakana-launcher`: its entry point, and the start-up failures that end it with status 2."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest

from lakana_worker.launcher import __main__ as entry
from lakana_worker.launcher.engine import EngineError
from lakana_worker.launcher.service import Launcher, StartError

_ENV = {
    "LAKANA_LAUNCHER__STACK": "lakana",
    "LAKANA_LAUNCHER__IMAGES": '{"3.13": "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.13"}',
}


def test_the_console_script_points_at_main() -> None:
    """`pip install` gives a `lakana-launcher` command that reaches `main`."""
    pyproject = Path(entry.__file__).resolve().parents[3] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]

    assert scripts["lakana-launcher"] == "lakana_worker.launcher.__main__:main"
    assert entry.main.__module__ == "lakana_worker.launcher.__main__"


def test_main_starts_the_launcher_and_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings from the environment, then `start`, then `serve`."""
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    calls: list[str] = []
    monkeypatch.setattr(entry, "remove_stale_socket", lambda path: calls.append(f"clear {path}"))
    monkeypatch.setattr(Launcher, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(entry, "serve", lambda launcher: calls.append("serve"))

    entry.main()

    # The stale socket goes first: the start (engine check, probes, pulls) can take minutes, and
    # the bundle's healthcheck must not find an earlier launcher's socket meanwhile.
    assert calls == ["clear /run/lakana-launcher/launcher.sock", "start", "serve"]


def test_main_exits_2_and_logs_the_reason_for_a_wrong_setting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing setting is named, with what to write; no traceback."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as caught:
        entry.main()

    assert caught.value.code == 2
    assert "LAKANA_LAUNCHER__" in caplog.text


@pytest.mark.parametrize(
    "error", [StartError("Docker Engine 28 or later is needed"), EngineError("cannot reach Docker")]
)
def test_main_exits_2_when_the_engine_is_not_fit_or_not_there(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """The reason is the log's last line; the service restarts only when the operator acts."""
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)

    def fail(self: Launcher) -> None:
        raise error

    monkeypatch.setattr(entry, "remove_stale_socket", lambda path: None)
    monkeypatch.setattr(Launcher, "start", fail)
    caplog.set_level(logging.ERROR)

    with pytest.raises(SystemExit) as caught:
        entry.main()

    assert caught.value.code == 2
    assert str(error) in caplog.text


@pytest.mark.parametrize(
    "error", [StartError("the socket directory is wrong"), OSError("address in use")]
)
def test_main_exits_2_when_the_socket_cannot_be_served(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """A planted entry or a wrong directory is a message, not a traceback."""
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(entry, "remove_stale_socket", lambda path: None)
    monkeypatch.setattr(Launcher, "start", lambda self: None)

    def fail(launcher: Launcher) -> None:
        raise error

    monkeypatch.setattr(entry, "serve", fail)

    with pytest.raises(SystemExit) as caught:
        entry.main()

    assert caught.value.code == 2
    assert str(error) in caplog.text


def test_main_exits_2_naming_a_malformed_refused_range(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A range with host bits set stops the launcher before it touches Docker."""
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LAKANA_LAUNCHER__EGRESS_REFUSED", "192.168.1.5/24")
    monkeypatch.setattr(Launcher, "start", lambda self: pytest.fail("the launcher started"))

    with pytest.raises(SystemExit) as caught:
        entry.main()

    assert caught.value.code == 2
    assert (
        "LAKANA_LAUNCHER__EGRESS_REFUSED: `192.168.1.5/24` is not a network (host bits set; did "
        "you mean 192.168.1.0/24?)"
    ) in caplog.text
