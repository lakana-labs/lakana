"""`lakana bundle init`: generate a Compose bundle and its secrets."""

from __future__ import annotations

import base64
import contextlib
import os
import re
import secrets
from importlib.resources import files
from pathlib import Path

import lakana
from lakana.cli._state import CliError

IMAGE_REPOSITORY = "ghcr.io/lakana-labs/lakana"
TASK_IMAGE_SUFFIX = "-task"
"""The task images' repository is the Lakana image's with this suffix, same tag."""
TEMPLATE_NAME = "compose.yaml"
WORKER_MEMORY = "4g"
"""Default hard memory limit of each worker container (`mem_limit`, no swap). The worker reads
its cgroup's limit and stops itself at 90 percent of it, so it fails with a message before the
kernel kills the container."""
_ENV_COMMENTS = {
    "LAKANA_TASK_IMAGE": "Task images, one per Python: this name plus -py3.11 ... -py3.14.",
    "LAKANA_RUNNER": (
        "docker: each Python task runs in a container of its own (isolated); local: as the "
        "worker's own processes (not isolated)."
    ),
    "LAKANA_DOCKER_SOCKET": "The Docker socket the launcher service mounts.",
    "LAKANA_EGRESS_UPSTREAM_PROXY": (
        "http://[user:password@]host:port of the proxy your network sends https through; empty "
        "for none."
    ),
    "LAKANA_EGRESS_REFUSED": (
        "Address ranges no task, build or connection may reach, comma-separated CIDRs or "
        "addresses: add this host's own LAN address or subnet, such as 192.168.1.0/24."
    ),
    "LAKANA_WORKER_MEMORY": (
        "Memory limit of each worker container; the worker stops itself at 90% of it "
        "(it is a limit per replica: LAKANA_WORKERS + LAKANA_WORKERS_INTERACTIVE + "
        "LAKANA_WORKERS_PIPELINE, times this, must fit the host)."
    ),
    "LAKANA_WORKERS_INTERACTIVE": (
        "Workers that take ad-hoc queries only (0: none). "
        "Keep a worker that takes pipeline work when you set this."
    ),
    "LAKANA_WORKERS_PIPELINE": (
        "Workers that take pipeline work only: deploys, tasks, maintenance (0: none)."
    ),
}
_RELEASE_VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")


def _cli_version() -> str:
    """Return the running CLI's version (a seam for tests).

    Returns
    -------
    str
        `lakana.__version__`.
    """
    return lakana.__version__


def default_image(version: str) -> str:
    """Return the image matching a release version.

    Parameters
    ----------
    version
        The CLI's version.

    Returns
    -------
    str
        `ghcr.io/lakana-labs/lakana:<version>`.

    Raises
    ------
    CliError
        For a dev or local build, whose version is not a valid image tag
        and has no published image (exit 2).
    """
    if not _RELEASE_VERSION.fullmatch(version):
        raise CliError(
            f"This CLI is a development build ({version}); there is no published image for it",
            hint="pass --image with an image you built, e.g. --image lakana:dev",
        )
    return f"{IMAGE_REPOSITORY}:{version}"


def task_image_for(image: str) -> str:
    """Name the task images that belong to a Lakana image.

    Parameters
    ----------
    image
        ``<repository>:<tag>``.

    Returns
    -------
    str
        ``<repository>-task:<tag>``; the launcher appends ``-py3.X``.

    Raises
    ------
    CliError
        For an image without a tag (a digest, or none), whose task images cannot be named; the
        hint says to pass `--task-image` (exit 2).
    """
    repository, _, tag = image.rpartition(":")
    if not repository or "/" in tag or "@" in image:
        raise CliError(
            f"Cannot tell the task image that goes with {image}",
            hint=(
                "pass --task-image with the task image built from the same release, without -py3.X"
            ),
        )
    return f"{repository}{TASK_IMAGE_SUFFIX}:{tag}"


def generate_env(
    *, image: str, task_image: str, bind: str, port: int, workers: int
) -> dict[str, str]:
    """Generate every `.env` value, with fresh secrets.

    Only URL-, YAML- and interpolation-safe alphabets are used.

    Parameters
    ----------
    image
        The Lakana image reference.
    task_image
        The task images' reference without ``-py3.X``.
    bind
        Host address the server port is published on.
    port
        Host port.
    workers
        Worker replicas.

    Returns
    -------
    dict
        Key to value, in file order.
    """
    return {
        "LAKANA_IMAGE": image,
        "LAKANA_TASK_IMAGE": task_image,
        "LAKANA_RUNNER": "docker",
        "LAKANA_DOCKER_SOCKET": "/var/run/docker.sock",
        "LAKANA_EGRESS_UPSTREAM_PROXY": "",
        "LAKANA_EGRESS_REFUSED": "",
        "LAKANA_BIND": bind,
        "LAKANA_PORT": str(port),
        "LAKANA_WORKERS": str(workers),
        "LAKANA_WORKERS_INTERACTIVE": "0",
        "LAKANA_WORKERS_PIPELINE": "0",
        "LAKANA_WORKER_MEMORY": WORKER_MEMORY,
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "LAKANA_S3_ACCESS_KEY": secrets.token_hex(10),
        "LAKANA_S3_SECRET_KEY": secrets.token_urlsafe(30),
        "LAKANA_SECRETS__MASTER_KEY": base64.b64encode(os.urandom(32)).decode(),
        "LAKANA_BOOTSTRAP_PAT_KEY_ID": secrets.token_hex(8),
        "LAKANA_BOOTSTRAP_PAT_SECRET": secrets.token_urlsafe(32),
    }


def bootstrap_token(env: dict[str, str]) -> str:
    """Return the admin token the bootstrap credential forms.

    Parameters
    ----------
    env
        The generated values.

    Returns
    -------
    str
        `lakana_pat_<key id>_<secret>`.
    """
    return f"lakana_pat_{env['LAKANA_BOOTSTRAP_PAT_KEY_ID']}_{env['LAKANA_BOOTSTRAP_PAT_SECRET']}"


def _bundle_paths(directory: Path) -> tuple[Path, Path]:
    """Return the two files a bundle consists of.

    Parameters
    ----------
    directory
        The bundle directory.

    Returns
    -------
    tuple of Path
        `(compose_path, env_path)`.
    """
    return directory / TEMPLATE_NAME, directory / ".env"


_ALREADY_EXISTS_HINT = (
    "re-running init would rotate the master key and lock you out of stored secrets"
)


def check_bundle_absent(directory: Path) -> None:
    """Raise if a bundle already exists at `directory`.

    Split out of `write_bundle` so a caller can run this check before a
    later, unrelated refusal (e.g. an existing profile name), so the
    refusal a user sees is always about whichever conflict is actually
    relevant to the target directory. This is a
    plain existence check, not a lock: a bundle can still be created by
    another process between this call and `write_bundle`'s own `O_EXCL`
    opens, which is what actually prevents two concurrent runs from
    overwriting each other.

    Parameters
    ----------
    directory
        Target directory.

    Raises
    ------
    CliError
        Exit 2, naming the file, if `compose.yaml` or `.env` already exists
        there or is a symlink (a dangling symlink counts as existing).
    """
    for existing in _bundle_paths(directory):
        if existing.exists() or existing.is_symlink():
            raise CliError(
                f"{existing} already exists; refusing to overwrite a deployment",
                hint=_ALREADY_EXISTS_HINT,
            )


def _create_file(path: Path, content: str, mode: int) -> None:
    """Create a new, non-symlinked file with exact content and mode.

    Parameters
    ----------
    path
        The file to create; must not already exist.
    content
        Its full text content.
    mode
        The file's permission bits.

    Raises
    ------
    OSError
        If the file can't be created (including if it already exists,
        raced into existence since the caller last checked).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)


def _cleanup_bundle(paths: list[Path], directory: Path, *, created_directory: bool) -> None:
    """Best-effort removal of only the files this `init` invocation itself created.

    Used both by `write_bundle` (a failure partway through the file writes
    themselves) and by the CLI command (a failure saving the profile,
    after both files were already written by this same call) so that a
    failed run never leaves a half-made bundle behind — re-running `init`
    afterwards must start clean rather than trip the "already exists"
    refusal over files that were never a real deployment. `paths` must never include a file this
    invocation did
    not itself create: a concurrent `init` racing on the same directory
    may have created the other one, and deleting that would destroy a
    real, already-completed deployment out from under it.

    Parameters
    ----------
    paths
        The files this invocation actually created, in any order.
    directory
        The bundle directory.
    created_directory
        Whether this invocation created `directory` (it did not exist
        beforehand). Only then is removing it (once empty) attempted.
    """
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()
    if created_directory:
        with contextlib.suppress(OSError):
            (directory / "repos").rmdir()
        with contextlib.suppress(OSError):
            directory.rmdir()


def write_bundle(directory: Path, env: dict[str, str]) -> None:
    """Write `compose.yaml` and `.env` (0600) into a directory, never overwriting.

    All-or-nothing: if writing either file fails partway through, whatever
    this call already wrote (and the directory, if this call created it and
    it's now empty) is removed before the error is reported, so a later
    re-run starts clean instead of tripping `check_bundle_absent` over
    debris from a run that never produced a working deployment. Only files
    this call's own `O_EXCL` open actually created are ever removed: if a
    concurrent `init` targeting the same directory wins the race on either
    file, this call's `O_EXCL` open of that file fails with `FileExistsError`
    and nothing belonging to the winner is touched.

    Also creates an empty `repos/` directory, mounted read-only into the
    server and worker as `/repos` (local repositories). Created here so
    Docker does not create it as root.

    Parameters
    ----------
    directory
        Target directory; created (0700) if missing.
    env
        The values for `.env`.

    Raises
    ------
    CliError
        Exit 2 if either file already exists, including a `FileExistsError`
        raced into existence by a concurrent invocation between this call's
        own `check_bundle_absent` and its `O_EXCL` opens; exit 3 naming the
        path on any other filesystem error.
    """
    check_bundle_absent(directory)
    compose_path, env_path = _bundle_paths(directory)
    created_directory = not directory.exists()
    created: list[Path] = []
    try:
        # A separate try/except: a `FileExistsError` here means `directory` itself
        # is an existing non-directory file (exit 3), never the
        # concurrent-bundle race the `_create_file` calls below distinguish.
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        _cleanup_bundle(created, directory, created_directory=created_directory)
        raise CliError(f"Can't write the deployment into {directory}: {exc}", 3) from exc

    try:
        template = files("lakana.cli.deploy").joinpath(TEMPLATE_NAME).read_text()
        body = "".join(
            (f"# {_ENV_COMMENTS[key]}\n" if key in _ENV_COMMENTS else "") + f"{key}='{value}'\n"
            for key, value in env.items()
        )
        _create_file(
            env_path,
            "# Generated by `lakana bundle init`. Contains secrets: keep it private.\n" + body,
            0o600,
        )
        created.append(env_path)
        _create_file(compose_path, template, 0o644)
        created.append(compose_path)
    except FileExistsError as exc:
        _cleanup_bundle(created, directory, created_directory=created_directory)
        raise CliError(
            f"{exc.filename or directory} already exists; refusing to overwrite a deployment",
            hint=_ALREADY_EXISTS_HINT,
        ) from exc
    except OSError as exc:
        _cleanup_bundle(created, directory, created_directory=created_directory)
        raise CliError(f"Can't write the deployment into {directory}: {exc}", 3) from exc

    try:
        (directory / "repos").mkdir(mode=0o755, exist_ok=True)
    except OSError as exc:
        _cleanup_bundle(created, directory, created_directory=created_directory)
        raise CliError(f"Can't create {directory / 'repos'}: {exc}", 3) from exc
