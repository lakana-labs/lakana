"""The launcher refuses every request shape it does not run, and fixes every field that matters.

The hostile shapes are the trial's sixteen and more: each one would, through a socket proxy,
have reached Docker as a host bind, a privileged container, the host's PID namespace, a device,
an added capability or the host network.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from lakana_worker.launcher.requests import (
    BUILD_DOWNLOADS,
    LABEL_OWNER,
    LABEL_STACK,
    MAX_EGRESS,
    MAX_REQUEST_BYTES,
    ROOTS_REFUSAL,
    BuildRequest,
    CheckRequest,
    CleanupRequest,
    Refused,
    RelayRoute,
    RunRequest,
    StatusRequest,
    StopRequest,
    allowed_roots,
    build_container_spec,
    build_targets,
    cache_volume,
    check_container_spec,
    environment_volume,
    network_spec,
    parse_request,
    probe_container_spec,
    relay_plan,
    run_container_spec,
    uv_index_value,
    volume_spec,
)
from lakana_worker.launcher.settings import LauncherSettings

TOKEN = "t" * 64
CACHE_KEY = b"k" * 32
KEY = "a" * 64
SETTINGS = LauncherSettings(
    stack="lakana",
    images={"3.13": "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.13"},
    work_volume="lakana-task-work",
    socket=Path("/run/lakana-launcher/launcher.sock"),
    ingestion_roots={"landing": "/srv/landing"},
)
RUN: dict[str, Any] = {
    "token": TOKEN,
    "op": "run",
    "task": "0f3c9a1e",
    "owner": "worker-1",
    "python": "3.13",
    "environment": KEY,
    "attempt": "attempt-abc_12",
    "memory_mb": 2048,
    "cpus": 1,
    "pids": 256,
    "timeout_seconds": 600,
    "kill_grace_seconds": 10,
    "stdin": '{"version": 2}',
    "roots": [],
    "trust": False,
    "egress": [],
    "relay": [],
}
BUILD: dict[str, Any] = {
    "token": TOKEN,
    "op": "build",
    "build": "b0f3c9a1e",
    "owner": "worker-1",
    "python": "3.13",
    "environment": KEY,
    "attempt": "attempt-abc_12",
    "indexes": [
        {"url": "https://pkgs.example.org/simple", "username": "ci", "password": "p@ss word"}
    ],
    "operator_index": None,
    "targets": ["pkgs.example.org:443", "pypi.org:443", "files.pythonhosted.org:443"],
    "no_build": True,
    "memory_mb": 2048,
    "cpus": 1,
    "pids": 256,
    "timeout_seconds": 600,
    "max_bytes": 5 * 1024**3,
    "trust": False,
}


def _raw(request: object) -> bytes:
    """Encode a request as the client sends it.

    Parameters
    ----------
    request
        Any JSON value.

    Returns
    -------
    bytes
        One line.
    """
    return json.dumps(request).encode() + b"\n"


def _with(base: dict[str, Any], **changes: object) -> dict[str, Any]:
    """Copy a request with some fields changed.

    Parameters
    ----------
    base
        The valid request.
    **changes
        Fields to set.

    Returns
    -------
    dict
        The changed copy.
    """
    request = copy.deepcopy(base)
    request.update(changes)
    return request


def _without(base: dict[str, Any], key: str) -> dict[str, Any]:
    """Copy a request without one field.

    Parameters
    ----------
    base
        The valid request.
    key
        The field to drop.

    Returns
    -------
    dict
        The copy.
    """
    request = copy.deepcopy(base)
    del request[key]
    return request


HOSTILE: list[Any] = [
    pytest.param(
        _with(RUN, HostConfig={"Privileged": True}), "run takes exactly", id="extra-hostconfig"
    ),
    pytest.param(_with(RUN, binds=["/:/host"]), "run takes exactly", id="extra-binds"),
    pytest.param(_with(RUN, privileged=True), "run takes exactly", id="extra-privileged"),
    pytest.param(_without(RUN, "roots"), "run takes exactly", id="missing-field"),
    pytest.param(_with(RUN, python="3.10"), "python must be one of 3.13", id="image-not-allowed"),
    pytest.param(
        _with(RUN, python="python:3.13-slim"), "python must be one of", id="image-reference"
    ),
    pytest.param(_with(RUN, environment="../../../etc"), "environment:", id="key-traversal"),
    pytest.param(_with(RUN, environment="A" * 64), "environment:", id="key-upper-case"),
    pytest.param(_with(RUN, attempt="../attempt-x"), "attempt:", id="attempt-traversal"),
    pytest.param(_with(RUN, attempt="attempt-x/../.."), "attempt:", id="attempt-inner-dots"),
    pytest.param(_with(RUN, attempt="attempt-"), "attempt:", id="attempt-empty"),
    pytest.param(_with(RUN, task="x&HostConfig.Privileged=1"), "task:", id="task-query"),
    pytest.param(_with(RUN, task="../x"), "task:", id="task-traversal"),
    pytest.param(_with(RUN, task="X"), "task:", id="task-upper-case"),
    pytest.param(_with(RUN, task="a" * 64), "task:", id="task-too-long"),
    pytest.param(_with(RUN, owner="worker 1"), "owner:", id="owner-space"),
    pytest.param(_with(RUN, owner="w/1"), "owner:", id="owner-slash"),
    pytest.param(
        _with(RUN, memory_mb=100_000), "memory_mb: an integer from 64 to", id="memory-ceiling"
    ),
    pytest.param(_with(RUN, memory_mb=True), "memory_mb:", id="memory-bool"),
    pytest.param(_with(RUN, memory_mb=1.5), "memory_mb:", id="memory-float"),
    pytest.param(_with(RUN, memory_mb=0), "memory_mb:", id="memory-zero"),
    pytest.param(_with(RUN, cpus="2"), "cpus:", id="cpus-text"),
    pytest.param(_with(RUN, cpus=0), "cpus:", id="cpus-zero"),
    pytest.param(_with(RUN, cpus=65), "cpus:", id="cpus-ceiling"),
    pytest.param(_with(RUN, pids=0), "pids:", id="pids-unlimited"),
    pytest.param(_with(RUN, pids=-1), "pids:", id="pids-negative"),
    pytest.param(_with(RUN, timeout_seconds=0), "timeout_seconds:", id="timeout-zero"),
    pytest.param(_with(RUN, kill_grace_seconds=301), "kill_grace_seconds:", id="grace-ceiling"),
    pytest.param(_with(RUN, stdin="x" * (256 * 1024 + 1)), "stdin:", id="stdin-too-long"),
    pytest.param(_with(RUN, stdin={"a": 1}), "stdin:", id="stdin-not-text"),
    pytest.param(_with(RUN, roots=["/"]), "roots:", id="root-a-path"),
    pytest.param(_with(RUN, roots=["landing", "landing"]), "roots:", id="root-twice"),
    pytest.param(_with(RUN, roots=[["landing"]]), "roots:", id="root-not-text"),
    pytest.param(_with(RUN, roots="landing"), "roots:", id="roots-not-a-list"),
    pytest.param(_with(RUN, pid="host"), "run takes exactly", id="extra-pid-host"),
    pytest.param(_with(RUN, PidMode="host"), "run takes exactly", id="extra-pidmode-host"),
    pytest.param(_with(RUN, devices=["/dev/sda"]), "run takes exactly", id="extra-device"),
    pytest.param(
        _with(RUN, Devices=[{"PathOnHost": "/dev/mem"}]), "run takes exactly", id="extra-Devices"
    ),
    pytest.param(_with(RUN, cap_add=["SYS_ADMIN"]), "run takes exactly", id="extra-cap-add"),
    pytest.param(_with(RUN, CapAdd=["ALL"]), "run takes exactly", id="extra-CapAdd"),
    pytest.param(_with(RUN, network="host"), "run takes exactly", id="extra-network-host"),
    pytest.param(
        _with(RUN, network_mode="host"), "run takes exactly", id="extra-network-mode-host"
    ),
    pytest.param(_with(RUN, image="alpine"), "run takes exactly", id="extra-image"),
    pytest.param(_with(BUILD, pid="host"), "build takes exactly", id="build-extra-pid-host"),
    pytest.param(_with(BUILD, binds=["/:/host"]), "build takes exactly", id="build-extra-binds"),
    pytest.param(
        _with(BUILD, cap_add=["SYS_ADMIN"]), "build takes exactly", id="build-extra-cap-add"
    ),
    pytest.param(
        _with(BUILD, network="host"), "build takes exactly", id="build-extra-network-host"
    ),
    pytest.param(_without(BUILD, "targets"), "build takes exactly", id="build-missing-field"),
    pytest.param(
        {"op": "status", "token": TOKEN, "python": "3.13", "environment": KEY, "pid": "host"},
        "status takes exactly",
        id="status-extra",
    ),
    pytest.param(
        {"op": "status", "token": TOKEN, "python": "3.13"},
        "status takes exactly",
        id="status-missing",
    ),
    pytest.param(
        {"op": "status", "token": TOKEN, "python": "3.10", "environment": KEY},
        "python must be one of",
        id="status-python",
    ),
    pytest.param(
        {"op": "cleanup", "token": TOKEN, "owner": "w", "all": True},
        "cleanup takes exactly",
        id="cleanup-extra",
    ),
    pytest.param({"op": "cleanup", "token": TOKEN}, "cleanup takes exactly", id="cleanup-missing"),
    pytest.param(
        {"op": "cleanup", "token": TOKEN, "owner": "w; rm -rf /"},
        "owner:",
        id="cleanup-owner-shell",
    ),
    pytest.param({"op": "stop", "token": TOKEN}, "stop takes exactly", id="stop-missing"),
    pytest.param(
        {"op": "stop", "token": TOKEN, "id": "a"}, "stop takes exactly", id="stop-without-owner"
    ),
    pytest.param(
        {"op": "stop", "token": TOKEN, "id": "a", "owner": "w; rm"}, "owner:", id="stop-owner-shell"
    ),
    pytest.param(
        _with(RUN, Mounts=[{"Type": "bind", "Source": "/"}]), "run takes exactly", id="extra-Mounts"
    ),
    pytest.param(
        _with(RUN, SecurityOpt=["seccomp=unconfined"]), "run takes exactly", id="extra-SecurityOpt"
    ),
    pytest.param(
        _with(BUILD, Mounts=[{"Type": "bind", "Source": "/"}]),
        "build takes exactly",
        id="build-extra-Mounts",
    ),
    pytest.param(
        _with(BUILD, SecurityOpt=["apparmor=unconfined"]),
        "build takes exactly",
        id="build-extra-SecurityOpt",
    ),
    pytest.param(
        _with(RUN, cpus=1e-10),
        "cpus: a number from 0.01 to 8 .LAKANA_LAUNCHER__MAX_CPUS",
        id="cpus-tiny",
    ),
    pytest.param(_with(RUN, cpus=5e-324), "cpus:", id="cpus-denormal"),
    pytest.param(_with(RUN, cpus=0.001), "cpus:", id="cpus-below-floor"),
    pytest.param(_with(RUN, cpus=-1), "cpus:", id="cpus-negative"),
    pytest.param(_with(RUN, cpus=-0.5), "cpus:", id="cpus-negative-float"),
    pytest.param(_with(RUN, cpus=64.5), "LAKANA_LAUNCHER__MAX_CPUS", id="cpus-over-ceiling"),
    pytest.param(
        _with(RUN, memory_mb=10**9), "LAKANA_LAUNCHER__MAX_MEMORY_MB", id="memory-names-setting"
    ),
    pytest.param(_with(RUN, pids=10**9), "LAKANA_LAUNCHER__MAX_PIDS", id="pids-names-setting"),
    pytest.param(
        _with(RUN, token="\ud800"), "does not carry the launcher's token", id="token-surrogate"
    ),
    pytest.param(_with(RUN, stdin="a\ud800"), "stdin:", id="stdin-surrogate"),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://pkgs.example.org/simple", "username": "", "password": "a\ud800"}
            ],
        ),
        "indexes:",
        id="password-surrogate",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://pkgs.example.org/simple", "username": "\udfff", "password": "p"}
            ],
        ),
        "indexes:",
        id="username-surrogate",
    ),
    pytest.param(_with(RUN, owner="w\ud800"), "owner:", id="owner-surrogate"),
    pytest.param(
        _with(
            BUILD,
            operator_index="https://op:pw@pkgs.example.org\\@evil.org/",
            targets=["pkgs.example.org:443", "evil.org:443"],
        ),
        "operator_index:",
        id="operator-backslash",
    ),
    pytest.param(
        _with(BUILD, targets=["127.0.0.1:443"], indexes=[]), "targets:", id="host-loopback"
    ),
    pytest.param(
        _with(BUILD, targets=["169.254.169.254:443"], indexes=[]), "targets:", id="host-metadata"
    ),
    pytest.param(
        _with(BUILD, targets=["10.1.2.3:443"], indexes=[]), "targets:", id="host-rfc1918-10"
    ),
    pytest.param(
        _with(BUILD, targets=["172.16.0.1:443"], indexes=[]), "targets:", id="host-rfc1918-172"
    ),
    pytest.param(
        _with(BUILD, targets=["192.168.0.1:443"], indexes=[]), "targets:", id="host-rfc1918-192"
    ),
    pytest.param(
        _with(BUILD, targets=["0.0.0.0:443"], indexes=[]), "targets:", id="host-unspecified"
    ),
    pytest.param(
        _with(BUILD, targets=["::1:443"], indexes=[]), "targets:", id="host-ipv6-loopback"
    ),
    pytest.param(_with(BUILD, targets=["fd00::1:443"], indexes=[]), "targets:", id="host-ipv6-ula"),
    pytest.param(
        _with(BUILD, targets=["fe80::1:443"], indexes=[]), "targets:", id="host-ipv6-link-local"
    ),
    pytest.param(
        _with(BUILD, targets=[":::443"], indexes=[]), "targets:", id="host-ipv6-unspecified"
    ),
    pytest.param(_with(BUILD, targets=["127.1:443"], indexes=[]), "targets:", id="host-short-ipv4"),
    pytest.param(
        _with(BUILD, targets=["127.0.0.0x1:443"], indexes=[]), "targets:", id="host-mixed-hex"
    ),
    pytest.param(
        _with(BUILD, targets=["0.0x0.0.0:443"], indexes=[]), "targets:", id="host-hex-label"
    ),
    pytest.param(
        _with(BUILD, targets=["100.64.0.1:443"], indexes=[]), "targets:", id="host-shared-space"
    ),
    pytest.param(
        _with(BUILD, targets=["224.0.0.1:443"], indexes=[]), "targets:", id="host-multicast"
    ),
    pytest.param(_with(RUN, op="exec"), "op must be one of", id="op-exec"),
    pytest.param(
        _with(RUN, token="x" * 64), "does not carry the launcher's token", id="wrong-token"
    ),
    pytest.param(_without(RUN, "token"), "does not carry the launcher's token", id="no-token"),
    pytest.param([1, 2], "one JSON object", id="a-list"),
    pytest.param("run", "one JSON object", id="a-string"),
    pytest.param(
        {"op": "stop", "token": TOKEN, "id": "../lakana-server", "owner": "w1"},
        "id:",
        id="stop-traversal",
    ),
    pytest.param(
        {"op": "stop", "token": TOKEN, "id": "a", "owner": "w1", "force": True},
        "stop takes exactly",
        id="stop-extra",
    ),
    pytest.param({"op": "cleanup", "token": TOKEN, "owner": ""}, "owner:", id="cleanup-empty"),
    pytest.param(
        {"op": "status", "token": TOKEN, "python": "3.13", "environment": "x"},
        "environment:",
        id="status-key",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[{"url": "http://pkgs.example.org/simple", "username": "", "password": "p"}],
        ),
        "indexes:",
        id="index-http",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://u:p@pkgs.example.org/simple", "username": "", "password": "p"}
            ],
        ),
        "indexes:",
        id="index-userinfo",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://pkgs.example.org/simple?x=1", "username": "", "password": "p"}
            ],
        ),
        "indexes:",
        id="index-query",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[{"url": "https://other.example.org/simple", "username": "", "password": "p"}],
        ),
        "every index's host and port must be in targets",
        id="index-host-not-allowed",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[{"url": "https://pkgs.example.org/simple", "username": "", "password": ""}],
        ),
        "indexes:",
        id="index-empty-password",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {"url": "https://pkgs.example.org/simple", "username": "", "password": "a\x00b"}
            ],
        ),
        "indexes:",
        id="index-nul",
    ),
    pytest.param(
        _with(
            BUILD,
            indexes=[
                {
                    "url": "https://pkgs.example.org/simple",
                    "username": "",
                    "password": "p",
                    "env": "LD_PRELOAD=x",
                }
            ],
        ),
        "indexes:",
        id="index-extra-key",
    ),
    pytest.param(
        _with(BUILD, indexes=[BUILD["indexes"][0]] * 17), "indexes:", id="too-many-indexes"
    ),
    pytest.param(_with(BUILD, targets=["PKGS.example.org:443"]), "targets:", id="host-upper-case"),
    pytest.param(_with(BUILD, targets=["pkgs.example.org"]), "targets:", id="target-without-port"),
    pytest.param(_with(BUILD, targets=[]), "targets:", id="no-target"),
    pytest.param(
        _with(BUILD, indexes=[], targets=[f"h{n}.example.org:443" for n in range(33)]),
        "targets:",
        id="too-many-targets",
    ),
    pytest.param(
        _with(BUILD, operator_index="https://in dex/simple"), "operator_index:", id="operator-space"
    ),
    pytest.param(
        _with(BUILD, operator_index="file:///etc/passwd"), "operator_index:", id="operator-file"
    ),
    pytest.param(
        _with(BUILD, operator_index="https://pkgs.example.org:99999/simple"),
        "operator_index:",
        id="operator-bad-port",
    ),
    pytest.param(_with(BUILD, no_build="yes"), "no_build:", id="no-build-text"),
    pytest.param(_with(BUILD, max_bytes=0), "max_bytes:", id="max-bytes-zero"),
]


@pytest.mark.parametrize(("request_", "reason"), HOSTILE)
def test_every_hostile_request_shape_is_refused(request_: object, reason: str) -> None:
    """Nothing outside the schema reaches a container spec.

    Parameters
    ----------
    request_
        The request as the client would send it.
    reason
        Part of the refusal's text.
    """
    with pytest.raises(Refused, match=reason):
        parse_request(_raw(request_), token=TOKEN, settings=SETTINGS)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        pytest.param(b"x" * (MAX_REQUEST_BYTES + 1), "512 KiB", id="too-large"),
        pytest.param(_raw({**RUN, "cpus": "@@"}).replace(b'"@@"', b"NaN"), "without NaN", id="nan"),
        pytest.param(
            _raw({**RUN, "cpus": "@@"}).replace(b'"@@"', b"Infinity"),
            "without Infinity",
            id="infinity",
        ),
        pytest.param(b"[" * 100_000, "one JSON object", id="deep"),
        pytest.param(b"\xff\xfe", "one JSON object", id="not-utf8"),
        pytest.param(b"", "one JSON object", id="empty"),
    ],
)
def test_a_request_that_is_not_one_bounded_json_object_is_refused(raw: bytes, reason: str) -> None:
    """Size, constants, depth and encoding are refused before any field is read.

    The NaN and Infinity cases carry a valid token, so only the constant can refuse them.

    Parameters
    ----------
    raw
        The bytes on the socket.
    reason
        Part of the refusal's text.
    """
    with pytest.raises(Refused, match=reason):
        parse_request(raw, token=TOKEN, settings=SETTINGS)


def test_the_valid_requests_parse_into_their_plain_values() -> None:
    """The five operations, as the worker sends them."""
    run = parse_request(_raw(RUN), token=TOKEN, settings=SETTINGS)
    build = parse_request(_raw(BUILD), token=TOKEN, settings=SETTINGS)
    stop = parse_request(
        _raw({"token": TOKEN, "op": "stop", "id": "0f3c9a1e", "owner": "worker-1"}),
        token=TOKEN,
        settings=SETTINGS,
    )
    status = parse_request(
        _raw({"token": TOKEN, "op": "status", "python": "3.13", "environment": KEY}),
        token=TOKEN,
        settings=SETTINGS,
    )
    cleanup = parse_request(
        _raw({"token": TOKEN, "op": "cleanup", "owner": "worker-1"}), token=TOKEN, settings=SETTINGS
    )

    assert (
        isinstance(run, RunRequest)
        and run.stdin == b'{"version": 2}'
        and run.limits.cpus == pytest.approx(1.0)
    )
    assert isinstance(build, BuildRequest) and build.indexes[0].host == "pkgs.example.org"
    assert "p@ss word" not in repr(build)
    assert isinstance(stop, StopRequest) and (stop.id, stop.owner) == ("0f3c9a1e", "worker-1")
    assert isinstance(status, StatusRequest) and status.environment == KEY
    assert isinstance(cleanup, CleanupRequest) and cleanup.owner == "worker-1"


def test_the_run_spec_fixes_every_field_that_matters() -> None:
    """User, capabilities, mounts, network, command and image come from the launcher alone."""
    run = parse_request(_raw(_with(RUN, roots=["landing"])), token=TOKEN, settings=SETTINGS)
    assert isinstance(run, RunRequest)

    spec = run_container_spec(run, SETTINGS, no_new_privileges=True)

    host = spec["HostConfig"]
    assert spec["Image"] == "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.13"
    assert spec["Entrypoint"] == ["/env/bin/python", "-P", "-m", "lakana.runner"]
    assert spec["User"] == "10001:10003"
    assert spec["WorkingDir"] == "/code"
    assert (host["CapDrop"], host["CapAdd"], host["Privileged"]) == (["ALL"], [], False)
    assert (host["ReadonlyRootfs"], host["Init"], host["IpcMode"], host["PidMode"]) == (
        True,
        True,
        "private",
        "",
    )
    assert host["NetworkMode"] == "lakana-task-0f3c9a1e"
    assert (host["Binds"], host["Devices"]) == ([], [])
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert (host["Memory"], host["MemorySwap"]) == (2048 * 2**20, 2048 * 2**20)
    assert (host["NanoCpus"], host["PidsLimit"]) == (1_000_000_000, 256)
    assert [(m["Type"], m["Source"], m["Target"], m["ReadOnly"]) for m in host["Mounts"]] == [
        ("volume", "lakana-task-work", "/code", True),
        ("volume", environment_volume(SETTINGS, "3.13", KEY), "/env", True),
        ("volume", "lakana-task-work", "/work", False),
        ("bind", "/srv/landing", "/ingest/landing", True),
    ]
    assert [m.get("VolumeOptions", {}).get("Subpath") for m in host["Mounts"]] == [
        "attempt-abc_12/code",
        None,
        "attempt-abc_12/task",
        None,
    ]
    assert not any("token" in variable.lower() for variable in spec["Env"])
    assert spec["Labels"][LABEL_STACK] == "lakana" and spec["Labels"][LABEL_OWNER] == "worker-1"
    assert "POLARS_OVERRIDE_TOTAL_MEMORY_MB=2147" in spec["Env"]
    assert (
        run_container_spec(run, SETTINGS, no_new_privileges=False)["HostConfig"]["SecurityOpt"]
        == []
    )


def test_the_build_spec_mounts_only_code_environment_and_cache() -> None:
    """The build writes its own volume and the cache; the credential is an environment value."""
    build = parse_request(_raw(BUILD), token=TOKEN, settings=SETTINGS)
    assert isinstance(build, BuildRequest)

    spec = build_container_spec(
        build, SETTINGS, no_new_privileges=False, proxy_url=None, cache_key=CACHE_KEY
    )

    host = spec["HostConfig"]
    assert spec["User"] == "10002:10002"
    assert spec["Entrypoint"] == ["/usr/local/bin/python", "-I", "/opt/lakana-task/build.py"]
    assert (host["CapDrop"], host["CapAdd"], host["Privileged"]) == (["ALL"], [], False)
    assert (host["ReadonlyRootfs"], host["PidMode"], host["IpcMode"]) == (True, "", "private")
    assert (host["Binds"], host["Devices"]) == ([], [])
    assert [(m["Source"], m["Target"], m["ReadOnly"]) for m in host["Mounts"]] == [
        ("lakana-task-work", f"/src/{build.build}", True),
        (environment_volume(SETTINGS, "3.13", KEY), "/env", False),
        (cache_volume(SETTINGS, build, key=CACHE_KEY), "/uv-cache", False),
    ]
    assert (
        "UV_INDEX=lakana-registered-0=https://ci:p%40ss%20word@pkgs.example.org/simple"
        in spec["Env"]
    )
    assert "LAKANA_BUILD_NO_BUILD=1" in spec["Env"]
    assert f"UV_CONCURRENT_DOWNLOADS={BUILD_DOWNLOADS}" in spec["Env"]
    assert BUILD_DOWNLOADS * 2 <= 32, "#302: uv's downloads fit well within one build's cap"
    assert not any(v.startswith(("HTTPS_PROXY=", "https_proxy=")) for v in spec["Env"])
    assert host["NetworkMode"] == "lakana-build-b0f3c9a1e"
    with_proxy = build_container_spec(
        build,
        SETTINGS,
        no_new_privileges=False,
        proxy_url="http://lakana:tok@egress:3128",
        cache_key=CACHE_KEY,
    )
    assert "HTTPS_PROXY=http://lakana:tok@egress:3128" in with_proxy["Env"]


def test_a_task_network_is_internal_and_has_no_gateway_on_the_host() -> None:
    """The trial's finding: `internal` alone still reaches the host through the gateway."""
    spec = network_spec(SETTINGS, "task", "0f3c9a1e", "worker-1")

    assert spec["Name"] == "lakana-task-0f3c9a1e"
    assert spec["Internal"] is True
    assert spec["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated"
    assert spec["Options"]["com.docker.network.bridge.gateway_mode_ipv6"] == "isolated"


def test_volume_names_change_with_the_image_and_the_index_set() -> None:
    """A venv links into its image, and a cache serves one set of indexes."""
    build = parse_request(_raw(BUILD), token=TOKEN, settings=SETTINGS)
    other = parse_request(
        _raw(
            _with(
                BUILD,
                targets=[
                    "pkgs.example.org:443",
                    "pypi.org:443",
                    "files.pythonhosted.org:443",
                    "x.org:443",
                ],
            )
        ),
        token=TOKEN,
        settings=SETTINGS,
    )
    newer = LauncherSettings(
        stack="lakana",
        images={"3.13": "ghcr.io/lakana-labs/lakana-task:0.15.0-py3.13"},
        work_volume="lakana-task-work",
    )
    assert isinstance(build, BuildRequest) and isinstance(other, BuildRequest)

    assert environment_volume(SETTINGS, "3.13", KEY) != environment_volume(newer, "3.13", KEY)
    assert environment_volume(SETTINGS, "3.13", KEY).startswith("lakana-env-")
    assert cache_volume(SETTINGS, build, key=CACHE_KEY) != cache_volume(
        SETTINGS, other, key=CACHE_KEY
    )


def _build_request(**changes: object) -> BuildRequest:
    """Parse a build request with some fields changed.

    Parameters
    ----------
    **changes
        Fields to replace.

    Returns
    -------
    BuildRequest
        The request.
    """
    request = parse_request(_raw(_with(BUILD, **changes)), token=TOKEN, settings=SETTINGS)
    assert isinstance(request, BuildRequest)
    return request


def test_a_wheels_only_build_shares_the_cache_of_its_index_set_across_environments() -> None:
    """With `--no-build` no package code runs, so the cache may be shared."""
    first = _build_request()
    other_environment = _build_request(environment="b" * 64)

    assert cache_volume(SETTINGS, first, key=CACHE_KEY) == cache_volume(
        SETTINGS, other_environment, key=CACHE_KEY
    )
    assert "private" not in cache_volume(SETTINGS, first, key=CACHE_KEY)


def test_a_build_that_may_run_package_code_never_mounts_the_shared_cache() -> None:
    """An sdist's or a path package's code runs with the cache writable: it gets its own."""
    shared = cache_volume(SETTINGS, _build_request(), key=CACHE_KEY)
    source = _build_request(no_build=False)
    other = _build_request(no_build=False, environment="b" * 64)

    spec = build_container_spec(
        source, SETTINGS, no_new_privileges=False, proxy_url=None, cache_key=CACHE_KEY
    )
    cache_mounts = [m for m in spec["HostConfig"]["Mounts"] if m["Target"] == "/uv-cache"]

    assert [m["Source"] for m in cache_mounts] == [cache_volume(SETTINGS, source, key=CACHE_KEY)]
    assert cache_mounts[0]["Source"] != shared
    assert "uvcache-private-" in cache_mounts[0]["Source"]
    assert cache_volume(SETTINGS, source, key=CACHE_KEY) != cache_volume(
        SETTINGS, other, key=CACHE_KEY
    )


def test_the_cache_key_holds_who_the_credential_belongs_to() -> None:
    """Two teams on one index URL do not share entries."""
    team_a = {"url": "https://pkgs.example.org/simple", "username": "team-a", "password": "x"}
    team_b = {"url": "https://pkgs.example.org/simple", "username": "team-b", "password": "x"}
    rotated = {"url": "https://pkgs.example.org/simple", "username": "team-a", "password": "y"}

    a = cache_volume(SETTINGS, _build_request(indexes=[team_a]), key=CACHE_KEY)
    b = cache_volume(SETTINGS, _build_request(indexes=[team_b]), key=CACHE_KEY)
    again = cache_volume(SETTINGS, _build_request(indexes=[rotated]), key=CACHE_KEY)

    assert a != b and a != again


def test_each_build_mounts_its_code_at_a_path_of_its_own() -> None:
    """A cached local wheel is keyed by its absolute `file://` URL: no two builds share one."""
    first = _build_request(build="b-one", attempt="attempt-aaa")
    second = _build_request(build="b-two", attempt="attempt-bbb")

    specs = [
        build_container_spec(
            r, SETTINGS, no_new_privileges=False, proxy_url=None, cache_key=CACHE_KEY
        )
        for r in (first, second)
    ]
    mounts = [
        next(m for m in spec["HostConfig"]["Mounts"] if m["Target"].startswith("/src"))
        for spec in specs
    ]

    assert [m["Target"] for m in mounts] == ["/src/b-one", "/src/b-two"]
    assert [spec["WorkingDir"] for spec in specs] == ["/src/b-one", "/src/b-two"]
    assert specs[0]["Env"].count("LAKANA_BUILD_SRC=/src/b-one") == 1


def test_the_cache_key_separates_tokens_that_share_a_user_name() -> None:
    """Teams on one index usually use one user name with different tokens."""
    one = {"url": "https://pkgs.example.org/simple", "username": "__token__", "password": "t1"}
    two = {"url": "https://pkgs.example.org/simple", "username": "__token__", "password": "t2"}

    a = cache_volume(SETTINGS, _build_request(indexes=[one]), key=CACHE_KEY)
    b = cache_volume(SETTINGS, _build_request(indexes=[two]), key=CACHE_KEY)
    again = cache_volume(SETTINGS, _build_request(indexes=[one]), key=CACHE_KEY)
    other_key = cache_volume(SETTINGS, _build_request(indexes=[one]), key=b"z" * 32)

    assert a != b and a == again and a != other_key
    assert "t1" not in a


def test_the_cache_key_separates_operator_index_tokens() -> None:
    """The operator's own credential counts as well."""
    targets = [*BUILD["targets"], "mirror.example.org:443"]
    one = _build_request(operator_index="https://u:one@mirror.example.org/simple", targets=targets)
    two = _build_request(operator_index="https://u:two@mirror.example.org/simple", targets=targets)

    assert cache_volume(SETTINGS, one, key=CACHE_KEY) != cache_volume(SETTINGS, two, key=CACHE_KEY)


def test_the_cache_key_holds_the_operator_index_user() -> None:
    """The operator's own credential is part of the identity too."""
    targets = [*BUILD["targets"], "mirror.example.org:443"]
    one = _build_request(operator_index="https://u1:p@mirror.example.org/simple", targets=targets)
    two = _build_request(operator_index="https://u2:p@mirror.example.org/simple", targets=targets)

    assert cache_volume(SETTINGS, one, key=CACHE_KEY) != cache_volume(SETTINGS, two, key=CACHE_KEY)


def test_environment_and_cache_volumes_are_created_with_labels() -> None:
    """An uninstall or an incident response finds them by the stack label."""
    spec = volume_spec(SETTINGS, "lakana-uvcache-x", "cache", shared=True)
    timed = volume_spec(SETTINGS, "p", "cache", shared=False, deadline=55)
    assert timed["Labels"]["lakana.launcher.deadline"] == "55"

    assert spec == {
        "Name": "lakana-uvcache-x",
        "Labels": {
            "lakana.launcher.stack": "lakana",
            "lakana.launcher.kind": "cache",
            "lakana.launcher.shared": "1",
        },
    }
    assert (
        volume_spec(SETTINGS, "e", "env", shared=False)["Labels"]["lakana.launcher.shared"] == "0"
    )


def test_a_network_gets_its_allocated_subnet_and_a_deadline() -> None:
    """A /29 from the launcher's range, so the host's address pools are not used up."""
    spec = network_spec(SETTINGS, "task", "t1", "w1", subnet="10.207.0.8/29", deadline=1234)

    assert spec["IPAM"] == {"Driver": "default", "Config": [{"Subnet": "10.207.0.8/29"}]}
    assert spec["Labels"]["lakana.launcher.deadline"] == "1234"
    assert "IPAM" not in network_spec(SETTINGS, "probe", "x", "launcher")


def test_containers_carry_their_deadline() -> None:
    """The reaper removes what outlives it, with or without a running launcher to ask."""
    run = parse_request(_raw(RUN), token=TOKEN, settings=SETTINGS)
    build = _build_request()
    assert isinstance(run, RunRequest)

    task = run_container_spec(run, SETTINGS, no_new_privileges=False, deadline=99)
    built = build_container_spec(
        build, SETTINGS, no_new_privileges=False, proxy_url=None, deadline=77, cache_key=CACHE_KEY
    )

    assert task["Labels"]["lakana.launcher.deadline"] == "99"
    assert built["Labels"]["lakana.launcher.deadline"] == "77"
    assert (
        "lakana.launcher.deadline"
        not in run_container_spec(run, SETTINGS, no_new_privileges=False)["Labels"]
    )


def test_the_control_probe_has_no_option_and_its_own_name() -> None:
    """It tells a Docker that refuses the option from one that cannot start the image."""
    control = probe_container_spec(SETTINGS, "img", no_new_privileges=False)

    assert control["HostConfig"]["SecurityOpt"] == []
    assert control["Labels"]["lakana.launcher.id"] == "no-new-privileges-control"
    assert probe_container_spec(SETTINGS, "img")["HostConfig"]["SecurityOpt"] == [
        "no-new-privileges"
    ]


def test_the_cache_volume_depends_on_the_operator_index_host_and_keeps_the_password_out() -> None:
    """Another host is another cache, and the password is not in the name."""
    targets = [*BUILD["targets"], "mirror.example.org:443", "other.example.org:443"]
    first = parse_request(
        _raw(
            _with(BUILD, operator_index="https://u:one@mirror.example.org/simple", targets=targets)
        ),
        token=TOKEN,
        settings=SETTINGS,
    )
    second = parse_request(
        _raw(
            _with(BUILD, operator_index="https://u:two@mirror.example.org/simple", targets=targets)
        ),
        token=TOKEN,
        settings=SETTINGS,
    )
    other = parse_request(
        _raw(
            _with(BUILD, operator_index="https://u:one@other.example.org/simple", targets=targets)
        ),
        token=TOKEN,
        settings=SETTINGS,
    )
    assert isinstance(first, BuildRequest) and isinstance(second, BuildRequest)
    assert isinstance(other, BuildRequest)

    names = {cache_volume(SETTINGS, r, key=CACHE_KEY) for r in (first, second, other)}
    assert len(names) == 3


def test_the_operator_index_comes_first_and_keeps_its_own_credential() -> None:
    """`LAKANA_RUN__INDEX_URL` may carry a password; it is passed as the operator wrote it."""
    build = parse_request(
        _raw(
            _with(
                BUILD,
                operator_index="https://op:pw@mirror.example.org/simple",
                targets=["pkgs.example.org:443", "mirror.example.org:443"],
            )
        ),
        token=TOKEN,
        settings=SETTINGS,
    )
    assert isinstance(build, BuildRequest)

    assert uv_index_value(build) == (
        "https://op:pw@mirror.example.org/simple "
        "lakana-registered-0=https://ci:p%40ss%20word@pkgs.example.org/simple"
    )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        pytest.param(
            b'{"op": "stop", "op": "run", "token": "' + TOKEN.encode() + b'", "id": "a"}',
            "repeats the field",
            id="repeated-op",
        ),
        pytest.param(
            b'{"op": "stop", "token": "' + TOKEN.encode() + b'", "id": "a", "id": "b"}',
            "repeats the field",
            id="repeated-id",
        ),
    ],
)
def test_a_repeated_json_key_is_refused(raw: bytes, reason: str) -> None:
    """The last of two equal keys must not silently win.

    Parameters
    ----------
    raw
        The bytes on the socket.
    reason
        Part of the refusal's text.
    """
    with pytest.raises(Refused, match=reason):
        parse_request(raw, token=TOKEN, settings=SETTINGS)


def test_public_ip_literals_and_host_names_are_allowed_hosts() -> None:
    """Only loopback, link-local, private and unspecified literals are refused."""
    build = parse_request(
        _raw(_with(BUILD, targets=["8.8.8.8:443", "pkgs.example.org:443"])),
        token=TOKEN,
        settings=SETTINGS,
    )
    assert isinstance(build, BuildRequest)


def test_the_smallest_cpu_share_is_a_real_limit() -> None:
    """A share Docker would read as no limit never reaches a spec."""
    run = parse_request(_raw(_with(RUN, cpus=0.01)), token=TOKEN, settings=SETTINGS)
    assert isinstance(run, RunRequest)

    assert run_container_spec(run, SETTINGS, no_new_privileges=True)["HostConfig"]["NanoCpus"] == (
        10_000_000
    )


def test_every_spec_overrides_the_entrypoint_with_its_own_command() -> None:
    """The image's own entrypoint never wraps the launcher's command."""
    run = parse_request(_raw(RUN), token=TOKEN, settings=SETTINGS)
    build = parse_request(_raw(BUILD), token=TOKEN, settings=SETTINGS)
    assert isinstance(run, RunRequest) and isinstance(build, BuildRequest)

    specs = [
        run_container_spec(run, SETTINGS, no_new_privileges=True),
        build_container_spec(
            build, SETTINGS, no_new_privileges=True, proxy_url=None, cache_key=CACHE_KEY
        ),
        probe_container_spec(SETTINGS, "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.13"),
    ]
    for spec in specs:
        assert spec["Cmd"] == []
        assert spec["Entrypoint"][0].startswith("/")
    assert specs[0]["Entrypoint"] == ["/env/bin/python", "-P", "-m", "lakana.runner"]
    assert specs[1]["Entrypoint"] == ["/usr/local/bin/python", "-I", "/opt/lakana-task/build.py"]


def test_a_build_may_reach_exactly_the_host_and_port_pairs_the_worker_sent() -> None:
    """#301: the token's pairs are the request's targets, whatever port an index is on."""
    build = parse_request(
        _raw(
            _with(
                BUILD,
                indexes=[
                    {
                        "url": "https://pkgs.example.org:8443/simple",
                        "username": "u",
                        "password": "p",
                    }
                ],
                operator_index="https://u:p@mirror.example.org:9443/simple",
                targets=[
                    "pkgs.example.org:8443",
                    "mirror.example.org:9443",
                    "open.example.org:7443",
                ],
            )
        ),
        token=TOKEN,
        settings=SETTINGS,
    )
    assert isinstance(build, BuildRequest)

    assert build_targets(build) == [
        ("pkgs.example.org", 8443),
        ("mirror.example.org", 9443),
        ("open.example.org", 7443),
    ]


@pytest.mark.parametrize(
    ("index", "operator", "targets"),
    [
        pytest.param(
            "https://pkgs.example.org:8443/simple",
            None,
            ["pkgs.example.org:443"],
            id="credential-port-missing",
        ),
        pytest.param(
            "https://pkgs.example.org/simple",
            None,
            ["pkgs.example.org:8443"],
            id="credential-default-port-missing",
        ),
        pytest.param(
            "https://pkgs.example.org/simple",
            "https://mirror.example.org:9443/simple",
            ["pkgs.example.org:443", "mirror.example.org:443"],
            id="operator-port-missing",
        ),
        pytest.param(
            "https://pkgs.example.org/simple",
            "http://mirror.example.org/simple",
            ["pkgs.example.org:443", "mirror.example.org:443"],
            id="operator-http-port-missing",
        ),
    ],
)
def test_every_index_with_a_url_needs_its_own_host_and_port_among_the_targets(
    index: str, operator: str | None, targets: list[str]
) -> None:
    """A credential or the operator's index is sent only where the build may connect.

    Parameters
    ----------
    index
        The credential's URL.
    operator
        The operator's index.
    targets
        The pairs sent.
    """
    request = _with(
        BUILD,
        indexes=[{"url": index, "username": "u", "password": "p"}],
        operator_index=operator,
        targets=targets,
    )

    with pytest.raises(Refused, match="every index's host and port must be in targets"):
        parse_request(_raw(request), token=TOKEN, settings=SETTINGS)


@pytest.mark.parametrize(
    "target",
    [
        "pkgs.example.org",
        "pkgs.example.org:",
        "pkgs.example.org:0",
        "pkgs.example.org:65536",
        "pkgs.example.org:0443",
        "pkgs.example.org:+443",
        "pkgs.example.org:44 3",
        "pkgs.example.org:443:443",
        ":443",
        "127.0.0.1:443",
        "[::1]:443",
        "pkgs.example.org:\u0664\u0664\u0663",
    ],
)
def test_a_target_is_a_permitted_host_and_a_port_in_one_spelling(target: str) -> None:
    """#301: a port is 1 to 65535 in digits without a sign or a leading zero.

    Parameters
    ----------
    target
        A malformed pair.
    """
    request = _with(BUILD, indexes=[], targets=[target.encode().decode("unicode_escape")])

    with pytest.raises(Refused, match="targets:"):
        parse_request(_raw(request), token=TOKEN, settings=SETTINGS)


def test_a_target_that_is_not_a_string_is_refused() -> None:
    """A number or a pair is not ``host:port``."""
    for target in (443, ["pkgs.example.org", 443]):
        with pytest.raises(Refused, match="targets:"):
            parse_request(
                _raw(_with(BUILD, indexes=[], targets=[target])), token=TOKEN, settings=SETTINGS
            )


def test_a_target_may_name_any_port_of_a_host_once() -> None:
    """Two ports of one host are two targets; the same pair twice is refused."""
    two = _with(BUILD, targets=["pkgs.example.org:443", "pkgs.example.org:8443"])
    twice = _with(BUILD, targets=["pkgs.example.org:443", "pkgs.example.org:443"])

    assert isinstance(parse_request(_raw(two), token=TOKEN, settings=SETTINGS), BuildRequest)
    with pytest.raises(Refused, match="targets:"):
        parse_request(_raw(twice), token=TOKEN, settings=SETTINGS)


@pytest.mark.parametrize("base", [RUN, BUILD], ids=["run", "build"])
def test_trust_is_required_and_a_boolean(base: dict[str, Any]) -> None:
    """Without `trust` the request has a field too few; anything but a boolean is refused.

    Parameters
    ----------
    base
        The valid request.
    """
    op = base["op"]
    with pytest.raises(Refused, match=f"{op} takes exactly the fields .*trust"):
        parse_request(_raw(_without(base, "trust")), token=TOKEN, settings=SETTINGS)
    with pytest.raises(Refused, match="the worker and the launcher must be the same release"):
        parse_request(_raw(_without(base, "trust")), token=TOKEN, settings=SETTINGS)
    for value in ("yes", 1, None, "true"):
        with pytest.raises(Refused, match="trust: true or false"):
            parse_request(_raw(_with(base, trust=value)), token=TOKEN, settings=SETTINGS)


def _trust_specs(trust: bool) -> list[dict[str, Any]]:
    """Build the task spec and the build spec of requests with `trust` set.

    Parameters
    ----------
    trust
        The field's value.

    Returns
    -------
    list of dict
        The run's container spec, then the build's.
    """
    run = parse_request(_raw(_with(RUN, trust=trust)), token=TOKEN, settings=SETTINGS)
    build = parse_request(_raw(_with(BUILD, trust=trust)), token=TOKEN, settings=SETTINGS)
    assert isinstance(run, RunRequest) and isinstance(build, BuildRequest)
    assert run.trust is trust and build.trust is trust
    return [
        run_container_spec(run, SETTINGS, no_new_privileges=True),
        build_container_spec(
            build, SETTINGS, no_new_privileges=True, proxy_url=None, cache_key=CACHE_KEY
        ),
    ]


def test_with_trust_both_containers_mount_the_attempts_bundle_read_only() -> None:
    """Subpath `<attempt>/trust` of the work volume at /trust, read-only, and both variables."""
    for spec in _trust_specs(True):
        mounts = [m for m in spec["HostConfig"]["Mounts"] if m["Target"] == "/trust"]
        assert mounts == [
            {
                "Type": "volume",
                "Source": "lakana-task-work",
                "Target": "/trust",
                "ReadOnly": True,
                "VolumeOptions": {"NoCopy": True, "Subpath": "attempt-abc_12/trust"},
            }
        ]
        assert "SSL_CERT_FILE=/trust/trust.pem" in spec["Env"]
        assert "LAKANA_CA_BUNDLE=/trust/trust.pem" in spec["Env"]


def test_without_trust_nothing_changes() -> None:
    """No mount at /trust and neither variable: today's containers."""
    for spec in _trust_specs(False):
        assert all(m["Target"] != "/trust" for m in spec["HostConfig"]["Mounts"])
        assert not any(v.startswith(("SSL_CERT_FILE=", "LAKANA_CA_BUNDLE=")) for v in spec["Env"])


_EGRESS_RULE = "egress: a list of at most 64 distinct [host, port] pairs"
_RELAY_RULE = (
    "relay: at most 16 routes, each a listen port from 15001 to 15064 (distinct) and a host "
    "and port"
)


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("egress", "db:5432", _EGRESS_RULE),
        ("egress", [["DB.example.com", 443]], _EGRESS_RULE),
        ("egress", [["api.example.com", 0]], _EGRESS_RULE),
        ("egress", [["api.example.com", "443"]], _EGRESS_RULE),
        ("egress", [["api.example.com", True]], _EGRESS_RULE),
        ("egress", [["api.example.com", 443, 1]], _EGRESS_RULE),
        ("egress", [["api.example.com", 443]] * 2, _EGRESS_RULE),
        ("egress", [[f"h{i}.example.com", 443] for i in range(65)], _EGRESS_RULE),
        ("relay", {"listen": 15001}, _RELAY_RULE),
        ("relay", [{"listen": 15000, "host": "db", "port": 5432}], _RELAY_RULE),
        ("relay", [{"listen": 15065, "host": "db", "port": 5432}], _RELAY_RULE),
        ("relay", [{"listen": 15001, "host": "db", "port": 5432}] * 2, _RELAY_RULE),
        ("relay", [{"listen": 15001, "host": "db", "port": 5432, "x": 1}], _RELAY_RULE),
        ("relay", [{"listen": 15001, "host": "db:1", "port": 5432}], _RELAY_RULE),
        ("relay", [{"listen": "15001", "host": "db", "port": 5432}], _RELAY_RULE),
        (
            "relay",
            [{"listen": 15001 + i, "host": "db", "port": 5432} for i in range(17)],
            _RELAY_RULE,
        ),
    ],
)
def test_run_refuses_a_malformed_egress_or_relay(field: str, value: object, rule: str) -> None:
    """Each field's rule is the refusal's text.

    Parameters
    ----------
    field
        `egress` or `relay`.
    value
        A value that breaks the rule.
    rule
        The refusal.
    """
    with pytest.raises(Refused) as caught:
        parse_request(_raw(_with(RUN, **{field: value})), token=TOKEN, settings=SETTINGS)

    assert str(caught.value) == rule


@pytest.mark.parametrize("field", ["egress", "relay"])
def test_egress_and_relay_are_required_in_a_run(field: str) -> None:
    """Required even empty: an older worker's request is refused as a release skew, not misread.

    Parameters
    ----------
    field
        The field left out.
    """
    with pytest.raises(Refused, match="the worker and the launcher must be the same release"):
        parse_request(_raw(_without(RUN, field)), token=TOKEN, settings=SETTINGS)


def test_a_run_reads_its_egress_and_relay_in_order() -> None:
    """Private addresses and the most pairs a token holds are accepted; the order is kept."""
    egress = [["db.example.com", 443], ["10.1.2.3", 9000]] + [
        [f"h{i}.example.com", 443] for i in range(MAX_EGRESS - 2)
    ]
    relay = [
        {"listen": 15002, "host": "db.example.com", "port": 5432},
        {"listen": 15001, "host": "erp.example.com", "port": 1433},
    ]

    request = parse_request(
        _raw(_with(RUN, egress=egress, relay=relay)), token=TOKEN, settings=SETTINGS
    )

    assert isinstance(request, RunRequest)
    assert request.egress[:2] == (("db.example.com", 443), ("10.1.2.3", 9000))
    assert len(request.egress) == MAX_EGRESS
    assert request.relay == (
        RelayRoute(15002, "db.example.com", 5432),
        RelayRoute(15001, "erp.example.com", 1433),
    )


def test_a_task_with_egress_gets_the_proxy_variable_and_no_standard_one() -> None:
    """`LAKANA_EGRESS_PROXY` is set; no variable a library reads unasked is."""
    request = parse_request(
        _raw(_with(RUN, egress=[["api.example.com", 443]])), token=TOKEN, settings=SETTINGS
    )
    assert isinstance(request, RunRequest)

    spec = run_container_spec(
        request,
        SETTINGS,
        no_new_privileges=True,
        proxy_url="http://lakana:v1.x.y@egress:3128",
    )
    env = dict(item.split("=", 1) for item in spec["Env"])

    assert env["LAKANA_EGRESS_PROXY"] == "http://lakana:v1.x.y@egress:3128"
    assert not {"HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy"} & set(env)


def test_a_task_without_a_proxy_url_has_no_proxy_variable() -> None:
    """The M7b posture: no egress, no variable."""
    request = parse_request(_raw(RUN), token=TOKEN, settings=SETTINGS)
    assert isinstance(request, RunRequest)

    spec = run_container_spec(request, SETTINGS, no_new_privileges=True)

    assert not any(item.startswith("LAKANA_EGRESS_PROXY=") for item in spec["Env"])


def test_a_relay_plan_always_refuses_the_launchers_range_and_holds_at_most_256() -> None:
    """The given ranges in their order, the task range added when missing; too many is an error."""
    route = (RelayRoute(15001, "db.example.com", 5432),)

    plan = json.loads(relay_plan(route, SETTINGS, "10.207.0.8/29", ["172.17.0.1/32"]))
    kept = json.loads(
        relay_plan(route, SETTINGS, "10.207.0.8/29", ["10.207.0.0/16", "172.17.0.1/32"])
    )

    assert plan == {
        "address": "10.207.0.14",
        "subnet": "10.207.0.8/29",
        "refused": ["172.17.0.1/32", "10.207.0.0/16"],
        "reachable": [],
        "routes": [{"listen": 15001, "host": "db.example.com", "port": 5432}],
    }
    assert kept["refused"] == ["10.207.0.0/16", "172.17.0.1/32"]
    many = [f"10.0.{n}.1/32" for n in range(256)]
    with pytest.raises(ValueError, match="257 refused ranges, more than 256"):
        relay_plan(route, SETTINGS, "10.207.0.8/29", many)


def test_a_relay_plan_carries_the_test_hooks_reachable_networks() -> None:
    """`LauncherSettings.relay_reachable` (never loaded from the environment) goes in as is."""
    route = (RelayRoute(15001, "db.example.com", 5432),)
    settings = dataclasses.replace(SETTINGS, relay_reachable=("172.30.0.0/16",))

    plan = json.loads(relay_plan(route, settings, "10.207.0.8/29", ["172.17.0.1/32"]))

    assert plan["reachable"] == ["172.30.0.0/16"]


@pytest.mark.parametrize(
    ("roots", "allowed"),
    [
        ({"landing": "/srv/landing", "raw": "/srv/raw"}, frozenset({"landing", "raw"})),
        ({}, frozenset()),
    ],
    ids=["two-roots", "none-is-set"],
)
def test_the_roots_refusal_names_the_roots_the_launcher_has(
    roots: dict[str, str], allowed: frozenset[str]
) -> None:
    """The worker reads the allowed names back from the launcher's own refusal text.

    Parameters
    ----------
    roots
        The launcher's ingestion roots.
    allowed
        What `allowed_roots` reads from the refusal.
    """
    settings = LauncherSettings(
        stack="lakana", images=SETTINGS.images, work_volume="w", ingestion_roots=roots
    )
    with pytest.raises(Refused) as caught:
        parse_request(_raw(_with(RUN, roots=["shared"])), token=TOKEN, settings=settings)

    assert str(caught.value).startswith(ROOTS_REFUSAL)
    assert allowed_roots(str(caught.value)) == allowed


@pytest.mark.parametrize(
    "text",
    [
        "python must be one of 3.14 (LAKANA_LAUNCHER__IMAGES)",
        "ingestion root 'x' holds a socket, pipe or device node",
        ROOTS_REFUSAL + "landing",
    ],
    ids=["other-field", "probe", "unterminated"],
)
def test_another_refusal_names_no_allowed_roots(text: str) -> None:
    """Only the roots rule itself is read; anything else is `None`.

    Parameters
    ----------
    text
        A refusal.
    """
    assert allowed_roots(text) is None


CHECK: dict[str, Any] = {
    "token": TOKEN,
    "op": "check",
    "check": "k0f3c9a1e",
    "owner": "worker-1",
    "python": "3.13",
    "attempt": "attempt-check_1",
    "stdin": '{"connection": {}}',
    "roots": [],
    "trust": False,
    "egress": [],
    "relay": [],
}


def test_a_check_is_read_with_the_runs_rules() -> None:
    """Every shared field keeps its run rule; a check has no environment, code or limits."""
    request = parse_request(
        _raw(
            _with(
                CHECK,
                roots=["landing"],
                trust=True,
                egress=[["api.example.com", 443]],
                relay=[{"listen": 15001, "host": "db.example.com", "port": 5432}],
            )
        ),
        token=TOKEN,
        settings=SETTINGS,
    )

    assert request == CheckRequest(
        check="k0f3c9a1e",
        owner="worker-1",
        python="3.13",
        attempt="attempt-check_1",
        stdin=b'{"connection": {}}',
        roots=("landing",),
        trust=True,
        egress=(("api.example.com", 443),),
        relay=(RelayRoute(15001, "db.example.com", 5432),),
    )
    assert "connection" not in repr(request)  # the secret travels in stdin


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"environment": KEY}, "check takes exactly the fields"),
        ({"memory_mb": 4096}, "check takes exactly the fields"),
        ({"attempt": "../x"}, "attempt"),
        ({"roots": ["elsewhere"]}, "roots: a list of distinct names"),
        ({"egress": [["DB", 1]]}, "egress: a list of at most 64"),
        ({"stdin": 5}, "stdin: a string of at most 256 KiB"),
    ],
)
def test_a_check_refuses_what_a_run_refuses(changes: dict[str, Any], rule: str) -> None:
    """Unknown fields, a bad attempt, roots, egress or stdin: refused with the field's rule.

    Parameters
    ----------
    changes
        Fields to set.
    rule
        Part of the refusal.
    """
    with pytest.raises(Refused, match=re.escape(rule)):
        parse_request(_raw(_with(CHECK, **changes)), token=TOKEN, settings=SETTINGS)


def test_a_check_container_runs_lakanas_check_and_nothing_of_a_task() -> None:
    """No /code, /env or /work; the image's Python on Lakana's own check; a 16 MiB /tmp."""
    request = parse_request(
        _raw(_with(CHECK, roots=["landing"], trust=True)), token=TOKEN, settings=SETTINGS
    )
    assert isinstance(request, CheckRequest)

    spec = check_container_spec(
        request,
        SETTINGS,
        no_new_privileges=True,
        deadline=99,
        proxy_url="http://lakana:t@egress:3128",
    )

    assert spec["Image"] == "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.13"
    assert spec["Entrypoint"] == ["/usr/local/bin/python", "-I", "-B", "/opt/lakana-check/check.py"]
    assert spec["User"] == SETTINGS.task_user
    targets = [m["Target"] for m in spec["HostConfig"]["Mounts"]]
    assert targets == ["/ingest/landing", "/trust"]
    assert all(m["ReadOnly"] for m in spec["HostConfig"]["Mounts"])
    assert spec["HostConfig"]["Mounts"][1]["VolumeOptions"]["Subpath"] == "attempt-check_1/trust"
    assert spec["HostConfig"]["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,nodev,size=16m"}
    assert spec["HostConfig"]["Memory"] == 256 * 1024 * 1024
    assert spec["HostConfig"]["NetworkMode"] == "lakana-check-k0f3c9a1e"
    assert spec["Labels"]["lakana.launcher.kind"] == "check"
    env = dict(item.split("=", 1) for item in spec["Env"])
    assert env["LAKANA_EGRESS_PROXY"] == "http://lakana:t@egress:3128"
    assert env["SSL_CERT_FILE"] == env["LAKANA_CA_BUNDLE"] == "/trust/trust.pem"


def test_a_check_must_name_a_check_attempt_never_a_tasks() -> None:
    """Strict: a check's attempt directory is ``attempt-check_*``, so it cannot name a task's."""
    with pytest.raises(Refused, match="attempt"):
        parse_request(
            _raw(_with(CHECK, attempt="attempt-abc_1234")), token=TOKEN, settings=SETTINGS
        )
    with pytest.raises(Refused, match="attempt"):
        parse_request(_raw(_with(CHECK, attempt="attempt-check_")), token=TOKEN, settings=SETTINGS)

    request = parse_request(
        _raw(_with(CHECK, attempt="attempt-check_ab12cd_3")), token=TOKEN, settings=SETTINGS
    )
    assert isinstance(request, CheckRequest) and request.attempt == "attempt-check_ab12cd_3"


def test_a_check_container_keeps_a_tasks_hardening_and_its_own_fixed_limits() -> None:
    """Pinned here, so a later change to `_host_config` cannot loosen a check silently."""
    request = parse_request(_raw(CHECK), token=TOKEN, settings=SETTINGS)
    assert isinstance(request, CheckRequest)

    spec = check_container_spec(request, SETTINGS, no_new_privileges=True, deadline=99)

    host = spec["HostConfig"]
    assert host["Memory"] == 256 * 1024 * 1024
    assert host["MemorySwap"] == host["Memory"]
    assert host["PidsLimit"] == 64
    assert host["NanoCpus"] == 500_000_000
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["Privileged"] is False
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert spec["User"] == SETTINGS.task_user and not spec["User"].startswith("0")
    assert host["NetworkMode"] == "lakana-check-k0f3c9a1e"
    assert "server" not in json.dumps(spec)
    assert not any(item.startswith("LAKANA_EGRESS_PROXY=") for item in spec["Env"])
