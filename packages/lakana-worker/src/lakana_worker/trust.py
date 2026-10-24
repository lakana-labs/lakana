"""The certificate authorities every build and task container trusts.

One file serves builds that reach a package index signed by a private certificate authority and
ingestion tasks that read a source behind a company CA: Mozilla's roots (certifi) followed by
the operator's `LAKANA_RUN__CA_BUNDLE`. Adding, never replacing, keeps public endpoints working.
The worker builds it once at start (`build_trust_bundle`) and, under the Docker runner, copies
it into each attempt's ``trust`` directory (`install_trust`); the launcher mounts that
read-only at ``/trust`` and points `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` at it. Under the
local runner the build and the task process get the same two variables, naming the worker's
own copy. Certificates only: a bundle holding a private key stops the worker, because every
task can read it.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from pathlib import Path

import certifi

from lakana_worker.ca_bundle import scan_pem
from lakana_worker.launcher.requests import TRUST_FILE

MAX_OPERATOR_BYTES = 1024 * 1024
"""The largest operator file accepted; a CA bundle of a company is a few kilobytes."""

TRUST_DIRECTORY = "trust"
"""The directory of an attempt that holds the bundle; the launcher mounts it at ``/trust``."""

_UNTRUSTED = re.compile(r"invalid peer certificate|certificate verify failed|UnknownIssuer")
"""How uv (rustls) and Python's `ssl` say that a server's certificate is not trusted."""

CERTIFICATE_HINT = (
    "a package index's certificate is not trusted; if your package index uses a private "
    "certificate authority, set LAKANA_RUN__CA_BUNDLE on the worker to a PEM file of it (see "
    'the how-to "Run Python tasks in containers")'
)
"""What a build failure adds when uv's output says a certificate was not trusted."""


class TrustBundleError(ValueError):
    """`LAKANA_RUN__CA_BUNDLE` names a file the containers cannot trust."""


def _refusal(path: Path, cause: str) -> str:
    """Word the refusal.

    Parameters
    ----------
    path
        The operator's file.
    cause
        What is wrong with it.

    Returns
    -------
    str
        What the worker says before it stops. The file's content is never quoted.
    """
    return (
        f"LAKANA_RUN__CA_BUNDLE names {path}, which {cause}. Point it at a file of PEM "
        "certificates only (at most 1 MiB)."
    )


def _write(target: Path, data: bytes) -> None:
    """Write a file readable by every container user, replacing what was there at once.

    Parameters
    ----------
    target
        The file; a link there is replaced, never followed.
    data
        Its content.
    """
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(temporary, 0o644)  # the umask may have taken bits off
    os.replace(temporary, target)


def build_trust_bundle(operator: Path | None, target: Path) -> Path | None:
    """Write the trust bundle, or nothing without an operator file.

    Parameters
    ----------
    operator
        `LAKANA_RUN__CA_BUNDLE`, or `None`.
    target
        Where to write the bundle (the worker's cache directory); a bundle an earlier start left
        there is removed when there is no operator file.

    Returns
    -------
    Path or None
        `target`, or `None` when there is no operator file.

    Raises
    ------
    TrustBundleError
        For a file that is not a readable regular file, is larger than 1 MiB, holds no
        certificate or holds a private key. The file is read once and exactly the bytes judged
        are written.
    """
    if operator is None:
        target.unlink(missing_ok=True)
        return None
    try:
        descriptor = os.open(operator, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise TrustBundleError(_refusal(operator, "is not a regular file"))
            data = handle.read(MAX_OPERATOR_BYTES + 1)
    except OSError as exc:
        raise TrustBundleError(
            _refusal(operator, f"cannot be read ({exc.strerror or exc})")
        ) from None
    if len(data) > MAX_OPERATOR_BYTES:
        raise TrustBundleError(_refusal(operator, "is larger than 1 MiB"))
    certificate, key = scan_pem(data)
    if key:
        raise TrustBundleError(
            _refusal(operator, "also holds a private key, and every task could read it")
        )
    if not certificate:
        raise TrustBundleError(_refusal(operator, "holds no PEM certificate"))
    roots = Path(certifi.where()).read_bytes()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(target, roots.rstrip(b"\n") + b"\n" + data)
    return target


def install_trust(bundle: Path, attempt_dir: Path) -> None:
    """Copy the worker's bundle into an attempt's ``trust`` directory for the launcher to mount.

    Copied, not linked: Docker refuses a subpath that leaves the volume through a link, and a
    link would not resolve in the container. Called before the build and again before the run
    of the same attempt; the second call replaces the file.

    Parameters
    ----------
    bundle
        What `build_trust_bundle` wrote.
    attempt_dir
        The attempt's private directory (0700, the worker's).

    Raises
    ------
    OSError
        If the directory cannot be made, or ``trust`` is there but is not a directory of the
        worker's own (a link, a file, another owner); the message names the path.
    """
    directory = attempt_dir / TRUST_DIRECTORY
    with contextlib.suppress(FileExistsError):
        os.mkdir(directory, 0o755)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(f"{directory} is not a directory of the worker's own")
    os.chmod(directory, 0o755)  # nosec B103 -- certificates only, inside the 0700 attempt
    _write(directory / TRUST_FILE, bundle.read_bytes())


def certificate_hint(output: str) -> str | None:
    """Say what to set when a build failed on an untrusted certificate.

    Parameters
    ----------
    output
        What uv printed.

    Returns
    -------
    str or None
        `CERTIFICATE_HINT` when the output says a certificate was not trusted, else `None`.
    """
    return CERTIFICATE_HINT if _UNTRUSTED.search(output) else None
