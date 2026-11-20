"""Uvicorn's loggers print through formatters that escape control characters."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import uvicorn

from lakana_server.log_config import uvicorn_log_config

_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


@pytest.fixture(autouse=True)
def _restore_uvicorn_loggers() -> Iterator[None]:
    """Give the uvicorn loggers the handlers, levels and propagation they had."""
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in _NAMES
    }
    yield
    for name, (handlers, level, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


async def _app(scope: object, receive: object, send: object) -> None:
    """Stand in for an ASGI application; it is never called.

    Parameters
    ----------
    scope
        Unused.
    receive
        Unused.
    send
        Unused.
    """


def test_the_error_logger_escapes_control_characters(capsys: pytest.CaptureFixture[str]) -> None:
    """Uvicorn's error and start-up lines are escaped, with a continuation prefix."""
    uvicorn.Config(_app, log_config=uvicorn_log_config(), log_level="info")
    logging.getLogger("uvicorn.error").error("bad \x1b[31m\r\nINFO:     forged line")
    err = capsys.readouterr().err
    assert err == "ERROR:    bad \\x1b[31m\\x0d\n    | INFO:     forged line\n"


def test_the_access_logger_escapes_the_request_line(capsys: pytest.CaptureFixture[str]) -> None:
    """A path with control characters, which a client chose, is written out as escapes."""
    uvicorn.Config(_app, log_config=uvicorn_log_config(), log_level="info")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/x\x1b[2J\nINFO: forged", "1.1", 404
    )
    out = capsys.readouterr().out
    assert out == (
        'INFO:     1.2.3.4:5 - "GET /x\\x1b[2J\n    | INFO: forged HTTP/1.1" 404 Not Found\n'
    )


def test_the_configuration_is_a_fresh_copy_and_does_not_share_uvicorns() -> None:
    """Changing one result changes neither uvicorn's default nor the next result."""
    first = uvicorn_log_config()
    first["formatters"]["default"]["fmt"] = "changed"
    assert uvicorn_log_config()["formatters"]["default"]["fmt"] != "changed"
    assert "Safe" not in uvicorn.config.LOGGING_CONFIG["formatters"]["default"]["()"]


def test_an_access_record_that_cannot_be_formatted_still_prints_a_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Uvicorn's access format needs five arguments; with fewer the record is not lost."""
    uvicorn.Config(_app, log_config=uvicorn_log_config(), log_level="info")
    logging.getLogger("uvicorn.access").info("%s", "only one")
    out = capsys.readouterr().out
    assert out.startswith("INFO")
    assert "could not be formatted" in out


def test_callback_query_is_not_in_the_access_log(capsys: pytest.CaptureFixture[str]) -> None:
    """The OIDC callback's code and state never reach the access log; other queries do."""
    uvicorn.Config(_app, log_config=uvicorn_log_config(), log_level="info")
    access = logging.getLogger("uvicorn.access")
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "1.2.3.4:5",
        "GET",
        "/api/v1/sign-in/oidc/callback?code=c0de-sentinel&state=st4te-sentinel",
        "1.1",
        303,
    )
    access.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/api/v1/tables?q=x", "1.1", 200)
    access.info(
        '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/api/v1/sign-in/oidc/callback", "1.1", 303
    )
    out = capsys.readouterr().out
    assert "sentinel" not in out
    assert '"GET /api/v1/sign-in/oidc/callback?… HTTP/1.1" 303' in out
    assert "/api/v1/tables?q=x" in out
    assert '"GET /api/v1/sign-in/oidc/callback HTTP/1.1" 303' in out


def test_callback_query_is_dropped_behind_a_root_path(capsys: pytest.CaptureFixture[str]) -> None:
    """Behind `--root-path`, uvicorn logs the prefixed path; the query is still dropped."""
    uvicorn.Config(_app, log_config=uvicorn_log_config(), log_level="info")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "1.2.3.4:5",
        "GET",
        "/lakana/api/v1/sign-in/oidc/callback?code=c0de-sentinel&state=st4te-sentinel",
        "1.1",
        303,
    )
    out = capsys.readouterr().out
    assert "sentinel" not in out
    assert '"GET /lakana/api/v1/sign-in/oidc/callback?… HTTP/1.1" 303' in out
