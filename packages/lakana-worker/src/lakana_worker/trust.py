"""The certificate authorities every build and task container trusts.

One bundle serves builds that reach a package index signed by a private certificate authority
and ingestion tasks that read a source behind a company CA: Mozilla's roots (certifi) followed
by the certificates of the operator's `LAKANA_RUN__CA_BUNDLE`. Adding, never replacing, keeps
public endpoints working. The worker builds it once at start (`build_trust_bundle`), in memory,
and keeps the bytes: under the Docker runner it writes them into each attempt's ``trust``
directory (`install_trust`), which the launcher mounts read-only at ``/trust`` and points
`SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` at. Under the local runner the build and the task
process get the same two variables, naming a file of the worker's cache (`write_trust_file`,
named by the bundle's digest because workers of a host share that directory). No worker reads
a file another worker could have replaced. Certificates only: of the operator's file just its
certificate blocks are used, and a file that also holds a private key stops the worker, because
every task can read the bundle.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from lakana_worker.ca_bundle import scan_pem
from lakana_worker.launcher.requests import TRUST_FILE

MAX_OPERATOR_BYTES = 1024 * 1024
"""The largest operator file accepted; a CA bundle of a company is a few kilobytes."""

TRUST_DIRECTORY = "trust"
"""The directory of an attempt that holds the bundle; the launcher mounts it at ``/trust``."""

_CERTIFICATE_BLOCK = re.compile(
    rb"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]*?-----END CERTIFICATE-----"
)
"""One PEM certificate block: header, base64 body, footer."""

_PGP_KEY = b"PRIVATE KEY BLOCK-----"
"""The end of a PGP private key's header, which the PEM scan does not know."""

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

    The temporary file has a name of its own (`tempfile.mkstemp`) in the target's directory, so
    workers that write at the same time never touch each other's.

    Parameters
    ----------
    target
        The file; a link there is replaced, never followed.
    data
        Its content.
    """
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(name, 0o644)  # mkstemp makes 0600
        os.replace(name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def build_trust_bundle(operator: Path | None) -> bytes | None:
    """Build the trust bundle in memory, or nothing without an operator file.

    Parameters
    ----------
    operator
        `LAKANA_RUN__CA_BUNDLE`, or `None`.

    Returns
    -------
    bytes or None
        Mozilla's roots, then each certificate of the operator's file; `None` when there is no
        operator file.

    Raises
    ------
    TrustBundleError
        For a file that is not a readable regular file, is larger than 1 MiB, holds no
        certificate or holds a private key, or when Mozilla's roots cannot be read. The file is
        read once. Only its ``CERTIFICATE`` blocks, each parsed as an X.509 certificate and
        written again in canonical form, reach the bundle: anything else in the file is dropped.
    """
    if operator is None:
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
    if scan_pem(data)[1] or _PGP_KEY in data:
        raise TrustBundleError(
            _refusal(operator, "also holds a private key, and every task could read it")
        )
    blocks = _CERTIFICATE_BLOCK.findall(data)
    if not blocks:
        raise TrustBundleError(_refusal(operator, "holds no PEM certificate"))
    pem = b""
    for block in blocks:
        try:
            certificate = x509.load_pem_x509_certificate(block)
        except ValueError:
            raise TrustBundleError(
                _refusal(operator, "holds a certificate block that is not a valid certificate")
            ) from None
        pem += certificate.public_bytes(serialization.Encoding.PEM)
    roots_path = certifi.where()
    try:
        roots = Path(roots_path).read_bytes()
    except OSError as exc:
        raise TrustBundleError(
            f"the worker cannot read Mozilla's certificate roots at {roots_path} "
            f"({exc.strerror or exc}); reinstall the worker's certifi package."
        ) from None
    return roots.rstrip(b"\n") + b"\n" + pem


def write_trust_file(bundle: bytes, directory: Path) -> Path:
    """Write the bundle for the local runner's processes, under a name of its content.

    The directory is shared by every worker of a host (the cache volume). The name holds the
    bundle's digest, so a sibling with the same bundle writes the same bytes to the same name,
    one with another bundle writes another file, and nobody deletes a file: an unset setting
    simply uses none.

    Parameters
    ----------
    bundle
        What `build_trust_bundle` returned.
    directory
        The worker's cache directory.

    Returns
    -------
    Path
        The file, readable by every user.

    Raises
    ------
    TrustBundleError
        If the file cannot be written; the message names the path and the cause.
    """
    target = directory / f"trust-{hashlib.sha256(bundle).hexdigest()[:16]}.pem"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(target, bundle)
    except OSError as exc:
        raise TrustBundleError(
            f"cannot write the trust bundle to {target} ({exc.strerror or exc}); "
            "check that the worker's cache directory is writable."
        ) from None
    return target


def install_trust(bundle: bytes, attempt_dir: Path) -> None:
    """Write the worker's bundle into an attempt's ``trust`` directory for the launcher to mount.

    Written from the bytes the worker holds, never copied from a file that a sibling worker
    could replace or delete; a file, never a link: Docker refuses a subpath that leaves the
    volume through a link, and a link would not resolve in the container. Called before the
    build and again before the run of the same attempt; the second call replaces the file.

    Parameters
    ----------
    bundle
        What `build_trust_bundle` returned.
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
    _write(directory / TRUST_FILE, bundle)


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
