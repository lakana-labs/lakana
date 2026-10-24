"""A throwaway stack on the real Docker Engine, for the Docker isolation tests.

A launcher in this process, a server stand-in, the work volume and one built environment.
Everything carries ``lakana-test=1`` and the stack's own ``lakana-test-stack=<name>``, and is
removed, also after a failure.
"""
# ruff: file-ignore[any-type]  (the Docker SDK is untyped: its client and objects are Any)

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import os
import sys
import tarfile
import threading
import uuid
import zipfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import lakana_worker.launcher
from lakana_worker.launcher.engine import DockerEngine
from lakana_worker.launcher.requests import (
    LABEL_KIND,
    LABEL_SHARED,
    LABEL_STACK,
    TRUST_FILE,
    BuildRequest,
    Limits,
    RunRequest,
    environment_volume,
)
from lakana_worker.launcher.server import LauncherServer, write_token
from lakana_worker.launcher.service import Launcher
from lakana_worker.launcher.settings import LauncherSettings

PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
LABELS = {"lakana-test": "1"}
REPO = Path(__file__).resolve().parents[3]
KEY = "d" * 64
STUB_HARNESS = (
    "import json, runpy, sys\n"
    "description = json.loads(sys.stdin.read())\n"
    'runpy.run_path("/code/" + description["asset_path"], run_name="__main__")\n'
)
"""Stands in for `lakana.runner`: runs the attempt's script, so a test's probe is the task."""

APP = "/opt/lakana-task"
"""Where a helper's source and files go in its container: a directory the task image has."""

Entry = tuple[str, str, bytes | str, int, int]
"""``(path, kind, content, mode, uid)``; kind ``dir`` or ``file`` (links: `Stack.link`)."""


def docker_socket() -> Path:
    """Find the engine's socket, the one the test's client and the launcher both use.

    The launcher speaks to Docker only over a Unix socket (`DockerEngine`), as in its Compose
    service, so `DOCKER_HOST` is honoured only as ``unix://``; anything else would let the test
    look at one engine while the launcher drives another.

    Returns
    -------
    Path
        `DOCKER_HOST`'s path, or ``/var/run/docker.sock`` without it.
    """
    host = os.environ.get("DOCKER_HOST", "")
    if not host:
        return Path("/var/run/docker.sock")
    if host.startswith("unix://"):
        return Path(host.removeprefix("unix://"))
    message = (
        f"DOCKER_HOST is {host!r}; the launcher reaches Docker only through a Unix socket, so "
        "these tests need DOCKER_HOST unset or unix://<path>"
    )
    if os.environ.get("LAKANA_TEST_DOCKER") == "1":
        pytest.fail(message)
    pytest.skip(message)


def docker_client() -> Any:
    """Connect to Docker on `docker_socket`, or skip (fail when `LAKANA_TEST_DOCKER=1`).

    Returns
    -------
    docker.DockerClient
        A client of an engine of version 28 or later.
    """
    docker = pytest.importorskip("docker")
    required = os.environ.get("LAKANA_TEST_DOCKER") == "1"
    socket_path = docker_socket()
    try:
        client = docker.DockerClient(base_url=f"unix://{socket_path}")
        version = str(client.version()["Version"])
    except Exception as exc:
        if required:
            pytest.fail(f"these tests need Docker here (LAKANA_TEST_DOCKER=1): {exc}")
        pytest.skip("no Docker on this machine")
    if int(version.split(".")[0]) < 28:
        message = f"these tests need Docker Engine 28 or later, this one is {version}"
        if required:
            pytest.fail(message)
        pytest.skip(message)
    return client


def build_task_image(client: Any) -> str:
    """Build the task image of this interpreter's Python from `Dockerfile.task`.

    The context is built here, holding only what `Dockerfile.task.dockerignore` admits: the
    Docker SDK reads the root `.dockerignore`, which admits only `dist/`.

    Parameters
    ----------
    client
        The Docker client.

    Returns
    -------
    str
        The tag.
    """
    tag = f"lakana-task:test-py{PYTHON}"
    context = io.BytesIO()
    with tarfile.open(fileobj=context, mode="w") as archive:
        for relative in (
            "Dockerfile.task",
            "packages/lakana-worker/src/lakana_worker/launcher/task_build.py",
        ):
            archive.add(REPO / relative, arcname=relative)
    context.seek(0)
    client.images.build(
        fileobj=context,
        custom_context=True,
        dockerfile="Dockerfile.task",
        buildargs={"PYTHON": PYTHON},
        tag=tag,
        labels=LABELS,
        rm=True,
    )
    return tag


def _tar(entries: list[Entry]) -> bytes:
    """Pack entries for `put_archive`, with their owners and modes.

    Parameters
    ----------
    entries
        ``(path, kind, content, mode, uid)``.

    Returns
    -------
    bytes
        The archive.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path, kind, content, mode, uid in entries:
            info = tarfile.TarInfo(path)
            info.uid = info.gid = uid
            info.mode = mode
            data = None
            if kind == "dir":
                info.type = tarfile.DIRTYPE
            else:
                payload = content if isinstance(content, bytes) else content.encode()
                info.size = len(payload)
                data = io.BytesIO(payload)
            archive.addfile(info, data)
    return buffer.getvalue()


@dataclass
class Stack:
    """The stack of one test module (or of one test that needs a launcher of its own).

    Parameters
    ----------
    client
        The Docker client.
    image
        The task image.
    name
        The stack (Compose project) name.
    root
        A directory under the test's basetemp: the launcher's state.
    launcher
        The started launcher.
    """

    client: Any
    image: str
    name: str
    root: Path
    launcher: Launcher | None = None

    @property
    def labels(self) -> dict[str, str]:
        """The labels of every object the test itself creates for this stack."""
        return {**LABELS, "lakana-test-stack": self.name}

    @property
    def work_volume(self) -> str:
        """The work volume's name."""
        return f"{self.name}-task-work"

    def put(self, volume: str, entries: list[Entry]) -> None:
        """Write entries into a volume through a stopped helper container.

        Parameters
        ----------
        volume
            The volume.
        entries
            As `_tar` takes them, relative to the volume's root.
        """
        helper = self.client.containers.create(
            self.image,
            ["true"],
            labels=self.labels,
            volumes={volume: {"bind": "/v", "mode": "rw"}},
        )
        try:
            helper.put_archive("/v", _tar(entries))
        finally:
            helper.remove(force=True)

    def list_volume(self, volume: str, *, user: str) -> list[str]:
        """List every path in a volume, as a user who can read it.

        Parameters
        ----------
        volume
            The volume.
        user
            ``uid:gid`` to list it as.

        Returns
        -------
        list of str
            Paths relative to the volume's root.
        """
        output = self.client.containers.run(
            self.image,
            ["find", "/v", "-mindepth", "1"],
            user=user,
            labels=self.labels,
            volumes={volume: {"bind": "/v", "mode": "ro"}},
            remove=True,
        )
        return [line.removeprefix("/v/") for line in output.decode().split("\n") if line]

    def attempt(
        self,
        script: str,
        *,
        code_link: str | None = None,
        task_link: str | None = None,
        files: dict[str, bytes] | None = None,
        trust: bytes | None = None,
    ) -> str:
        """Lay out an attempt directory whose code is one probe script.

        Parameters
        ----------
        script
            The probe, run as the task (``code/probe.py``).
        code_link
            Make ``code`` a symbolic link to this target instead.
        task_link
            Make ``task`` a symbolic link to this target instead.
        files
            More files for ``code``, path to content (directories are made, read-only).
        trust
            A trust bundle: written as ``trust/trust.pem`` (0644, in a 0755 directory, both the
            task user's) the way `lakana_worker.trust.install_trust` lays it out.

        Returns
        -------
        str
            The attempt directory's name.
        """
        name = f"attempt-{uuid.uuid4().hex[:12]}"
        entries: list[Entry] = [(name, "dir", b"", 0o700, 10001)]
        links = {"code": code_link, "task": task_link}
        if code_link is None:
            entries += [
                (f"{name}/code", "dir", b"", 0o555, 10001),
                (f"{name}/code/probe.py", "file", script, 0o444, 10001),
            ]
            directories = sorted({str(Path(path).parent) for path in files or {}} - {"."}, key=len)
            entries += [(f"{name}/code/{d}", "dir", b"", 0o555, 10001) for d in directories]
            entries += [
                (f"{name}/code/{path}", "file", data, 0o444, 10001)
                for path, data in (files or {}).items()
            ]
        if trust is not None:
            entries += [
                (f"{name}/trust", "dir", b"", 0o755, 10001),
                (f"{name}/trust/{TRUST_FILE}", "file", trust, 0o644, 10001),
            ]
        if task_link is None:
            entries += [
                (f"{name}/task", "dir", b"", 0o700, 10001),
                (f"{name}/task/home", "dir", b"", 0o700, 10001),
                (f"{name}/task/tmp", "dir", b"", 0o700, 10001),
            ]
        self.put(self.work_volume, entries)
        for part, target in links.items():
            if target is not None:
                self.link(f"{name}/{part}", target)
        return name

    def link(self, path: str, target: str) -> None:
        """Make a symbolic link in the work volume with `ln -s` in a short-lived container.

        Not through `put_archive`: newer engines (API 1.48) refuse an archive whose link points
        out of the extraction directory ("invalid symlink"), which is the very link a test needs.

        Parameters
        ----------
        path
            The link, relative to the volume's root; its directory is the task user's.
        target
            What it points to, as written.
        """
        self.client.containers.run(
            self.image,
            ["ln", "-s", target, f"/v/{path}"],
            user="10001:10001",
            labels=self.labels,
            volumes={self.work_volume: {"bind": "/v", "mode": "rw"}},
            remove=True,
        )

    def run(
        self,
        attempt: str,
        *,
        memory_mb: int = 256,
        cpus: float = 1.0,
        pids: int = 128,
        timeout: int = 60,
        task: str | None = None,
        environment: str = KEY,
        trust: bool = False,
    ) -> tuple[dict[str, object], bytes]:
        """Run an attempt's probe as a task through the launcher.

        Parameters
        ----------
        attempt
            The attempt directory.
        memory_mb
            Its memory.
        cpus
            Its CPUs.
        pids
            Its process count.
        timeout
            Its timeout.
        task
            Its id; a random one by default.
        environment
            The key of the built environment it runs in.
        trust
            Mount the attempt's trust bundle at ``/trust`` (`Stack.attempt`'s ``trust``).

        Returns
        -------
        tuple
            The launcher's result and the task's output.
        """
        assert self.launcher is not None
        output: list[bytes] = []
        result = self.launcher.run(
            RunRequest(
                task=task or uuid.uuid4().hex[:20],
                owner="test",
                python=PYTHON,
                environment=environment,
                attempt=attempt,
                limits=Limits(memory_mb, cpus, pids, timeout),
                kill_grace_seconds=2,
                stdin=b'{"asset_path": "probe.py"}',
                trust=trust,
            ),
            lambda line: output.append(base64.b64decode(str(line["log"]))),
        )
        return result, b"".join(output)

    def build(
        self,
        attempt: str,
        *,
        environment: str,
        no_build: bool,
        targets: tuple[tuple[str, int], ...] = (),
        trust: bool = False,
    ) -> tuple[dict[str, object], bytes]:
        """Build an environment from an attempt's ``code`` through the launcher.

        Parameters
        ----------
        attempt
            The attempt whose code holds `pyproject.toml`, `uv.lock` and the wheels it names.
        environment
            The environment key.
        no_build
            Wheels only, which shares the cache of the index set.
        targets
            The host:port pairs the build may reach through the egress service
            (`start_egress`); none keeps it offline.
        trust
            Mount the attempt's trust bundle at ``/trust`` (`Stack.attempt`'s ``trust``).

        Returns
        -------
        tuple
            The launcher's result and the build's output.
        """
        assert self.launcher is not None
        output: list[bytes] = []
        result = self.launcher.build(
            BuildRequest(
                build=uuid.uuid4().hex,
                owner="test",
                python=PYTHON,
                environment=environment,
                attempt=attempt,
                indexes=(),
                operator_index=None,
                targets=targets,
                no_build=no_build,
                limits=Limits(1024, 1.0, 256, 300),
                max_bytes=1 << 30,
                trust=trust,
            ),
            lambda line: output.append(base64.b64decode(str(line["log"]))),
        )
        return result, b"".join(output)

    def caches(self) -> dict[str, str]:
        """List this stack's uv caches.

        Returns
        -------
        dict
            Volume name to its ``lakana.launcher.shared`` label (``1`` shared, ``0`` private).
        """
        volumes = self.client.volumes.list(
            filters={"label": [f"{LABEL_STACK}={self.name}", f"{LABEL_KIND}=cache"]}
        )
        return {v.name: v.attrs["Labels"].get(LABEL_SHARED, "") for v in volumes}

    def labelled(self) -> list[str]:
        """List what the launcher created and did not remove.

        Returns
        -------
        list of str
            Names of containers and networks with this stack's label.
        """
        return labelled_names(self.client.api, f"{LABEL_STACK}={self.name}")


def labelled_names(api: Any, label: str) -> list[str]:
    """Name the containers and networks with a label, from the list calls' own answers.

    The high-level `containers.list` inspects every container it lists, and a container the
    launcher removes in between raises `NotFound`: the very moment a test waits for. The list
    answers already carry the names, so nothing is inspected and a removed object is simply
    not listed.

    Parameters
    ----------
    api
        The low-level client (`DockerClient.api`).
    label
        ``key=value``.

    Returns
    -------
    list of str
        Container names (without Docker's leading ``/``), then network names.
    """
    containers = [
        str(name).lstrip("/")
        for container in api.containers(all=True, filters={"label": [label]})
        for name in container.get("Names") or []
    ]
    networks = [str(network["Name"]) for network in api.networks(filters={"label": [label]})]
    return containers + networks


def host_processes(marker: str) -> list[str]:
    """List the host's processes whose command line holds a marker.

    Parameters
    ----------
    marker
        A unique word a probe passed to a process it detached.

    Returns
    -------
    list of str
        Their command lines.
    """
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                line = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
                text = line.decode(errors="replace")
                if marker in text:
                    found.append(text)
    return found


@contextlib.contextmanager
def stack(client: Any, image: str, root: Path) -> Generator[Stack]:
    """Bring up a stack: work volume, server stand-in, one environment, the launcher.

    Parameters
    ----------
    client
        The Docker client.
    image
        The task image.
    root
        The basetemp directory for the launcher's state.

    Yields
    ------
    Stack
        The stack; everything of it is removed afterwards (also a network a test made with its
        labels), also what a failing test left, and nothing of another stack.
    """
    name = f"lt{uuid.uuid4().hex[:10]}"
    current = Stack(client=client, image=image, name=name, root=root)
    socket_path = docker_socket()
    settings = LauncherSettings(
        stack=name,
        docker_socket=socket_path,
        images={PYTHON: image},
        work_volume=current.work_volume,
        socket=Path("l.sock"),
        state_dir=root / "state",
        egress_key=root / "egress.key",
        keep_environments=5,
    )
    mine = {"label": f"lakana-test-stack={name}"}
    try:
        client.volumes.create(current.work_volume, labels=current.labels)
        client.containers.run(
            image,
            ["python", "-m", "http.server", "8000"],
            detach=True,
            user="10001:10001",
            labels={
                **current.labels,
                "com.docker.compose.project": name,
                "com.docker.compose.service": "server",
            },
        )
        env = environment_volume(settings, PYTHON, KEY)
        client.volumes.create(env, labels=current.labels)
        client.containers.run(
            image,
            ["python", "-m", "venv", "--without-pip", "/env"],
            user="10002:10002",
            volumes={env: {"bind": "/env", "mode": "rw"}},
            labels=current.labels,
            remove=True,
        )
        site = f"lib/python{PYTHON}/site-packages/lakana"
        current.put(
            env,
            [
                (site, "dir", b"", 0o755, 10002),
                (f"{site}/__init__.py", "file", "", 0o644, 10002),
                (f"{site}/runner.py", "file", STUB_HARNESS, 0o644, 10002),
            ],
        )
        current.launcher = Launcher(settings, DockerEngine(socket_path))
        current.launcher.start()
        (root / "state" / "environments" / f"{env}.complete").write_text(env)
        yield current
    finally:
        for container in client.containers.list(all=True, filters=mine, ignore_removed=True):
            with contextlib.suppress(Exception):
                container.remove(force=True)
        if current.launcher is not None:
            with contextlib.suppress(Exception):
                # What a failing test left: the launcher's own start-up sweep, run once more.
                current.launcher._remove_labelled({LABEL_STACK: name}, spare_live=False)
        for wanted in (mine, {"label": f"{LABEL_STACK}={name}"}):
            for volume in client.volumes.list(filters=wanted):
                with contextlib.suppress(Exception):
                    volume.remove(force=True)
        for network in client.networks.list(filters=mine):
            with contextlib.suppress(Exception):
                network.remove()


def serve(launcher: Launcher) -> tuple[LauncherServer, threading.Thread]:
    """Serve a started launcher on `l.sock` in the working directory.

    Parameters
    ----------
    launcher
        The launcher.

    Returns
    -------
    tuple
        The server and its thread; `server.shutdown()` stops it.
    """
    token = write_token(Path("."))
    server = LauncherServer("l.sock", launcher=launcher, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def make_wheel(name: str, version: str, files: dict[str, str]) -> tuple[str, bytes]:
    """Make a pure-Python wheel.

    Parameters
    ----------
    name
        The distribution name.
    version
        Its version.
    files
        Path in the wheel to text.

    Returns
    -------
    tuple
        The wheel's file name and its bytes.
    """
    dist = f"{name.replace('-', '_')}-{version}"
    entries = {
        **files,
        f"{dist}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: lakana-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = []
    for path, text in entries.items():
        digest = hashlib.sha256(text.encode()).digest()
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        record.append(f"{path},sha256={encoded},{len(text.encode())}")
    record.append(f"{dist}.dist-info/RECORD,,")
    entries[f"{dist}.dist-info/RECORD"] = "\n".join(record) + "\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, text in entries.items():
            archive.writestr(path, text)
    return f"{dist}-py3-none-any.whl", buffer.getvalue()


def _package() -> bytes:
    """Pack the launcher package for a container that runs the proxy from source.

    Returns
    -------
    bytes
        A tar of `lakana_worker/__init__.py` and `lakana_worker/launcher/`.
    """
    buffer = io.BytesIO()
    root = Path(lakana_worker.launcher.__file__).parent
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(root.parent / "__init__.py", arcname="lakana_worker/__init__.py")
        for path in root.glob("*.py"):
            archive.add(path, arcname=f"lakana_worker/launcher/{path.name}")
    return buffer.getvalue()


def _key_archive(key: bytes) -> bytes:
    """Pack the launcher's key file, readable by the proxy's user only.

    Parameters
    ----------
    key
        The key file's content.

    Returns
    -------
    bytes
        A tar of `egress.key`.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("egress.key")
        info.size, info.mode, info.uid, info.gid = len(key), 0o400, 10001, 10001
        archive.addfile(info, io.BytesIO(key))
    return buffer.getvalue()


def start_egress(stack: Stack, *, network: str | None = None, reachable: str | None = None) -> Any:
    """Start the stack's egress service, run from this checkout's proxy.

    Labelled as the stack's ``egress`` Compose service, so the launcher attaches it to every
    build network; it reads the launcher's key from a copy in its container.

    Parameters
    ----------
    stack
        A started stack.
    network
        A network to create the container on instead of Docker's default bridge.
    reachable
        A network (CIDR) the proxy may connect to although it is attached to it, which it
        otherwise refuses: `EgressSettings.reachable`, which is not a setting, so the entry sets
        it in code.

    Returns
    -------
    docker.models.containers.Container
        The started container; the stack removes it.
    """
    assert stack.launcher is not None
    key = stack.launcher.settings.egress_key.read_bytes()
    prefix = f"import sys; sys.path.insert(0, {APP!r}); "
    if reachable is None:
        entry = prefix + "from lakana_worker.launcher.egress import main; main()"
    else:
        entry = prefix + (
            "import dataclasses, ipaddress, os; "
            "from lakana_worker.launcher.egress import EgressServer, load_settings; "
            "settings = dataclasses.replace(load_settings(os.environ), "
            f"reachable=frozenset({{ipaddress.ip_network({reachable!r})}})); "
            "EgressServer(settings).serve_forever()"
        )
    egress = stack.client.containers.create(
        stack.image,
        ["python", "-I", "-S", "-c", entry],
        user="10001:10001",
        environment={"LAKANA_EGRESS__KEY": f"{APP}/egress.key"},
        network=network,
        labels={
            **stack.labels,
            "com.docker.compose.project": stack.name,
            "com.docker.compose.service": "egress",
        },
    )
    egress.put_archive(APP, _package())
    egress.put_archive(APP, _key_archive(key))
    egress.start()
    return egress
