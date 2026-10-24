"""The certificate authorities the query process trusts for HTTPS reads.

DuckDB's httpfs reads its CA bundle from the local disk, lazily, on the first HTTPS request. In
allow-list mode the query process may open only what the worker names, so the worker names one
PEM file: it becomes the connection's ``ca_cert_file`` and its only ``allowed_paths`` entry.

The file is resolved once, the first time it is asked for (the worker asks at start-up and logs
the path, never the content):

1. ``AWS_CA_BUNDLE``, if set: what an operator of an S3-compatible store with a private
   certificate authority already sets for the worker's own S3 client.
2. ``SSL_CERT_FILE``, if set.
3. The system bundle: the first readable file of `SYSTEM_BUNDLES` (DuckDB 2.0's own list). It
   keeps a corporate CA installed into the system store working.
4. `certifi.where()`: Mozilla's bundle, shipped with the worker.

A variable that names a missing, unreadable or non-PEM file stops the worker at start-up
(`CaBundleError`) instead of failing every HTTPS read later, and so does a bundle that also
holds a private key (the query process may read its bundle). No setting of Lakana's own exists
for this; see ``docs/site/reference/network-settings.md``.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Callable, Mapping, Sequence

import certifi

VARIABLES = ("AWS_CA_BUNDLE", "SSL_CERT_FILE")
"""The environment variables consulted, in order."""

SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)
"""DuckDB 2.0's own list of system bundles (Debian/Ubuntu, Fedora/RHEL, openSUSE, Alpine, macOS),
as its refusal to read one names them."""

_PEM_MARK = b"-----BEGIN CERTIFICATE-----"
_PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*" + rb"PRIVATE KEY-----")
"""The header of any PEM private key: PKCS#8 (``PRIVATE KEY``, ``ENCRYPTED PRIVATE KEY``) and
the algorithm-specific forms (``RSA``, ``EC``, ``DSA``, ``OPENSSH`` ...)."""


class CaBundleError(ValueError):
    """A variable names a file the query process cannot use as its CA bundle."""


def _message(variable: str, path: str) -> str:
    """Word the refusal of a variable's file.

    Parameters
    ----------
    variable
        The environment variable.
    path
        The path it names.

    Returns
    -------
    str
        What the worker says before it stops.
    """
    return (
        f"{variable} names {path}, which is not a readable file of PEM certificates. Point it "
        "at your object store's certificate authority, or unset it."
    )


def _key_message(source: str, path: str) -> str:
    """Word the refusal of a bundle that also holds a private key.

    Parameters
    ----------
    source
        The variable that named the file, or ``the system's bundle``.
    path
        The file.

    Returns
    -------
    str
        What the worker says before it stops. The key itself is never quoted.
    """
    variable = source if source in VARIABLES else VARIABLES[0]
    return (
        f"{source} names {path}, which also holds a private key, and a query can read the "
        f"bundle. Point {variable} at a file of certificates only."
    )


def _scan(path: str) -> tuple[bool, bool]:
    """Read a bundle line by line for certificates and private keys.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    tuple of (bool, bool)
        Whether the file could be read and holds ``-----BEGIN CERTIFICATE-----``, and whether
        it holds the header of a private key anywhere. An unreadable file gives two `False`.
    """
    certificate = key = False
    try:
        with open(path, "rb") as bundle:
            for line in bundle:
                certificate = certificate or _PEM_MARK in line
                key = key or _PRIVATE_KEY.search(line) is not None
    except OSError:
        return False, False
    return certificate, key


def scan_pem(data: bytes) -> tuple[bool, bool]:
    """Look for certificates and private keys in a PEM text held in memory.

    The same two marks as the start-up scan of a file, for a caller that read the file once
    and must judge exactly the bytes it will use.

    Parameters
    ----------
    data
        The text.

    Returns
    -------
    tuple of (bool, bool)
        Whether it holds ``-----BEGIN CERTIFICATE-----``, and whether it holds the header of a
        private key anywhere.
    """
    return _PEM_MARK in data, _PRIVATE_KEY.search(data) is not None


def find_ca_bundle(
    environ: Mapping[str, str] | None = None,
    system_bundles: Sequence[str] = SYSTEM_BUNDLES,
    fallback: Callable[[], str] = certifi.where,
) -> tuple[str, str]:
    """Find the CA bundle of the query process and say where it came from.

    See the module's docstring for the order.

    Parameters
    ----------
    environ
        The environment; the worker's own (`os.environ`) by default.
    system_bundles
        The system paths tried after the variables.
    fallback
        Names the bundle used when nothing else exists.

    Returns
    -------
    tuple of (str, str)
        The real path of the bundle: absolute (a relative variable is resolved against the
        worker's working directory) with symlinks resolved, because DuckDB stores
        ``ca_cert_file`` and ``allowed_paths`` by real path (on macOS ``/etc/ssl/cert.pem`` reads
        back as ``/private/etc/ssl/cert.pem``), so the path the worker logs, passes and allows is
        the one DuckDB uses. And
        its source for the start-up log: the variable's name, ``the system's bundle`` or
        ``certifi``.

    Raises
    ------
    CaBundleError
        If a set variable names a missing, unreadable or non-PEM file (an empty variable counts
        as not set), or if the chosen bundle also holds a private key: the query process may
        read its bundle, so a combined certificate-and-key file would hand the key to SQL that
        got past the guard.
    """
    environment = os.environ if environ is None else environ
    for variable in VARIABLES:
        value = environment.get(variable, "")
        if value:
            certificate, key = _scan(value)
            if not certificate:
                raise CaBundleError(_message(variable, value))
            if key:
                raise CaBundleError(_key_message(variable, value))
            return os.path.realpath(value), variable
    for candidate in system_bundles:
        if os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            if _scan(candidate)[1]:
                raise CaBundleError(_key_message("the system's bundle", candidate))
            return os.path.realpath(candidate), "the system's bundle"
    return os.path.realpath(fallback()), "certifi"


def resolve_ca_bundle(
    environ: Mapping[str, str] | None = None,
    system_bundles: Sequence[str] = SYSTEM_BUNDLES,
    fallback: Callable[[], str] = certifi.where,
) -> str:
    """Find the CA bundle of the query process (`find_ca_bundle` without the source).

    Parameters
    ----------
    environ
        As for `find_ca_bundle`.
    system_bundles
        As for `find_ca_bundle`.
    fallback
        As for `find_ca_bundle`.

    Returns
    -------
    str
        The real path of the bundle.

    Raises
    ------
    CaBundleError
        As `find_ca_bundle`.
    """
    return find_ca_bundle(environ, system_bundles, fallback)[0]


@functools.cache
def query_ca_bundle() -> str:
    """Resolve the CA bundle once for the life of the worker.

    Returns
    -------
    str
        What `resolve_ca_bundle` returned the first time.

    Raises
    ------
    CaBundleError
        As `resolve_ca_bundle`; nothing is cached then, so the worker's start-up check raises it
        before any query runs.
    """
    return resolve_ca_bundle()
