"""A private-CA package index and a task behind a company CA, on a real Docker Engine.

With `LAKANA_RUN__CA_BUNDLE` the worker writes Mozilla's roots plus the operator's certificates
into each attempt; the launcher mounts them read-only at ``/trust`` and points `SSL_CERT_FILE`
and `LAKANA_CA_BUNDLE` at them. Here a TLS index with a certificate of a private authority runs
in a container of its own, on a labelled network, as ``index.lakana.test:8443``; the egress
service is attached to that network and may connect to it (`start_egress`'s ``reachable``,
since the proxy otherwise refuses an address on a network it is attached to). A build that
trusts the bundle installs from it; one that does not fails with uv's certificate error, which
the worker's hint recognises. A task trusts the bundle too, and cannot write to it.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess  # nosec B404 -- runs uv to lock a test project, fixed argv
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import index_fixture
import pytest
from docker_stack import (
    APP,
    STUB_HARNESS,
    Stack,
    build_task_image,
    docker_client,
    make_wheel,
    stack,
    start_egress,
)
from tls_fixture import issue_leaf, make_authority

from lakana_worker.trust import build_trust_bundle, certificate_hint

pytestmark = [pytest.mark.integration, pytest.mark.docker]

_HOST = "index.lakana.test"
_PORT = 8443
_INDEX_ENTRY = f"""import http.server, ssl, sys
sys.path.insert(0, {APP!r})
from index_fixture import _Handler, _Server
server = _Server.__new__(_Server)
http.server.ThreadingHTTPServer.__init__(server, ("0.0.0.0", {_PORT}), _Handler)
server.credentials, server.refusal, server.challenge_everything = {{}}, 401, False
server.redirect_files_to, server.seen = None, []
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain({APP!r} + "/leaf.pem", {APP!r} + "/leaf.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
"""
"""Serves `index_fixture`'s handler with TLS on every address of the index container."""

_PROBE = r"""
import errno, json, os, socket, ssl, threading
server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
server_context.load_cert_chain("/code/leaf.pem", "/code/leaf.key")
listener = socket.create_server(("127.0.0.1", 0))
port = listener.getsockname()[1]
def serve():
    connection, _ = listener.accept()
    try:
        with server_context.wrap_socket(connection, server_side=True) as tls:
            tls.recv(1)
    except (OSError, ssl.SSLError):
        pass
threading.Thread(target=serve, daemon=True).start()
try:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname="127.0.0.1"):
            pass
    tls = "ok"
except ssl.SSLError as exc:
    tls = str(exc)[:200]
try:
    open("/trust/trust.pem", "ab").close()
    append = "written"
except OSError as exc:
    append = errno.errorcode.get(exc.errno, str(exc.errno))
print(json.dumps({
    "tls": tls,
    "ssl_cert_file": os.environ.get("SSL_CERT_FILE"),
    "lakana_ca_bundle": os.environ.get("LAKANA_CA_BUNDLE"),
    "append": append,
    "listing": sorted(os.listdir("/trust")) if os.path.isdir("/trust") else None,
}))
"""
"""The task: a TLS server of the private authority on the container's own loopback, and a
client with Python's default trust; then the trust mount's write refusal and its listing."""


@dataclass
class Trusted:
    """The module's stack, the index project and the bundle.

    Parameters
    ----------
    stack
        The stack, with the index container and the egress service running.
    project
        The index project's files for an attempt's ``code``.
    bundle
        What the worker would build from the private authority's certificate.
    leaf
        A leaf certificate and key of the private authority for ``127.0.0.1`` (test material
        of the task probe, never shipped).
    """

    stack: Stack
    project: dict[str, bytes]
    bundle: bytes
    leaf: dict[str, bytes]


def _files(entries: dict[str, bytes]) -> bytes:
    """Pack files for the index container, readable by its user only.

    Parameters
    ----------
    entries
        Name to content.

    Returns
    -------
    bytes
        The archive.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid = len(data), 0o400, 10001, 10001
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _project(directory: Path) -> dict[str, bytes]:
    """Write and lock a project that takes `team-utils` from the private-CA index.

    Locked on the host against `index_fixture`'s credential-free index on loopback (the host
    cannot reach the container's name), then rewritten to ``https://index.lakana.test:8443``;
    the handler serves any path prefix, so the lock's file URLs resolve there too.

    Parameters
    ----------
    directory
        Where to write it.

    Returns
    -------
    dict
        Path in the code directory to content: `pyproject.toml`, `uv.lock` and the stub
        `lakana` wheel.
    """
    uv = shutil.which("uv")
    if uv is None:
        if os.environ.get("LAKANA_TEST_DOCKER") == "1":
            pytest.fail("the trust tests lock a project with uv, which is not on the PATH")
        pytest.skip("uv is not on the PATH")
    wheel_name, wheel = make_wheel(
        "lakana", "0.0.0", {"lakana/__init__.py": "", "lakana/runner.py": STUB_HARNESS}
    )
    with index_fixture.serving() as indexes:
        url = indexes.other.url()
        pyproject = index_fixture.project_files(url)["pyproject.toml"]
        pyproject = pyproject.replace(
            'dependencies = ["team-utils==1.4.0"]', 'dependencies = ["team-utils==1.4.0", "lakana"]'
        ).replace(
            "[tool.uv.sources]\n",
            f'[tool.uv.sources]\nlakana = {{ path = "wheels/{wheel_name}" }}\n',
        )
        (directory / "wheels").mkdir(parents=True)
        (directory / "wheels" / wheel_name).write_bytes(wheel)
        (directory / "pyproject.toml").write_text(pyproject)
        locked = subprocess.run(  # nosec B603 -- uv from the PATH, fixed argv
            [
                uv,
                "lock",
                "--no-config",
                "--no-cache",
                "--python",
                sys.executable,
                "--project",
                str(directory),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if locked.returncode != 0:
            pytest.fail(f"uv could not lock the test project: {locked.stderr}")
        origin = f"http://127.0.0.1:{indexes.other.port}"
    target = f"https://{_HOST}:{_PORT}"
    return {
        "pyproject.toml": pyproject.replace(origin, target).encode(),
        "uv.lock": (directory / "uv.lock").read_text().replace(origin, target).encode(),
        f"wheels/{wheel_name}": wheel,
    }


@pytest.fixture(scope="module")
def trusted(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Trusted]:
    """Start a stack with a private-CA index container and an egress that may reach it.

    Parameters
    ----------
    tmp_path_factory
        For the launcher's state, the authority and the project.

    Yields
    ------
    Trusted
        The stack, the project, the bundle and a loopback leaf.
    """
    material = tmp_path_factory.mktemp("trust")
    project = _project(material / "project")
    authority = make_authority(material)
    (material / "index").mkdir()
    (material / "loopback").mkdir()
    index_cert, index_key = issue_leaf(authority, material / "index", names=(_HOST,))
    loopback_cert, loopback_key = issue_leaf(authority, material / "loopback")
    bundle = build_trust_bundle(authority.ca_file)
    assert bundle is not None
    client = docker_client()
    image = build_task_image(client)
    try:
        with stack(client, image, tmp_path_factory.mktemp("stack")) as current:
            network: Any = client.networks.create(
                f"{current.name}-index", driver="bridge", labels=current.labels
            )
            network.reload()
            subnet = network.attrs["IPAM"]["Config"][0]["Subnet"]
            index = client.containers.create(
                image,
                ["python", "-I", "-c", _INDEX_ENTRY],
                user="10001:10001",
                labels=current.labels,
                network=network.name,
                networking_config={
                    network.name: client.api.create_endpoint_config(aliases=[_HOST])
                },
            )
            index.put_archive(
                APP,
                _files(
                    {
                        "index_fixture.py": Path(index_fixture.__file__).read_bytes(),
                        "leaf.pem": index_cert.read_bytes(),
                        "leaf.key": index_key.read_bytes(),
                    }
                ),
            )
            index.start()
            start_egress(current, network=network.name, reachable=subnet)
            yield Trusted(
                stack=current,
                project=project,
                bundle=bundle,
                leaf={
                    "leaf.pem": loopback_cert.read_bytes(),
                    "leaf.key": loopback_key.read_bytes(),
                },
            )
    finally:
        client.close()


def test_a_build_trusts_a_private_ca_index_with_the_bundle(trusted: Trusted) -> None:
    """With the bundle mounted, uv installs `team-utils` from the private-CA index."""
    running = trusted.stack
    attempt = running.attempt("", files=trusted.project, trust=trusted.bundle)
    result, output = running.build(
        attempt, environment="a1" * 32, no_build=True, targets=((_HOST, _PORT),), trust=True
    )
    assert result["complete"] is True, output.decode(errors="replace")


def test_a_build_without_the_bundle_fails_naming_the_certificate(trusted: Trusted) -> None:
    """Without it uv refuses the certificate, in words the worker's hint recognises.

    Not wheels-only: such a build gets a private cache, so the wheels the trusting build put in
    the shared cache of this index set cannot spare it the connection.
    """
    running = trusted.stack
    attempt = running.attempt("", files=trusted.project, trust=trusted.bundle)
    result, output = running.build(
        attempt, environment="b2" * 32, no_build=False, targets=((_HOST, _PORT),), trust=False
    )
    text = output.decode(errors="replace")
    assert result["complete"] is not True, text
    assert "invalid peer certificate" in text, text
    assert certificate_hint(text) is not None


def _probe(trusted: Trusted, *, trust: bool) -> dict[str, Any]:
    """Run the task probe in an attempt that holds the bundle.

    Parameters
    ----------
    trusted
        The module's stack and material.
    trust
        Whether the run mounts the bundle.

    Returns
    -------
    dict
        The probe's report.
    """
    running = trusted.stack
    attempt = running.attempt(_PROBE, files=trusted.leaf, trust=trusted.bundle)
    _, output = running.run(attempt, trust=trust)
    assert output.strip(), "the probe printed nothing; the task did not run"
    return json.loads(output.strip().splitlines()[-1])


def test_a_task_trusts_the_bundle(trusted: Trusted) -> None:
    """A task's Python trusts the private authority with the mount, and not without it."""
    with_bundle = _probe(trusted, trust=True)
    without = _probe(trusted, trust=False)

    assert with_bundle["tls"] == "ok", with_bundle
    assert with_bundle["ssl_cert_file"] == with_bundle["lakana_ca_bundle"] == "/trust/trust.pem"
    assert "CERTIFICATE_VERIFY_FAILED" in without["tls"], without
    assert without["ssl_cert_file"] is None and without["lakana_ca_bundle"] is None


def test_the_trust_mount_is_read_only(trusted: Trusted) -> None:
    """The task user owns the file, so only the read-only mount refuses the write."""
    seen = _probe(trusted, trust=True)

    assert seen["append"] == "EROFS", seen
    assert seen["listing"] == ["trust.pem"]
