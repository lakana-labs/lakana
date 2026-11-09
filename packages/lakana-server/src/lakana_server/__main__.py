"""Entry point for the `lakana-server` console script.

`lakana-server` serves the API with uvicorn; `lakana-server migrate` applies the
metadata database migrations and exits. Both read `LAKANA_*` settings.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from lakana_core import migrate
from lakana_core.log_setup import configure_logging
from lakana_server.app import create_app
from lakana_server.log_config import uvicorn_log_config
from lakana_server.settings import Settings
from lakana_server.ui import UiNotBuiltError


def main(argv: list[str] | None = None) -> None:
    """Serve the API, or run a subcommand.

    Parameters
    ----------
    argv
        Command-line arguments without the program name; `sys.argv[1:]` when
        `None`.

    Raises
    ------
    SystemExit
        With code 1 when `LAKANA_UI__ENABLED` is true and the installation has no usable
        build of the interface; the message (cause and next steps) goes to stderr alone,
        without a traceback.

    Notes
    -----
    Logging is set up once the settings are read (`lakana_core.log_setup.configure_logging`):
    `LAKANA_LOG__LEVEL` (INFO by default) and above to stderr with time, level and logger
    name, like the worker, so the INFO lines of `lakana_*` loggers reach
    `docker compose logs server`. Uvicorn keeps its own handlers, with formatters that escape
    control characters like the root handler's (`lakana_server.log_config`), and gets the same
    level, so its access log (one line per request) is printed at INFO and DEBUG only, and not
    at all with `LAKANA_LOG__ACCESS=false`. Forwarded headers (`X-Forwarded-For`,
    `X-Forwarded-Proto`) are believed only from `LAKANA_TRUSTED_PROXIES`, and from no one when
    it is empty, whatever uvicorn's own default or `FORWARDED_ALLOW_IPS` say.
    """
    parser = argparse.ArgumentParser(prog="lakana-server", description="Run the Lakana API server.")
    parser.add_argument("command", nargs="?", choices=["migrate"], help="migrate: apply migrations")
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log.level)
    if args.command == "migrate":
        migrate.upgrade(settings.database.url)
        return
    try:
        app = create_app(settings)
    except UiNotBuiltError as error:
        print(f"lakana-server: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log.level.lower(),
        log_config=uvicorn_log_config(),
        access_log=settings.log.access,
        # Uvicorn would trust `X-Forwarded-*` from 127.0.0.1 (or `FORWARDED_ALLOW_IPS`) unasked.
        proxy_headers=bool(settings.trusted_proxies),
        forwarded_allow_ips=",".join(settings.trusted_proxies),
    )


if __name__ == "__main__":  # pragma: no cover — exercised only by `python -m lakana_server`
    main()
