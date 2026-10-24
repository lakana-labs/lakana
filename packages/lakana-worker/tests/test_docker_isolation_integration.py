"""The exit criterion of task isolation, one test per property, on a real Docker Engine.

"With DockerRunner selected, a Python task cannot read the worker's credentials or another
project's environment, its memory, CPU and process limits are enforced by the container runtime,
and a stopped task leaves no process behind." Each probe below is the task; the launcher runs it
as DockerRunner would. Skipped without Docker, failed then when `LAKANA_TEST_DOCKER=1`.

Beyond the criterion, the real-engine cases the reviews of the launcher asked for: no IPv6
address and no host link-local reach, no DNS resolution of outside names, the isolated gateway,
symbolic links as subpaths, no anonymous volume, no setuid file in the image, a launcher that
stops removes its tasks, 40 tasks at once, the ingestion-root probe on a pipe and on a directory
the task user cannot list, and the uv caches of builds (private unless wheels only; a shared one
never hands a project another project's local wheel, and runs no installed code).
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import ipaddress
import json
import os
import shutil
import socket
import subprocess  # nosec B404 -- runs uv to lock a test project, fixed argv
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from docker_stack import (
    KEY,
    PYTHON,
    STUB_HARNESS,
    Stack,
    build_task_image,
    docker_client,
    host_processes,
    make_wheel,
    serve,
    stack,
)

from lakana_worker.launcher.engine import EngineError
from lakana_worker.launcher.requests import (
    LABEL_STACK,
    StopRequest,
    container_name,
    environment_volume,
)
from lakana_worker.launcher.service import Launcher

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture(scope="module")
def running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """One stack for the module, with a credential in this process's environment.

    Parameters
    ----------
    tmp_path_factory
        For the launcher's state.

    Yields
    ------
    Stack
        The stack.
    """
    client = docker_client()
    image = build_task_image(client)
    os.environ["LAKANA_TEST_WORKER_SECRET"] = "s3cr3t-worker-credential"
    root = tmp_path_factory.mktemp("stack")
    try:
        with stack(client, image, root) as current:
            yield current
    finally:
        os.environ.pop("LAKANA_TEST_WORKER_SECRET", None)
        client.close()


def _json(output: bytes) -> dict[str, Any]:
    """Read the probe's last output line.

    Parameters
    ----------
    output
        The task's output.

    Returns
    -------
    dict
        The decoded line.
    """
    assert output.strip(), "the probe printed nothing; the task did not run"
    return json.loads(output.strip().splitlines()[-1])


def _until(condition: Callable[[], object], seconds: float, what: str | Callable[[], str]) -> None:
    """Wait for a condition, failing with a message when it does not come true in time.

    Parameters
    ----------
    condition
        Checked every 0.2 s.
    seconds
        The bound.
    what
        Named in the failure; a callable is asked then, so it can say what is left.
    """
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            text = what() if callable(what) else what
            pytest.fail(f"{text} (waited {seconds:g}s)")
        time.sleep(0.2)


def _in_background(work: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    """Run a call on a thread, keeping what it returned or raised.

    Parameters
    ----------
    work
        The call.

    Returns
    -------
    tuple
        The started thread and a list that receives the result or the exception.
    """
    outcome: list[object] = []

    def target() -> None:
        """Run the call and keep its outcome."""
        try:
            outcome.append(work())
        except Exception as exc:  # handed to the test, which names it
            outcome.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


# --- the exit criterion -----------------------------------------------------------------------

_ISOLATION = r"""
import errno, json, os
table = [line.split() for line in open("/proc/self/mountinfo")]
mounts = sorted({fields[4] for fields in table})
options = {fields[4]: fields[5] for fields in table}
writes = {}
for path in ("/env/x", "/code/x", "/x"):
    try:
        open(path, "w").close()
        writes[path] = "written"
    except OSError as exc:
        writes[path] = errno.errorcode[exc.errno]
status = {l.split(":")[0]: l.split()[1] for l in open("/proc/self/status") if l.startswith("Cap")}
print(json.dumps({
    "secret": "LAKANA_TEST_WORKER_SECRET" in os.environ,
    "env": sorted(os.environ),
    "docker_socket": os.path.exists("/var/run/docker.sock"),
    "caps": status,
    "uid": os.getuid(),
    "gid": os.getgid(),
    "groups": sorted(os.getgroups()),
    "mounts": [m for m in mounts if not m.startswith(("/proc", "/sys", "/dev"))],
    "writes": writes,
    "options": {path: options.get(path, "") for path in ("/", "/code", "/env")},
}))
"""
_MOUNTS = {"/", "/code", "/env", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf", "/tmp", "/work"}
"""What a task sees mounted, outside /proc, /sys and /dev."""
_INIT = {"/sbin/docker-init", "/usr/sbin/docker-init"}
"""Docker's init (`Init: true`), bind-mounted read-only (the path depends on the engine)."""


def test_a_task_cannot_read_the_workers_credentials_or_another_projects_environment(
    running: Stack,
) -> None:
    """No variable, file or socket of the worker; only its own code, environment and scratch."""
    result, output = running.run(running.attempt(_ISOLATION))

    seen = _json(output)
    assert result["exit"] == 0, output
    assert seen["secret"] is False, "the worker's environment reached the task"
    assert not any(name.startswith("LAKANA_") for name in seen["env"]), seen["env"]
    assert seen["docker_socket"] is False, "the Docker socket is visible in the task"
    zero = "0000000000000000"
    caps = {name: seen["caps"].get(name) for name in ("CapEff", "CapPrm", "CapBnd", "CapAmb")}
    assert caps == dict.fromkeys(caps, zero), f"capabilities left (CapDrop ALL broken?): {caps}"
    assert (seen["uid"], seen["gid"]) == (10001, 10003)
    assert set(seen["groups"]) <= {10003}, f"supplementary groups: {seen['groups']}"
    mounts = set(seen["mounts"])
    assert mounts >= _MOUNTS, f"missing mounts: {sorted(_MOUNTS - mounts)}"
    assert len(mounts - _MOUNTS) <= 1 and mounts - _MOUNTS <= _INIT, f"extra: {sorted(mounts)}"
    not_read_only = [p for p, o in seen["options"].items() if "ro" not in o.split(",")]
    assert not_read_only == [], f"mounted writable: {not_read_only} ({seen['options']})"
    assert seen["writes"] == dict.fromkeys(seen["writes"], "EROFS"), seen["writes"]


_SIBLINGS = r"""
import json, os
table = {fields[4]: fields[3] for fields in (line.split() for line in open("/proc/self/mountinfo"))}
found = [
    os.path.join(directory, name)
    for top in ("/work", "/code")
    for directory, _, names in os.walk(top)
    for name in names
    if "CANARY" in name
]
print(json.dumps({"found": found, "work": table.get("/work"), "code": table.get("/code")}))
"""


def test_a_task_sees_no_other_attempt_in_the_shared_work_volume(running: Stack) -> None:
    """Every attempt of every project lives in one volume, owned by the task's own uid.

    Only the `/code` and `/work` subpaths keep a task to its own attempt: a sibling attempt's
    canary files must be out of reach, and the mounts' roots must be the attempt's own.
    """
    canary = f"CANARY-{uuid.uuid4().hex}"
    sibling = running.attempt("", files={canary: b"another project's code"})
    running.put(
        running.work_volume, [(f"{sibling}/task/{canary}", "file", "its data", 0o600, 10001)]
    )
    attempt = running.attempt(_SIBLINGS)

    result, output = running.run(attempt)

    seen = _json(output)
    assert result["exit"] == 0, output
    assert seen["found"] == [], f"the task sees another attempt: {seen['found']}"
    assert str(seen["work"]).endswith(f"/{attempt}/task"), f"/work is not the subpath: {seen}"
    assert str(seen["code"]).endswith(f"/{attempt}/code"), f"/code is not the subpath: {seen}"


_MEMORY = "blocks = []\nwhile True:\n    blocks.append(bytearray(16 * 1024 * 1024))\n"
_PIDS = r"""
import os, time
count = 0
try:
    while True:
        if os.fork() == 0:
            time.sleep(30)
            os._exit(0)
        count += 1
except OSError:
    pass
print(count, flush=True)
"""
_CPU = 'print(open("/sys/fs/cgroup/cpu.max").read().strip())\n'


def test_memory_cpu_and_process_limits_are_enforced_by_the_runtime(running: Stack) -> None:
    """The trial's figures: an OOM kill under the memory limit, the CPU quota, the fork ceiling."""
    memory, _ = running.run(running.attempt(_MEMORY), memory_mb=128)
    pids, pids_output = running.run(running.attempt(_PIDS), pids=32)
    cpu, cpu_output = running.run(running.attempt(_CPU), cpus=0.5)

    assert memory["oom_killed"] is True, f"no OOM kill at 128 MiB: {memory}"
    assert pids["exit"] == 0 and 0 < int(pids_output.strip()) < 32, (pids, pids_output)
    assert cpu_output.strip() == b"50000 100000", (cpu, cpu_output)


_GRANDCHILD = r"""
import os, sys
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        os.execvp("python", ["python", "-c", "import time; time.sleep(600)", "MARKER"])
    os._exit(0)
print("detached", flush=True)
"""


def test_a_detached_grandchild_is_gone_when_the_task_ends(running: Stack) -> None:
    """A double fork and `setsid` do not outlive the container."""
    marker = f"lakana-test-{uuid.uuid4().hex}"

    result, output = running.run(running.attempt(_GRANDCHILD.replace("MARKER", marker)))

    assert result["exit"] == 0 and b"detached" in output, (result, output)
    _until(lambda: not host_processes(marker), 10, "the detached grandchild outlived its task")
    assert running.labelled() == []


_NETWORK = r"""
import ipaddress, json, socket
def reach(host, port):
    try:
        socket.create_connection((host, port), timeout=3).close()
        return "open"
    except OSError as exc:
        return type(exc).__name__
routes = [line.split() for line in open("/proc/net/route")][1:]
gateway = next((r[2] for r in routes if r[1] == "00000000"), None)
me = socket.gethostbyname(socket.gethostname())
subnet_gateway = str(ipaddress.ip_network(me + "/29", strict=False).network_address + 1)
print(json.dumps({
    "server": reach("server", 8000),
    "internet": reach("1.1.1.1", 443),
    "docker0": reach("172.17.0.1", PORT),
    "subnet_gateway": [subnet_gateway, reach(subnet_gateway, PORT)],
    "gateway": gateway,
}))
"""


def _listener(family: int = socket.AF_INET) -> socket.socket:
    """Listen on every interface of the host on a free port, as a host service would.

    Parameters
    ----------
    family
        ``AF_INET`` or ``AF_INET6`` (dual stack).

    Returns
    -------
    socket.socket
        The listening socket.
    """
    listener = socket.socket(family)
    if family == socket.AF_INET6:
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind(("::", 0))
    else:
        listener.bind(("0.0.0.0", 0))  # nosec B104 -- the probe must find nothing on any interface
    listener.listen(8)
    return listener


def test_the_task_reaches_only_the_server(running: Stack) -> None:
    """Not the internet, not the host's listeners, and there is no default route at all.

    The gateway address of the task's own /29 answers nothing: the network's gateway mode is
    isolated, so the host has no address on it (an internal bridge otherwise reaches every host
    service listening on all interfaces there).
    """
    listener = _listener()
    port = listener.getsockname()[1]
    try:
        _, output = running.run(running.attempt(_NETWORK.replace("PORT", str(port))))
    finally:
        listener.close()

    seen = _json(output)
    assert seen["server"] == "open", seen
    assert seen["internet"] != "open" and seen["docker0"] != "open", seen
    assert seen["subnet_gateway"][1] != "open", f"the host answers on the task's gateway: {seen}"
    assert seen["gateway"] is None, f"the task has a default route: {seen}"


_IPV6 = r"""
import json, socket
def reach(address, port):
    try:
        socket.create_connection((address + "%eth0", port), timeout=3).close()
        return "open"
    except OSError as exc:
        return type(exc).__name__
try:
    addresses = [line.split() for line in open("/proc/net/if_inet6")]
except OSError:
    addresses = []
try:
    disabled = open("/proc/sys/net/ipv6/conf/eth0/disable_ipv6").read().strip()
except OSError:
    disabled = "absent"
print(json.dumps({
    "addresses": [[fields[0], fields[5]] for fields in addresses],
    "eth0_disable_ipv6": disabled,
    "host": {address: reach(address, PORT) for address in HOSTS},
}))
"""


def _host_link_local() -> list[str]:
    """List the host's IPv6 link-local addresses.

    Returns
    -------
    list of str
        Addresses in ``fe80::`` form; empty without IPv6.
    """
    try:
        lines = Path("/proc/net/if_inet6").read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 6 and fields[3] == "20":  # scope 0x20: link
            found.append(str(ipaddress.IPv6Address(int(fields[0], 16))))
    return found


def test_the_task_has_no_ipv6_address_and_reaches_no_host_link_local_address(
    running: Stack,
) -> None:
    """Review focus: a link-local address would reach the host past the IPv4 isolation."""
    try:
        listener = _listener(socket.AF_INET6)
    except OSError:
        listener = None  # no IPv6 on this host: only the task's own addresses are checked
    port = listener.getsockname()[1] if listener is not None else 9
    probe = _IPV6.replace("PORT", str(port)).replace("HOSTS", repr(_host_link_local()))
    try:
        _, output = running.run(running.attempt(probe))
    finally:
        if listener is not None:
            listener.close()

    seen = _json(output)
    outside_lo = [entry for entry in seen["addresses"] if entry[1] != "lo"]
    assert outside_lo == [], f"the task has IPv6 addresses: {outside_lo} ({seen})"
    assert not any(state == "open" for state in seen["host"].values()), seen


_DNS = r"""
import json, socket
def resolve(name):
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(name, 80)})
    except socket.gaierror:
        return "gaierror"
def udp(address):
    query = bytes.fromhex("abcd01000001000000000000076578616d706c6503636f6d0000010001")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        sock.sendto(query, (address, 53))
        sock.recv(512)
        return "answered"
    except OSError as exc:
        return type(exc).__name__
servers = [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")]
print(json.dumps({
    "nameservers": servers,
    "server": resolve("server"),
    "public": resolve("example.com"),
    "unique": resolve("NAME"),
    "udp": udp("8.8.8.8"),
}))
"""


def test_the_task_resolves_no_outside_name(running: Stack) -> None:
    """Review focus: a forwarded query is an exfiltration channel (a name it makes up leaks).

    Docker's resolver answers the stack's own names on an internal network and forwards
    nothing else; port 53 outside is unreachable like every other port. The proof that nothing
    is forwarded is `example.com` failing to resolve: it exists, so only a resolver that never
    asks outside fails it. A made-up name fails either way (a forwarded query for it comes back
    as "no such name"); its check only shows that it, too, gets no answer.
    """
    unique = f"lakana-exfil-{uuid.uuid4().hex}.example.com"

    _, output = running.run(running.attempt(_DNS.replace("NAME", unique)))

    seen = _json(output)
    assert seen["server"] not in ([], "gaierror"), f"the server's name does not resolve: {seen}"
    assert seen["public"] == "gaierror", f"an outside name resolves: {seen}"
    assert seen["unique"] == "gaierror", f"a made-up outside name resolves: {seen}"
    assert seen["udp"] != "answered", f"a DNS server outside answered: {seen}"


_SLEEPER = r"""
import os, time
if os.fork() == 0:
    os.execvp("python", ["python", "-c", "import time; time.sleep(600)", "MARKER"])
print("started", flush=True)
time.sleep(600)
"""


def test_a_worker_that_disappears_leaves_no_container_and_no_process(
    running: Stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus: the connection closes, nothing sends `stop`, and still nothing is left."""
    monkeypatch.chdir(tmp_path)
    assert running.launcher is not None
    server, _ = serve(running.launcher)
    marker = f"lakana-test-{uuid.uuid4().hex}"
    attempt = running.attempt(_SLEEPER.replace("MARKER", marker))
    token = Path("launcher.token").read_text()
    request = {
        "token": token,
        "op": "run",
        "task": uuid.uuid4().hex[:20],
        "owner": "test",
        "python": PYTHON,
        "environment": KEY,
        "attempt": attempt,
        "memory_mb": 256,
        "cpus": 1,
        "pids": 64,
        "timeout_seconds": 600,
        "kill_grace_seconds": 2,
        "stdin": '{"asset_path": "probe.py"}',
        "roots": [],
        "trust": False,
    }
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect("l.sock")
        client.sendall(json.dumps(request).encode() + b"\n")
        _until(lambda: host_processes(marker), 30, "the task never started")

        client.close()

        _until(
            lambda: not host_processes(marker) and not running.labelled(),
            20,
            lambda: (
                f"after the connection closed: processes {host_processes(marker)}, "
                f"objects {running.labelled()}"
            ),
        )
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("code_link", "task_link"),
    [("/", None), (None, "/"), (None, "../../../../../../../..")],
    ids=["code-to-host-root", "task-to-host-root", "task-up-and-out"],
)
def test_a_subpath_that_is_a_link_out_of_the_volume_is_refused_by_docker(
    running: Stack, code_link: str | None, task_link: str | None
) -> None:
    """Review focus: the name passes the launcher's pattern; Docker's own check refuses the link.

    Parameters
    ----------
    running
        The stack.
    code_link
        Where ``code`` points, or `None` for a directory.
    task_link
        Where ``task`` points, or `None` for a directory.
    """
    attempt = running.attempt("print('ran')", code_link=code_link, task_link=task_link)

    # Docker's own subpath check, not a missing directory: the link was followed and refused.
    with pytest.raises(EngineError, match="escapes the base directory"):
        running.run(attempt)

    assert running.labelled() == []


# --- what the image and the containers carry ---------------------------------------------------


def test_the_task_image_has_no_setuid_or_setgid_file(running: Stack) -> None:
    """Where no-new-privileges is off (snap Docker), such a file would be uid 0 again."""
    output = running.client.containers.run(
        running.image,
        ["find", "/", "-xdev", "-perm", "/6000", "-type", "f"],
        user="0:0",
        labels=running.labels,
        remove=True,
    )

    assert output.decode().split() == []


def test_a_task_container_mounts_only_its_volumes_and_makes_no_anonymous_one(
    running: Stack,
) -> None:
    """Review focus: a VOLUME in the image would give every task a writable, lasting volume."""
    assert running.launcher is not None
    assert not running.client.images.get(running.image).attrs["Config"].get("Volumes")
    before = {volume.name for volume in running.client.volumes.list()}
    task = uuid.uuid4().hex[:20]
    marker = f"lakana-test-{uuid.uuid4().hex}"
    attempt = running.attempt(_SLEEPER.replace("MARKER", marker))
    thread, outcome = _in_background(lambda: running.run(attempt, task=task, timeout=120))
    _until(lambda: host_processes(marker), 30, "the task never started")
    name = container_name(running.launcher.settings, "task", task)
    mounts = running.client.containers.get(name).attrs["Mounts"]

    assert running.launcher.stop(StopRequest(task, "test")) == {"stopped": True}
    thread.join(60)

    env = environment_volume(running.launcher.settings, PYTHON, KEY)
    volumes = sorted((m["Name"], m["Destination"]) for m in mounts if m["Type"] == "volume")
    assert volumes == sorted(
        [(running.work_volume, "/code"), (env, "/env"), (running.work_volume, "/work")]
    ), mounts
    assert [m for m in mounts if m["Type"] not in ("volume", "tmpfs")] == [], mounts
    assert {volume.name for volume in running.client.volumes.list()} - before == set()
    assert not thread.is_alive() and not isinstance(outcome[0], Exception), outcome


_STUBBORN = r"""
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.fork() == 0:
    os.execvp("python", ["python", "-c", "import time; time.sleep(600)", "MARKER"])
print("started", flush=True)
time.sleep(600)
"""
"""Ignores SIGTERM, and so does its child (an ignored signal stays ignored across exec)."""


def test_a_stopped_task_leaves_no_process_and_nothing_labelled(running: Stack) -> None:
    """The exit criterion's "a stopped task leaves no process behind", through `stop`.

    `stop` is what DockerRunner sends on a cancel, a lost lease or a drain. The task ignores
    SIGTERM, so it ends only by the SIGKILL after its grace.
    """
    assert running.launcher is not None
    task = uuid.uuid4().hex[:20]
    marker = f"lakana-test-{uuid.uuid4().hex}"
    attempt = running.attempt(_STUBBORN.replace("MARKER", marker))
    thread, outcome = _in_background(lambda: running.run(attempt, task=task, timeout=120))
    _until(lambda: host_processes(marker), 30, "the task never started")

    assert running.launcher.stop(StopRequest(task, "test")) == {"stopped": True}

    _until(
        lambda: not host_processes(marker) and not running.labelled(),
        20,
        lambda: f"after stop: processes {host_processes(marker)}, objects {running.labelled()}",
    )
    thread.join(30)
    assert not thread.is_alive(), "the run did not end after its container was stopped"
    result = outcome[0]
    assert isinstance(result, tuple) and result[0]["stopped"] is True, outcome


def test_a_launcher_that_stops_removes_its_running_tasks(running: Stack, tmp_path: Path) -> None:
    """Review focus: SIGTERM to the launcher must not leave a task running, untimed."""
    with stack(running.client, running.image, tmp_path) as own:
        assert own.launcher is not None
        marker = f"lakana-test-{uuid.uuid4().hex}"
        attempt = own.attempt(_SLEEPER.replace("MARKER", marker))
        thread, _ = _in_background(lambda: own.run(attempt, timeout=600))
        _until(lambda: host_processes(marker), 30, "the task never started")

        own.launcher.shutdown()

        _until(
            lambda: not host_processes(marker) and not own.labelled(),
            30,
            lambda: (
                f"after the launcher stopped: processes {host_processes(marker)}, "
                f"objects {own.labelled()}"
            ),
        )
        thread.join(60)
        assert not thread.is_alive(), "the run did not end when its container was removed"


_CONCURRENT = (
    "import socket, time\ntime.sleep(3)\n"
    'print("ok", socket.gethostname(), socket.gethostbyname(socket.gethostname()), flush=True)\n'
)


def test_forty_tasks_run_at_once_each_in_its_own_container_and_network(running: Stack) -> None:
    """Review focus: the /29 allocation, the server's 40 attachments and the cleanup under load."""
    attempts = [running.attempt(_CONCURRENT) for _ in range(40)]
    attached: dict[str, frozenset[str]] = {}
    sampling = threading.Event()

    def sample() -> None:
        """Note the networks each running task container is attached to, keyed by hostname."""
        label = f"{LABEL_STACK}={running.name}"
        while not sampling.is_set():
            for container in running.client.api.containers(filters={"label": [label]}):
                networks = container["NetworkSettings"]["Networks"]
                attached[container["Id"][:12]] = frozenset(
                    n["NetworkID"] for n in networks.values()
                )
            sampling.wait(0.1)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
        runs = list(
            pool.map(lambda a: running.run(a, memory_mb=64, pids=32, timeout=180), attempts)
        )

    sampling.set()
    sampler.join(10)
    failed = [(result, output) for result, output in runs if result["exit"] != 0]
    assert failed == [], failed[:3]
    hosts = {output.split()[1].decode() for _, output in runs}
    assert len(hosts) == 40, "two tasks shared a container"
    assert hosts <= attached.keys(), f"{len(hosts - attached.keys())} containers never sampled"
    networks = {attached[host] for host in hosts}
    assert all(len(ids) == 1 for ids in networks), networks
    assert len(networks) == 40, f"two tasks shared a network: {len(networks)} networks for 40"
    assert running.labelled() == []


# --- the ingestion-root probe ----------------------------------------------------------------


def test_an_ingestion_root_with_a_pipe_or_a_directory_the_task_cannot_list_is_unusable(
    running: Stack, tmp_path: Path
) -> None:
    """The start-up probe looks at each root from inside a container, as the task user."""
    assert running.launcher is not None
    roots = {}
    for name in ("plain", "pipe", "listless"):
        directory = tmp_path / name
        directory.mkdir()
        directory.chmod(0o755)
        (directory / "data.csv").write_text("a\n1\n")
        (directory / "data.csv").chmod(0o644)
        roots[name] = str(directory)
    os.mkfifo(tmp_path / "pipe" / "feed")
    (tmp_path / "pipe" / "feed").chmod(0o666)
    hidden = tmp_path / "listless" / "inner"
    hidden.mkdir()
    hidden.chmod(0o711)
    probing = Launcher(
        dataclasses.replace(running.launcher.settings, ingestion_roots=roots),
        running.launcher.engine,
    )

    probing._check_ingestion_roots()

    reasons = probing.unusable_roots
    assert set(reasons) == {"pipe", "listless"}, reasons
    assert "socket, pipe or device node" in reasons["pipe"]
    assert "cannot list or examine" in reasons["listless"]
    assert not any(str(tmp_path) in reason for reason in reasons.values()), reasons
    assert running.labelled() == []


# --- the uv caches of builds -------------------------------------------------------------------

_PTH = (
    'import os; os.access("/uv-cache", os.W_OK) and open("/uv-cache/lakana-pth-ran", "w").close()\n'
)
"""Installed with the probe package: it runs whenever the environment's interpreter starts."""
_VALUE = "import probe_pkg\nprint(probe_pkg.VALUE, flush=True)\n"


def _project(directory: Path, value: str) -> dict[str, bytes]:
    """Write and lock a project whose only dependencies are two wheels in its repository.

    The stub `lakana` wheel carries the harness the build checks for; `probe-pkg` holds the
    project's own `VALUE` and a `.pth` that marks the shared cache if anything runs it there.

    Parameters
    ----------
    directory
        Where to write it.
    value
        `probe_pkg.VALUE`.

    Returns
    -------
    dict
        Path in the code directory to content: `pyproject.toml`, `uv.lock` and the wheels.
    """
    uv = shutil.which("uv")
    if uv is None:
        if os.environ.get("LAKANA_TEST_DOCKER") == "1":
            pytest.fail("the build tests lock a project with uv, which is not on the PATH")
        pytest.skip("uv is not on the PATH")
    lakana = make_wheel(
        "lakana", "0.0.0", {"lakana/__init__.py": "", "lakana/runner.py": STUB_HARNESS}
    )
    probe = make_wheel(
        "probe-pkg",
        "1.0",
        {"probe_pkg/__init__.py": f"VALUE = {value!r}\n", "probe_pkg_marker.pth": _PTH},
    )
    files = {f"wheels/{name}": data for name, data in (lakana, probe)}
    files["pyproject.toml"] = (
        "[project]\n"
        'name = "probe-project"\n'
        'version = "0"\n'
        f'requires-python = ">={PYTHON}"\n'
        'dependencies = ["lakana", "probe-pkg"]\n\n'
        "[tool.uv.sources]\n"
        f'lakana = {{ path = "wheels/{lakana[0]}" }}\n'
        f'probe-pkg = {{ path = "wheels/{probe[0]}" }}\n'
    ).encode()
    for path, data in files.items():
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_bytes(data)
    locked = subprocess.run(  # nosec B603 -- uv from the PATH, fixed argv
        [
            uv,
            "lock",
            "--offline",
            "--no-config",
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
    files["uv.lock"] = (directory / "uv.lock").read_bytes()
    return files


def test_build_caches_are_private_unless_wheels_only_and_never_share_a_localmake_wheel(
    running: Stack, tmp_path: Path
) -> None:
    """PR 2 security review C1 and R1, end to end, plus what a wheels-only build runs.

    A build that may run package code gets a cache of its own, gone when it ends. Two
    wheels-only builds of the same index set share one cache, yet each environment gets its
    own repository's wheel, although both lie at the same relative path with the same name and
    version (the code is mounted at ``/src/<build id>``). And uv runs none of the installed code
    while it builds, so nothing it installs can write to the shared cache.
    """
    first_project = _project(tmp_path / "a", "A")
    second_project = _project(tmp_path / "b", "B")

    private, output = running.build(
        running.attempt("", files=first_project), environment="c" * 64, no_build=False
    )
    assert private["complete"] is True, output.decode(errors="replace")
    assert running.caches() == {}, "a private cache outlived its build"

    first, first_output = running.build(
        running.attempt("", files=first_project), environment="e" * 64, no_build=True
    )
    second, second_output = running.build(
        running.attempt("", files=second_project), environment="f" * 64, no_build=True
    )
    assert first["complete"] is True, first_output.decode(errors="replace")
    assert second["complete"] is True, second_output.decode(errors="replace")
    caches = running.caches()
    assert list(caches.values()) == ["1"], f"expected one shared cache: {caches}"

    _, seen_first = running.run(running.attempt(_VALUE), environment="e" * 64)
    _, seen_second = running.run(running.attempt(_VALUE), environment="f" * 64)
    assert (seen_first.strip(), seen_second.strip()) == (b"A", b"B"), (
        "a wheels-only build installed another project's local wheel from the shared cache"
    )
    listing = running.list_volume(next(iter(caches)), user="10002:10002")
    assert "lakana-pth-ran" not in listing, (
        "uv ran the environment's interpreter after installing (a .pth ran during a "
        "wheels-only build), so installed code can write to the shared cache"
    )
