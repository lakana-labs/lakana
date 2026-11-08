"""`lakana server start --local`, `status`, `stop` and `reset`: run Lakana on this machine."""

from __future__ import annotations

import contextlib
import html
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Annotated, NoReturn, Protocol
from urllib.parse import urlsplit

import httpx
import typer

from lakana.cli import _output, _profiles
from lakana.cli._filelock import try_locked
from lakana.cli._state import (
    CliError,
    CliState,
    confirm,
    is_loopback_url,
    selected_url,
    state_of,
)
from lakana.cli.local import _reset
from lakana.cli.local._datadir import (
    DataDir,
    default_data_dir,
    load_or_create_master_key,
    prepare_data_dir,
    resolve_data_dir,
)
from lakana.cli.local._pidfile import (
    clear_lock_owner,
    pid_alive,
    read_lock_owner,
    read_pidfile,
    remove_pidfile,
    write_lock_owner,
    write_pidfile,
)
from lakana.cli.local._scripts import find_script
from lakana.cli.local._supervisor import Supervisor, SupervisorConfig, SupervisorError
from lakana.errors import LakanaError, NotFound

server_app = typer.Typer(help="Run Lakana on this machine (local only).", no_args_is_help=True)

_DEFAULT_PORT = 8765
_HEALTH_TIMEOUT = 2.0
_STOP_TIMEOUT_SECONDS = 30.0
_STOP_POLL_SECONDS = 0.1
_BOOTSTRAP_ENV_VARS = ("LAKANA_BOOTSTRAP_PAT_KEY_ID", "LAKANA_BOOTSTRAP_PAT_SECRET")
_LOOPBACK_URL_RE = re.compile(r"http://127\.0\.0\.1:\d+")
_HAND_OFF_SECONDS = 60.0
"""How long the browser hand-off file may exist at most."""
_TRUE_WORDS = frozenset({"1", "true", "t", "yes", "y", "on"})
"""The spellings of true the server's settings accept for a boolean, in lower case."""

_SignalHandler = Callable[[int, FrameType | None], object] | int | None
"""A signal disposition as `signal.signal` accepts and returns it.

`signal.signal`'s own `_HANDLER` type alias isn't public, so this mirrors
it: a callable handler, `signal.SIG_IGN`/`signal.SIG_DFL` (both plain
`int`s), or `None` (no handler set, POSIX-only).
"""


class _SupportsSetSockOpt(Protocol):
    """The one `socket.socket` method `_reuse_addr_if_supported` needs.

    A `Protocol`, not `socket.socket` itself, so a test double can stand in
    for it without constructing a real socket.
    """

    def setsockopt(self, level: int, optname: int, value: int, /) -> None:
        """Set a socket option.

        Parameters
        ----------
        level
            The protocol level (`socket.SOL_SOCKET`, here).
        optname
            The option to set (`socket.SO_REUSEADDR`, here).
        value
            The option's new value.
        """
        ...


def make_supervisor(config: SupervisorConfig) -> Supervisor:
    """Build the process supervisor for one `start` run.

    A module-level seam: tests replace this with a fake supervisor via
    `monkeypatch.setattr`.

    Parameters
    ----------
    config
        The supervisor's configuration.

    Returns
    -------
    Supervisor
        A new supervisor.
    """
    return Supervisor(config)


def _reuse_addr_if_supported(sock: _SupportsSetSockOpt) -> None:
    """Set `SO_REUSEADDR` on `sock`, except on Windows.

    On Windows, `SO_REUSEADDR` allows binding a port already in use by
    another socket, which would make `port_free`'s pre-check always report
    "free" there.

    Parameters
    ----------
    sock
        The socket to configure.
    """
    if os.name == "nt":
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def port_free(port: int) -> bool:
    """Report whether a TCP port is free to bind on `127.0.0.1`.

    Parameters
    ----------
    port
        The port to check.

    Returns
    -------
    bool
        `True` if a socket could bind to it (the socket is immediately
        closed again); `False` if binding failed, e.g. because another
        process already listens on it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        _reuse_addr_if_supported(sock)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def shown_url(port: int) -> str:
    """Name the address a person opens this machine's instance at.

    The server binds `127.0.0.1`, but browsers tie passkeys to a name, so every address shown to
    a person is `localhost` (the server sends a browser at `127.0.0.1` on to it).

    Parameters
    ----------
    port
        The instance's port.

    Returns
    -------
    str
        `http://localhost:<port>`.
    """
    return f"http://localhost:{port}"


def _ui_enabled(env: Mapping[str, str]) -> bool:
    """Tell whether the server will serve the web interface, as its settings read the variable.

    Parameters
    ----------
    env
        The children's environment.

    Returns
    -------
    bool
        True when `LAKANA_UI__ENABLED` is a spelling of true.
    """
    return env.get("LAKANA_UI__ENABLED", "").strip().lower() in _TRUE_WORDS


def _schedule(seconds: float, action: Callable[[], None]) -> Callable[[], None]:
    """Run an action once after a delay, on a daemon thread (a seam for tests).

    Parameters
    ----------
    seconds
        The delay.
    action
        What to run.

    Returns
    -------
    Callable
        Cancels the action if it has not run yet.
    """
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer.cancel


def _stdout_is_terminal() -> bool:
    """Tell whether stdout is a terminal (a seam for tests).

    Returns
    -------
    bool
        `sys.stdout.isatty()`.
    """
    return sys.stdout.isatty()


class _HandOff:
    """The file that hands the local sign-in link to the browser, removed once.

    Parameters
    ----------
    path
        The file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.cancel: Callable[[], None] = lambda: None

    def remove(self) -> None:
        """Delete the file if it is still there; safe to call more than once."""
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def _hand_off_page(url: str) -> str:
    """Build the page that sends the browser on to the sign-in link at once.

    Parameters
    ----------
    url
        The link.

    Returns
    -------
    str
        A small HTML page with a refresh to `url` and a link for a browser that ignores it.
    """
    target = html.escape(url, quote=True)
    return (
        "<!doctype html>\n"
        '<meta charset="utf-8">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f'<meta http-equiv="refresh" content="0;url={target}">\n'
        "<title>Signing in to Lakana</title>\n"
        f'<p>Signing in to Lakana… <a href="{target}">Continue</a></p>\n'
    )


def _write_hand_off(path: Path, url: str) -> None:
    """Write the hand-off page readable by this user only, never through a symlink.

    Parameters
    ----------
    path
        The file, inside the 0700 data directory.
    url
        The sign-in link.
    """
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(_hand_off_page(url))


def _sign_the_browser_in(
    state: CliState,
    data_dir: DataDir,
    port: int,
    token: str | None,
    env: Mapping[str, str],
) -> _HandOff | None:
    """Open the browser signed in, with a link asked for with the person's own token.

    The link never appears on a command line: it is written to `sign-in.html` in the data
    directory (mode 0600), and the browser is given that file's path. The file is removed after
    a minute, or at shutdown if that comes first. Snap and Flatpak browsers cannot read the data
    directory, so a terminal also gets the link to open by hand; a pipe or a file never does.
    Every failure is a warning: the instance keeps running.

    Parameters
    ----------
    state
        The CLI state, for `login_client_factory`.
    data_dir
        The data directory.
    port
        The instance's port.
    token
        The `local` profile's token; `None` when there is no usable one (nothing happens).
    env
        The children's environment, for `LAKANA_UI__ENABLED`.

    Returns
    -------
    _HandOff or None
        The hand-off file to remove at shutdown, or None when none was written.
    """
    if not _ui_enabled(env):
        sys.stdout.write(
            "The web interface is off; to open it signed in, start again with "
            "LAKANA_UI__ENABLED=true.\n"
        )
        return None
    if token is None:
        return None
    try:
        with state.login_client_factory(f"http://127.0.0.1:{port}", token) as client:
            link = client.sign_in.local_link()
    except LakanaError as exc:
        sys.stderr.write(
            f"Couldn't open the browser signed in: {exc}. Open {shown_url(port)} and sign in "
            "with your passkey, or ask an administrator for a recovery link.\n"
        )
        return None
    if urlsplit(link.url).scheme not in {"http", "https"}:
        sys.stderr.write(
            f"Couldn't open the browser signed in: the server answered a link that is not a web "
            f"address. Open {shown_url(port)} and sign in with your passkey.\n"
        )
        return None
    hand_off = _HandOff(data_dir.sign_in_path)
    try:
        _write_hand_off(hand_off.path, link.url)
    except OSError as exc:
        sys.stderr.write(
            f"Couldn't write {hand_off.path} to open the browser signed in ({exc}); open "
            f"{shown_url(port)} and sign in with your passkey.\n"
        )
        return None
    hand_off.cancel = _schedule(_HAND_OFF_SECONDS, hand_off.remove)
    with contextlib.suppress(webbrowser.Error):
        webbrowser.open(hand_off.path.as_uri())
    if _stdout_is_terminal():
        sys.stdout.write(
            f"If the browser did not open signed in, open this link within 10 minutes: {link.url}\n"
        )
    return hand_off


def _probe_health(url: str) -> bool:
    """Check whether a platform's `/healthz` endpoint answers `200`.

    Parameters
    ----------
    url
        The platform's base URL (`http://127.0.0.1:<port>`).

    Returns
    -------
    bool
        `True` if `GET {url}/healthz` answered `200`; `False` on any other
        status or on a connection failure.

    Notes
    -----
    Sent with `trust_env=False`: this is always a loopback request, and
    `httpx`'s default of honouring `HTTP_PROXY`/`HTTPS_PROXY` would
    otherwise route it through a configured proxy — a dead or unreachable
    one turning a perfectly good local server into a spurious readiness
    timeout or `status` failure.
    """
    try:
        response = httpx.get(f"{url}/healthz", timeout=_HEALTH_TIMEOUT, trust_env=False)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    """Turn a delivered signal into a `KeyboardInterrupt`.

    Installed as the `SIGTERM` handler so `SIGTERM` and `SIGINT` (whose
    default handler already raises `KeyboardInterrupt`) take the same
    shutdown path in `start`.

    Parameters
    ----------
    signum
        The signal number received.
    frame
        The interrupted stack frame; unused.

    Raises
    ------
    KeyboardInterrupt
        Always.
    """
    raise KeyboardInterrupt


def _install_shutdown_handlers() -> dict[int, _SignalHandler]:
    """Install this run's shutdown-signal handlers.

    `SIGTERM` gets `_raise_keyboard_interrupt`, so it takes the same
    shutdown path as Ctrl-C. `SIGINT` gets `signal.default_int_handler`
    installed *explicitly* — its usual default action already raises
    `KeyboardInterrupt`, but a backgrounded run (`lakana server start --local
    &` from a non-interactive shell) inherits `SIGINT` as `SIG_IGN` from
    its shell, which would otherwise make `kill -INT` on this process do
    nothing. `SIGHUP`, on POSIX only, gets the
    same handler as `SIGTERM` unless it is already `SIG_IGN` — installing
    over that would defeat `nohup`, whose entire point is to make a hangup
    harmless. Without this, closing the terminal (or the SSH session) that
    started a foreground `start` orphaned both children with live
    pidfiles: the kernel's hangup never reaches them (they run in their own
    session), so nothing stops them, and the next `start` then refused with
    exit 2.

    Returns
    -------
    dict of int to object
        Each installed signal's previous handler, keyed by signal number.
        Pass to `_restore_signal_handlers` to put them back, or to
        `_ignore_signals` to silence them for a critical section.
    """
    previous: dict[int, _SignalHandler] = {
        signal.SIGTERM: signal.signal(signal.SIGTERM, _raise_keyboard_interrupt),
        signal.SIGINT: signal.signal(signal.SIGINT, signal.default_int_handler),
    }
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None and signal.getsignal(sighup) != signal.SIG_IGN:
        previous[sighup] = signal.signal(sighup, _raise_keyboard_interrupt)
    return previous


def _restore_signal_handlers(previous: dict[int, _SignalHandler]) -> None:
    """Put back the signal handlers `_install_shutdown_handlers` replaced.

    Parameters
    ----------
    previous
        The mapping it returned.
    """
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def _ignore_signals(signals: Iterable[int]) -> None:
    """Set `SIG_IGN` for each of `signals`.

    Used to make the final `Supervisor.stop()` uninterruptible: a second
    Ctrl-C/SIGTERM/SIGHUP arriving while it runs would otherwise raise
    `KeyboardInterrupt` again partway through cleanup, leaving a child that
    ignores its own graceful-stop signal running untracked, its pidfile
    already removed.

    Parameters
    ----------
    signals
        The signal numbers to ignore.
    """
    for sig in signals:
        signal.signal(sig, signal.SIG_IGN)


def _write_pidfile_or_fail(path: Path, pid: int, port: int | None = None) -> None:
    """Write a pidfile, reporting a failure as a clean `CliError`.

    Parameters
    ----------
    path
        The pidfile.
    pid
        The process id.
    port
        The server's port, for `server.pid`.

    Raises
    ------
    CliError
        If writing fails with an `OSError` (exit 3), naming `path` (plan
        ruling C).
    """
    try:
        write_pidfile(path, pid, port)
    except OSError as exc:
        raise CliError(f"{path}: {exc}", 3) from exc


def _read_pidfile_or_fail(path: Path) -> tuple[int, int | None] | None:
    """Read a pidfile, reporting a failure as a clean `CliError`.

    Parameters
    ----------
    path
        The pidfile.

    Returns
    -------
    tuple or None
        `read_pidfile`'s result.

    Raises
    ------
    CliError
        If reading fails with an `OSError` (exit 3), naming `path` — e.g.
        the pidfile path is actually a directory (plan ruling C). This is
        deliberately not treated as "no pidfile": a directory where a
        pidfile belongs is a broken data directory, not evidence that no
        instance is running.
    """
    try:
        return read_pidfile(path)
    except OSError as exc:
        raise CliError(f"{path}: {exc}", 3) from exc


def _remove_pidfiles(data_dir: DataDir) -> None:
    """Remove both pidfiles, attempting each even if the other fails.

    Parameters
    ----------
    data_dir
        The data directory.

    Raises
    ------
    CliError
        If any removal fails with an `OSError` (exit 3, naming that path;
        plan ruling C) — raised only after every removal has been
        attempted, so one failing path never leaves the other behind.
    """
    first_error: CliError | None = None
    for path in (data_dir.server_pid, data_dir.worker_pid):
        try:
            remove_pidfile(path)
        except OSError as exc:
            if first_error is None:
                first_error = CliError(f"{path}: {exc}", 3)
    if first_error is not None:
        raise first_error


def _raise_already_running(data_dir: DataDir) -> NoReturn:
    """Refuse to start: this data directory already has a running instance.

    Parameters
    ----------
    data_dir
        The data directory.

    Raises
    ------
    CliError
        Exit 2, naming the instance's URL when a pidfile records its port;
        exit 3 if the server pidfile can't be read.
    """
    info = _read_pidfile_or_fail(data_dir.server_pid)
    message = f"Lakana is already running from {data_dir.root}"
    if info is not None and info[1] is not None:
        message += f" at {shown_url(info[1])}"
    raise CliError(message)


def _refuse_lockless_instance(data_dir: DataDir) -> None:
    """Refuse if a lock-less (older lakana) instance is live; otherwise clear stale pidfiles.

    Called with the lock already held. A recorded server PID counts as live
    only if the process exists *and* its recorded port answers `/healthz`
    (a PID alone may belong to a reused, unrelated process). Never signals.

    Parameters
    ----------
    data_dir
        The data directory.

    Raises
    ------
    CliError
        If such an instance is live (exit 2), or a pidfile can't be read or
        removed (exit 3).
    """
    info = _read_pidfile_or_fail(data_dir.server_pid)
    if info is not None:
        pid, port = info
        if port is not None and pid_alive(pid) and _probe_health(f"http://127.0.0.1:{port}"):
            _raise_already_running(data_dir)
    _remove_pidfiles(data_dir)


def _lock_held(data_dir: DataDir) -> bool:
    """Report whether a running instance holds the data directory's lock.

    A non-blocking try-acquire, released immediately. Creates nothing: a
    data directory with no lock file has never been started, so nothing
    holds it.

    Parameters
    ----------
    data_dir
        The data directory.

    Returns
    -------
    bool
        `True` if another process holds `<data-dir>/lock`.

    Raises
    ------
    CliError
        If the lock file is a symlink (exit 2) or can't be opened (exit 3).
    """
    if not os.path.lexists(data_dir.lock_path):
        return False
    with try_locked(data_dir.lock_path) as free:
        return not free


def _child_env(
    data_dir: DataDir,
    port: int,
    key: str,
    *,
    key_id: str | None,
    secret: str | None,
    log_requests: bool = False,
) -> dict[str, str]:
    """Build the environment passed to both child processes.

    Parameters
    ----------
    data_dir
        The data directory, for the SQLite database URL.
    port
        The port the server binds to.
    key
        The secrets master key.
    key_id
        The bootstrap credential's key id; `None` after the first run.
    secret
        The bootstrap credential's secret; `None` after the first run.
    log_requests
        Keep the server's request log. Without it the server is told to print no line per
        request (`LAKANA_LOG__ACCESS=false`), so the ready line is not scrolled away; every other
        log line is unchanged, and a `LAKANA_LOG__ACCESS` already in the environment wins.

    Returns
    -------
    dict of str to str
        `os.environ` plus the local-mode overrides. The bootstrap
        credential is included only when `key_id`/`secret` are given — it
        is never printed or logged, only ever placed in this mapping
        (ruling F). On a later run (`key_id` is `None`), any
        `LAKANA_BOOTSTRAP_PAT_KEY_ID`/`SECRET` already inherited from the
        parent shell's own environment is dropped, so a stray value there
        never lets a later run silently re-bootstrap. `LAKANA_REPO_ROOTS`
        defaults to the user's home directory (resolved), so a local
        repository anywhere under it can be deployed; an inherited value wins.
        `LAKANA_BOOTSTRAP_TOKEN_FILE` defaults to a file in the data directory, for the one later
        run that has to mint a token (a first run whose migration failed); an inherited value wins.
        `LAKANA_RUN__SERVER_URL` defaults to this instance's own address, so the
        local worker can run Python assets when `uv` is on the `PATH`; an
        inherited value wins. `LAKANA_UI__PUBLIC_URL` defaults to
        `http://localhost:<port>`, the address browsers open (an inherited value wins), and
        `LAKANA_UI__LOCAL_SIGN_IN` is always `true`, so the person's own token can ask for a
        sign-in link; the server refuses it unless it binds a loopback address and the public
        address is on `localhost`.
    """
    env = {
        **os.environ,
        "LAKANA_DATABASE__URL": data_dir.database_url,
        "LAKANA_SECRETS__MASTER_KEY": key,
        "LAKANA_HOST": "127.0.0.1",
        "LAKANA_PORT": str(port),
        "PYTHONUNBUFFERED": "1",
    }
    if key_id is not None and secret is not None:
        env["LAKANA_BOOTSTRAP_PAT_KEY_ID"] = key_id
        env["LAKANA_BOOTSTRAP_PAT_SECRET"] = secret
    else:
        for name in _BOOTSTRAP_ENV_VARS:
            env.pop(name, None)
    env.setdefault("LAKANA_BOOTSTRAP_TOKEN_FILE", str(data_dir.bootstrap_token_path))
    if not log_requests:
        env.setdefault("LAKANA_LOG__ACCESS", "false")
    env.setdefault("LAKANA_RUN__SERVER_URL", f"http://127.0.0.1:{port}")
    env.setdefault("LAKANA_UI__PUBLIC_URL", shown_url(port))
    env["LAKANA_UI__LOCAL_SIGN_IN"] = "true"
    env.setdefault("LAKANA_REPO_ROOTS", json.dumps([os.path.realpath(Path.home())]))
    return env


def _first_run_setup(
    state: CliState, data_dir: DataDir, port: int, key_id: str, secret: str
) -> str:
    """Bootstrap a freshly created instance: save the `local` profile, then create `dev`.

    Saves the profile first: if `environments.create`
    below then fails or is interrupted, the admin token is already saved,
    so it's recoverable rather than lost with no way back short of
    deleting the data directory. A later run's `_later_run_profile_check`
    then calls `_ensure_dev_environment`, which creates `dev` itself; run
    by hand instead, the recovery command is `lakana env create dev
    --storage-root file://<data-dir>/lake/dev` — not `lakana environment
    create`, which isn't a command. The `dev` environment is
    created over the REST API (rule 2), authenticated as the bootstrap
    credential the CLI itself minted. The profile becomes the file's
    `default_profile` only if it has none yet. An existing `local` profile
    pointing anywhere other than this machine's own loopback address is
    still overwritten — a first run has to save its freshly minted
    credential somewhere named `local` — but a warning names the URL it
    replaced first, since that overwrite silently discards whatever
    instance (a remote server, or another data directory's own first run)
    held that token.

    Parameters
    ----------
    state
        The CLI state, for `login_client_factory` and the profile path.
    data_dir
        The data directory, for the `dev` environment's storage root.
    port
        The port the server is bound to.
    key_id
        The bootstrap credential's key id.
    secret
        The bootstrap credential's secret.

    Returns
    -------
    str
        The platform URL, for the ready line.
    """
    url = f"http://127.0.0.1:{port}"
    token = f"lakana_pat_{key_id}_{secret}"
    existing = _profiles.load(state.profile_path()).get("profiles", {}).get("local")
    old_url = None if existing is None else existing.get("url")
    if old_url is not None and not _LOOPBACK_URL_RE.fullmatch(old_url):
        sys.stderr.write(
            f"Replacing profile 'local', which pointed at {old_url!r}, not this machine's "
            "loopback address.\n"
        )
    _profiles.set_profile(
        state.profile_path(), "local", url=url, token=token, default_environment="dev"
    )
    with state.login_client_factory(url, token) as client:
        client.environments.create(
            "dev", storage={"scheme": "file", "root": data_dir.env_root("dev"), "options": {}}
        )
    return url


def _later_run_profile_check(state: CliState, data_dir: DataDir, url: str) -> str | None:
    """Reconcile the `local` profile on a later run: recovery hint, URL update, or a warning.

    A `local` profile missing its `token` is treated exactly like a
    missing `local` profile (the recovery hint, nothing else touched) —
    `validate_profile_document` allows a token-less profile (e.g. one
    relying on `LAKANA_TOKEN`), and this must never raise `KeyError` and
    tear down an already-running instance over it.

    The URL is rewritten only when the profile's *current* URL already
    looks like one of this machine's own previous runs
    (`http://127.0.0.1:<port>`) — never a remote server or another data
    directory's instance, which would otherwise be silently repointed at
    this run while keeping its original, now-mismatched token. A non-loopback URL gets a warning
    instead, and is
    left untouched — and, since that profile isn't ours to act on, `dev`
    is not checked either.

    Once the profile is confirmed to be this instance's own (its URL either
    already matches or was just updated to), `_ensure_dev_environment`
    checks and, if needed, creates `dev` — recovering a first run
    interrupted before that step.

    Parameters
    ----------
    state
        The CLI state, for the profile path.
    data_dir
        The data directory, named in the recovery hint.
    url
        This run's platform URL (`--port` may differ from an earlier run's).

    Returns
    -------
    str or None
        The profile's token once the profile is confirmed to be this instance's own; None
        otherwise (no profile, no token, or a profile of another instance).
    """
    profiles = _profiles.load(state.profile_path()).get("profiles", {})
    local_profile = profiles.get("local")
    token = None if local_profile is None else local_profile.get("token")
    if local_profile is None or token is None:
        sys.stderr.write(
            "No 'local' profile found; to start fresh, stop Lakana and run "
            f"'lakana server reset --local --data-dir {data_dir.root}'\n"
        )
        return None
    old_url = local_profile.get("url")
    if old_url != url:
        if old_url is None or not _LOOPBACK_URL_RE.fullmatch(old_url):
            sys.stderr.write(
                f"Profile 'local' points at {old_url!r}, not this machine's loopback address; "
                "leaving it unchanged.\n"
            )
            return None
        _profiles.set_profile(
            state.profile_path(), "local", url=url, token=token, default_environment=None
        )
        sys.stderr.write(f"Updated profile 'local': {old_url} -> {url} (--port changed)\n")
    _ensure_dev_environment(state, data_dir, url, token)
    return token


def _ensure_dev_environment(state: CliState, data_dir: DataDir, url: str, token: str) -> None:
    """Create `dev` if a later run, with a usable `local` profile, finds it missing.

    Recovers an instance whose first run was interrupted between saving the
    `local` profile (which happens first, see `_first_run_setup`) and
    creating `dev`: rather than leave that to a manually-run `lakana env
    create dev --storage-root file://<data-dir>/lake/dev`, a later run
    notices and creates it itself, over the REST API (rule 2), using the
    profile's own credential — the same one `_first_run_setup` would have
    used.

    Parameters
    ----------
    state
        The CLI state, for `login_client_factory`.
    data_dir
        The data directory, for `dev`'s storage root.
    url
        This run's platform URL.
    token
        The `local` profile's token.

    Notes
    -----
    Best-effort: any `LakanaError` (a rejected token, a server error, a
    connection failure) prints a warning and returns, so it never tears
    down the instance that just started.
    """
    try:
        with state.login_client_factory(url, token) as client:
            try:
                client.environments.get("dev")
            except NotFound:
                root = data_dir.env_root("dev")
                client.environments.create(
                    "dev", storage={"scheme": "file", "root": root, "options": {}}
                )
                sys.stderr.write(f"Created environment 'dev' at {root}\n")
    except LakanaError as exc:
        sys.stderr.write(
            f"Couldn't check environment 'dev' with profile 'local' ({type(exc).__name__}); "
            "if the profile's token is stale, stop Lakana and run "
            f"'lakana server reset --local --data-dir {data_dir.root}' to start fresh\n"
        )


def _ready_line(url: str) -> str:
    """Build the one-line confirmation printed once the instance is ready.

    Parameters
    ----------
    url
        The platform URL.

    Returns
    -------
    str
        The ready line, naming the profile, environment, and a first query
        to try.
    """
    return (
        f"Lakana is running at {url} — profile 'local', environment 'dev'. "
        'Try: lakana sql "SELECT 42"'
    )


def _check_process_supervision() -> None:
    """Refuse to start when the worker would refuse: this Python has no `os.waitid`.

    The worker watches each query process with `os.waitid`, which CPython on macOS has from 3.13
    only, and exits at start-up without it. Checking here, before anything is started, puts the
    cause and the next step in front of the user instead of a worker that stops after the server
    is up. The message repeats the worker's key sentence (`lakana_worker.__main__`; a test in
    the worker's tests keeps the two in step), because this package does not import the worker.

    Raises
    ------
    CliError
        If `os.waitid` does not exist (exit 2).
    """
    if not hasattr(os, "waitid"):
        raise CliError(
            "Local mode cannot start: this Python lacks os.waitid, which the worker needs to "
            "supervise query processes (macOS has it from Python 3.13).",
            hint=(
                "on macOS, install lakana with Python 3.13 or newer, then run "
                "'lakana server start --local' again."
            ),
        )


@server_app.command("start")
def start(
    ctx: typer.Context,
    local: Annotated[
        bool, typer.Option("--local", help="Run this machine's own instance.")
    ] = False,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Data directory (default: XDG).")
    ] = None,
    port: Annotated[
        int, typer.Option("--port", help="Port to bind on 127.0.0.1 (default 8765).")
    ] = _DEFAULT_PORT,
    log_requests: Annotated[
        bool,
        typer.Option(
            "--log-requests",
            help="Print a line for every request the server answers (default: none).",
        ),
    ] = False,
    no_browser: Annotated[
        bool,
        typer.Option(
            "--no-browser",
            help="Don't open the web interface signed in (it opens when LAKANA_UI__ENABLED=true).",
        ),
    ] = False,
) -> None:
    """Start Lakana on this machine: migrate, then the server and worker.

    With the web interface on (LAKANA_UI__ENABLED=true) it then opens http://localhost:<port>
    in your browser, signed in as you. Stop it with Ctrl-C, or from another terminal with
    'lakana server stop'.

    Parameters
    ----------
    ctx
        The Typer context.
    local
        Required: local-mode deployment on this machine.
    data_dir
        The data directory; defaults to `default_data_dir()`.
    port
        The port `lakana-server` binds to on `127.0.0.1`.
    log_requests
        Keep the server's request log.
    no_browser
        `--no-browser`: neither open the browser nor print a sign-in link.

    Raises
    ------
    CliError
        Without `--local` (exit 2); on a Python without `os.waitid` (exit 2); if the data
        directory, master key or a pidfile can't be prepared, read, written or removed
        (exit 2 or 3, see the helpers); if an instance already runs from this data
        directory or the port is in use (exit 2); if the `lakana[server]`
        extra isn't installed (exit 3).
    typer.Exit
        `0` on a clean Ctrl-C/SIGTERM shutdown; `1` if migration or a child
        failed; otherwise the exiting child's own exit code (plan ruling
        3).
    """
    if not local:
        raise CliError(
            "Team-mode deployment uses the Compose bundle (`lakana bundle init`); "
            "for this machine use --local"
        )
    _check_process_supervision()
    state = state_of(ctx)
    root = prepare_data_dir(data_dir or default_data_dir())
    # The lock, not the pidfiles, says whether an instance runs: it is held
    # from here until the supervisor has stopped (released last), covers the
    # window before any pidfile exists, and the OS drops it if this process
    # dies. An instance from an older lakana holds no lock, so a recorded PID
    # that is alive and answers `/healthz` still refuses; other pidfiles are stale.
    with try_locked(root.lock_path) as acquired:
        if not acquired:
            _raise_already_running(root)
        _refuse_lockless_instance(root)
        # Empty first: a supervisor that was killed left its pid here, and until ours is
        # written, no `stop` may trust it (the pid may belong to an unrelated process by now).
        clear_lock_owner(root.lock_path)
        try:
            _record_lock_owner(root)
            _run_locked(state, root, port, log_requests, open_browser=not no_browser)
        finally:
            clear_lock_owner(root.lock_path)


def _record_lock_owner(root: DataDir) -> None:
    """Write this process's PID into the lock file, so `lakana server stop` can find it.

    A failure is reported on stderr and start-up goes on: the instance runs, and `stop` then
    refuses with the Ctrl-C hint instead of guessing.

    Parameters
    ----------
    root
        The data directory.
    """
    if os.name == "nt":
        return
    try:
        write_lock_owner(root.lock_path, os.getpid())
    except OSError as exc:
        sys.stderr.write(
            f"Warning: couldn't record this instance's process in {root.lock_path} ({exc}); "
            "'lakana server stop' will not work for it, stop it with Ctrl-C.\n"
        )


def _process_cmdline(pid: int) -> str | None:
    """Read the command line of a process, where the platform exposes it.

    Parameters
    ----------
    pid
        The process id.

    Returns
    -------
    str or None
        The command line with its arguments joined by spaces; `None` when `/proc/<pid>` does
        not exist (no `/proc` on this platform, or no such process).
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode(errors="replace")


def _run_locked(
    state: CliState,
    root: DataDir,
    port: int,
    log_requests: bool = False,
    open_browser: bool = True,
) -> None:
    """Run the local instance; the caller holds the data directory's lock.

    Parameters
    ----------
    state
        The CLI state.
    root
        The prepared data directory.
    port
        The port `lakana-server` binds to on `127.0.0.1`.
    log_requests
        Keep the server's request log (see `_child_env`).
    open_browser
        Open the web interface signed in once the instance is ready (`_sign_the_browser_in`).

    Raises
    ------
    CliError
        If the port is in use (exit 2), the `lakana[server]` extra isn't
        installed (exit 3), or the master key or a pidfile can't be read
        or written (exit 2 or 3).
    typer.Exit
        `0` on a clean Ctrl-C/SIGTERM shutdown; `1` if migration or a child
        failed; otherwise the exiting child's own exit code.
    """
    if not port_free(port):
        raise CliError(
            f"Port {port} is in use.",
            hint=(
                "another program, or a Lakana instance started with a different --data-dir "
                "(check it with 'lakana server status --data-dir DIR'), is using it. Pass "
                "another port with --port; 'lakana server start' then points the profile "
                "'local' at it, so later commands find this instance (add --profile local if "
                "another profile is your default)."
            ),
        )
    server_script = find_script("lakana-server")
    worker_script = find_script("lakana-worker")

    first_run = not root.db_path.exists()
    key = load_or_create_master_key(root)
    key_id = secrets.token_hex(8) if first_run else None
    secret = secrets.token_urlsafe(32) if first_run else None
    env = _child_env(root, port, key, key_id=key_id, secret=secret, log_requests=log_requests)

    sup = make_supervisor(
        SupervisorConfig(
            server_cmd=[str(server_script)],
            worker_cmd=[str(worker_script)],
            migrate_cmd=[str(server_script), "migrate"],
            env=env,
            health_url=f"http://127.0.0.1:{port}/healthz",
        )
    )

    previous_handlers = _install_shutdown_handlers()
    hand_off: _HandOff | None = None
    try:
        try:
            sup.migrate()
            server = sup.start_server()
            _write_pidfile_or_fail(root.server_pid, server.pid, port)
            sup.wait_ready(server)
            token: str | None
            if key_id is not None and secret is not None:
                _first_run_setup(state, root, port, key_id, secret)
                token = f"lakana_pat_{key_id}_{secret}"
            else:
                token = _later_run_profile_check(state, root, f"http://127.0.0.1:{port}")
            worker = sup.start_worker()
            _write_pidfile_or_fail(root.worker_pid, worker.pid)
            if open_browser:
                hand_off = _sign_the_browser_in(state, root, port, token, env)
            sys.stdout.write(_ready_line(shown_url(port)) + "\n")
            # Flush explicitly: stdout is block-buffered, not line-buffered,
            # whenever it isn't a tty (piped to a file, `tee`, or a
            # subprocess reading it) -- a caller blocked in `readline()`
            # waiting for this exact line would otherwise hang until some
            # unrelated child output happened to trigger a flush, or forever
            # if none ever does (found when
            # a 283 s "pass" of the end-to-end test was this bug, not slow
            # startup).
            sys.stdout.flush()
            raise typer.Exit(sup.watch())
        except KeyboardInterrupt:
            raise typer.Exit(0) from None
        except SupervisorError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            if exc.output:
                sys.stderr.write(exc.output)
            raise typer.Exit(1) from exc
    finally:
        _ignore_signals(previous_handlers)
        if hand_off is not None:
            hand_off.cancel()
            hand_off.remove()
        try:
            sup.stop()
        finally:
            try:
                _remove_pidfiles(root)
            finally:
                _restore_signal_handlers(previous_handlers)


@dataclass(frozen=True)
class ServerStatus:
    """`lakana server status`'s result.

    Parameters
    ----------
    running
        Whether an instance runs: the data directory's lock is held, or the
        recorded server PID is alive and answers `/healthz`.
    url
        Its platform URL; `None` when not running or when no port is
        recorded (a held lock with no pidfile yet).
    data_dir
        The checked data directory.
    server_pid
        The recorded server PID, if any.
    worker_pid
        The recorded worker PID, if any.
    """

    running: bool
    url: str | None
    data_dir: str
    server_pid: int | None
    worker_pid: int | None


@server_app.command("status")
def status(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Data directory (default: XDG).")
    ] = None,
) -> None:
    """Report whether a local-mode instance is running.

    Running means the data directory's lock is held (even before any pidfile
    exists, or while `/healthz` is slow), or the recorded server PID is
    alive and answers `/healthz`.

    Parameters
    ----------
    ctx
        The Typer context.
    data_dir
        The data directory; defaults to `default_data_dir()`.

    Raises
    ------
    CliError
        If a pidfile can't be read, e.g. it's actually a directory (exit
        3; plan ruling C).
    typer.Exit
        `0` when running, `1` otherwise.
    """
    state = state_of(ctx)
    root = DataDir(resolve_data_dir(data_dir or default_data_dir()))
    server_info = _read_pidfile_or_fail(root.server_pid)
    worker_info = _read_pidfile_or_fail(root.worker_pid)
    server_pid = server_info[0] if server_info is not None else None
    worker_pid = worker_info[0] if worker_info is not None else None
    port = server_info[1] if server_info is not None else None
    # Running if the lock is held (authoritative: a live supervisor, even
    # one still migrating or slow to answer) or, for an instance started
    # before the lock existed, if the recorded server PID is alive and
    # answers `/healthz`. A free lock with live-looking pidfiles that do
    # not answer is stale.
    running = _lock_held(root) or (
        server_pid is not None
        and port is not None
        and pid_alive(server_pid)
        and _probe_health(f"http://127.0.0.1:{port}")
    )
    url = shown_url(port) if running and port is not None else None
    _output.emit(
        state,
        ServerStatus(
            running=running,
            url=url,
            data_dir=str(root.root),
            server_pid=server_pid,
            worker_pid=worker_pid,
        ),
    )
    raise typer.Exit(0 if running else 1)


def _can_signal() -> bool:
    """Report whether `lakana server stop` can signal the running instance on this platform.

    Returns
    -------
    bool
        `False` on Windows, where a console process cannot be sent the stop signal from another.
    """
    return os.name != "nt"


@server_app.command("stop")
def stop(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Data directory (default: XDG).")
    ] = None,
) -> None:
    """Stop this machine's local instance, as Ctrl-C in its terminal does.

    Asks the instance in the data directory to shut down and waits up to 30 seconds for it to
    leave. Nothing running is not an error. It stops only the instance on this machine: a
    Compose stack stops with 'docker compose down' in its bundle directory.

    Parameters
    ----------
    ctx
        The Typer context.
    data_dir
        The data directory; defaults to `default_data_dir()`.

    Raises
    ------
    CliError
        If aimed at a remote instance or run where signals are not supported (exit 2); if the
        instance was started by an older lakana, which did not record its process (exit 2); if
        it does not leave in time (exit 1); if a pidfile can't be read or removed (exit 3).
    """
    state = state_of(ctx)
    _refuse_remote_target(state, stopping=True)
    if not _can_signal():
        raise CliError(
            "'lakana server stop' is not supported on this platform",
            hint="stop the instance with Ctrl-C in the terminal where it runs",
        )
    root = DataDir(resolve_data_dir(data_dir or default_data_dir()))
    info = _read_pidfile_or_fail(root.server_pid)
    port = info[1] if info is not None else None
    where = f"at {shown_url(port)}" if port is not None else f"in {root.root}"
    if not _lock_held(root):
        _stop_without_lock(root, info)
        return
    owner = read_lock_owner(root.lock_path)
    if owner is None:
        raise CliError(
            f"Lakana is running from {root.root}, but this instance did not record which "
            "process to stop (it was started by an older lakana)",
            hint="stop it with Ctrl-C in its terminal",
        )
    if not pid_alive(owner):
        raise CliError(
            f"Lakana is running from {root.root}, but the process {owner} recorded for it is gone",
            hint="stop it with Ctrl-C in its terminal",
        )
    cmdline = _process_cmdline(owner)
    if cmdline is not None and "lakana" not in cmdline:
        raise CliError(
            f"Process {owner}, recorded as the one running from {root.root}, is not a Lakana "
            "process; nothing was signalled",
            hint="stop the instance with Ctrl-C in its terminal",
        )
    try:
        os.kill(owner, signal.SIGTERM)
    except OSError as exc:
        raise CliError(
            f"Can't signal process {owner}, which holds {root.root}: {exc}",
            hint="stop it with Ctrl-C in its terminal, or as the user who started it",
        ) from exc
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while _lock_held(root):
        if time.monotonic() >= deadline:
            raise CliError(
                f"The local instance (process {owner}) is still shutting down after "
                f"{_STOP_TIMEOUT_SECONDS:g} seconds",
                1,
                hint=f"wait and run 'lakana server stop' again, or end it with 'kill {owner}' "
                "(Lakana never kills it for you)",
            )
        time.sleep(_STOP_POLL_SECONDS)
    sys.stdout.write(f"Stopped the local instance {where}.\n")


def _stop_without_lock(root: DataDir, info: tuple[int, int | None] | None) -> None:
    """Report that nothing holds the lock: clean stale pidfiles, or refuse an older instance.

    Parameters
    ----------
    root
        The data directory.
    info
        The server pidfile's content, if any.

    Raises
    ------
    CliError
        If a lock-less instance of an older lakana is alive (exit 2), or a pidfile can't be
        read or removed (exit 3).
    """
    if info is not None:
        pid, port = info
        if port is not None and pid_alive(pid) and _probe_health(f"http://127.0.0.1:{port}"):
            raise CliError(
                f"Lakana is running from {root.root} at {shown_url(port)}, but this "
                "instance did not record which process to stop (it was started by an older "
                "lakana)",
                hint="stop it with Ctrl-C in its terminal",
            )
    had_pidfiles = any(
        _read_pidfile_or_fail(path) is not None for path in (root.server_pid, root.worker_pid)
    )
    if had_pidfiles:
        _remove_pidfiles(root)
        sys.stdout.write(
            f"No local instance is running in {root.root}; removed the stale pidfiles it left. "
            "Start one with 'lakana server start --local'.\n"
        )
        return
    sys.stdout.write(
        f"No local instance is running in {root.root}. "
        "Start one with 'lakana server start --local'.\n"
    )


def _refuse_remote_target(state: CliState, *, stopping: bool = False) -> None:
    """Refuse a reset or stop when `--url`, `LAKANA_URL` or a profile points off this machine.

    The command never talks to a server; this only stops someone who is aimed at a remote
    instance from believing the command reset or stopped it.

    Parameters
    ----------
    state
        The CLI state with `--url` and `--profile`.
    stopping
        Refuse for `lakana server stop` (with its hint) instead of `reset`.

    Raises
    ------
    CliError
        If the explicit URL, or the URL of the profile chosen with `--profile` or
        `LAKANA_PROFILE`, is not a loopback address (exit 2).
    """
    url = selected_url(state)
    if url is not None and not is_loopback_url(url) and stopping:
        raise CliError(
            f"{url} is a remote instance; 'lakana server stop' stops only the instance on "
            "this machine",
            hint="a Compose stack stops with 'docker compose down' in the bundle directory "
            "(see https://lakana-labs.github.io/lakana/how-to/deploy-with-compose/). "
            "To stop this machine's instance, unset LAKANA_URL and leave out --url and --profile.",
        )
    if url is not None and not is_loopback_url(url):
        raise CliError(
            f"{url} is a remote instance; 'lakana server reset' resets only the instance on "
            "this machine",
            hint="to reset this machine's instance, unset LAKANA_URL and leave out --url and "
            "--profile. To start a team instance over, use the Compose bundle: see "
            "'Starting over' in https://lakana-labs.github.io/lakana/how-to/deploy-with-compose/",
        )


@server_app.command("reset")
def reset(
    ctx: typer.Context,
    local: Annotated[
        bool, typer.Option("--local", help="Reset this machine's own instance.")
    ] = False,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Data directory (default: XDG).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete this machine's local instance's data, so the next start is a first run.

    Deletes the database, the tables' files and the secrets key of the instance in the data
    directory, and nothing else: your project files are never touched. It refuses while the
    instance runs, and in any directory that is not a local instance's data directory.

    Parameters
    ----------
    ctx
        The Typer context.
    local
        Required: the reset applies to the instance on this machine only.
    data_dir
        The data directory; defaults to `default_data_dir()`.
    yes
        Skip the confirmation prompt.

    Raises
    ------
    CliError
        Without `--local`, or aimed at a remote instance (exit 2); if the directory is not a
        local instance's, is a symlink, the home directory or a filesystem root, or an
        instance runs (exit 2); if the user declines or there is no terminal and no
        `--yes` (exit 2); if a pidfile or a deletion fails (exit 3).
    """
    state = state_of(ctx)
    if not local:
        raise CliError(
            "'lakana server reset' resets the instance on this machine only; "
            "it can't reset a remote or Compose instance",
            hint="add --local for this machine's instance. For a Compose deployment, see "
            "'Starting over' in https://lakana-labs.github.io/lakana/how-to/deploy-with-compose/",
        )
    _refuse_remote_target(state)
    given = data_dir or default_data_dir()
    root_path = _reset.refuse_unsafe_root(given, Path.home())
    if root_path is None:
        sys.stdout.write(f"Nothing to reset: there is no local instance in {given}.\n")
        return
    root = DataDir(root_path)
    with try_locked(root.lock_path) as acquired:
        if acquired:
            clear_lock_owner(root.lock_path)
        if not acquired:
            raise CliError(
                f"Lakana is running from {root.root}; nothing was deleted",
                hint="stop it with 'lakana server stop' (or Ctrl-C in its terminal), then run the "
                "reset again",
            )
        info = _read_pidfile_or_fail(root.server_pid)
        if (
            info is not None
            and info[1] is not None
            and pid_alive(info[0])
            and _probe_health(f"http://127.0.0.1:{info[1]}")
        ):
            raise CliError(
                f"Lakana is running from {root.root} at {shown_url(info[1])}; nothing was deleted",
                hint="stop it with 'lakana server stop' (or Ctrl-C in its terminal), then run the "
                "reset again",
            )
        plan = _reset.plan_reset(root_path)
        sys.stdout.write("\n".join(plan.describe()) + "\n")
        confirm(state, "Delete them?", yes)
        _reset.delete_targets(plan)
    sys.stdout.write(
        "Reset. Start again with 'lakana server start --local'; the next start is a first run.\n"
    )


def register(app: typer.Typer) -> None:
    """Add the `server` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(server_app, name="server")
