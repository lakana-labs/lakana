"""The server's entry point prints the server's own INFO lines, once."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_CHILD_TIMEOUT_SECONDS = 120
_STAMP = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}"

_SERVE = (
    "import logging\n"
    "import uvicorn\n"
    "import lakana_server.__main__ as m\n"
    "def fake_run(app, **kw):\n"
    "    uvicorn.Config(app, **kw)  # applies uvicorn's real logging configuration\n"
    "    logging.getLogger('lakana_core.audit.authorization').info('denial line')\n"
    "    logging.getLogger('uvicorn.error').info('uvicorn line')\n"
    "m.uvicorn.run = fake_run\n"
    "m.main([])\n"
)
_ALL_LEVELS = (
    "import logging\n"
    "import uvicorn\n"
    "import lakana_server.__main__ as m\n"
    "def fake_run(app, **kw):\n"
    "    uvicorn.Config(app, **kw)\n"
    "    for name in ('lakana_server.x', 'lakana_core.y', 'uvicorn.error'):\n"
    "        for level in ('debug', 'info', 'warning', 'error'):\n"
    "            getattr(logging.getLogger(name), level)(f'{level} line of {name}')\n"
    "    logging.getLogger('uvicorn.access').info('%s - \"%s %s HTTP/%s\" %d',\n"
    "                                             '1.2.3.4:5', 'GET', '/x', '1.1', 200)\n"
    "    logging.getLogger('some.library').info('library info')\n"
    "m.uvicorn.run = fake_run\n"
    "m.main([])\n"
)
_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3}


def _child(
    code: str, tmp_path: Path, level: str | None = None, access: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter, where no test fixture has touched logging.

    Parameters
    ----------
    code
        The program text.
    tmp_path
        Pytest's per-test directory (the child's database).
    level
        The value of `LAKANA_LOG__LEVEL` in the child, or `None` for the default.
    access
        The value of `LAKANA_LOG__ACCESS` in the child, or `None` for the default.

    Returns
    -------
    subprocess.CompletedProcess
        The finished child, output captured as text.
    """
    env = {**os.environ, "LAKANA_DATABASE__URL": f"sqlite:///{tmp_path}/lakana.db"}
    env.pop("LAKANA_LOG__LEVEL", None)
    env.pop("LAKANA_LOG__ACCESS", None)
    if access is not None:
        env["LAKANA_LOG__ACCESS"] = access
    if level is not None:
        env["LAKANA_LOG__LEVEL"] = level
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.mark.slow
def test_serving_prints_a_lakana_info_line_with_time_level_and_name(tmp_path: Path) -> None:
    """The suppressed-denial line of the audit logger reaches stderr when the server serves."""
    done = _child(_SERVE, tmp_path)
    assert done.returncode == 0, done.stderr
    assert any(
        re.fullmatch(rf"{_STAMP} INFO lakana_core\.audit\.authorization: denial line", line)
        for line in done.stderr.splitlines()
    ), done.stderr


@pytest.mark.slow
def test_a_uvicorn_line_is_printed_once(tmp_path: Path) -> None:
    """Uvicorn's loggers do not propagate to the root handler this adds, so nothing doubles."""
    done = _child(_SERVE, tmp_path)
    assert done.returncode == 0, done.stderr
    assert sum("uvicorn line" in line for line in done.stderr.splitlines()) == 1, done.stderr


@pytest.mark.slow
def test_a_handler_the_embedding_process_installed_is_the_only_one(tmp_path: Path) -> None:
    """With a root handler in place the entry point adds none: each line is printed once."""
    done = _child(
        "import logging\nlogging.getLogger().addHandler(logging.StreamHandler())\n"
        + _SERVE
        + "print(len(logging.getLogger().handlers), logging.getLogger().level)\n",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["1", str(logging.WARNING)]


@pytest.mark.slow
def test_importing_the_server_package_configures_no_logging(tmp_path: Path) -> None:
    """A process that imports `lakana_server` as a library keeps its own logging."""
    done = _child(
        "import logging\n"
        "import lakana_server, lakana_server.__main__, lakana_server.app\n"
        "print(len(logging.getLogger().handlers), logging.getLogger().level)\n",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["0", str(logging.WARNING)]


@pytest.mark.parametrize("level", ["debug", "INFO", "Warning", "ERROR"])
@pytest.mark.slow
def test_each_level_prints_each_line_once_with_uvicorns_real_logging_configuration(
    tmp_path: Path, level: str
) -> None:
    """Lakana's and uvicorn's lines follow the level, none twice; the access log INFO, DEBUG."""
    done = _child(_ALL_LEVELS, tmp_path, level)
    assert done.returncode == 0, done.stderr
    printed = done.stderr.splitlines() + done.stdout.splitlines()  # uvicorn's access log: stdout
    floor = _RANK[level.lower()]
    expected = [
        f"{lv} line of {name}"
        for name in ("lakana_server.x", "lakana_core.y", "uvicorn.error")
        for lv in _RANK
        if _RANK[lv] >= floor
    ]
    for line in expected:
        assert sum(line in p for p in printed) == 1, (line, printed)
    own_and_uvicorn = [p for p in printed if " line of " in p]
    assert len(own_and_uvicorn) == len(expected), printed
    access = [p for p in printed if "GET /x" in p]
    assert len(access) == (1 if floor <= _RANK["info"] else 0), printed
    assert not any("library info" in p for p in printed), printed


_ACCESS_FLAG = (
    "import lakana_server.__main__ as m\n"
    "m.uvicorn.run = lambda app, **kw: print('access_log', kw['access_log'])\n"
    "m.main([])\n"
)


@pytest.mark.parametrize(("setting", "expected"), [(None, "True"), ("false", "False")])
@pytest.mark.slow
def test_the_access_log_follows_lakana_log_access(
    tmp_path: Path, setting: str | None, expected: str
) -> None:
    """The request log is on by default; `LAKANA_LOG__ACCESS=false` turns off only that."""
    done = _child(_ACCESS_FLAG, tmp_path, access=setting)
    assert done.returncode == 0, done.stderr
    assert f"access_log {expected}" in done.stdout
