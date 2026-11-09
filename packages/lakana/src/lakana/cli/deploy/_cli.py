"""The `lakana bundle init` command."""

from __future__ import annotations

import ipaddress
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer

from lakana.cli import _profiles
from lakana.cli._state import CliError, state_of
from lakana.cli.deploy import _init
from lakana.cli.deploy._init import TEMPLATE_NAME

deploy_app = typer.Typer(help="Generate a team deployment bundle.", no_args_is_help=True)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_IMAGE_RE = re.compile(r"^[A-Za-z0-9._/:@-]+$")
_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
"""One label of a host name, as the server's `LAKANA_UI__PUBLIC_URL` check accepts it."""


def _is_loopback(host: str) -> bool:
    """Report whether a URL host refers to this machine only.

    Parameters
    ----------
    host
        A URL's hostname, lower-cased or not.

    Returns
    -------
    bool
        `True` for `127.0.0.1`, `localhost`, or `::1`.
    """
    return host.lower() in _LOOPBACK_HOSTS


def _validate_public_url(url: str) -> None:
    """Require `--public-url` to be an absolute `http(s)` URL with a host.

    Parameters
    ----------
    url
        The raw `--public-url` value.

    Raises
    ------
    CliError
        If the scheme isn't `http`/`https` or no host is present (exit 2).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CliError(f"--public-url must be an absolute http:// or https:// URL, got {url!r}")


def _is_ip_address(host: str) -> bool:
    """Tell whether a browser would read a URL host as an IP address.

    The same rule as the server's `LAKANA_UI__PUBLIC_URL` check: an IPv4 or IPv6 address, or
    any name whose last label is a number (`10.1`, `0x7f.1`), which browsers read as IPv4 too.

    Parameters
    ----------
    host
        A URL's hostname, lower case, without brackets.

    Returns
    -------
    bool
        True for an address.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        last = host.rstrip(".").rsplit(".", 1)[-1]
        return last.isdigit() or bool(re.fullmatch(r"0x[0-9a-f]*", last))
    return True


def _interface_url(public_url: str | None, port: int, ui: bool) -> tuple[str, bool]:
    """Choose the web interface's address (`LAKANA_UI__PUBLIC_URL`) for the bundle.

    Parameters
    ----------
    public_url
        `--public-url`, already checked by `_validate_public_url`, or None.
    port
        `--port`, the host port browsers reach the server at.
    ui
        `--ui`: whether the bundle turns the interface on.

    Returns
    -------
    tuple of (str, bool)
        The address, and whether it is the `http://localhost:<port>` fallback (which the
        command then names in one line).

    Raises
    ------
    CliError
        With `--ui`: for an IP address or anything else that is not a host name (passkeys
        need a name), or plain `http://` off `localhost` (exit 1). Without `--ui` such an address
        still serves the CLI profile, and the interface gets the fallback.
    """
    fallback = f"http://localhost:{port}"
    if public_url is None:
        return fallback, True
    parts = urlsplit(public_url)
    host = (parts.hostname or "").lower()
    if _is_loopback(host):
        return fallback, True
    if _is_ip_address(host) or not all(_LABEL_RE.fullmatch(p) for p in host.split(".")):
        if ui:
            raise CliError(
                "--public-url must use a host name for the web interface (passkeys are tied to "
                f"a name): use a DNS name, or leave it out for {fallback}.",
                1,
            )
        return fallback, True
    if ui and parts.scheme == "http":
        raise CliError(
            "--public-url must start with https:// for the web interface on a team server: "
            "browsers keep passkeys and the sign-in cookie safe only on a secure address. Put "
            "TLS in front of the server, or leave --public-url out for "
            f"{fallback} on this machine.",
            1,
        )
    shown = host if parts.port is None else f"{host}:{parts.port}"
    return f"{parts.scheme}://{shown}", False


def _validate_image(image: str, option: str = "--image") -> None:
    """Require an image option to use only characters safe in `.env` and Compose.

    Parameters
    ----------
    image
        The raw value.
    option
        The option's name, for the message.

    Raises
    ------
    CliError
        If it contains anything outside `[A-Za-z0-9._/:@-]` (exit 2) —
        notably quotes, `$`, `#`, whitespace, or shell metacharacters, any
        of which would corrupt `.env` or the generated Compose file.
    """
    if not _IMAGE_RE.fullmatch(image):
        raise CliError(f"{option} contains unsafe characters, got {image!r}")


_PYTHON_SUFFIX = re.compile(r"-py3\.\d+\Z")


def _validate_task_image(image: str) -> None:
    """Require `--task-image` to be a reference the launcher can extend with ``-py3.X``.

    Parameters
    ----------
    image
        The raw `--task-image` value.

    Raises
    ------
    CliError
        If it has unsafe characters, has no tag or a digest, already ends in ``-py3.X``, or has
        upper-case letters, which Docker refuses in a repository and the launcher in a task
        image (exit 2).
    """
    _validate_image(image, "--task-image")
    repository, _, tag = image.rpartition(":")
    if not repository or "/" in tag or "@" in image:
        raise CliError(
            f"--task-image must end in a tag, not a digest, got {image!r}",
            hint="the launcher appends -py3.11 ... -py3.14 to the tag, e.g. lakana-task:0.20.0",
        )
    if found := _PYTHON_SUFFIX.search(tag):
        raise CliError(
            f"--task-image names all four task images, so give it without {found.group()}",
            hint=f"--task-image {image[: -len(found.group())]}",
        )
    if image != image.lower():
        raise CliError(f"--task-image must be lower-case, got {image!r}")


@dataclass(frozen=True)
class _Bind:
    """A validated `--bind` value, classified for the default profile URL and warning.

    Parameters
    ----------
    loopback
        Whether it addresses only this machine: `127.0.0.0/8`, `::1`, or
        the literal `localhost`.
    wildcard
        Whether it's an "any address" bind (`0.0.0.0` or `::`) — not
        loopback, but the default profile URL still uses `127.0.0.1`,
        since that is how this same machine reaches it.
    host
        `bind` itself, bracketed if it's an IPv6 address (`[::1]`) —
        Compose's short port syntax and a URL both need the brackets.
        Used as `LAKANA_BIND` unconditionally, and as the profile URL's host
        only for a specific, non-loopback, non-wildcard address.
    """

    loopback: bool
    wildcard: bool
    host: str


def _parse_bind(bind: str) -> _Bind:
    """Validate and classify `--bind`.

    Parameters
    ----------
    bind
        The raw `--bind` value.

    Returns
    -------
    _Bind
        Its classification.

    Raises
    ------
    CliError
        If `bind` is neither the literal `localhost` nor a valid IP
        address (exit 2).
    """
    if bind == "localhost":
        return _Bind(loopback=True, wildcard=False, host="localhost")
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        raise CliError(f"--bind must be an IP address or 'localhost', got {bind!r}") from None
    host = f"[{bind}]" if isinstance(address, ipaddress.IPv6Address) else bind
    return _Bind(loopback=address.is_loopback, wildcard=address.is_unspecified, host=host)


@deploy_app.command("init")
def init(
    ctx: typer.Context,
    directory: Annotated[Path, typer.Argument(help="Target directory for the bundle.")] = Path(
        "lakana-deploy"
    ),
    public_url: Annotated[
        str | None,
        typer.Option("--public-url", help="The URL clients and the saved profile use."),
    ] = None,
    bind: Annotated[
        str, typer.Option("--bind", help="Host address the server port is published on.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Host port for the server.")] = 8000,
    workers: Annotated[int, typer.Option("--workers", help="Worker replicas.")] = 2,
    image: Annotated[
        str | None, typer.Option("--image", help="Override the Lakana image reference.")
    ] = None,
    task_image: Annotated[
        str | None,
        typer.Option("--task-image", help="Override the task images' reference (without -py3.X)."),
    ] = None,
    ui: Annotated[
        bool,
        typer.Option(
            "--ui", help="Turn the web interface on; --public-url must then be an https DNS name."
        ),
    ] = False,
) -> None:
    """Generate compose.yaml and .env for a team deployment, and save a profile.

    Parameters
    ----------
    ctx
        The Typer context.
    directory
        Where to write `compose.yaml` and `.env`.
    public_url
        The URL the saved profile (and operators) should use; defaults to
        `http://127.0.0.1:<port>` for a loopback or wildcard `--bind`, or
        to `http://<bind>:<port>` for a specific non-loopback address. With a host name it is
        also the web interface's address (`LAKANA_UI__PUBLIC_URL`); otherwise that is
        `http://localhost:<port>`.
    bind
        The host address the server's port is published on: an IP address
        or the literal `localhost`.
    port
        The host port the server is published on.
    workers
        How many worker replicas to run.
    image
        The Lakana image; defaults to the release image matching this CLI's
        version.
    task_image
        The task images' reference without ``-py3.X`` (the launcher appends one per Python);
        defaults to the Lakana image's repository with ``-task`` and the same tag
        (`task_image_for`).
    ui
        `--ui`: write `LAKANA_UI__ENABLED=true`, so the server serves the web interface.

    Raises
    ------
    CliError
        If `--workers`, `--port`, `--image`, `--task-image`, `--bind` or
        `--public-url` are invalid (exit 2); with `--ui`, if `--public-url` is an IP address
        or plain `http://` off `localhost` (exit 1); if `--image` has no tag and no
        `--task-image` is given (exit 2, `task_image_for`); if a bundle
        already exists at `directory` (exit 2, checked before the
        profile-name check so the refusal shown is always the relevant
        one); if a profile of the target name already
        exists (exit 2); if this CLI is a dev/local build and no `--image`
        was given (exit 2, `default_image`); or if writing the bundle
        fails (exit 3 for a filesystem error naming the path,
        `write_bundle`). A failure saving the profile after the bundle was
        written removes the whole bundle before propagating.
    """
    if workers < 1:
        raise CliError("--workers must be at least 1")
    if not (1 <= port <= 65535):
        raise CliError("--port must be between 1 and 65535")
    if image is not None:
        _validate_image(image)
    if task_image is not None:
        _validate_task_image(task_image)
    parsed_bind = _parse_bind(bind)
    if public_url is not None:
        _validate_public_url(public_url)
    interface_url, fallback = _interface_url(public_url, port, ui)

    # Not redundant with write_bundle's own internal check further down: this early call exists
    # purely for
    # refusal *ordering* (this refusal before the profile-name one
    # below), while write_bundle's own check is its documented contract for
    # any direct caller and the TOCTOU race write_bundle's O_EXCL opens
    # ultimately guard against.
    _init.check_bundle_absent(directory)

    state = state_of(ctx)
    name = state.profile or "team"
    existing = _profiles.load(state.profile_path()).get("profiles", {})
    if name in existing:
        raise CliError(
            f"Profile {name!r} already exists; pass --profile NAME to choose another name",
            hint="or remove the old one first with 'lakana profile remove'",
        )

    resolved_image = image or _init.default_image(_init._cli_version())
    resolved_task = task_image or _init.task_image_for(resolved_image)
    # Bracketed for any IPv6 address: Compose's short port syntax
    # `${LAKANA_BIND}:${LAKANA_PORT}:8000` can't parse a bare `::1`, only `[::1]`.
    # Docker's `host_ip` must be an IP literal, so `localhost` becomes `127.0.0.1`
    # rather than the bare word.
    env_bind = "127.0.0.1" if bind == "localhost" else parsed_bind.host
    env = _init.generate_env(
        image=resolved_image,
        task_image=resolved_task,
        bind=env_bind,
        port=port,
        workers=workers,
        public_url=interface_url,
        ui=ui,
    )

    created_directory = not directory.exists()
    _init.write_bundle(directory, env)

    default_host = (
        "127.0.0.1" if (parsed_bind.loopback or parsed_bind.wildcard) else parsed_bind.host
    )
    url = public_url or f"http://{default_host}:{port}"
    try:
        _profiles.set_profile(
            state.profile_path(),
            name,
            url=url,
            token=_init.bootstrap_token(env),
            default_environment="dev",
        )
    except Exception:
        # Both files are unconditionally this invocation's own here: write_bundle
        # already returned successfully, so its own O_EXCL opens are what created
        # them (a concurrent racer would instead have made write_bundle itself
        # raise FileExistsError, not gotten this far).
        _init._cleanup_bundle(
            list(_init._bundle_paths(directory)), directory, created_directory=created_directory
        )
        raise

    if not parsed_bind.loopback:
        sys.stderr.write(
            f"Warning: --bind {bind} is not loopback; the server accepts plain HTTP "
            "connections from your network.\n"
        )
    if public_url is not None:
        parts = urlsplit(public_url)
        if parts.scheme == "http" and not _is_loopback(parts.hostname or ""):
            sys.stderr.write(
                f"Warning: --public-url {public_url} is plain HTTP; put TLS in front of "
                "the server before exposing it.\n"
            )

    if fallback:
        sys.stderr.write(
            f"The web interface's address is {interface_url} (LAKANA_UI__PUBLIC_URL in "
            f"{directory / '.env'}); for a team, set it to the https:// DNS name people open.\n"
        )

    sign_in = "  lakana sign-in setup-link   # open it to sign in to the web interface\n"
    sys.stdout.write(
        f"Wrote {directory / TEMPLATE_NAME} and {directory / '.env'}\n\n"
        "Next steps:\n"
        f"  cd {directory} && docker compose up -d --wait\n"
        '  lakana sql "SELECT 42"\n' + (sign_in if ui else "")
    )


def register(app: typer.Typer) -> None:
    """Add the `bundle` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(deploy_app, name="bundle")
