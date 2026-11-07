"""Tests for the logging set-up the server and the worker share."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from lakana_core.log_setup import LAKANA_LOGGERS, LOG_FORMAT, LOG_LEVELS, configure_logging

_CHILD_TIMEOUT_SECONDS = 120
_STAMP = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}"


@pytest.fixture(autouse=True)
def _restore_lakana_logger_levels() -> Iterator[None]:
    """Give each in-process test the logger levels it found, whatever it set."""
    before = {name: logging.getLogger(name).level for name in LAKANA_LOGGERS}
    yield
    for name, level in before.items():
        logging.getLogger(name).setLevel(level)


def _child(code: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter, where no test fixture has touched logging.

    Parameters
    ----------
    code
        The program text.

    Returns
    -------
    subprocess.CompletedProcess
        The finished child, output captured as text.
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )


def test_a_denial_line_of_the_audit_logger_is_printed_with_time_level_and_name() -> None:
    """The suppressed-denial trace is an INFO line of `lakana_core.audit.authorization`."""
    done = _child(
        "import logging\n"
        "from lakana_core.log_setup import configure_logging\n"
        "configure_logging()\n"
        "log = logging.getLogger('lakana_core.audit.authorization')\n"
        "log.info('AUTHORIZATION_DENIED not recorded')\n"
        "log.debug('not shown')\n"
    )
    assert done.returncode == 0, done.stderr
    [line] = done.stderr.splitlines()
    assert re.fullmatch(
        rf"{_STAMP} INFO lakana_core\.audit\.authorization: AUTHORIZATION_DENIED not recorded",
        line,
    )


def test_a_root_logger_that_has_a_handler_is_left_alone() -> None:
    """An embedding process, or a test, that configured logging keeps exactly what it set up."""
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    assert before  # pytest installs its capture handlers
    configure_logging()
    assert root.handlers == before
    assert root.level == level


def test_a_fresh_process_with_its_own_handler_gets_no_second_one() -> None:
    """With a handler installed first, a line is printed once, in the format that handler has."""
    done = _child(
        "import logging\n"
        "logging.getLogger().addHandler(logging.StreamHandler())\n"
        "from lakana_core.log_setup import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger().warning('once')\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr.splitlines() == ["once"]


def test_without_a_root_handler_it_keeps_the_root_at_warning_in_the_shared_format() -> None:
    """With no root handler, `configure_logging` calls `basicConfig` with the root at WARNING."""
    with (
        mock.patch.object(logging.getLogger(), "handlers", []),
        mock.patch("lakana_core.log_setup.logging.basicConfig") as basic_config,
    ):
        configure_logging()
    basic_config.assert_called_once()
    assert basic_config.call_args.kwargs["level"] == logging.WARNING
    [handler] = basic_config.call_args.kwargs["handlers"]
    assert handler.formatter._fmt == LOG_FORMAT
    assert [logging.getLogger(name).level for name in LAKANA_LOGGERS] == [logging.INFO] * 4


def test_the_root_is_as_strict_as_the_setting_when_that_is_stricter_than_warning() -> None:
    """At ERROR the root follows the setting, so a third-party warning is not printed either."""
    with (
        mock.patch.object(logging.getLogger(), "handlers", []),
        mock.patch("lakana_core.log_setup.logging.basicConfig") as basic_config,
    ):
        configure_logging("error")
    basic_config.assert_called_once()
    assert basic_config.call_args.kwargs["level"] == logging.ERROR
    assert [logging.getLogger(name).level for name in LAKANA_LOGGERS] == [logging.ERROR] * 4


def test_a_level_that_is_not_one_of_the_four_is_a_value_error() -> None:
    """The settings refuse it first; a caller that bypasses them gets the same four names."""
    with pytest.raises(ValueError, match="DEBUG, INFO, WARNING or ERROR"):
        configure_logging("TRACE")
    assert LOG_LEVELS == ("DEBUG", "INFO", "WARNING", "ERROR")


_PROBE = (
    "import logging\n"
    "from lakana_core.log_setup import configure_logging\n"
    "configure_logging({level!r})\n"
    "for name in ('lakana_core.a', 'lakana_server.a.b', 'lakana_worker.a', 'lakana.cli',\n"
    "             'lakana'):\n"
    "    log = logging.getLogger(name)\n"
    "    for level in ('debug', 'info', 'warning', 'error'):\n"
    "        getattr(log, level)(level + ' of ' + name)\n"
    "for name in ('urllib3', 'botocore.credentials', 'sqlalchemy.engine', 'asyncio'):\n"
    "    log = logging.getLogger(name)\n"
    "    for level in ('debug', 'info', 'warning', 'error'):\n"
    "        getattr(log, level)(level + ' of ' + name)\n"
)
_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3}


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_lakana_loggers_follow_the_level_and_third_party_loggers_stay_at_warning(
    level: str,
) -> None:
    """Each level prints Lakana's lines from it up; a library's from WARNING (ERROR at ERROR)."""
    done = _child(_PROBE.format(level=level))
    assert done.returncode == 0, done.stderr
    printed = set()
    for line in done.stderr.splitlines():
        match = re.fullmatch(rf"{_STAMP} (\w+) ([\w.]+): (\w+) of ([\w.]+)", line)
        assert match, line
        assert match[1].lower() == match[3] and match[2] == match[4]
        printed.add((match[3], match[4]))
    own = ("lakana_core.a", "lakana_server.a.b", "lakana_worker.a", "lakana.cli", "lakana")
    other = ("urllib3", "botocore.credentials", "sqlalchemy.engine", "asyncio")
    floor = _RANK[level.lower()]
    third_floor = max(floor, _RANK["warning"])
    expected = {(lv, name) for name in own for lv in _RANK if _RANK[lv] >= floor}
    expected |= {(lv, name) for name in other for lv in _RANK if _RANK[lv] >= third_floor}
    assert printed == expected
    assert len(done.stderr.splitlines()) == len(expected)  # nothing is printed twice


def test_debug_does_not_switch_on_a_library_debug_output() -> None:
    """At DEBUG, the libraries that log a request, a query or a credential stay at WARNING."""
    done = _child(
        "import logging\n"
        "from lakana_core.log_setup import configure_logging\n"
        "configure_logging('DEBUG')\n"
        "names = ('urllib3', 'botocore', 'boto3', 's3fs', 'fsspec', 'httpx', 'httpcore',\n"
        "         'sqlalchemy.engine', 'sqlalchemy.pool', 'pyiceberg', 'aiobotocore')\n"
        "print([n for n in names if logging.getLogger(n).isEnabledFor(logging.INFO)])\n"
        "print(logging.getLogger('lakana_core.x').isEnabledFor(logging.DEBUG))\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["[]", "True"]


def test_the_default_prints_what_it_printed_before_the_level_existed() -> None:
    """With no argument the level is INFO: Lakana's INFO line, in the shared format, once."""
    done = _child(
        _PROBE.format(level="INFO").replace("configure_logging('INFO')", "configure_logging()")
    )
    assert done.returncode == 0, done.stderr
    assert "info of lakana_core.a" in done.stderr
    assert "debug of lakana_core.a" not in done.stderr


def test_a_root_handler_keeps_the_levels_of_the_embedding_process_too() -> None:
    """With a handler in place no level is set, on the root or on a Lakana logger."""
    done = _child(
        "import logging\n"
        "logging.getLogger().addHandler(logging.StreamHandler())\n"
        "from lakana_core.log_setup import configure_logging\n"
        "configure_logging('DEBUG')\n"
        "print(logging.getLogger().level, [logging.getLogger(n).level for n in "
        "('lakana_core', 'lakana_server', 'lakana_worker', 'lakana')])\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["30", "[0,", "0,", "0,", "0]"]


def test_importing_the_core_package_configures_no_logging() -> None:
    """A process that imports `lakana_core` as a library keeps its own logging."""
    done = _child(
        "import logging\n"
        "import lakana_core, lakana_core.log_setup, lakana_core.audit.authorization\n"
        "print(len(logging.getLogger().handlers), logging.getLogger().level)\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["0", str(logging.WARNING)]


@pytest.mark.parametrize(
    ("level", "printed"), [("INFO", True), ("DEBUG", True), ("WARNING", False)]
)
def test_the_migration_progress_follows_the_level(level: str, printed: bool) -> None:
    """Alembic's "Running upgrade" line is printed at the default, INFO and DEBUG, not above."""
    done = _child(
        "import logging\n"
        "from lakana_core.log_setup import configure_logging\n"
        f"configure_logging({level!r})\n"
        "logging.getLogger('alembic.runtime.migration').info('Running upgrade a -> b')\n"
    )
    assert done.returncode == 0, done.stderr
    line = "INFO alembic.runtime.migration: Running upgrade a -> b"
    assert (line in done.stderr) is printed


def test_the_default_prints_the_migration_progress() -> None:
    """With no argument, an operator watching an upgrade sees each step."""
    done = _child(
        "import logging\n"
        "from lakana_core.log_setup import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger('alembic.runtime.migration').info('Running upgrade a -> b')\n"
    )
    assert "INFO alembic.runtime.migration: Running upgrade a -> b" in done.stderr


def test_a_real_migration_prints_its_steps_at_the_default(tmp_path: Path) -> None:
    """`migrate.upgrade` after `configure_logging()` prints a "Running upgrade" line per step.

    Two real steps (to revision 0002) show it; running every migration to `head` would only
    make the test slower with each new revision.
    """
    done = _child(
        "from lakana_core import migrate\n"
        "from lakana_core.log_setup import configure_logging\n"
        "configure_logging()\n"
        f"migrate.upgrade('sqlite:///{tmp_path}/m.db', '7c957aaa4637')\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr.count("INFO alembic.runtime.migration: Running upgrade") == 2
