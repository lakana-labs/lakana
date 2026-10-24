"""A build reaches its package index and nothing else, on a real Docker Engine."""

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import Iterator

import pytest
from docker_stack import Stack, build_task_image, docker_client, stack, start_egress

pytestmark = [pytest.mark.integration, pytest.mark.docker]

_PROBE = r"""
import base64, http.client, json, os, socket
from urllib.parse import urlsplit
proxy = urlsplit(os.environ["HTTPS_PROXY"])
credential = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
def tunnel(host, token=None):
    secret = credential if token is None else base64.b64encode(f"x:{token}".encode()).decode()
    connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=15)
    connection.set_tunnel(host, 443, headers={"Proxy-Authorization": "Basic " + secret})
    try:
        connection.connect()
        return "open"
    except OSError as exc:
        return str(exc)[:160]
def direct(host):
    try:
        socket.create_connection((host, 443), timeout=5).close()
        return "open"
    except OSError as exc:
        return type(exc).__name__
print(json.dumps({
    "index": tunnel("pypi.org"),
    "other": tunnel("example.com"),
    "forged": tunnel("pypi.org", proxy.password[:-1] + ("0" if proxy.password[-1] != "0" else "1")),
    "direct": direct("1.1.1.1"),
}))
"""


@pytest.fixture(scope="module")
def running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """Start a stack with an egress service run from this checkout's proxy.

    Parameters
    ----------
    tmp_path_factory
        For the launcher's state.

    Yields
    ------
    Stack
        The stack.
    """
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
    except OSError:
        if os.environ.get("LAKANA_TEST_DOCKER") == "1":
            pytest.fail("this test needs the internet: the index it reaches is pypi.org")
        pytest.skip("no internet here")
    client = docker_client()
    image = build_task_image(client)
    with stack(client, image, tmp_path_factory.mktemp("egress")) as current:
        assert current.launcher is not None
        start_egress(current)
        yield current


def test_a_build_reaches_only_its_index(running: Stack) -> None:
    """Through the proxy: the token's host opens; another host and a forged token are refused."""
    assert running.launcher is not None
    build = uuid.uuid4().hex[:20]
    network, proxy = running.launcher.open_build_network(build, "test", [("pypi.org", 443)], 120)
    try:
        assert proxy is not None
        output = running.client.containers.run(
            running.image,
            ["python", "-c", _PROBE],
            network=network,
            labels=running.labels,
            environment={"HTTPS_PROXY": proxy},
            user="10002:10002",
            remove=True,
        )
    finally:
        running.launcher.close_network(network)

    seen = json.loads(output.decode().strip().splitlines()[-1])
    assert seen["index"] == "open"
    assert "403" in seen["other"]
    assert "407" in seen["forged"]
    assert seen["direct"] != "open"


def test_a_token_is_refused_from_another_network(running: Stack) -> None:
    """A build's token copied into another build's container gets nothing."""
    assert running.launcher is not None
    first, proxy = running.launcher.open_build_network(
        uuid.uuid4().hex[:20], "test", [("pypi.org", 443)], 120
    )
    second, _ = running.launcher.open_build_network(
        uuid.uuid4().hex[:20], "test", [("example.com", 443)], 120
    )
    try:
        assert proxy is not None
        output = running.client.containers.run(
            running.image,
            ["python", "-c", _PROBE],
            network=second,
            labels=running.labels,
            environment={"HTTPS_PROXY": proxy},
            user="10002:10002",
            remove=True,
        )
    finally:
        running.launcher.close_network(first)
        running.launcher.close_network(second)

    seen = json.loads(output.decode().strip().splitlines()[-1])
    assert "403" in seen["index"]
