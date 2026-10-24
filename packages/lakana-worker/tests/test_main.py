"""Tests for the `python -m lakana_worker` entry point."""

import logging
import math
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

import pytest

from lakana_core.audit.content import install_query_text, stored_query_text
from lakana_core.config import Settings
from lakana_core.projects.limits import DeployLimits
from lakana_worker import deploy_sweep, pipeline
from lakana_worker.__main__ import _python_runtime, configure_logging, main, make_worker_id
from lakana_worker.docker_environments import DockerEnvironments
from lakana_worker.launcher_client import LauncherUnavailable
from lakana_worker.python_support import PythonSupport
from lakana_worker.python_tasks import PythonRuntime
from lakana_worker.runners import DockerRunner, LocalProcessRunner


class _StandInHeartbeat:
    """Stands in for `WorkerHeartbeat`: no thread, no database write."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args, self.kwargs = args, kwargs
        self.thread = mock.MagicMock()
        self.thread.is_alive.return_value = False
        self.stopped = 0
        self.unregistered = 0

    def stale(self) -> bool:
        """Say the heartbeat is fresh.

        Returns
        -------
        bool
            Always `False`.
        """
        return False

    def may_claim(self) -> bool:
        """Say the worker may claim.

        Returns
        -------
        bool
            Always `True`.
        """
        return True

    def start(self) -> mock.MagicMock:
        """Pretend to start the thread.

        Returns
        -------
        mock.MagicMock
            A thread stand-in.
        """
        return self.thread

    def stop(self) -> None:
        """Count a stop request."""
        self.stopped += 1

    def unregister(self) -> None:
        """Count the removal of the row."""
        self.unregistered += 1


@pytest.fixture(autouse=True)
def heartbeat_stand_in(monkeypatch: pytest.MonkeyPatch) -> list[_StandInHeartbeat]:
    """Keep `main()` from starting a real heartbeat thread on an un-migrated SQLite file.

    The tests of the heartbeat itself are in `test_heartbeat.py`; the new tests below read what
    `main` did with the stand-in.

    Parameters
    ----------
    monkeypatch
        The pytest fixture.

    Returns
    -------
    list of _StandInHeartbeat
        Every stand-in `main` built, in order.
    """
    built: list[_StandInHeartbeat] = []

    def build(*args: object, **kwargs: object) -> _StandInHeartbeat:
        """Build and remember a stand-in.

        Parameters
        ----------
        *args
            The heartbeat's positional arguments.
        **kwargs
            The heartbeat's keyword arguments.

        Returns
        -------
        _StandInHeartbeat
            The stand-in.
        """
        built.append(_StandInHeartbeat(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr("lakana_worker.__main__.WorkerHeartbeat", build)
    return built


def test_main_wires_settings_and_engine_into_poll_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`main` builds settings and an engine from them, and hands both to `poll_forever`.

    `authz_factory`'s result must be an `AuditingAuthorization` that wraps a real
    `GrantAuthorization` specifically -- not just any `AuthorizationService` -- since
    this is the only place in the whole repo that exercises `main()`'s wiring at all,
    and `isinstance(..., AuditingAuthorization)` alone would pass just as well for an
    `AllowAllAuthorization` mistakenly swapped in behind it.
    """
    from lakana_core.audit.authorization import AuditingAuthorization
    from lakana_core.grants.authorization import GrantAuthorization
    from lakana_core.projects.run_policy import RunCaps

    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__MAX_RESULT_ROWS", "7")
    monkeypatch.setenv("LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE", "2")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS", "5000")
    monkeypatch.setenv("LAKANA_REPO_ROOTS", '["/srv/repos"]')
    monkeypatch.setenv("LAKANA_RUN__MAX_RETRIES", "3")
    monkeypatch.setenv("LAKANA_RUN__LEASE_SECONDS", "30")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS", "120")
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    monkeypatch.setenv("LAKANA_GIT__CA_BUNDLE", str(bundle))
    monkeypatch.setenv("LAKANA_GIT__HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("LAKANA_GIT__NO_PROXY", "gitlab.corp")
    monkeypatch.setenv("LAKANA_RUN__SERVER_URL", "http://server:8000")
    monkeypatch.setenv("LAKANA_RUN__KILL_GRACE", "3")
    monkeypatch.setenv("LAKANA_RUN__VENV_KEEP", "4")
    monkeypatch.setenv("LAKANA_RUN__VENV_CACHE", str(tmp_path / "venvs"))
    monkeypatch.setenv("LAKANA_RUN__VENV_BUILD_TIMEOUT", "77")
    monkeypatch.setenv("LAKANA_RUN__VENV_MAX_BYTES", "4096")
    monkeypatch.setenv("LAKANA_RUN__INDEX_URL", "https://svc:hunter2@pypi.corp/simple")
    monkeypatch.setenv("LAKANA_RUN__HTTPS_PROXY", "http://svc:hunter2@proxy.corp:3128")
    monkeypatch.setenv("LAKANA_RUN__NO_PROXY", "pypi.corp")
    monkeypatch.setenv("LAKANA_RUN__CA_BUNDLE", str(bundle))
    monkeypatch.setenv("LAKANA_TASK_TOKENS__MAX_TTL_SECONDS", "120")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    from lakana_core.db import create_engine

    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll_forever_mock,
        mock.patch("lakana_worker.__main__.create_engine", wraps=create_engine) as engine_mock,
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal") as signal_mock,
        mock.patch("lakana_worker.python_support.shutil.which", return_value="/usr/local/bin/uv"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()

    assert engine_mock.call_args.kwargs["lease_seconds"] == 30  # the keepalives follow the lease
    poll_forever_mock.assert_called_once()
    session_factory, authz_factory, key_provider = poll_forever_mock.call_args.args
    assert callable(session_factory)
    session = session_factory()
    try:
        assert session.bind is not None
        assert callable(authz_factory)
        authz = authz_factory(session)
        assert isinstance(authz, AuditingAuthorization)
        assert isinstance(authz._wrapped, GrantAuthorization)
        again = authz_factory(session)
        assert isinstance(again, AuditingAuthorization)
        assert again._limiter is authz._limiter
        admitted = [authz._limiter.admit("p", "READ_TABLE")[0] for _ in range(3)]
        assert admitted == [True, True, False]
    finally:
        session.close()
    assert key_provider is not None
    assert poll_forever_mock.call_args.kwargs["limits"].max_result_rows == 7
    deploy_settings = poll_forever_mock.call_args.kwargs["deploy"]
    assert deploy_settings.repo_roots == ("/srv/repos",)
    assert deploy_settings.ca_bundle == bundle
    assert deploy_settings.https_proxy == "http://proxy.corp:3128"
    assert "proxy.corp" not in repr(deploy_settings)
    assert deploy_settings.index_url == "https://svc:hunter2@pypi.corp/simple"
    assert "hunter2" not in repr(deploy_settings)
    assert deploy_settings.no_proxy == "gitlab.corp"
    assert deploy_settings.python.server_url == "http://server:8000"
    assert deploy_settings.caps == RunCaps(max_retries=3, max_timeout_seconds=120)
    assert deploy_settings.limits.statement_chars == 5000
    engine = poll_forever_mock.call_args.kwargs["engine"]
    drain = poll_forever_mock.call_args.kwargs["drain"]
    assert engine.drain is drain
    assert engine.settings.worker_id.count(":") == 2
    assert engine.settings.limits.max_result_rows == 7
    python = engine.python
    assert isinstance(python, PythonRuntime)
    assert python.support == PythonSupport(server_url="http://server:8000", uv="/usr/local/bin/uv")
    assert python.max_ttl_seconds == 120
    assert math.isclose(python.kill_grace, 3.0)
    assert python.limits == DeployLimits()
    assert python.work_dir == tmp_path / "xdg" / "lakana-worker" / "attempts"
    assert python.venvs.uv == "/usr/local/bin/uv"
    assert python.venvs.cache == tmp_path / "venvs"
    assert python.venvs.uv_cache == tmp_path / "xdg" / "lakana-worker" / "uv"
    assert python.venvs.keep == 4
    assert math.isclose(python.venvs.build_timeout, 77.0)
    assert python.venvs.max_bytes == 4096
    assert python.venvs.index_url == "https://svc:hunter2@pypi.corp/simple"
    assert python.venvs.https_proxy == "http://svc:hunter2@proxy.corp:3128"
    assert python.venvs.no_proxy == "pypi.corp"
    assert python.venvs.ca_bundle == bundle
    assert "hunter" not in repr(python)
    assert isinstance(python.runner, LocalProcessRunner)
    assert math.isclose(python.runner._kill_grace, 3.0)
    assert (
        "LAKANA_TASK_TOKENS__MAX_TTL_SECONDS is 120, below the 183 s a Python task with the "
        "default timeout (120 s) needs; such tasks will fail until it is raised"
    ) in caplog.text
    scheduler_mock.assert_called_once()
    assert scheduler_mock.call_args.args[1] == engine.settings.worker_id
    thread = scheduler_mock.return_value.start.return_value
    scheduler_mock.return_value.start.assert_called_once()
    scheduler_mock.return_value.stop.assert_called_once()
    thread.join.assert_called_once_with(timeout=20.0)
    signum, handler = signal_mock.call_args.args
    assert signum == signal.SIGTERM
    handler(signum, None)
    assert drain.is_set()


@pytest.fixture(autouse=True)
def delivery_loop_stand_in(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Keep `main()` from starting a real delivery thread on an un-migrated SQLite file.

    Parameters
    ----------
    monkeypatch
        The pytest fixture.

    Returns
    -------
    mock.MagicMock
        The stand-in for the `DeliveryLoop` class.
    """
    loop = mock.MagicMock()
    loop.return_value.start.return_value.is_alive.return_value = False
    monkeypatch.setattr("lakana_worker.__main__.DeliveryLoop", loop)
    return loop


def test_main_runs_the_delivery_loop_on_its_own_thread_and_stops_it_with_the_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivery_loop_stand_in: mock.MagicMock
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_WEBHOOK__ALLOW_HOSTS", "chat.corp")
    monkeypatch.setenv("LAKANA_SECRETS__MASTER_KEY", "a" * 43 + "=")
    _run_with_loop(tmp_path, monkeypatch, mock.MagicMock())
    delivery_loop_stand_in.assert_called_once()
    cfg = delivery_loop_stand_in.call_args.args[1]
    assert cfg.owner.count(":") == 2
    assert cfg.webhook.allowed_hosts() == ("chat.corp",)
    assert cfg.key_provider is not None
    loop = delivery_loop_stand_in.return_value
    loop.start.assert_called_once()
    loop.stop.assert_called_once()
    loop.start.return_value.join.assert_called_once()


def test_a_worker_without_a_usable_secrets_key_starts_no_delivery_loop_and_says_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery_loop_stand_in: mock.MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delenv("LAKANA_SECRETS__MASTER_KEY", raising=False)
    with caplog.at_level("WARNING", logger="lakana_worker.__main__"):
        _run_with_loop(tmp_path, monkeypatch, mock.MagicMock())
    delivery_loop_stand_in.assert_not_called()
    assert caplog.text.count("LAKANA_SECRETS__MASTER_KEY") == 1


def test_the_delivery_loop_stops_even_when_the_dequeue_loop_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivery_loop_stand_in: mock.MagicMock
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_SECRETS__MASTER_KEY", "a" * 43 + "=")
    with (
        mock.patch("lakana_worker.__main__.poll_forever", side_effect=RuntimeError("boom")),
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(RuntimeError),
    ):
        main()
    delivery_loop_stand_in.return_value.stop.assert_called_once()


def test_main_starts_no_delivery_loop_when_notifications_are_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery_loop_stand_in: mock.MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_WORKER__NOTIFICATIONS", "false")
    with caplog.at_level("INFO", logger="lakana_worker.__main__"):
        _run_with_loop(tmp_path, monkeypatch, mock.MagicMock())
    delivery_loop_stand_in.assert_not_called()
    assert "LAKANA_WORKER__NOTIFICATIONS" in caplog.text


def test_main_registers_the_configured_takes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_stand_in: list[_StandInHeartbeat],
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_WORKER__TAKES", "Pipeline")
    _run_with_loop(tmp_path, monkeypatch, mock.MagicMock())
    (heartbeat,) = heartbeat_stand_in
    assert heartbeat.kwargs["takes"] == "pipeline"


def test_main_stops_and_joins_the_scheduler_even_when_the_loop_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resign()` runs on the way out: stop is requested and the thread joined."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    with (
        mock.patch("lakana_worker.__main__.poll_forever", side_effect=RuntimeError("boom")),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(RuntimeError),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    scheduler_mock.return_value.stop.assert_called_once()
    scheduler_mock.return_value.start.return_value.join.assert_called_once()


def test_main_warns_when_the_scheduler_thread_does_not_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stuck scheduler thread delays shutdown by a bounded time, and says so."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_RUN__DRAIN_GRACE_SECONDS", "1")
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        caplog.at_level("WARNING", logger="lakana_worker.__main__"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = True
        main()
    scheduler_mock.return_value.start.return_value.join.assert_called_once_with(timeout=5.0)
    assert "did not stop within 5 s" in caplog.text


def test_main_warns_when_the_notification_thread_does_not_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery_loop_stand_in: mock.MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delivery thread stuck in a send delays shutdown by a bounded time, and says so."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_SECRETS__MASTER_KEY", "a" * 43 + "=")
    monkeypatch.setenv("LAKANA_RUN__DRAIN_GRACE_SECONDS", "1")
    thread = delivery_loop_stand_in.return_value.start.return_value
    thread.is_alive.return_value = True
    with caplog.at_level("WARNING", logger="lakana_worker.__main__"):
        _run_with_loop(tmp_path, monkeypatch, mock.MagicMock())
    thread.join.assert_called_once_with(timeout=5.0)
    assert "The notification thread did not stop within 5 s" in caplog.text


def test_main_refuses_a_ca_bundle_the_worker_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker, not the shared settings, checks the CA file, with a clear message."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_GIT__CA_BUNDLE", str(tmp_path / "missing.pem"))
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit, match=r"LAKANA_GIT__CA_BUNDLE.*readable file"),
    ):
        main()
    poll.assert_not_called()
    (tmp_path / "dir").mkdir()
    monkeypatch.setenv("LAKANA_GIT__CA_BUNDLE", str(tmp_path / "dir"))
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit, match="LAKANA_GIT__CA_BUNDLE"),
    ):
        main()
    poll.assert_not_called()


@pytest.mark.parametrize("variable", ["AWS_CA_BUNDLE", "SSL_CERT_FILE"])
def test_main_refuses_a_query_ca_bundle_that_is_not_a_pem_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """A bad bundle stops the worker at start, before any query child, naming the variable."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
    monkeypatch.setenv(variable, str(tmp_path / "missing.pem"))

    def no_child(*_: object, **__: object) -> None:
        raise AssertionError("no query process may be started with a bad CA bundle")

    monkeypatch.setattr("lakana_worker.__main__.check_limits", no_child)
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit) as caught,
    ):
        main()
    assert str(caught.value) == (
        f"{variable} names {tmp_path / 'missing.pem'}, which is not a readable file of PEM "
        "certificates. Point it at your object store's certificate authority, or unset it."
    )
    poll.assert_not_called()


def test_main_refuses_a_query_ca_bundle_that_holds_a_private_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A combined certificate-and-key file stops the worker before any query child."""
    bundle = tmp_path / "combined.pem"
    bundle.write_bytes(
        b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        + "".join(
            f"{'-' * 5}{edge} EC PRIVATE KEY{'-' * 5}\n{body}"
            for edge, body in (("BEGIN", "KEYDATA\n"), ("END", ""))
        ).encode()
    )
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("AWS_CA_BUNDLE", str(bundle))
    monkeypatch.setattr(
        "lakana_worker.__main__.check_limits",
        lambda *_a, **_k: pytest.fail("no query process may start with a key in the bundle"),
    )
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit) as caught,
    ):
        main()
    assert "AWS_CA_BUNDLE" in str(caught.value) and str(bundle) in str(caught.value)
    assert "private key" in str(caught.value) and "KEYDATA" not in str(caught.value)
    poll.assert_not_called()


def test_main_logs_the_query_ca_bundle_it_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The start-up log names the bundle's path, never its content."""
    bundle = tmp_path / "store-ca.pem"
    bundle.write_bytes(b"-----BEGIN CERTIFICATE-----\nSECRETISH\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("AWS_CA_BUNDLE", str(bundle))
    monkeypatch.setattr("lakana_worker.__main__.check_limits", lambda *_a, **_k: None)
    with (
        caplog.at_level("INFO", logger="lakana_worker.__main__"),
        mock.patch("lakana_worker.__main__.poll_forever", side_effect=SystemExit(0)),
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit),
    ):
        main()
    assert (
        f"Query processes trust the certificate authorities in {bundle} (AWS_CA_BUNDLE)."
        in caplog.text
    )
    assert "SECRETISH" not in caplog.text


def test_main_refuses_a_run_ca_bundle_that_is_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory is not a bundle either: `LAKANA_RUN__CA_BUNDLE` names its own variable."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delenv("LAKANA_GIT__CA_BUNDLE", raising=False)
    (tmp_path / "ca").mkdir()
    monkeypatch.setenv("LAKANA_RUN__CA_BUNDLE", str(tmp_path / "ca"))
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit, match="LAKANA_RUN__CA_BUNDLE must name a readable file"),
    ):
        main()
    poll.assert_not_called()


def test_main_refuses_a_webhook_ca_bundle_the_worker_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_WEBHOOK__CA_BUNDLE` is checked at startup and named in the message."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_WEBHOOK__CA_BUNDLE", str(tmp_path / "missing.pem"))
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit, match="LAKANA_WEBHOOK__CA_BUNDLE must name a readable file"),
    ):
        main()
    poll.assert_not_called()


@pytest.mark.parametrize(
    ("server_url", "uv", "expected"),
    [
        (
            "http://server:8000",
            "/usr/local/bin/uv",
            "Python assets: enabled (uv at /usr/local/bin/uv)",
        ),
        (
            "http://server:8000",
            None,
            "Python assets: disabled: uv is not on the worker's PATH. To enable it: install uv "
            "where the worker runs and put it on the worker's PATH",
        ),
        (
            None,
            "/usr/local/bin/uv",
            "Python assets: disabled: LAKANA_RUN__SERVER_URL is not set. To enable it: set "
            "LAKANA_RUN__SERVER_URL to the address the worker reaches the server at",
        ),
    ],
)
def test_main_logs_whether_it_can_run_python_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    server_url: str | None,
    uv: str | None,
    expected: str,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    for name in ("LAKANA_RUN__SERVER_URL", "LAKANA_RUN__CA_BUNDLE", "LAKANA_GIT__CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    if server_url is not None:
        monkeypatch.setenv("LAKANA_RUN__SERVER_URL", server_url)
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        mock.patch("lakana_worker.python_support.shutil.which", return_value=uv),
        caplog.at_level("INFO", logger="lakana_worker.__main__"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    assert expected in caplog.text


def test_main_refuses_a_run_ca_bundle_the_worker_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delenv("LAKANA_GIT__CA_BUNDLE", raising=False)
    monkeypatch.setenv("LAKANA_RUN__CA_BUNDLE", str(tmp_path / "missing.pem"))
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit, match="LAKANA_RUN__CA_BUNDLE must name a readable file"),
    ):
        main()
    poll.assert_not_called()


def test_main_builds_a_python_runtime_with_default_caches_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No uv and no settings: the runtime exists (it reports why a task cannot run)."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    for name in (
        "LAKANA_RUN__SERVER_URL",
        "LAKANA_RUN__CA_BUNDLE",
        "LAKANA_GIT__CA_BUNDLE",
        "LAKANA_RUN__VENV_CACHE",
        "LAKANA_RUN__INDEX_URL",
        "LAKANA_RUN__HTTPS_PROXY",
        "LAKANA_GIT__HTTPS_PROXY",
        "LAKANA_TASK_TOKENS__MAX_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll_forever_mock,
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        mock.patch("lakana_worker.python_support.shutil.which", return_value=None),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    python = poll_forever_mock.call_args.kwargs["engine"].python
    assert isinstance(python, PythonRuntime)
    assert python.support.reason() == "LAKANA_RUN__SERVER_URL is not set"
    assert python.venvs.uv == "uv"
    assert python.venvs.cache == tmp_path / "xdg" / "lakana-worker" / "venvs"
    assert (python.venvs.index_url, python.venvs.https_proxy) == (None, None)
    assert python.max_ttl_seconds == 86_400
    assert "LAKANA_TASK_TOKENS__MAX_TTL_SECONDS" not in caplog.text
    assert poll_forever_mock.call_args.kwargs["deploy"].https_proxy is None
    assert poll_forever_mock.call_args.kwargs["deploy"].index_url is None
    assert not (tmp_path / "xdg").exists()
    assert python.sweeper is not None
    assert poll_forever_mock.call_args.kwargs["maintenance"] == python.maintain
    attempts = tmp_path / "xdg" / "lakana-worker" / "attempts"
    assert python.work_dir == attempts
    assert python.sweeper._work_dir == attempts
    assert math.isclose(python.sweeper._interval, 3600.0)
    # token lifetime (900 s task timeout + 10 s kill grace + 60 s margin) + 600 s venv build
    assert math.isclose(python.sweeper._max_age, 900 + 10 + 60 + 600)


@pytest.mark.parametrize(
    ("setting", "limit"), [("2GB", 2 * 1024**3), ("512MB", 512 * 1024**2), ("off", None)]
)
def test_main_gives_the_task_runner_the_limit_the_watchdog_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, limit: int | None
) -> None:
    """Polars inside a task is told the same limit the watchdog stops the worker at."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LAKANA_WORKER__MEMORY_LIMIT", setting)
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll_forever_mock,
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.start_watchdog"),
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    runner = poll_forever_mock.call_args.kwargs["engine"].python.runner
    assert isinstance(runner, LocalProcessRunner)
    assert runner._memory_limit == limit


def _run_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **environment: str
) -> tuple[mock.MagicMock, mock.MagicMock]:
    """Run `main` with the scheduler and the dequeue loop replaced.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        The fixture.
    **environment
        Extra environment variables.

    Returns
    -------
    tuple
        The `SchedulerLoop` mock and the `sweep_artifacts` mock.
    """
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.sweep_artifacts") as sweep_mock,
        mock.patch("lakana_worker.__main__.sweep_code_archives"),
        mock.patch("lakana_worker.__main__.sweep_table_locations"),
        mock.patch("lakana_worker.__main__.backfill_table_columns"),
        mock.patch("lakana_worker.__main__.measure_table_sizes"),
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
        # The housekeeping callable is built inside `main`; call it while `sweep_artifacts`
        # is still the recorder.
        stop = threading.Event()
        for _ in range(2):
            scheduler_mock.call_args.kwargs["housekeeping"](
                datetime(2026, 10, 5, 12, 0, tzinfo=UTC), stop
            )
    return scheduler_mock, sweep_mock


def test_the_scheduler_gets_the_artifact_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.config import Settings
    from lakana_core.storage.resolver import StorageResolver

    scheduler_mock, sweep_mock = _run_main(
        tmp_path,
        monkeypatch,
        LAKANA_RUN__SWEEP_INTERVAL="90",
        LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS="1234",
    )
    settings = Settings()
    kwargs = scheduler_mock.call_args.kwargs
    assert math.isclose(kwargs["housekeeping_interval"], settings.run.sweep_interval)
    assert sweep_mock.call_count == 2
    first, second = sweep_mock.call_args_list
    assert isinstance(first.args[1], StorageResolver)
    assert first.kwargs["now"] == datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    assert first.kwargs["upload_ttl_seconds"] == 1234
    assert "max_seconds" not in first.kwargs  # the sweep's own deadline, off the scheduler thread
    # The rotation grows by one per run of this process, never from the clock.
    assert (first.kwargs["rotation"], second.kwargs["rotation"]) == (0, 1)
    stop = first.kwargs["should_stop"]
    assert stop() is False


def _housekeeping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifacts: mock.MagicMock,
    code: mock.MagicMock,
    fires: mock.MagicMock | None = None,
    reads: mock.MagicMock | None = None,
    columns: mock.MagicMock | None = None,
    locations: mock.MagicMock | None = None,
    sizes: mock.MagicMock | None = None,
) -> None:
    """Run `main`, then the housekeeping callable it gave the scheduler, once.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        The fixture.
    artifacts
        Stands in for `sweep_artifacts`.
    code
        Stands in for `sweep_code_archives`.
    fires
        Stands in for `sweep_job_fires`; a mock that succeeds when `None`.
    reads
        Stands in for `sweep_table_reads`; a mock that succeeds when `None`.
    columns
        Stands in for `backfill_table_columns`; a mock that succeeds when `None`.
    locations
        Stands in for `sweep_table_locations`; a mock that succeeds when `None`.
    sizes
        Stands in for `measure_table_sizes`; a mock that succeeds when `None`.
    """
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.sweep_artifacts", artifacts),
        mock.patch("lakana_worker.__main__.sweep_code_archives", code),
        mock.patch("lakana_worker.__main__.sweep_job_fires", fires or mock.MagicMock()),
        mock.patch("lakana_worker.__main__.sweep_table_reads", reads or mock.MagicMock()),
        mock.patch("lakana_worker.__main__.backfill_table_columns", columns or mock.MagicMock()),
        mock.patch("lakana_worker.__main__.sweep_table_locations", locations or mock.MagicMock()),
        mock.patch("lakana_worker.__main__.measure_table_sizes", sizes or mock.MagicMock()),
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
        scheduler_mock.call_args.kwargs["housekeeping"](
            datetime(2026, 10, 5, 12, 0, tzinfo=UTC), threading.Event()
        )


def test_housekeeping_also_sweeps_the_code_archives_with_the_same_time_and_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts, code = mock.MagicMock(), mock.MagicMock()
    _housekeeping(tmp_path, monkeypatch, artifacts, code)
    assert code.call_count == 1
    assert code.call_args.kwargs["now"] == artifacts.call_args.kwargs["now"]
    assert code.call_args.kwargs["rotation"] == artifacts.call_args.kwargs["rotation"] == 0
    assert code.call_args.kwargs["should_stop"]() is False


def test_a_failing_artifact_sweep_does_not_stop_the_code_archive_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    artifacts, code = mock.MagicMock(side_effect=RuntimeError("boom")), mock.MagicMock()
    with caplog.at_level(logging.WARNING):
        _housekeeping(tmp_path, monkeypatch, artifacts, code)
    assert code.call_count == 1
    assert "RuntimeError" in caplog.text
    assert "sweep_failed" in {getattr(r, "event", None) for r in caplog.records}


def test_a_failing_code_archive_sweep_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    artifacts, code = mock.MagicMock(), mock.MagicMock(side_effect=RuntimeError("boom"))
    with caplog.at_level(logging.WARNING):
        _housekeeping(tmp_path, monkeypatch, artifacts, code)
    assert artifacts.call_count == 1
    assert "RuntimeError" in caplog.text
    assert "sweep_failed" in {getattr(r, "event", None) for r in caplog.records}


def test_housekeeping_also_deletes_the_old_job_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fires = mock.MagicMock()
    _housekeeping(tmp_path, monkeypatch, mock.MagicMock(), mock.MagicMock(), fires)
    assert fires.call_count == 1


def test_a_failing_job_fire_sweep_is_logged_and_the_others_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    artifacts, code = mock.MagicMock(), mock.MagicMock()
    with caplog.at_level(logging.WARNING):
        _housekeeping(tmp_path, monkeypatch, artifacts, code, mock.MagicMock(side_effect=OSError))
    assert artifacts.call_count == code.call_count == 1
    assert "OSError" in caplog.text
    assert "sweep_failed" in {getattr(r, "event", None) for r in caplog.records}


def _housekeeping_with_table_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reads: mock.MagicMock
) -> None:
    """Run `main` with `LAKANA_TABLE_READS__KEEP_DAYS=45`, then its housekeeping once.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        The fixture.
    reads
        Stands in for `sweep_table_reads`.
    """
    monkeypatch.setenv("LAKANA_TABLE_READS__KEEP_DAYS", "45")
    _housekeeping(tmp_path, monkeypatch, mock.MagicMock(), mock.MagicMock(), reads=reads)


def test_housekeeping_also_deletes_the_reads_past_the_kept_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = mock.MagicMock()
    _housekeeping_with_table_reads(tmp_path, monkeypatch, reads)
    assert reads.call_count == 1
    assert reads.call_args.kwargs == {
        "keep_days": 45,
        "now": datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    }


def test_a_failing_table_reads_sweep_is_logged_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _housekeeping_with_table_reads(tmp_path, monkeypatch, mock.MagicMock(side_effect=OSError))
    assert "The table reads sweep failed (OSError)" in caplog.text
    assert "sweep_failed" in {getattr(r, "event", None) for r in caplog.records}


def test_housekeeping_also_stores_the_columns_of_tables_that_have_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.storage.resolver import StorageResolver

    columns = mock.MagicMock()
    _housekeeping(tmp_path, monkeypatch, mock.MagicMock(), mock.MagicMock(), columns=columns)
    assert columns.call_count == 1
    assert isinstance(columns.call_args.args[1], StorageResolver)


def test_a_failing_column_backfill_is_logged_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _housekeeping(
            tmp_path,
            monkeypatch,
            mock.MagicMock(),
            mock.MagicMock(),
            columns=mock.MagicMock(side_effect=OSError),
        )
    assert "The table column backfill failed (OSError)" in caplog.text
    assert "sweep_failed" in {getattr(r, "event", None) for r in caplog.records}


def test_housekeeping_also_sweeps_table_locations_with_the_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.storage.resolver import StorageResolver

    monkeypatch.setenv("LAKANA_TABLES__ABANDONED_GRACE_HOURS", "36")
    artifacts, locations = mock.MagicMock(), mock.MagicMock()
    _housekeeping(tmp_path, monkeypatch, artifacts, mock.MagicMock(), locations=locations)
    assert locations.call_count == 1
    call = locations.call_args
    assert isinstance(call.args[1], StorageResolver)
    assert call.kwargs["now"] == artifacts.call_args.kwargs["now"]
    assert call.kwargs["rotation"] == artifacts.call_args.kwargs["rotation"] == 0
    assert call.kwargs["grace_hours"] == 36
    assert call.kwargs["should_stop"]() is False


def test_housekeeping_expires_tables_with_the_undo_period_and_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKANA_TABLES__UNDO_DAYS", "3")
    monkeypatch.setenv("LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP", "12")
    locations = mock.MagicMock()
    _housekeeping(tmp_path, monkeypatch, mock.MagicMock(), mock.MagicMock(), locations=locations)
    assert locations.call_args.kwargs["undo_days"] == 3
    assert locations.call_args.kwargs["max_expiries"] == 12


def test_a_failing_table_location_sweep_is_logged_and_the_others_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    columns = mock.MagicMock()
    with caplog.at_level(logging.WARNING):
        _housekeeping(
            tmp_path,
            monkeypatch,
            mock.MagicMock(),
            mock.MagicMock(),
            columns=columns,
            locations=mock.MagicMock(side_effect=OSError),
        )
    assert columns.call_count == 1
    assert "The table location sweep failed (OSError)" in caplog.text
    assert "sweep_failed" in {getattr(r, "event", None) for r in caplog.records}


def test_the_start_up_log_says_when_nothing_collects_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        scheduler_mock, _ = _run_main(tmp_path, monkeypatch, LAKANA_RUN__SWEEP_INTERVAL="0")
    assert math.isclose(scheduler_mock.call_args.kwargs["housekeeping_interval"], 0.0)
    lines = [
        r.getMessage() for r in caplog.records if "LAKANA_RUN__SWEEP_INTERVAL=0" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "artifact" in lines[0] and "not removed" in lines[0]
    assert "tables whose create never finished" in lines[0]
    assert "package cache is not trimmed" in lines[0]


def test_the_worker_warns_at_start_up_only_when_old_format_secrets_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One WARNING when `LAKANA_SECRETS__ACCEPT_FORMAT_1` is on, and the provider carries it."""
    with caplog.at_level(logging.WARNING):
        _run_main(tmp_path, monkeypatch)
    assert "ACCEPT_FORMAT_1" not in caplog.text

    with caplog.at_level(logging.WARNING):
        _run_main(tmp_path, monkeypatch, LAKANA_SECRETS__ACCEPT_FORMAT_1="true")
    lines = [r.getMessage() for r in caplog.records if "ACCEPT_FORMAT_1" in r.getMessage()]
    assert len(lines) == 1
    assert "any old-format ciphertext" in lines[0] and "lakana secret rotate" in lines[0]


def test_the_worker_hands_the_setting_to_its_key_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's key provider reads format 1 exactly when the setting is on."""
    from lakana_worker.__main__ import RunEngine

    seen: list[bool] = []
    for environment in ({}, {"LAKANA_SECRETS__ACCEPT_FORMAT_1": "true"}):
        with mock.patch("lakana_worker.__main__.RunEngine", wraps=RunEngine) as engine:
            _run_main(tmp_path, monkeypatch, **environment)
        seen.append(engine.call_args.kwargs["key_provider"].accepts_format_1)
        monkeypatch.delenv("LAKANA_SECRETS__ACCEPT_FORMAT_1", raising=False)
    assert seen == [False, True]


def test_the_start_up_log_is_quiet_when_the_sweep_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        _run_main(tmp_path, monkeypatch)
    assert "LAKANA_RUN__SWEEP_INTERVAL=0" not in caplog.text


def test_main_starts_the_memory_watchdog_with_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_WORKER__MEMORY_LIMIT` is passed on as written; unset is `None`."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    seen: list[object] = []
    for value in (None, "2g"):
        if value is None:
            monkeypatch.delenv("LAKANA_WORKER__MEMORY_LIMIT", raising=False)
        else:
            monkeypatch.setenv("LAKANA_WORKER__MEMORY_LIMIT", value)
        with (
            mock.patch(
                "lakana_worker.__main__.start_watchdog", side_effect=lambda *a: seen.append(a[0])
            ),
            mock.patch("lakana_worker.__main__.poll_forever"),
            mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
            mock.patch("lakana_worker.__main__.signal.signal"),
            mock.patch("lakana_worker.__main__.RunEngine"),
        ):
            scheduler_mock.return_value.start.return_value.is_alive.return_value = False
            main()
    assert seen == [None, "2g"]


def test_main_installs_the_drain_for_running_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running query gets the drain grace, then is asked to stop and killed."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_RUN__DRAIN_GRACE_SECONDS", "7")
    installed: list[tuple[object, ...]] = []
    with (
        mock.patch(
            "lakana_worker.__main__.install_drain", side_effect=lambda *a: installed.append(a)
        ),
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        mock.patch("lakana_worker.__main__.RunEngine"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    assert len(installed) == 1
    assert installed[0][0] is poll.call_args.kwargs["drain"]
    assert installed[0][1] == pytest.approx(7.0)


def test_main_installs_the_maintenance_timeout_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_RUN__MAINTENANCE_TIMEOUT` reaches the pipeline's deadline."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_RUN__MAINTENANCE_TIMEOUT", "7")
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        mock.patch("lakana_worker.__main__.RunEngine"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    assert pytest.approx(7.0) == pipeline._MAINTENANCE_TIMEOUT


def test_main_installs_what_the_audit_stores_of_query_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_AUDIT__QUERY_TEXT` reaches every `QUERY_COMPLETED` the worker writes."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_AUDIT__QUERY_TEXT", "off")
    try:
        with (
            mock.patch("lakana_worker.__main__.poll_forever"),
            mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
            mock.patch("lakana_worker.__main__.signal.signal"),
            mock.patch("lakana_worker.__main__.RunEngine"),
        ):
            scheduler_mock.return_value.start.return_value.is_alive.return_value = False
            main()
        assert stored_query_text("SELECT 1") is None
    finally:
        install_query_text("full")


_CHILD_TIMEOUT_SECONDS = 120


def _child(code: str, tmp_path: Path, **environment: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter, where no test fixture has touched logging.

    Parameters
    ----------
    code
        The program text.
    tmp_path
        Pytest's per-test directory (the child's database and cache).
    **environment
        Extra environment variables.

    Returns
    -------
    subprocess.CompletedProcess
        The finished child, output captured as text.
    """
    env = {
        **os.environ,
        "LAKANA_DATABASE__URL": f"sqlite:///{tmp_path}/lakana.db",
        "XDG_CACHE_HOME": str(tmp_path / "xdg"),
        "TMPDIR": str(tmp_path),  # the child's sweeps must never see the machine's temp directory
        **environment,
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )


def test_the_entry_point_prints_info_lines_with_time_level_and_name(tmp_path: Path) -> None:
    """In a fresh process, `configure_logging` makes an INFO line appear on stderr."""
    done = _child(
        "import logging\n"
        "from lakana_worker.__main__ import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger('lakana_worker.probe').info('hello from the worker')\n"
        "logging.getLogger('lakana_worker.probe').debug('not shown')\n",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    lines = done.stderr.splitlines()
    assert len(lines) == 1
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO lakana_worker\.probe: hello from the worker",
        lines[0],
    )


def test_importing_the_worker_package_configures_no_logging(tmp_path: Path) -> None:
    """A process that imports `lakana_worker` as a library keeps its own logging."""
    done = _child(
        "import logging\n"
        "import lakana_worker, lakana_worker.__main__\n"
        "print(len(logging.getLogger().handlers), logging.getLogger().level)\n",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["0", str(logging.WARNING)]


def test_the_entry_point_adds_no_handler_where_one_exists() -> None:
    """An embedding process, or a test, that configured logging keeps exactly what it set up."""
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    assert before  # pytest installs its capture handlers
    configure_logging()
    assert root.handlers == before
    assert root.level == level


@pytest.mark.slow
def test_the_sweeps_off_notice_is_printed_by_the_real_entry_point(tmp_path: Path) -> None:
    """`main` in a fresh process prints the start-up notice for `LAKANA_RUN__SWEEP_INTERVAL=0`."""
    done = _child(
        "from unittest import mock\n"
        "import lakana_worker.__main__ as m\n"
        "with (mock.patch.object(m, 'start_watchdog'),\n"
        "      mock.patch.object(m, 'sweep_spill_directories'),\n"
        "      mock.patch.object(m, 'poll_forever'),\n"
        "      mock.patch.object(m, 'SchedulerLoop') as loop,\n"
        "      mock.patch.object(m.signal, 'signal')):\n"
        "    loop.return_value.start.return_value.is_alive.return_value = False\n"
        "    m.main()\n",
        tmp_path,
        LAKANA_RUN__SWEEP_INTERVAL="0",
    )
    assert done.returncode == 0, done.stderr
    assert "INFO lakana_worker.__main__: LAKANA_RUN__SWEEP_INTERVAL=0: the sweeps are off" in (
        done.stderr
    )


@pytest.mark.slow
def test_the_start_up_lines_are_printed_when_the_module_runs_as_main(tmp_path: Path) -> None:
    """Under `python -m lakana_worker` the module's own INFO lines reach stderr at the default.

    Run as `__main__` the logger name must still sit under `lakana_worker`, or the level the
    set-up gives Lakana's loggers does not reach it and every start-up line is dropped.
    """
    done = _child(
        "import runpy\n"
        "from unittest import mock\n"
        "with (mock.patch('lakana_worker.memory.start_watchdog'),\n"
        "      mock.patch('lakana_worker.spill.sweep_spill_directories'),\n"
        "      mock.patch('lakana_worker.poller.poll_forever'),\n"
        "      mock.patch('lakana_worker.scheduler.SchedulerLoop') as loop,\n"
        "      mock.patch('signal.signal')):\n"
        "    loop.return_value.start.return_value.is_alive.return_value = False\n"
        "    runpy.run_module('lakana_worker', run_name='__main__')\n",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert re.search(r" INFO lakana_worker\.__main__: Python assets: ", done.stderr), done.stderr


@pytest.mark.slow
def test_a_bad_log_level_stops_both_entry_points_before_any_side_effect(tmp_path: Path) -> None:
    """`python -m lakana_server` and `python -m lakana_worker` refuse `LAKANA_LOG__LEVEL=loud`.

    The refusal names the setting, and nothing else has happened: the database file the URL
    names was not created.
    """
    for module in ("lakana_server", "lakana_worker"):
        env = {
            **os.environ,
            "LAKANA_DATABASE__URL": f"sqlite:///{tmp_path}/{module}.db",
            "XDG_CACHE_HOME": str(tmp_path / "xdg"),
            "LAKANA_LOG__LEVEL": "loud",
        }
        done = subprocess.run(
            [sys.executable, "-m", module],
            env=env,
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_SECONDS,
            check=False,
        )
        assert done.returncode != 0, module
        assert "LAKANA_LOG__LEVEL is 'loud'" in done.stderr, (module, done.stderr)
        assert not (tmp_path / f"{module}.db").exists(), module
        assert not (tmp_path / "xdg").exists(), module


def test_the_entry_point_sets_lakana_loggers_to_info_where_no_handler_exists() -> None:
    """With no root handler, the default sets the root to WARNING and Lakana's loggers to INFO."""
    names = ("lakana_core", "lakana_server", "lakana_worker", "lakana")
    before = {name: logging.getLogger(name).level for name in names}
    try:
        with (
            mock.patch.object(logging.getLogger(), "handlers", []),
            mock.patch("lakana_worker.__main__.logging.basicConfig") as basic_config,
        ):
            configure_logging()
        basic_config.assert_called_once()
        assert basic_config.call_args.kwargs["level"] == logging.WARNING
        assert [logging.getLogger(name).level for name in names] == [logging.INFO] * 4
    finally:
        for name, level in before.items():
            logging.getLogger(name).setLevel(level)


def test_main_hands_the_configured_log_level_to_the_log_set_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_LOG__LEVEL` reaches `configure_logging`, in upper case."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_LOG__LEVEL", "debug")
    with (
        mock.patch("lakana_worker.__main__.configure_logging") as configure,
        mock.patch("lakana_worker.__main__.start_watchdog"),
        mock.patch("lakana_worker.__main__.sweep_spill_directories"),
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    configure.assert_called_once_with("DEBUG")


def test_main_refuses_a_bad_log_level_before_it_does_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A level that is not one of the four stops the worker with a message naming the setting."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_LOG__LEVEL", "loud")
    with pytest.raises(Exception, match="LAKANA_LOG__LEVEL is 'loud'"):
        main()


def test_main_starts_and_warns_once_per_setting_about_a_token_like_url_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A segment that looks like a token is a warning at start-up, not a refusal."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    for name in ("LAKANA_RUN__CA_BUNDLE", "LAKANA_GIT__CA_BUNDLE", "LAKANA_RUN__HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAKANA_RUN__INDEX_URL", "https://pkg.corp/my-project-123456/simple/")
    monkeypatch.setenv("LAKANA_GIT__HTTPS_PROXY", "https://proxy.corp/Team-Python-Mirror-01/")
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        caplog.at_level("WARNING", logger="lakana_worker.__main__"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    poll.assert_called_once()
    warned = [r.getMessage() for r in caplog.records]
    assert sum("LAKANA_RUN__INDEX_URL" in m for m in warned) == 1
    assert sum("LAKANA_GIT__HTTPS_PROXY" in m for m in warned) == 1
    assert not any("123456" in m or "Team-Python" in m or "corp" in m for m in warned)


def test_main_writes_the_pending_denial_counts_when_the_worker_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limiter's pending counts are flushed once, with a session of the worker's own."""
    from lakana_core.audit.denial_limiter import DenialAuditLimiter

    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    for name in ("LAKANA_RUN__CA_BUNDLE", "LAKANA_GIT__CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        mock.patch("lakana_worker.__main__.flush_suppressed") as flush,
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    flush.assert_called_once()
    assert isinstance(flush.call_args.args[0], DenialAuditLimiter)


def test_main_refuses_to_start_where_the_python_has_no_waitid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Python without `os.waitid` (macOS before 3.13) stops the worker at start."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delattr(os, "waitid")

    def no_child(*_: object, **__: object) -> None:
        raise AssertionError("no query process may be started on a Python without os.waitid")

    monkeypatch.setattr("lakana_worker.__main__.check_limits", no_child)
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit) as caught,
    ):
        main()
    assert str(caught.value) == (
        "The worker cannot start: this Python lacks os.waitid, which the worker needs to "
        "supervise query processes (macOS has it from Python 3.13). Run the worker with "
        "Python 3.13 or newer."
    )
    poll.assert_not_called()


def test_the_worker_and_local_mode_say_the_same_thing_about_a_python_without_waitid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`lakana server start --local` repeats the worker's key sentence (no import between).

    The two packages do not import each other, so the sentence is written twice; this test
    (the worker's tests may import `lakana`) keeps the two in step.
    """
    from lakana.cli import server as local_mode
    from lakana.cli._state import CliError
    from lakana_worker.__main__ import _check_process_supervision

    monkeypatch.delattr(os, "waitid")
    key = (
        "this Python lacks os.waitid, which the worker needs to supervise query processes "
        "(macOS has it from Python 3.13)"
    )
    with pytest.raises(SystemExit) as worker:
        _check_process_supervision()
    with pytest.raises(CliError) as cli:
        local_mode._check_process_supervision()
    assert key in str(worker.value)
    assert key in str(cli.value)
    assert "3.13 or newer" in str(worker.value) and "3.13 or newer" in (cli.value.hint or "")


def test_main_refuses_a_memory_limit_duckdb_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed query memory limit stops the worker at start-up, not every query."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__MEMORY_LIMIT", "lots")
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll,
        mock.patch("lakana_worker.__main__.SchedulerLoop"),
        mock.patch("lakana_worker.__main__.signal.signal"),
        pytest.raises(SystemExit, match="LAKANA_QUERY_LIMITS__MEMORY_LIMIT"),
    ):
        main()
    poll.assert_not_called()


def _run_with_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poll: mock.MagicMock | None = None
) -> mock.MagicMock:
    """Run `main` with the loop and the scheduler replaced.

    Parameters
    ----------
    tmp_path
        Holds the database file.
    monkeypatch
        Sets the environment.
    poll
        The stand-in for `poll_forever`; a new mock when `None`.

    Returns
    -------
    mock.MagicMock
        The stand-in for `poll_forever`.
    """
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    poll = poll or mock.MagicMock()
    with (
        mock.patch("lakana_worker.__main__.poll_forever", poll),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    return poll


def test_main_starts_the_heartbeat_before_the_loop_and_hands_the_loop_its_id_and_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_stand_in: list[_StandInHeartbeat],
) -> None:
    from lakana_core.config import Settings
    from lakana_worker import executor as executor_module
    from lakana_worker.deploy import platform_version

    order: list[str] = []
    poll = mock.MagicMock(side_effect=lambda *a, **k: order.append("loop"))
    monkeypatch.setattr(
        _StandInHeartbeat, "start", lambda self: order.append("start") or self.thread
    )
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    _run_with_loop(tmp_path, monkeypatch, poll)
    assert order == ["start", "loop"]
    (heartbeat,) = heartbeat_stand_in
    settings = Settings()
    worker_id = poll.call_args.kwargs["worker_id"]
    assert heartbeat.args == (heartbeat.args[0], worker_id)
    assert callable(heartbeat.args[0])
    assert heartbeat.kwargs == {
        "host": socket.gethostname(),
        "version": platform_version(),
        "renew_seconds": settings.run.renew_seconds,
        "lease_seconds": settings.run.lease_seconds,
        "takes": "all",
        "caches": poll.call_args.kwargs["engine"].python.caches.latest,
    }
    assert worker_id.count(":") == 2
    assert poll.call_args.kwargs["may_claim"] == heartbeat.may_claim
    assert poll.call_args.kwargs["engine"].settings.worker_id == worker_id
    # `install_heartbeat` was handed the stale check, and conftest resets it after the test.
    stale = executor_module._WORKER_STALE
    assert stale is not None and stale == heartbeat.stale


def test_main_stops_and_joins_the_heartbeat_thread_on_every_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_stand_in: list[_StandInHeartbeat],
) -> None:
    _run_with_loop(tmp_path, monkeypatch)
    (clean,) = heartbeat_stand_in
    assert clean.stopped == 1
    clean.thread.join.assert_called_once_with(timeout=5.0)
    heartbeat_stand_in.clear()
    with pytest.raises(RuntimeError):
        _run_with_loop(tmp_path, monkeypatch, mock.MagicMock(side_effect=RuntimeError("boom")))
    (failed,) = heartbeat_stand_in
    assert failed.stopped == 1
    failed.thread.join.assert_called_once_with(timeout=5.0)


def test_main_removes_its_row_after_a_clean_stop_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_stand_in: list[_StandInHeartbeat],
) -> None:
    seen: list[int] = []

    def loop(*args: object, **kwargs: object) -> None:
        seen.append(heartbeat_stand_in[0].unregistered)

    _run_with_loop(tmp_path, monkeypatch, mock.MagicMock(side_effect=loop))
    assert seen == [0]  # the row is still there while the loop runs
    assert heartbeat_stand_in[0].unregistered == 1
    heartbeat_stand_in.clear()
    with pytest.raises(RuntimeError):
        _run_with_loop(tmp_path, monkeypatch, mock.MagicMock(side_effect=RuntimeError("boom")))
    assert heartbeat_stand_in[0].unregistered == 0


@pytest.mark.parametrize(
    ("renew", "lease", "warned"), [(20, 60, False), (30, 60, False), (31, 60, True)]
)
def test_main_warns_when_the_renew_interval_is_more_than_half_the_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    renew: int,
    lease: int,
    warned: bool,
) -> None:
    monkeypatch.setenv("LAKANA_RUN__RENEW_SECONDS", str(renew))
    monkeypatch.setenv("LAKANA_RUN__LEASE_SECONDS", str(lease))
    with caplog.at_level("WARNING", logger="lakana_worker.__main__"):
        _run_with_loop(tmp_path, monkeypatch)
    lines = [
        r.getMessage() for r in caplog.records if "LAKANA_RUN__RENEW_SECONDS" in r.getMessage()
    ]
    if warned:
        (line,) = lines
        assert line.startswith(
            f"LAKANA_RUN__RENEW_SECONDS ({renew}) is more than half of "
            f"LAKANA_RUN__LEASE_SECONDS ({lease}): "
        )
        assert "Set the lease to at least twice the renew interval" in line
    else:
        assert lines == []


def test_a_real_heartbeat_registers_during_the_loop_and_is_gone_after_a_clean_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end on a SQLite file: the row exists while the loop runs, none after it returns."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from lakana_core.config import DatabaseConfig
    from lakana_core.db import create_engine
    from lakana_core.models import Base, Worker
    from lakana_worker.heartbeat import WorkerHeartbeat

    url = f"sqlite:///{tmp_path}/lakana.db"
    engine = create_engine(DatabaseConfig(url=url))
    Base.metadata.create_all(engine)
    monkeypatch.setattr("lakana_worker.__main__.WorkerHeartbeat", WorkerHeartbeat)
    seen: list[list[str]] = []

    def loop(*args: object, **kwargs: object) -> None:
        may_claim = cast("Callable[[], bool]", kwargs["may_claim"])
        pause = threading.Event()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with Session(engine) as session:
                ids = list(session.scalars(select(Worker.id)))
            # The row is committed before the success time is recorded: wait for both.
            if ids and may_claim():
                seen.append(ids)
                return
            pause.wait(0.01)

    try:
        _run_with_loop(tmp_path, monkeypatch, mock.MagicMock(side_effect=loop))
        with Session(engine) as session:
            left = list(session.scalars(select(Worker.id)))
    finally:
        engine.dispose()
    assert len(seen) == 1 and seen[0][0].count(":") == 2
    assert left == []
    assert [t for t in threading.enumerate() if t.name == "lakana-heartbeat"] == []


def test_a_long_host_name_is_cut_so_the_worker_id_fits_its_column() -> None:
    """The host part gives way; `:pid:token` stays and the id fits `Worker.id`."""
    from lakana_core.models import Worker

    length = cast(int, Worker.id.type.length)  # type: ignore[attr-defined]
    worker_id = make_worker_id("h" * 300)
    assert len(worker_id) <= length
    assert re.fullmatch(r"h+:\d+:[0-9a-f]{8}", worker_id)
    assert make_worker_id("box").startswith("box:")
    assert make_worker_id("h" * 300) != worker_id


def test_a_heartbeat_thread_that_does_not_stop_keeps_its_row_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A write that hangs past the join: no `unregister` (it could be undone by that write)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from lakana_core.config import DatabaseConfig
    from lakana_core.db import create_engine
    from lakana_core.models import Base, Worker
    from lakana_worker.heartbeat import WorkerHeartbeat

    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    Base.metadata.create_all(engine)
    entered, release = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []

    class Hanging(WorkerHeartbeat):
        """A heartbeat whose first write commits and then hangs until released."""

        def beat_once(self) -> bool:
            """Write, then hang.

            Returns
            -------
            bool
                The write's result.
            """
            result = super().beat_once()
            entered.set()
            release.wait(30)
            return result

        def start(self) -> threading.Thread:
            """Start the thread and remember it.

            Returns
            -------
            threading.Thread
                The thread.
            """
            threads.append(super().start())
            return threads[-1]

    def loop(*args: object, **kwargs: object) -> None:
        assert entered.wait(30)

    monkeypatch.setattr("lakana_worker.__main__.WorkerHeartbeat", Hanging)
    monkeypatch.setattr("lakana_worker.__main__._SCHEDULER_JOIN_FLOOR_SECONDS", 0.05)
    try:
        with caplog.at_level("WARNING", logger="lakana_worker.__main__"):
            _run_with_loop(tmp_path, monkeypatch, mock.MagicMock(side_effect=loop))
        with Session(engine) as session:
            left = list(session.scalars(select(Worker.id)))
        assert len(left) == 1
        assert "left to age out instead of being removed" in caplog.text
    finally:
        release.set()
        for thread in threads:
            thread.join(30)
        engine.dispose()
    assert not any(t.is_alive() for t in threads)


def test_main_gives_the_scheduler_the_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_stand_in: list[_StandInHeartbeat],
) -> None:
    """Without the lease the leader sweeps nothing: a worker killed mid-query stays `RUNNING`."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_RUN__LEASE_SECONDS", "45")
    monkeypatch.setenv("LAKANA_RUN__RENEW_SECONDS", "10")
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    assert scheduler_mock.call_args.kwargs["lease_seconds"] == 45
    assert scheduler_mock.call_args.kwargs["renew_seconds"] == 10


def test_main_hands_the_pool_and_the_cap_to_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool carries the cap too."""
    from lakana_worker.poller import WorkerPool

    monkeypatch.setenv("LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL", "3")
    monkeypatch.setenv("LAKANA_RUN__LEASE_SECONDS", "45")
    poll = _run_with_loop(tmp_path, monkeypatch)
    assert poll.call_args.kwargs["pool"] == WorkerPool(
        takes="all", running_per_principal=3, lease_seconds=45
    )


def test_main_hands_the_defaults_of_a_worker_that_takes_interactive_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing else set: `auto` and the default lease go down with `takes`."""
    from lakana_worker.poller import WorkerPool

    monkeypatch.setenv("LAKANA_WORKER__TAKES", "interactive")
    monkeypatch.delenv("LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL", raising=False)
    monkeypatch.delenv("LAKANA_RUN__LEASE_SECONDS", raising=False)
    poll = _run_with_loop(tmp_path, monkeypatch)
    assert poll.call_args.kwargs["pool"] == WorkerPool(
        takes="interactive", running_per_principal="auto", lease_seconds=60
    )


@pytest.mark.parametrize(
    ("takes", "line"),
    [
        ("all", "This worker takes interactive and pipeline work (LAKANA_WORKER__TAKES=all)."),
        (
            "interactive",
            "This worker takes interactive work only (LAKANA_WORKER__TAKES=interactive): "
            "ad-hoc queries. It runs no deploy, no pipeline task and no maintenance; keep "
            "another worker on all or pipeline.",
        ),
        (
            "pipeline",
            "This worker takes pipeline work only (LAKANA_WORKER__TAKES=pipeline): deploys, "
            "pipeline tasks and their queries, and maintenance. It runs no ad-hoc query; keep "
            "another worker on all or interactive.",
        ),
    ],
)
def test_main_logs_what_the_worker_takes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    takes: str,
    line: str,
) -> None:
    monkeypatch.setenv("LAKANA_WORKER__TAKES", takes)
    with caplog.at_level("INFO", logger="lakana_worker.__main__"):
        _run_with_loop(tmp_path, monkeypatch)
    assert line in [record.getMessage() for record in caplog.records]


def test_a_worker_that_takes_only_interactive_work_still_starts_the_scheduler_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler role does not depend on `takes`: an interactive worker can lead."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_WORKER__TAKES", "interactive")
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    scheduler_mock.assert_called_once()
    scheduler_mock.return_value.start.assert_called_once()
    scheduler_mock.return_value.stop.assert_called_once()


def _main_with_deploy_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sweep: Callable[..., int],
    **environment: str,
) -> None:
    """Run `main` with the dequeue loop and the scheduler replaced and a given deploy sweep.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        The fixture.
    sweep
        Stands in for `sweep_deploy_checkouts`.
    **environment
        Extra environment variables.
    """
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("lakana_worker.__main__.sweep_deploy_checkouts", sweep)
    with (
        mock.patch("lakana_worker.__main__.poll_forever"),
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()


def test_start_up_sweeps_the_deploy_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweep = mock.MagicMock(return_value=0)
    _main_with_deploy_sweep(tmp_path, monkeypatch, sweep)
    assert sweep.call_count == 1
    assert sweep.call_args.kwargs["max_age"] == DeployLimits().job_seconds


def test_start_up_does_not_sweep_the_deploy_checkouts_when_the_sweeps_are_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweep = mock.MagicMock(return_value=0)
    _main_with_deploy_sweep(tmp_path, monkeypatch, sweep, LAKANA_RUN__SWEEP_INTERVAL="0")
    assert sweep.call_count == 0


def test_start_up_sweeps_the_configured_temp_directory_and_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / "configured-temp"
    temp.mkdir()
    old = temp / "lakana-deploy-abcd1234"
    old.mkdir(mode=0o700)
    os.utime(old, (1.0, 1.0))
    bystander = temp / "unrelated"
    bystander.mkdir()
    os.utime(bystander, (1.0, 1.0))
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    _main_with_deploy_sweep(tmp_path, monkeypatch, deploy_sweep.sweep_deploy_checkouts)
    assert not old.exists()
    assert bystander.exists()


def test_a_failing_sweep_in_housekeeping_keeps_its_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    artifacts = mock.MagicMock(side_effect=RuntimeError("boom"))
    code = mock.MagicMock(side_effect=ValueError("bang"))
    with caplog.at_level(logging.WARNING):
        _housekeeping(tmp_path, monkeypatch, artifacts, code)
    errors = [r.exc_info[0] for r in caplog.records if r.exc_info]
    assert errors == [RuntimeError, ValueError]


def test_the_worker_creates_its_attempt_directory_at_start_when_it_can_run_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image no longer creates `attempts` (two workers copying it into one volume raced)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    settings = Settings(database={"url": f"sqlite:///{tmp_path}/lakana.db"})  # type: ignore[arg-type]
    runtime = _python_runtime(settings, PythonSupport(server_url="http://s", uv="/bin/uv"))
    attempts = tmp_path / "xdg" / "lakana-worker" / "attempts"
    assert runtime.work_dir == attempts
    assert stat.S_IMODE(os.lstat(attempts).st_mode) == 0o700
    _python_runtime(settings, PythonSupport(server_url="http://s", uv="/bin/uv"))


def test_the_worker_starts_and_says_why_when_its_attempt_directory_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A link in place of `attempts` is not followed; Python tasks fail, SQL keeps working."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    cache = tmp_path / "xdg" / "lakana-worker"
    cache.mkdir(parents=True, mode=0o700)
    (tmp_path / "elsewhere").mkdir()
    (cache / "attempts").symlink_to(tmp_path / "elsewhere")
    settings = Settings(database={"url": f"sqlite:///{tmp_path}/lakana.db"})  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        _python_runtime(settings, PythonSupport(server_url="http://s", uv="/bin/uv"))
    assert "attempts" in caplog.text
    assert "real directory" in caplog.text
    assert (cache / "attempts").is_symlink()


def test_housekeeping_also_measures_the_sizes_of_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.storage.resolver import StorageResolver

    sizes = mock.MagicMock()
    _housekeeping(tmp_path, monkeypatch, mock.MagicMock(), mock.MagicMock(), sizes=sizes)
    assert sizes.call_count == 1
    assert isinstance(sizes.call_args.args[1], StorageResolver)
    assert sizes.call_args.kwargs["should_stop"]() is False


def test_a_failing_table_size_measurement_is_logged_and_the_others_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    columns = mock.MagicMock()
    with caplog.at_level(logging.WARNING):
        _housekeeping(
            tmp_path,
            monkeypatch,
            mock.MagicMock(),
            mock.MagicMock(),
            columns=columns,
            sizes=mock.MagicMock(side_effect=OSError),
        )
    assert "The table size measurement failed (OSError)" in caplog.text
    assert "sweep_failed" in {getattr(r, "event", None) for r in caplog.records}
    assert columns.call_count == 1


def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **environment: str) -> PythonRuntime:
    """Build the Python runtime from settings read from the environment.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory; the worker's cache directory is under it.
    monkeypatch
        The fixture.
    **environment
        Settings to set.

    Returns
    -------
    PythonRuntime
        The runtime, with `uv` at ``/usr/bin/uv``.
    """
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LAKANA_RUN__VENV_CACHE", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    support = PythonSupport(server_url=None, uv="/usr/bin/uv")
    return _python_runtime(Settings(), support, threading.Event())


def test_the_runtime_measures_the_three_caches_and_trims_the_package_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_worker.caches import CachePaths, PackageCacheTrim

    runtime = _runtime(
        tmp_path,
        monkeypatch,
        LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES="5000",
        LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS="2",
        LAKANA_RUN__SWEEP_INTERVAL="120",
    )
    keeper = runtime.caches
    assert keeper is not None
    cache = tmp_path / "xdg" / "lakana-worker"
    assert keeper._measure is not None
    trim = keeper._trim
    assert isinstance(trim, PackageCacheTrim)
    assert trim._paths == CachePaths(
        environments=cache / "venvs", packages=cache / "uv", tasks=cache / "attempts"
    )
    assert (trim._settings.uv, trim._settings.max_bytes) == ("/usr/bin/uv", 5000)
    assert math.isclose(trim._settings.min_age_seconds, 7200)
    assert math.isclose(keeper._interval, 120)
    assert keeper._stop is not None


def test_without_uv_or_with_a_limit_of_zero_the_caches_are_only_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES="0")
    assert runtime.caches is not None and runtime.caches._trim is None
    monkeypatch.setenv("LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES", "5000")
    runtime = _python_runtime(Settings(), PythonSupport(server_url=None, uv=None))
    assert runtime.caches is not None and runtime.caches._trim is None


def test_the_heartbeat_writes_what_the_cache_keeper_measured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_stand_in: list[_StandInHeartbeat],
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll_forever_mock,
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    keeper = poll_forever_mock.call_args.kwargs["engine"].python.caches
    assert heartbeat_stand_in[0].kwargs["caches"] == keeper.latest


def _main_with_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup: Callable[[object, str], int]
) -> mock.MagicMock:
    """Run `main` with `LAKANA_RUN__RUNNER=docker` and a stand-in for the launcher's cleanup.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        The fixture.
    cleanup
        Replaces `LauncherClient.cleanup`.

    Returns
    -------
    mock.MagicMock
        The `poll_forever` mock.
    """
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_RUN__RUNNER", "docker")
    monkeypatch.setenv("LAKANA_RUN__TASK_MEMORY", "1GB")
    monkeypatch.setenv("LAKANA_RUN__SERVER_URL", "http://server:8000")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    for name in ("LAKANA_RUN__CA_BUNDLE", "LAKANA_RUN__HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("lakana_worker.launcher_client.LauncherClient.cleanup", cleanup)
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll_forever_mock,
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
        mock.patch("lakana_worker.__main__.socket.gethostname", return_value="worker-7"),
        mock.patch("lakana_worker.python_support.shutil.which", return_value=None),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    return poll_forever_mock


def test_main_builds_a_docker_runtime_and_cleans_up_what_it_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`LAKANA_RUN__RUNNER=docker`: DockerRunner, DockerEnvironments and one `cleanup` at start."""
    caplog.set_level(logging.INFO)
    cleaned: list[str] = []

    def cleanup(client: object, owner: str) -> int:
        """Record the owner and say two containers were removed.

        Parameters
        ----------
        client
            The launcher client.
        owner
            Whose containers.

        Returns
        -------
        int
            Two.
        """
        cleaned.append(owner)
        return 2

    poll_forever_mock = _main_with_docker(tmp_path, monkeypatch, cleanup)

    python = poll_forever_mock.call_args.kwargs["engine"].python
    assert isinstance(python.runner, DockerRunner)
    assert isinstance(python.environments, DockerEnvironments)
    owner = python.environments.owner
    token = (tmp_path / "xdg" / "lakana-worker" / "launcher-owner").read_text()
    assert (owner, python.environments.memory_mb) == (f"worker-7.{token}", 1024)
    assert python.runner._owner == owner
    assert python.environments.client.socket_path == Path("/run/lakana-launcher/launcher.sock")
    assert python.support.reason() is None
    assert cleaned == [owner]
    assert "Python tasks run in containers through the launcher" in caplog.text
    assert "Python assets: enabled (in containers)." in caplog.text
    assert "Removed 2 task containers an earlier process of this worker left." in caplog.text
    assert str(tmp_path / "xdg" / "lakana-worker" / "attempts") in caplog.text


def test_a_docker_worker_starts_while_the_launcher_is_down_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The launcher may start after the worker: a warning, and each task says it again."""
    caplog.set_level(logging.INFO)

    def cleanup(client: object, owner: str) -> int:
        """Fail as an unreachable launcher does.

        Parameters
        ----------
        client
            The launcher client.
        owner
            Whose containers.

        Raises
        ------
        LauncherUnavailable
            Always.
        """
        raise LauncherUnavailable("runner unavailable: the launcher's socket cannot be reached")

    poll_forever_mock = _main_with_docker(tmp_path, monkeypatch, cleanup)

    poll_forever_mock.assert_called_once()
    assert "runner unavailable: the launcher's socket cannot be reached" in caplog.text
    assert "Removed" not in caplog.text


def test_a_local_worker_has_no_environments_and_asks_no_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default: LocalProcessRunner, venvs built by the worker, no cleanup call."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LAKANA_RUN__RUNNER", raising=False)
    cleanup = mock.Mock()
    monkeypatch.setattr("lakana_worker.launcher_client.LauncherClient.cleanup", cleanup)
    with (
        mock.patch("lakana_worker.__main__.poll_forever") as poll_forever_mock,
        mock.patch("lakana_worker.__main__.SchedulerLoop") as scheduler_mock,
        mock.patch("lakana_worker.__main__.signal.signal"),
    ):
        scheduler_mock.return_value.start.return_value.is_alive.return_value = False
        main()
    python = poll_forever_mock.call_args.kwargs["engine"].python
    assert isinstance(python.runner, LocalProcessRunner)
    assert python.environments is None
    cleanup.assert_not_called()


def test_a_docker_worker_that_finds_nothing_left_says_nothing_about_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A clean start: no line about removed containers."""
    caplog.set_level(logging.INFO)

    def cleanup(client: object, owner: str) -> int:
        """Say nothing was left.

        Parameters
        ----------
        client
            The launcher client.
        owner
            Whose containers.

        Returns
        -------
        int
            Zero.
        """
        return 0

    _main_with_docker(tmp_path, monkeypatch, cleanup)

    assert "Python tasks run in containers through the launcher" in caplog.text
    assert "Removed" not in caplog.text
