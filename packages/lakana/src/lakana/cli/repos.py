"""`lakana repo *`: register and manage git repositories."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from lakana._models import HostKey, RepositoryProbe
from lakana.cli import _output
from lakana.cli._state import CliError, CliState, confirm, resolve_environment, state_of
from lakana.resources.repositories import UPDATABLE

repo_app = typer.Typer(help="Register and manage git repositories.", no_args_is_help=True)

_LIST_COLUMNS = ["name", "kind", "url", "default_ref", "credential_secret"]


def _read_known_hosts(path: Path | None) -> str | None:
    """Read a known_hosts file given on the command line.

    Parameters
    ----------
    path
        `--known-hosts-file`, or `None`.

    Returns
    -------
    str or None
        Its text.

    Raises
    ------
    CliError
        If it cannot be read (exit 2).
    """
    if path is None:
        return None
    try:
        return path.read_text()
    except OSError as exc:
        raise CliError(f"Can't read {path}: {exc}") from exc


@repo_app.command("add")
def add(
    ctx: typer.Context,
    name: str,
    kind: Annotated[str, typer.Option("--kind", help="local, https or ssh.")],
    url: Annotated[str, typer.Option("--url", help="URL, or a path for --kind local.")],
    credential_secret: Annotated[
        str | None,
        typer.Option("--credential-secret", help="Secret name, resolved per environment."),
    ] = None,
    credential_username: Annotated[
        str | None, typer.Option("--credential-username", help="HTTPS username (default oauth2).")
    ] = None,
    known_hosts_file: Annotated[
        Path | None, typer.Option("--known-hosts-file", help="known_hosts lines for --kind ssh.")
    ] = None,
    default_ref: Annotated[
        str | None, typer.Option("--default-ref", help="Ref deployed by default.")
    ] = None,
) -> None:
    """Register repository NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository name.
    kind
        Its kind.
    url
        Its URL or path.
    credential_secret
        Secret name.
    credential_username
        HTTPS username.
    known_hosts_file
        A known_hosts file.
    default_ref
        Default ref.
    """
    state = state_of(ctx)
    if kind == "local":
        url = str(Path(url).expanduser().resolve())
    repo = state.client().repositories.create(
        name,
        kind,
        url,
        credential_secret=credential_secret,
        credential_username=credential_username,
        ssh_known_hosts=_read_known_hosts(known_hosts_file),
        default_ref=default_ref,
    )
    _output.done(state, f"Registered repository {_output.inline(name)}", repo)


@repo_app.command("list")
def list_repos(ctx: typer.Context) -> None:
    """List repositories.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(
        state,
        state.client().repositories.list(),
        columns=_LIST_COLUMNS,
        droppable=["credential_secret", "default_ref", "kind"],
    )


@repo_app.command("show")
def show(ctx: typer.Context, name: str) -> None:
    """Show repository NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository name.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().repositories.get(name))


@repo_app.command("update")
def update(
    ctx: typer.Context,
    name: str,
    url: Annotated[str | None, typer.Option("--url")] = None,
    credential_secret: Annotated[str | None, typer.Option("--credential-secret")] = None,
    credential_username: Annotated[str | None, typer.Option("--credential-username")] = None,
    known_hosts_file: Annotated[Path | None, typer.Option("--known-hosts-file")] = None,
    default_ref: Annotated[str | None, typer.Option("--default-ref")] = None,
    clear: Annotated[
        list[str] | None, typer.Option("--clear", help="Clear a field (repeatable).")
    ] = None,
) -> None:
    """Change repository NAME; only the options given change.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository name.
    url
        New URL.
    credential_secret
        New secret name.
    credential_username
        New HTTPS username.
    known_hosts_file
        New known_hosts file.
    default_ref
        New default ref.
    clear
        Fields to clear.

    Raises
    ------
    CliError
        If nothing would change, `--clear` names an unknown field, or a field is
        both set and cleared (exit 2).
    """
    state = state_of(ctx)
    changes: dict[str, str | None] = {
        key: value
        for key, value in {
            "url": url,
            "credential_secret": credential_secret,
            "credential_username": credential_username,
            "ssh_known_hosts": _read_known_hosts(known_hosts_file),
            "default_ref": default_ref,
        }.items()
        if value is not None
    }
    for field in clear or []:
        if field not in UPDATABLE - {"url"}:
            raise CliError(
                f"--clear accepts {', '.join(sorted(UPDATABLE - {'url'}))}; got {field!r}"
            )
        if field in changes:
            raise CliError(f"Can't both set and clear {field}; pass only one.")
        changes[field] = None
    if not changes:
        raise CliError("Nothing to change; pass at least one option.")
    repo = state.client().repositories.update(name, **changes)
    _output.done(state, f"Updated repository {_output.inline(name)}", repo)


@repo_app.command("remove")
def remove(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete repository NAME and its deploy history.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository name.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Delete repository {name} and its deploy history?", yes)
    state.client().repositories.delete(name)
    _output.done(state, f"Deleted repository {_output.inline(name)}")


_clock = time.monotonic
"""The clock the status line counts on (a seam for the tests)."""

STATUS_AFTER_SECONDS = 1.0
"""How long a check may wait before the command says it waits."""

HOST_KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)
"""Host key types, strongest first: `--save` writes the strongest one unless `--type` picks."""

COMPARE = (
    "Compare this fingerprint with your host's published fingerprints before you use the file."
)
"""The warning `--save` prints: each saved key must be one the person compared."""


class _StatusLine:
    """Say once on standard error that a check still waits, after `STATUS_AFTER_SECONDS`.

    Parameters
    ----------
    state
        The CLI state (nothing is printed with ``--output json``).
    what
        What is being checked, for the line.
    """

    def __init__(self, state: CliState, what: str) -> None:
        self._state = state
        self._what = what
        self._started = _clock()
        self._printed = False

    def __call__(self, probe: RepositoryProbe) -> None:
        """Print the line if it is time and it was not printed yet.

        Parameters
        ----------
        probe
            The check as just polled.
        """
        if self._printed or self._state.output == "json":
            return
        if _clock() - self._started < STATUS_AFTER_SECONDS:
            return
        self._printed = True
        doing = "to take" if probe.state == "queued" else "to finish"
        sys.stderr.write(_output.safe_text(f"Waiting for a worker {doing} {self._what}…") + "\n")


def _finish(state: CliState, probe: RepositoryProbe) -> None:
    """Print a done check's outcome; exit 1 when it is not ``ok``.

    A host-key check that is ``ok`` printed its fingerprints already.

    Parameters
    ----------
    state
        The CLI state.
    probe
        The check, done.

    Raises
    ------
    typer.Exit
        With code 1 when the outcome is not ``ok``.
    """
    if state.output == "json":
        _output.print_json(probe)
    elif probe.outcome != "ok" or probe.kind != "host_keys":
        line = f"{_output.inline(probe.outcome or 'failed')}: {_output.inline(probe.message or '')}"
        sys.stdout.write(_output.safe_text(line) + "\n")
    if probe.outcome != "ok":
        raise typer.Exit(1)


def _chosen_keys(keys: list[HostKey], types: list[str] | None) -> list[HostKey]:
    """Pick the keys `--save` writes.

    Parameters
    ----------
    keys
        The keys the host offered.
    types
        `--type` values; `None` picks the strongest key only.

    Returns
    -------
    list of HostKey
        One key without `types` (ed25519 when offered); every key of each given type otherwise.

    Raises
    ------
    CliError
        If a given type was not offered (exit 2).
    """
    if not types:
        ranked = sorted(
            (k for k in keys if k.type in HOST_KEY_TYPES),
            key=lambda k: HOST_KEY_TYPES.index(k.type),
        )
        return ranked[:1]
    offered = {k.type for k in keys}
    missing = [t for t in types if t not in offered]
    if missing:
        raise CliError(
            f"The host offered no {', '.join(missing)} key; it offered "
            f"{', '.join(sorted(offered)) or 'none'}. Pick one of those with --type."
        )
    return [k for k in keys if k.type in types]


def _refuse_link(path: Path) -> None:
    """Refuse a `--save` target that is a symbolic link.

    Parameters
    ----------
    path
        The target.

    Raises
    ------
    CliError
        If it is a symbolic link, dangling or not (exit 2).
    """
    if path.is_symlink():
        raise CliError(
            f"{path} is a symbolic link; --save never writes through one, so it cannot replace "
            "a file elsewhere. Name the real file, or a new path."
        )


def _check_target(path: Path, force: bool) -> None:
    """Refuse a `--save` target before a worker is asked.

    Parameters
    ----------
    path
        The target.
    force
        Whether an existing file may be replaced.

    Raises
    ------
    CliError
        For a symbolic link, or an existing file without `force` (exit 2).
    """
    _refuse_link(path)
    if path.exists() and not force:
        raise CliError(
            f"{path} already exists; --save would replace it (if it is your own known_hosts, "
            "its keys would be lost). Name a new file, or add --force to replace it."
        )


def _write_known_hosts(path: Path, text: str, *, force: bool) -> None:
    """Write the chosen known_hosts lines, never through a symbolic link.

    Parameters
    ----------
    path
        The target.
    text
        The lines.
    force
        Whether an existing file may be replaced; without it the file must be new.

    Raises
    ------
    CliError
        If the target became a link or appeared meanwhile, or cannot be written (exit 2).
    """
    _refuse_link(path)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_TRUNC if force else os.O_EXCL
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise CliError(f"{path} already exists; add --force to replace it.") from exc
    except OSError as exc:
        raise CliError(f"Can't write {path}: {exc.strerror or exc}") from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


@repo_app.command("host-keys")
def host_keys(
    ctx: typer.Context,
    url: str,
    save: Annotated[
        Path | None,
        typer.Option(
            "--save",
            help="Write the known_hosts line of the strongest key (ssh-ed25519 when offered) "
            "to FILE, for `repo add --known-hosts-file`.",
        ),
    ] = None,
    key_type: Annotated[
        list[str] | None,
        typer.Option(
            "--type", help="With --save: write this key type instead (repeatable), e.g. ssh-rsa."
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="With --save: replace FILE if it already exists.")
    ] = False,
) -> None:
    """Fetch the SSH host keys of URL's host and print their fingerprints.

    A worker connects to the host (the server never does); the command waits up to 60 seconds.
    Compare each fingerprint with your git host's published fingerprints before you trust a
    key. Exits 1 unless the worker found keys.

    Parameters
    ----------
    ctx
        The Typer context.
    url
        An SSH repository URL, e.g. ``git@github.com:team/analytics.git``.
    save
        A file to write the chosen known_hosts line(s) to; an existing one only with `force`,
        a symbolic link never.
    key_type
        Key types to write instead of the strongest one.
    force
        Replace an existing `save` file.

    Raises
    ------
    CliError
        If no worker finished the check in time (exit 1); or, before anything is asked, for
        `--type` or `--force` without `--save`, an existing FILE without `--force` or a FILE
        that is a symbolic link; or if the file cannot be written or a `--type` was not offered
        (exit 2).
    """
    state = state_of(ctx)
    if save is None and (key_type or force):
        raise CliError(
            f"{'--type' if key_type else '--force'} only says what --save writes; add "
            "--save FILE, or leave it out to just print the fingerprints."
        )
    if save is not None:
        _check_target(save, force)
    try:
        probe = state.client().repositories.host_keys(
            url, on_update=_StatusLine(state, f"the host keys of {_output.inline(url)}")
        )
    except TimeoutError as exc:
        raise CliError(str(exc), 1, kind="RepositoryCheckTimeout") from exc
    if probe.outcome == "ok" and state.output != "json":
        for key in probe.host_keys or []:
            sys.stdout.write(_output.safe_text(f"{key.type}  {key.fingerprint}") + "\n")
    if probe.outcome == "ok" and save is not None:
        chosen = _chosen_keys(probe.host_keys or [], key_type)
        _write_known_hosts(save, "".join(k.line + "\n" for k in chosen), force=force)
        for key in chosen:
            _output.next_step(state, f"Saved {key.type} {key.fingerprint} to {save}.")
        _output.next_step(state, COMPARE)
        _output.next_step(
            state,
            f"Then: lakana repo add NAME --kind ssh --url {_output.inline(url)} "
            f"--known-hosts-file {save}",
        )
    _finish(state, probe)


@repo_app.command("test")
def test_access(
    ctx: typer.Context,
    name: str,
    env: Annotated[
        str | None, typer.Option("--env", help="Environment (default: the profile's).")
    ] = None,
) -> None:
    """Test that a worker can read repository NAME with ENV's credential.

    The worker runs `git ls-remote` with the repository's known hosts and ENV's secret, as a
    deploy would, and fetches nothing. Prints ``ok: …`` or ``<outcome>: …`` with what to do
    next; exits 1 unless the outcome is ``ok``. Waits up to 120 seconds.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository's name.
    env
        The environment whose credential is used.

    Raises
    ------
    CliError
        If no worker finished the check in time (exit 1).
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    try:
        probe = state.client().repositories.test_access(
            name,
            environment,
            on_update=_StatusLine(state, f"the access check of {_output.inline(name)}"),
        )
    except TimeoutError as exc:
        raise CliError(str(exc), 1, kind="RepositoryCheckTimeout") from exc
    _finish(state, probe)


@repo_app.command("deploy-key")
def deploy_key(
    ctx: typer.Context,
    name: str,
    environment: Annotated[
        list[str] | None,
        typer.Option(
            "--environment",
            "--env",
            help="Only this environment (repeatable). Default: every environment you may manage "
            "secrets in that has no key yet.",
        ),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help="Replace the key of each --environment; the old key stops working.",
        ),
    ] = False,
    secret: Annotated[
        str | None,
        typer.Option("--secret", help="Secret name for the keys (default: NAME-deploy-key)."),
    ] = None,
) -> None:
    """Make an SSH deploy key for repository NAME in each environment.

    The server makes one key pair per environment and keeps the private key as that
    environment's secret; it is never shown. The command prints each environment's public key:
    add each one at your git host as a read-only deploy key. An environment that has a key
    keeps it unless you name it with --environment and --replace.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The repository's name; an ssh repository with confirmed host keys.
    environment
        Environments to limit the call to.
    replace
        Replace the keys of `environment`.
    secret
        The secret name.
    """
    state = state_of(ctx)
    made = state.client().repositories.deploy_key(
        name, secret=secret, environments=environment, replace=replace
    )
    if state.output == "json":
        _output.print_json(made)
        return
    for key in made.keys:
        status = "new" if key.made else "made earlier"
        sys.stdout.write(_output.safe_text(f"{key.environment} ({status}):") + "\n")
        sys.stdout.write(_output.safe_text(key.public_key) + "\n")
    for note in made.notes:
        _output.next_step(state, note)
    for skipped in made.skipped:
        _output.next_step(
            state,
            f"No key in {_output.inline(skipped)}: you may not manage its secrets. Someone who "
            f"may runs: lakana repo deploy-key {_output.inline(name)}",
        )
    if made.keys:
        _output.next_step(state, "Add each key as a read-only deploy key at your git host.")
        _output.next_step(
            state,
            f"Then: lakana repo test {_output.inline(name)} --env "
            f"{_output.inline(made.keys[0].environment)}",
        )


def register(app: typer.Typer) -> None:
    """Add the `repo` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(repo_app, name="repo")
