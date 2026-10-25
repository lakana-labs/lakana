"""The launcher's operations against Docker: run, build, stop, status and cleanup.

`Launcher` creates a network per task or build (internal, no gateway on the host), a container
from the specs of `lakana_worker.launcher.requests`, streams the container's output to the
caller, and removes both at the end, on a stop, on the timeout, and when the caller's connection
closes. At start it checks the engine, finds out whether `no-new-privileges` works, and removes
every container and network that carries its stack label: their owners run them again.

Everything the launcher creates carries the label ``lakana.launcher.stack=<project>``. To
remove all of it by hand (an uninstall, or after a worker was compromised)::

    docker rm -f -v $(docker ps -aq --filter label=lakana.launcher.stack=<project>)
    docker network rm $(docker network ls -q --filter label=lakana.launcher.stack=<project>)
    docker volume rm $(docker volume ls -q --filter label=lakana.launcher.stack=<project>)

Each container and network also carries ``lakana.launcher.deadline`` (epoch seconds); a reaper
thread removes what outlives it, whether or not a run still supervises it.

Not limited: the disk a task, a build or a cache may fill (Docker volumes have no quota on
every storage driver), and nothing evicts a cache beyond the age limit of the reaper.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

from lakana_worker.launcher.egress import PORT as EGRESS_PORT
from lakana_worker.launcher.egress_token import mint, rotate_key
from lakana_worker.launcher.engine import API_VERSION, Engine, EngineError
from lakana_worker.launcher.requests import (
    DEADLINE_MARGIN,
    EGRESS_MISSING,
    LABEL_DEADLINE,
    LABEL_KIND,
    LABEL_OWNER,
    LABEL_SHARED,
    LABEL_STACK,
    BuildRequest,
    CleanupRequest,
    Refused,
    Request,
    RunRequest,
    StatusRequest,
    StopRequest,
    build_container_spec,
    build_targets,
    cache_volume,
    container_name,
    environment_volume,
    ingestion_probe_spec,
    network_spec,
    probe_container_spec,
    run_container_spec,
    volume_spec,
)
from lakana_worker.launcher.settings import LauncherSettings

logger = logging.getLogger("lakana_worker.launcher")

MIN_ENGINE_MAJOR = 28
"""The first Docker Engine with `gateway_mode_ipv4=isolated`."""

Emit = Callable[[dict[str, object]], None]
"""Sends one answer line to the caller; raises `OSError` when the caller is gone."""

_STOPPED_BEFORE_START: dict[str, object] = {
    "exit": None,
    "oom_killed": False,
    "timed_out": False,
    "stopped": True,
}
"""The result of a run whose caller left, or that was stopped, before its container started."""
_PULL_SECONDS = 600.0
_PROBE_SECONDS = 300.0
_SIGKILL_EXIT = 128 + 9
"""The exit status Docker's init reports for a task process killed by SIGKILL."""
_WAIT_MARGIN = 60.0
"""Seconds past a run's timeout and grace that the wait for its container may last."""
_SUBNET_TRIES = 8
"""How many subnets are tried when Docker says one overlaps an address pool of its own."""
_LOCK_POLL = 0.2
"""Seconds between looks at a queued build's stop while it waits for its environment's lock."""
_REST_MARGIN = 60
"""Seconds past its last token's expiry that a build's subnet stays out of use."""
_CACHE_MAX_AGE = 14 * 86400
"""Seconds after which an idle shared cache is removed; the next build fills a new one."""
_MAX_FRAME_PIECE = 1 << 20
"""The most one read of a container's output takes, whatever size a frame header claims."""
_SPECIAL_MOUNT = 3
_SOCKET_FOUND = 4
_TOO_MANY = 5
_UNREADABLE = 6
"""Exit statuses of the ingestion-root probe."""


_KEY_TEMPORARY = re.compile(r"\.[0-9a-f]{16}\.tmp\Z")
_STALE_TEMPORARY_SECONDS = 60.0
"""How old a temporary key file must be before a start removes it as a crash's leftover."""


def _sweep_key_temporaries(directory: Path, name: str) -> None:
    """Remove the temporary key files a launcher killed while writing `cache.key` left behind.

    Only names of the form ``<name>.<16 hex>.tmp`` older than `_STALE_TEMPORARY_SECONDS` are
    removed: a younger one may belong to a launcher starting next to this one on the same
    volume, which links it in a moment.

    Parameters
    ----------
    directory
        The launcher's state directory.
    name
        The key file's name.
    """
    stale_before = time.time() - _STALE_TEMPORARY_SECONDS
    with contextlib.suppress(OSError):
        for entry in os.scandir(directory):
            suffix = entry.name[len(name) :] if entry.name.startswith(name) else ""
            if _KEY_TEMPORARY.match(suffix) is None:
                continue
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_mtime < stale_before:
                    os.unlink(entry.path)


def _sync_directory(directory: Path) -> None:
    """Flush a directory's entries to disk, so a link made in it survives a crash.

    Parameters
    ----------
    directory
        The directory.
    """
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class StartError(Exception):
    """The launcher cannot run on this Docker; the text says what to change."""


@dataclass
class _Live:
    """A run or build the launcher supervises, from its registration to its end.

    Parameters
    ----------
    owner
        The worker that started it: only its `stop` and its closed connection end the run.
    grace
        Seconds between SIGTERM and SIGKILL on a stop.
    memory_mb
        The memory this run asked for, held against the total ceiling.
    container_id
        Docker's id, once the container exists.
    stopped
        Set by `stop` and `abandon`; checked before every step that cannot be undone.
    timed_out
        Set by the timer.
    """

    owner: str
    grace: int
    memory_mb: int = 0
    container_id: str | None = None
    stopped: threading.Event = field(default_factory=threading.Event)
    timed_out: threading.Event = field(default_factory=threading.Event)


def _expired(labels: object, moment: float) -> bool:
    """Say whether a container's or network's deadline label has passed.

    Parameters
    ----------
    labels
        Its labels.
    moment
        Epoch seconds to measure against.

    Returns
    -------
    bool
        `False` without a deadline label or with one that is not a number.
    """
    raw = labels.get(LABEL_DEADLINE) if isinstance(labels, dict) else None
    try:
        return raw is not None and int(raw) < moment
    except ValueError:
        return False


def _same_owner(live: _Live, owner: str) -> bool:
    """Say whether a request's owner is the owner of a registered run.

    Parameters
    ----------
    live
        The registration.
    owner
        The owner a request named.

    Returns
    -------
    bool
        Compared in constant time: the owner is the only thing that sets one worker's runs apart
        from another's.
    """
    return hmac.compare_digest(live.owner.encode("utf-8"), owner.encode("utf-8"))


def _deadline(seconds: int) -> int:
    """Compute the epoch second after which the reaper removes a run's container and network.

    Parameters
    ----------
    seconds
        The run's timeout plus its kill grace.

    Returns
    -------
    int
        Now plus `seconds` and the margin.
    """
    return int(time.time()) + seconds + DEADLINE_MARGIN


def _message(body: object) -> str:
    """Pick Docker's own message out of an error body, bounded.

    Parameters
    ----------
    body
        The decoded body.

    Returns
    -------
    str
        At most 300 characters.
    """
    text = body.get("message") if isinstance(body, dict) else None
    return str(text if text else "no message")[:300]


class _Receiver(Protocol):
    """Anything that hands out bytes the way a socket does."""

    def recv(self, size: int, /) -> bytes:
        """Return up to `size` bytes, or nothing at the end.

        Parameters
        ----------
        size
            The most to return.

        Returns
        -------
        bytes
            The next bytes.
        """
        ...


def _send_input(stream: socket.socket, data: bytes) -> None:
    """Write a task's input and close the write side.

    A task that exited at once leaves nothing to read its input: its exit status says so, so a
    broken pipe is not an error here. Closing the stream ends the write too.

    Parameters
    ----------
    stream
        The attached stream.
    data
        The input.
    """
    with contextlib.suppress(OSError):
        stream.sendall(data)
        stream.shutdown(socket.SHUT_WR)


def _mtime(path: Path) -> float:
    """Read a file's modification time, 0 when it is gone.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    float
        Seconds since the epoch; 0 for a file removed meanwhile (the oldest).
    """
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _exact(stream: _Receiver, size: int) -> bytes | None:
    """Read exactly `size` bytes, or `None` at the end of the stream.

    Parameters
    ----------
    stream
        The attached stream.
    size
        How many bytes.

    Returns
    -------
    bytes or None
        The bytes.
    """
    data = b""
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_frames(stream: _Receiver) -> Iterator[bytes]:
    """Yield the payloads of Docker's multiplexed stdout and stderr frames.

    Parameters
    ----------
    stream
        The attached stream.

    Yields
    ------
    bytes
        A frame's payload, in pieces of at most 1 MiB: the size in a header is not trusted with
        one allocation.
    """
    while (header := _exact(stream, 8)) is not None:
        remaining = struct.unpack(">I", header[4:])[0]
        while remaining:
            payload = _exact(stream, min(remaining, _MAX_FRAME_PIECE))
            if payload is None:
                return
            remaining -= len(payload)
            yield payload


class Launcher:
    """Runs tasks and builds in containers, from parsed requests.

    Parameters
    ----------
    settings
        The allow-lists, ceilings and names.
    engine
        The Docker Engine client.
    """

    def __init__(self, settings: LauncherSettings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.no_new_privileges = settings.no_new_privileges == "on"
        self._live: dict[str, _Live] = {}
        self._abandoned: set[tuple[str, str]] = set()
        self.unusable_roots: dict[str, str] = {}
        self._lock = threading.Lock()
        self._build_locks: dict[str, threading.Lock] = {}
        self._pool = ipaddress.IPv4Network(settings.network_range)
        self._max_networks = self._pool.num_addresses // 8 * 3 // 4
        self._subnets: dict[str, str] = {}
        self._used_subnets: set[str] = set()
        self._resting: dict[str, float] = {}
        self._token_expiry: dict[str, int] = {}
        self._reserved_mb = 0
        self._key: bytes | None = None
        self._egress_key: bytes | None = None
        self._reaper_stop = threading.Event()

    # --- start-up -----------------------------------------------------------------------

    def start(self) -> None:
        """Check the engine and the ingestion roots, clear what an earlier launcher left.

        Raises
        ------
        StartError
            If the engine is older than `MIN_ENGINE_MAJOR` or cannot make an isolated network,
            the option is ``on`` and does not work here, or an ingestion root fails its probe.
        EngineError
            If Docker cannot be reached.
        """
        status, version = self.engine.request("GET", "/version")
        text = str(version.get("Version", "")) if isinstance(version, dict) else ""
        head = text.split(".")[0]
        if status == 400:
            raise StartError(
                f"Docker does not accept API version {API_VERSION.removeprefix('/v')}, the one "
                f"this release of Lakana speaks (Docker Engine {MIN_ENGINE_MAJOR} or later): "
                f"{self.redact(_message(version))}. API version 1.47 is not supported by this "
                "Docker; upgrade Lakana to a release that supports it, or use a Docker Engine "
                "that still serves this API version."
            )
        if status != 200:
            raise StartError(
                f"Docker answered status {status} to the version request: "
                f"{self.redact(_message(version))}. Check that the socket mounted into the "
                "launcher is Docker's, and `docker compose logs launcher`."
            )
        if not head.isdigit() or int(head) < MIN_ENGINE_MAJOR:
            raise StartError(
                f"Docker Engine {MIN_ENGINE_MAJOR} or later is needed: it can keep a task's "
                "network away from the host (gateway_mode_ipv4=isolated). This engine reports "
                f"'{text or 'no version'}'; upgrade Docker."
            )
        self._check_socket_group()
        try:
            removed = self._remove_labelled({LABEL_STACK: self.settings.stack}, spare_live=False)
        except EngineError as exc:
            raise StartError(
                f"cannot list the containers of stack '{self.settings.stack}' ({exc}); "
                "tasks an earlier launcher left may still be running, so the launcher does not "
                "start. Check `docker compose logs launcher` and the Docker daemon."
            ) from None
        self._recover_subnets()
        if removed:
            logger.info(
                "Removed %d containers an earlier launcher left; their workers run them again.",
                removed,
            )
        self._check_isolated_network(text)
        works = self._probe_no_new_privileges()
        if self.settings.no_new_privileges == "on" and not works:
            raise StartError(
                "LAKANA_LAUNCHER__NO_NEW_PRIVILEGES is on, but this Docker cannot start a "
                "container with no-new-privileges (Docker installed as a snap refuses every "
                "one). Set it to auto, which leaves the option off where it breaks."
            )
        self.no_new_privileges = works
        logger.info(
            "no-new-privileges is %s for task and build containers.",
            "on" if works else "off: this Docker refuses it",
        )
        (self.settings.state_dir / "environments").mkdir(mode=0o700, parents=True, exist_ok=True)
        self._key = self._load_cache_key()
        try:
            # A new key at every start: no token of an earlier launcher stays valid, so the
            # subnets it kept resting (in its memory only) are safe to hand out again.
            self._egress_key = rotate_key(self.settings.egress_key)
        except OSError as exc:
            raise StartError(
                f"cannot write the egress proxy's key {self.settings.egress_key} "
                f"({exc.strerror or type(exc).__name__}); mount the egress-key volume into the "
                "launcher, writable (see compose.yaml)."
            ) from None
        self._check_ingestion_roots()

    def _check_isolated_network(self, version: str) -> None:
        """Make an isolated network and see that Docker kept the mode, then remove it.

        An engine that does not know the mode may accept the network and drop the option; the
        task would then reach the host's services through the network's gateway.

        Parameters
        ----------
        version
            The engine's version, for the message.

        Raises
        ------
        StartError
            If the network cannot be made, or Docker did not keep the option.
        """
        spec = network_spec(self.settings, "probe", "isolated-check", "launcher")
        name = str(spec["Name"])
        status, body = self.engine.request("POST", "/networks/create", spec)
        if status != 201:
            raise StartError(
                "Docker cannot create an isolated network "
                f"(gateway_mode_ipv4=isolated): {_message(body)}. Docker Engine {version} is "
                f"running; Docker Engine {MIN_ENGINE_MAJOR} or later is needed."
            )
        try:
            _, info = self.engine.request("GET", f"/networks/{quote(name, safe='')}")
            options = info.get("Options") if isinstance(info, dict) else None
            if not isinstance(options, dict) or (
                options.get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
            ):
                raise StartError(
                    "Docker created a network but did not keep gateway_mode_ipv4=isolated, so a "
                    f"task could reach the host through its gateway. Docker Engine {version} is "
                    f"running; Docker Engine {MIN_ENGINE_MAJOR} or later is needed."
                )
        finally:
            self.close_network(name)

    def _run_probe(self, name: str, spec: dict[str, Any]) -> tuple[int | None, str]:
        """Create, start and wait for a probe container, then remove it.

        Parameters
        ----------
        name
            The probe's name.
        spec
            Its spec.

        Returns
        -------
        tuple
            The container's exit status (`None` when it did not start or Docker gave none) and
            Docker's message when it did not start (empty otherwise).

        Raises
        ------
        EngineError
            If Docker refuses the container or cannot be reached.
        """
        with contextlib.suppress(EngineError):
            self.engine.request(
                "DELETE", f"/containers/{quote(name, safe='')}", query={"force": "1", "v": "1"}
            )
        cid = self._create(name, spec)
        try:
            status, body = self.engine.request("POST", f"/containers/{cid}/start")
            if status != 204:
                return None, _message(body)
            _, waited = self.engine.request(
                "POST", f"/containers/{cid}/wait", timeout=_PROBE_SECONDS
            )
            code = waited.get("StatusCode") if isinstance(waited, dict) else None
            return (code if isinstance(code, int) else None), ""
        finally:
            self._remove_container(cid)

    def _probe_image(self) -> str:
        """Pick the image the probes run in, pulling it if the host has not got it.

        Returns
        -------
        str
            The reference.
        """
        image = self.settings.images[min(self.settings.images)]
        self._ensure_image(image)
        return image

    def _probe_no_new_privileges(self) -> bool:
        """Find out whether Docker starts a container with the option.

        A probe with the option that does not exit 0 is followed by a control without it: only
        when the control runs does the failure belong to the option (Docker installed as a snap
        refuses every exec with it). If the control fails too, the image or the host is at fault.

        Returns
        -------
        bool
            Whether the option works on this Docker.

        Raises
        ------
        StartError
            If not even the control container runs.
        """
        image = self._probe_image()
        name = container_name(self.settings, "probe", "no-new-privileges")
        code, _ = self._run_probe(name, probe_container_spec(self.settings, image))
        if code == 0:
            return True
        control = container_name(self.settings, "probe", "no-new-privileges-control")
        control_code, detail = self._run_probe(
            control, probe_container_spec(self.settings, image, no_new_privileges=False)
        )
        if control_code != 0:
            raise StartError(
                f"Docker cannot start a container at all from the task image {image} "
                f"({detail or f'exit status {control_code}'}); check that the image runs "
                "(`docker run --rm <image> python -c pass`) and `docker compose logs launcher`."
            )
        logger.warning(
            "Docker refuses no-new-privileges (a container with the option does not run, one "
            "without it does); task and build containers run without it."
        )
        return False

    @property
    def _cache_key(self) -> bytes:
        """The key that keeps credentials apart in cache names.

        Returns
        -------
        bytes
            The 32-byte key `start` loaded.

        Raises
        ------
        RuntimeError
            If the launcher has not started: an empty key would make cache names guessable.
        """
        if self._key is None:
            raise RuntimeError("the cache key is only available after start()")
        return self._key

    def _load_cache_key(self) -> bytes:
        """Read the key that keeps credentials apart in cache names, making it on first use.

        The key never leaves the launcher: only HMACs made with it reach a Docker volume name.
        A new key is written to a temporary file (mode 0600, flushed to disk) and linked to
        `cache.key` only when whole, so the file never exists half written and two launchers
        racing on one volume agree on one key. The file is read back with the same strictness
        on every start. Fail closed: a file that is not a regular file of this user, is
        accessible to group or others, or does not hold exactly 32 bytes (planted, truncated by
        hand or by a full disk under an older launcher) is refused, never repaired or replaced,
        because an operator must decide whether the old key leaked; deleting it makes the next
        start create a new one.

        Returns
        -------
        bytes
            32 random bytes, from `cache.key` in the launcher's state directory (mode 0600).

        Raises
        ------
        StartError
            If the file exists and is not a private regular file of 32 bytes.
        """
        path = self.settings.state_dir / "cache.key"
        _sweep_key_temporaries(self.settings.state_dir, path.name)
        try:
            return self._read_cache_key(path)
        except FileNotFoundError:
            pass
        key = secrets.token_bytes(32)
        temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)
        except FileExistsError:
            return self._read_cache_key(path)
        finally:
            temporary.unlink(missing_ok=True)
        _sync_directory(self.settings.state_dir)
        return key

    @staticmethod
    def _read_cache_key(path: Path) -> bytes:
        """Read `cache.key`, refusing anything but a private regular file of 32 bytes.

        Parameters
        ----------
        path
            The key file.

        Returns
        -------
        bytes
            The key.

        Raises
        ------
        FileNotFoundError
            If there is no such file.
        StartError
            If the file is a link, not a regular file, owned by another user, accessible to
            group or others, or not 32 bytes long.
        """
        fix = "delete it and restart (the shared caches then start empty)"
        try:
            # O_NONBLOCK: a FIFO planted here would otherwise block the open until a writer came.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                reason = "is a symbolic link"
            elif exc.errno in (errno.EACCES, errno.EPERM):
                reason = "cannot be read by the launcher's user (permission denied)"
            else:
                reason = f"cannot be opened ({exc.strerror or type(exc).__name__})"
            raise StartError(f"{path} {reason}; {fix}.") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise StartError(f"{path} is not a regular file; {fix}.")
            if info.st_uid != os.getuid():
                raise StartError(
                    f"{path} is owned by user {info.st_uid}, not by the launcher; {fix}."
                )
            if info.st_mode & 0o077:
                raise StartError(
                    f"{path} is accessible to group or others (mode "
                    f"{stat.S_IMODE(info.st_mode):04o}); {fix}."
                )
            key = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if len(key) != 32:
            raise StartError(f"{path} does not hold the 32 bytes of the cache key; {fix}.")
        return key

    def _check_socket_group(self) -> None:
        """Refuse a Docker socket whose group is one tasks or builds run in.

        Raises
        ------
        StartError
            If the socket's group is the group of the task or the build user.
        """
        try:
            info = self.settings.docker_socket.stat()
        except OSError:
            return  # the engine check that follows names an unreachable socket
        gid = info.st_gid
        if info.st_mode & 0o002:
            raise StartError(
                f"The Docker socket {self.settings.docker_socket} is writable by everyone, which "
                "is root on the host for every user of the machine. Restrict it to the docker "
                "group (`chmod 660`, owned by root:docker) on the host."
            )
        users = (self.settings.task_user, self.settings.build_user)
        if gid in {int(user.split(":")[1]) for user in users}:
            raise StartError(
                f"The group of the Docker socket ({gid}) is a group that task or build "
                "containers run in, so a task that ever saw the socket could use it. Give the "
                "Docker group another id on the host."
            )

    def _check_ingestion_roots(self) -> None:
        """Look at every allowed ingestion root from inside a container, as a task would.

        A root that fails is recorded in `unusable_roots` with the reason; the launcher starts
        anyway, and only requests that name the root are refused.
        """
        self.unusable_roots = {}
        for root in sorted(self.settings.ingestion_roots):
            problem = self._root_problem(root)
            if problem is not None:
                self.unusable_roots[root] = problem
                logger.warning("%s The launcher refuses requests that name it.", problem)

    def _root_problem(self, root: str) -> str | None:
        """Probe one ingestion root.

        Parameters
        ----------
        root
            The root's name.

        Returns
        -------
        str or None
            Why the root cannot be used (naming it, never its host path), or `None`.
        """
        image = self._probe_image()
        name = container_name(self.settings, "probe", f"ingest-{root}")
        spec = ingestion_probe_spec(
            self.settings, image, root, no_new_privileges=self.no_new_privileges
        )
        setting = "(LAKANA_LAUNCHER__INGESTION_ROOTS)"
        try:
            code, refusal = self._run_probe(name, spec)
        except EngineError as exc:
            return (
                f"cannot check ingestion root '{root}': {self.redact(str(exc))}; check that its "
                f"directory exists on the host {setting}"
            )
        if refusal:
            return (
                f"cannot check ingestion root '{root}': Docker could not start the probe: "
                f"{self.redact(refusal)}; check that its directory exists on the host {setting}"
            )
        if code == _SPECIAL_MOUNT:
            return (
                f"ingestion root '{root}' is, or holds, a proc, sysfs or devtmpfs mount; a task "
                f"would see the host through it. Choose a plain data directory {setting}, not a "
                "link to one."
            )
        if code == _SOCKET_FOUND:
            return (
                f"ingestion root '{root}' holds a socket, pipe or device node; a read-only mount "
                "does not stop a task from using it. Move it out of the directory or choose "
                f"another one {setting}."
            )
        if code == _TOO_MANY:
            return (
                f"ingestion root '{root}' has more than {self.settings.probe_max_entries} "
                "entries, so it was not checked for sockets. Raise "
                "LAKANA_LAUNCHER__PROBE_MAX_ENTRIES, or point the root at a smaller directory "
                f"{setting}."
            )
        if code == _UNREADABLE:
            return (
                f"ingestion root '{root}' holds something the task user cannot list or examine, "
                "so it cannot be checked for sockets. `lost+found` at the top of a filesystem is "
                "the usual cause: point the root at a subdirectory, or make the directories "
                f"readable by uid 10001 {setting}."
            )
        if code != 0:
            return (
                f"ingestion root '{root}' could not be checked (the probe ended with "
                f"{code if code is not None else 'no exit status'}); check its permissions and "
                "`docker compose logs launcher`"
            )
        return None

    # --- operations ---------------------------------------------------------------------

    def handle(self, request: Request, emit: Emit) -> dict[str, object]:
        """Run one parsed request.

        Parameters
        ----------
        request
            The request.
        emit
            Sends log lines to the caller.

        Returns
        -------
        dict
            The result line's value.
        """
        if isinstance(request, RunRequest):
            return self.run(request, emit)
        if isinstance(request, BuildRequest):
            return self.build(request, emit)
        if isinstance(request, StopRequest):
            return self.stop(request)
        if isinstance(request, StatusRequest):
            return self.status(request)
        return self.cleanup(request)

    def run(self, request: RunRequest, emit: Emit) -> dict[str, object]:
        """Run one task attempt to its end.

        A run with ``egress`` gets the egress proxy on its network and a token for exactly those
        pairs in `LAKANA_EGRESS_PROXY`; a run with ``relay`` gets its relay container first. A
        run with neither reaches the server alone.

        Parameters
        ----------
        request
            The run.
        emit
            Sends ``{"log": <base64>}`` lines; called from one thread at a time.

        Returns
        -------
        dict
            ``exit``, ``oom_killed``, ``timed_out``, ``stopped``.

        Raises
        ------
        Refused
            If the run names an ingestion root that failed its start-up probe, has egress or
            relay routes while the egress service is not running (`EGRESS_MISSING`), or its
            pairs do not fit in a token; nothing is left behind.
        EngineError
            If Docker refuses a step, the relay does not start or the environment is not built;
            the containers and the network are removed first.
        RuntimeError
            If a run with egress reaches a launcher that has not started.
        Exception
            Whatever `emit` raises when the caller is gone; likewise.
        """
        for root in request.roots:
            if root in self.unusable_roots:
                raise Refused(self.unusable_roots[root])
        if (request.egress or request.relay) and not self._running(self.settings.egress_service):
            raise Refused(EGRESS_MISSING)
        if request.egress and self._egress_key is None:
            raise RuntimeError("the egress key is only available after start()")
        name = container_name(self.settings, "task", request.task)
        live = self._register(
            name, request.owner, request.kill_grace_seconds, request.limits.memory_mb
        )
        deadline = _deadline(request.limits.timeout_seconds + request.kill_grace_seconds)
        try:
            volume = environment_volume(self.settings, request.python, request.environment)
            if not self._built(volume):
                raise self._not_built(request.python, request.environment)
            self._touch(volume)
            if live.stopped.is_set():
                return dict(_STOPPED_BEFORE_START)
            services = [(self.settings.server_service, self.settings.server_alias)]
            if request.egress:
                services.append((self.settings.egress_service, "egress"))
            network = self._open_network("task", request.task, request.owner, services, deadline)
            try:
                proxy_url = (
                    self._proxy_url(
                        network,
                        request.egress,
                        request.limits.timeout_seconds + request.kill_grace_seconds,
                        "task",
                    )
                    if request.egress
                    else None
                )
                spec = run_container_spec(
                    request,
                    self.settings,
                    no_new_privileges=self.no_new_privileges,
                    deadline=deadline,
                    proxy_url=proxy_url,
                )
                self._ensure_image(spec["Image"])
                # A pull takes minutes; a prune or an eviction in that time must not let Docker
                # create the volume empty at the mount.
                if not self._built(volume):
                    raise self._not_built(request.python, request.environment)
                return self._supervise(
                    name,
                    spec,
                    live,
                    request.limits.timeout_seconds,
                    emit,
                    stdin=request.stdin,
                )
            finally:
                self.close_network(network)
        finally:
            self.forget(name, request.owner, live)

    def _proxy_url(
        self, network: str, pairs: tuple[tuple[str, int], ...], seconds: int, purpose: str
    ) -> str:
        """Mint an egress token bound to a network and its lifetime plus a minute.

        Parameters
        ----------
        network
            The run's network, open already; its subnet rests until the token expires.
        pairs
            The host and port pairs.
        seconds
            The run's timeout and kill grace.
        purpose
            ``task`` or ``connection-test``: picks the proxy's next step in a refusal.

        Returns
        -------
        str
            ``http://lakana:<token>@egress:3128``.

        Raises
        ------
        Refused
            If the pairs do not fit in a token.
        """
        expires = int(time.time()) + seconds + 60
        with self._lock:
            subnet = self._subnets[network]
            self._token_expiry[network] = expires
        try:
            token = mint(
                cast("bytes", self._egress_key),
                pairs,
                expires,
                purpose=purpose,
                network=ipaddress.ip_network(subnet),
            )
        except ValueError as exc:
            raise Refused(f"egress: {exc}") from None
        return f"http://lakana:{token}@egress:{EGRESS_PORT}"

    @staticmethod
    def _not_built(python: str, environment: str) -> EngineError:
        """Describe a run for an environment that is not built.

        Parameters
        ----------
        python
            The Python version.
        environment
            The environment key.

        Returns
        -------
        EngineError
            The error to raise.
        """
        return EngineError(
            f"the environment {environment[:12]} for Python {python} is not built (it may have "
            "been removed); build it first"
        )

    def build(self, request: BuildRequest, emit: Emit) -> dict[str, object]:
        """Build one environment, unless it is built already; one build per environment at once.

        Parameters
        ----------
        request
            The build.
        emit
            Sends the build's output.

        Returns
        -------
        dict
            ``complete`` and, when a build ran, its ``exit``, ``oom_killed``, ``timed_out`` and
            ``stopped``.

        Raises
        ------
        EngineError
            If Docker refuses a step, or the volume of an earlier attempt cannot be removed.
        Exception
            Whatever `emit` raises when the caller is gone.
        """
        name = container_name(self.settings, "build", request.build)
        live = self._register(name, request.owner, 0, request.limits.memory_mb)
        try:
            volume = environment_volume(self.settings, request.python, request.environment)
            with self._lock:
                lock = self._build_locks.setdefault(volume, threading.Lock())
            # A build of the same environment may run for minutes: a stop or a closed connection
            # ends the wait for it, so a cancelled task does not wait for another's build.
            while not lock.acquire(timeout=_LOCK_POLL):
                if live.stopped.is_set():
                    return {**_STOPPED_BEFORE_START, "complete": False}
            try:
                return self._build_locked(request, emit, name, live, volume)
            finally:
                lock.release()
        finally:
            self.forget(name, request.owner, live)

    def _build_locked(
        self, request: BuildRequest, emit: Emit, name: str, live: _Live, volume: str
    ) -> dict[str, object]:
        """Build one environment, holding its lock.

        Parameters
        ----------
        request
            The build.
        emit
            Sends the build's output.
        name
            The build container's name.
        live
            The build's registration.
        volume
            The environment volume.

        Returns
        -------
        dict
            As `build`.

        Raises
        ------
        EngineError
            If Docker refuses a step, or an old volume cannot be removed.
        """
        if live.stopped.is_set():
            return {**_STOPPED_BEFORE_START, "complete": False}
        if self._built(volume):
            self._touch(volume)
            return {
                "complete": True,
                "exit": 0,
                "oom_killed": False,
                "timed_out": False,
                "stopped": False,
            }
        self._marker(volume).unlink(missing_ok=True)
        if not self._remove_volume(volume):
            raise EngineError(
                f"the volume of environment {request.environment[:12]} is still in use or cannot "
                "be removed; wait for the tasks that use it, or check "
                "`docker compose logs launcher`"
            )
        deadline = _deadline(request.limits.timeout_seconds)
        cache = cache_volume(self.settings, request, key=self._cache_key)
        if not request.no_build and not self._remove_volume(cache):
            # A private cache is never reused: what a dead launcher left must be gone first.
            raise EngineError(
                f"the private cache of build {request.build[:12]} from an earlier run is still "
                "in use or cannot be removed; wait for the container that mounts it to stop, "
                "or check `docker compose logs launcher`"
            )
        network: str | None = None
        try:
            self._create_volume(volume_spec(self.settings, volume, "env", shared=False))
            self._create_volume(
                volume_spec(
                    self.settings,
                    cache,
                    "cache",
                    shared=request.no_build,
                    deadline=None if request.no_build else deadline,
                )
            )
            # Pull first: the token's time starts now and must not be spent on a pull.
            self._ensure_image(self.settings.images[request.python])
            network, proxy = self.open_build_network(
                request.build,
                request.owner,
                build_targets(request),
                request.limits.timeout_seconds,
            )
            if proxy is None:
                emit(
                    {
                        "log": base64.b64encode(
                            b"lakana: the egress service is not running, so this build reaches "
                            b"no package index; start it (docker compose up -d egress)\n"
                        ).decode("ascii")
                    }
                )
            spec = build_container_spec(
                request,
                self.settings,
                no_new_privileges=self.no_new_privileges,
                proxy_url=proxy,
                cache_key=self._cache_key,
                deadline=deadline,
            )
            result = self._supervise(name, spec, live, request.limits.timeout_seconds, emit)
        finally:
            if network is not None:
                self.close_network(network)
            if not request.no_build:
                # A build that may run package code had a cache of its own: nothing of it stays.
                self._remove_volume(cache)
        complete = (
            result["exit"] == 0
            and not result["oom_killed"]
            and not result["timed_out"]
            and not result["stopped"]
        )
        if complete:
            self._mark_complete(volume)
            self._evict(keep=volume)
        else:
            self._remove_volume(volume)
        return {**result, "complete": complete}

    def stop(self, request: StopRequest) -> dict[str, object]:
        """Stop a running task or build of the asking worker: SIGTERM, SIGKILL after its grace.

        Only a run the same owner started is stopped. A run of another owner gets the answer an
        id nobody registered gets, so the answer does not tell whether the id exists.

        Parameters
        ----------
        request
            The id and the owner.

        Returns
        -------
        dict
            ``stopped``: `True` when a run of this owner is registered under that id and Docker
            stopped its container, found it not running or gone, or it has none yet (a run that
            has not started its container never will). `False` when no run of this owner is
            registered under the id, or Docker did not carry out the stop: the caller asks again.
        """
        for kind in ("task", "build"):
            name = container_name(self.settings, kind, request.id)
            with self._lock:
                live = self._live.get(name)
            if live is not None and _same_owner(live, request.owner):
                live.stopped.set()
                cid = live.container_id
                if cid is None:
                    return {"stopped": True}
                try:
                    status, body = self.engine.request(
                        "POST",
                        f"/containers/{cid}/stop",
                        query={"t": str(live.grace)},
                        timeout=live.grace + 30.0,
                    )
                except EngineError as exc:
                    status, body = None, {"message": str(exc)}
                # 304: not running any more; 404: removed meanwhile. Either way nothing runs.
                if status in (204, 304, 404):
                    return {"stopped": True}
                logger.error(
                    "Docker did not stop container %s (status %s: %s); the worker asks again.",
                    name,
                    status if status is not None else "none",
                    self.redact(_message(body)),
                )
                return {"stopped": False}
        return {"stopped": False}

    def abandon(self, name: str, owner: str) -> None:
        """Kill a run at once and never start it: its caller's connection closed.

        The name is remembered with its owner, because the connection can close before the
        request reaches `run` or `build`, which looks the pair up when it registers. A run of
        another owner under the name is left alone: a request that repeats a live id of another
        worker, refused as a duplicate, must not end that worker's run when it disconnects.

        Parameters
        ----------
        name
            The container's name.
        owner
            The owner the departed connection's request named.
        """
        with self._lock:
            self._abandoned.add((name, owner))
            live = self._live.get(name)
        if live is None or not _same_owner(live, owner):
            return
        live.stopped.set()
        self._kill(live)

    def forget(self, name: str, owner: str, live: _Live | None = None) -> None:
        """Drop a finished run: its registration and any abandonment of its name by its owner.

        Parameters
        ----------
        name
            The container's name.
        owner
            The owner the request named.
        live
            The registration to drop. Another run's registration under the same name is left
            alone; `None` (the socket server's call, after the run unregistered itself) drops
            only the abandonment.
        """
        with self._lock:
            if live is not None and self._live.get(name) is live:
                del self._live[name]
                self._reserved_mb -= live.memory_mb
            self._abandoned.discard((name, owner))

    def status(self, request: StatusRequest) -> dict[str, object]:
        """Say whether an environment is built.

        Parameters
        ----------
        request
            Python and key.

        Returns
        -------
        dict
            ``complete``: the marker and the volume both exist.
        """
        volume = environment_volume(self.settings, request.python, request.environment)
        complete = self._built(volume)
        if complete:
            self._touch(volume)
        return {"complete": complete}

    def cleanup(self, request: CleanupRequest) -> dict[str, object]:
        """Remove a worker's containers that no live connection supervises.

        Parameters
        ----------
        request
            The worker.

        Returns
        -------
        dict
            ``removed``: how many containers.
        """
        removed = self._remove_labelled(
            {LABEL_STACK: self.settings.stack, LABEL_OWNER: request.owner}, spare_live=True
        )
        return {"removed": removed}

    def _register(self, name: str, owner: str, grace: int, memory_mb: int) -> _Live:
        """Register a run or build, stopped at once when its caller left already.

        Parameters
        ----------
        name
            The container's name.
        owner
            The worker that asked.
        grace
            Seconds between SIGTERM and SIGKILL.
        memory_mb
            The memory the run asks for, counted against the total ceiling.

        Returns
        -------
        _Live
            The registration.

        Raises
        ------
        EngineError
            If a run is registered under that name already, or the live runs would hold more
            memory in total than the ceiling allows.
        """
        live = _Live(owner, grace, memory_mb)
        with self._lock:
            if name in self._live:
                ident = name.removeprefix(f"{self.settings.stack}-").split("-", 1)[-1]
                raise EngineError(
                    f"a run with the id {ident} is already live; wait for it to end or use "
                    "another id"
                )
            if self._reserved_mb + memory_mb > self.settings.max_total_memory_mb:
                raise EngineError(
                    f"the live runs already hold {self._reserved_mb} MiB of the "
                    f"{self.settings.max_total_memory_mb} MiB allowed in total; wait for one to "
                    "end, or raise LAKANA_LAUNCHER__MAX_TOTAL_MEMORY_MB"
                )
            self._reserved_mb += memory_mb
            self._live[name] = live
            if (name, owner) in self._abandoned:
                live.stopped.set()
        return live

    def redact(self, text: str) -> str:
        """Replace the host path of every ingestion root in a text by the root's name.

        Parameters
        ----------
        text
            A message, such as Docker's.

        Returns
        -------
        str
            The text without any root's host path.
        """
        for name, path in sorted(
            self.settings.ingestion_roots.items(), key=lambda item: -len(item[1])
        ):
            # Whole path components only: `/srv` is not found inside `/srv2` or `/x/srv`.
            pattern = rf"(?<![\w.-]){re.escape(path)}(?![\w-]|\.\w)"
            text = re.sub(pattern, lambda _match, name=name: f"ingestion root '{name}'", text)
        return text

    def _kill(self, live: _Live) -> None:
        """Kill a container if it exists; a container that has not started ignores it.

        Parameters
        ----------
        live
            The registration.
        """
        if live.container_id is not None:
            with contextlib.suppress(EngineError):
                self.engine.request("POST", f"/containers/{live.container_id}/kill")

    # --- containers ---------------------------------------------------------------------

    def _supervise(
        self,
        name: str,
        spec: dict[str, Any],
        live: _Live,
        timeout: int,
        emit: Emit,
        *,
        stdin: bytes | None = None,
    ) -> dict[str, object]:
        """Create, attach, start, stream and remove one container.

        The registration is checked after the create, before the start and after the start: a
        stop or a closed connection in any of those windows finds a container that ignores a
        stop or a kill, so the step after it must not go on.

        Parameters
        ----------
        name
            Its name.
        spec
            Its spec.
        live
            Its registration.
        timeout
            Seconds before it is stopped.
        emit
            Sends its output.
        stdin
            Bytes for its stdin, or `None`.

        Returns
        -------
        dict
            ``exit``, ``oom_killed``, ``timed_out``, ``stopped``.

        Raises
        ------
        EngineError
            If Docker refuses a step.
        """
        if live.stopped.is_set():
            return dict(_STOPPED_BEFORE_START)
        cid = live.container_id = self._create(name, spec)
        began = time.monotonic()
        timer = threading.Timer(timeout, self._expire, (live,))
        timer.daemon = True
        try:
            if live.stopped.is_set():
                return dict(_STOPPED_BEFORE_START)
            stream = self.engine.attach(cid, stdin=stdin is not None)
            try:
                if live.stopped.is_set():
                    return dict(_STOPPED_BEFORE_START)
                status, body = self.engine.request("POST", f"/containers/{cid}/start")
                if status != 204:
                    raise EngineError(f"Docker could not start the container: {_message(body)}")
                if live.stopped.is_set():
                    self._kill(live)
                timer.start()
                if stdin is not None:
                    # Input goes in while output is read: a task that writes before it reads
                    # fills one pipe while the launcher fills the other.
                    threading.Thread(target=_send_input, args=(stream, stdin), daemon=True).start()
                for payload in read_frames(stream):
                    emit({"log": base64.b64encode(payload).decode("ascii")})
            finally:
                stream.close()
            # The stream ended, so the container is gone or about to be: wait no longer than its
            # timeout, its grace and a margin, then the removal below is forced.
            left = max(timeout - (time.monotonic() - began), 0.0)
            _, waited = self.engine.request(
                "POST", f"/containers/{cid}/wait", timeout=left + live.grace + _WAIT_MARGIN
            )
            _, info = self.engine.request("GET", f"/containers/{cid}/json")
            state = info.get("State", {}) if isinstance(info, dict) else {}
            code = waited.get("StatusCode") if isinstance(waited, dict) else None
            timed_out, stopped = live.timed_out.is_set(), live.stopped.is_set()
            # Docker sets `OOMKilled` from a containerd event that is lost when the container's
            # cgroup goes first. A SIGKILL the launcher did not send (it neither stopped nor timed
            # out the run) is read as the memory limit, its most likely sender; a task killing
            # itself, an operator's `docker kill` or the host's OOM killer read the same way.
            killed = code == _SIGKILL_EXIT and not timed_out and not stopped
            return {
                "exit": code if isinstance(code, int) else None,
                "oom_killed": bool(state.get("OOMKilled")) or killed,
                "timed_out": timed_out,
                "stopped": stopped,
            }
        finally:
            timer.cancel()
            self._remove_container(cid)

    def _expire(self, live: _Live) -> None:
        """Stop a container whose timeout passed.

        Parameters
        ----------
        live
            The container.
        """
        live.timed_out.set()
        with contextlib.suppress(EngineError):
            self.engine.request(
                "POST",
                f"/containers/{live.container_id}/stop",
                query={"t": str(live.grace)},
                timeout=live.grace + 30.0,
            )

    def _create(self, name: str, spec: dict[str, Any]) -> str:
        """Create a container.

        Parameters
        ----------
        name
            Its name.
        spec
            Its spec.

        Returns
        -------
        str
            Its id.

        Raises
        ------
        EngineError
            If Docker refuses it, with Docker's message (a subpath escape is refused here).
        """
        status, body = self.engine.request("POST", "/containers/create", spec, query={"name": name})
        if status != 201 or not isinstance(body, dict):
            raise EngineError(f"Docker refused the container: {self.redact(_message(body))}")
        return str(body["Id"])

    def _remove_container(self, cid: str) -> None:
        """Remove a container and kill what still runs in it.

        Parameters
        ----------
        cid
            Its id.
        """
        try:
            self.engine.request(
                "DELETE", f"/containers/{quote(cid, safe='')}", query={"force": "1", "v": "1"}
            )
        except EngineError as exc:
            logger.error("Could not remove container %s: %s", cid, exc)

    def _ensure_image(self, image: str) -> None:
        """Pull an allowed image once if it is not on the host.

        Parameters
        ----------
        image
            The reference from the allow-list.

        Raises
        ------
        EngineError
            If the pull did not bring it.
        """
        # Slashes, the tag's colon and the digest's @ stay as they are, as the Docker CLI sends
        # them.
        path = f"/images/{quote(image, safe='/:@')}/json"
        status, _ = self.engine.request("GET", path)
        if status == 200:
            return
        self.engine.request(
            "POST", "/images/create", query={"fromImage": image}, timeout=_PULL_SECONDS
        )
        status, _ = self.engine.request("GET", path)
        if status != 200:
            raise EngineError(
                f"Docker could not pull the task image {image}; check that the host reaches its "
                "registry, or pull it once on the host"
            )

    def _remove_labelled(self, wanted: dict[str, str], *, spare_live: bool) -> int:
        """Remove containers and networks with these labels.

        Parameters
        ----------
        wanted
            Label to value.
        spare_live
            Leave alone what a live connection supervises.

        Returns
        -------
        int
            How many containers were removed.

        Raises
        ------
        EngineError
            If Docker does not list the containers or the networks: nothing is removed then,
            and the caller must not take that for "nothing is there".
        """
        filters = json.dumps({"label": [f"{k}={v}" for k, v in sorted(wanted.items())]})
        containers = self._list("/containers/json", {"all": "1", "filters": filters}, "containers")
        networks = self._list("/networks", {"filters": filters}, "networks")
        # The live runs are read after the listing: a run registers before it creates anything,
        # so everything listed that belongs to a live run is found among them.
        with self._lock:
            live = set(self._live) if spare_live else set()
        removed = 0
        for container in containers:
            names = {str(n).lstrip("/") for n in container.get("Names", [])}
            if names & live:
                continue
            self._remove_container(str(container["Id"]))
            removed += 1
        for network in networks:
            if network.get("Name") not in live:
                self.close_network(str(network["Name"]))
        return removed

    def _list(self, path: str, query: dict[str, str], what: str) -> list[dict[str, Any]]:
        """List objects of Docker and insist on an answer.

        Parameters
        ----------
        path
            ``/containers/json`` or ``/networks``.
        query
            The query parameters.
        what
            ``containers`` or ``networks``, for the message.

        Returns
        -------
        list of dict
            The objects.

        Raises
        ------
        EngineError
            If Docker does not answer 200 with a list.
        """
        status, body = self.engine.request("GET", path, query=query)
        if status != 200 or not isinstance(body, list):
            raise EngineError(f"Docker could not list the {what}: {_message(body)}")
        return body

    # --- reaper and shutdown ------------------------------------------------------------

    def reap(self, now: float | None = None) -> int:
        """Remove containers and networks past their deadline, and caches that are too old.

        Runs without the registry's help: after a restart, or when a run's own timer failed,
        the labels are all that is left.

        Parameters
        ----------
        now
            Epoch seconds to measure against; the clock by default.

        Returns
        -------
        int
            How many containers were removed.
        """
        moment = time.time() if now is None else now
        filters = json.dumps({"label": [f"{LABEL_STACK}={self.settings.stack}"]})
        removed = 0
        try:
            for container in self._list(
                "/containers/json", {"all": "1", "filters": filters}, "containers"
            ):
                if _expired(container.get("Labels"), moment):
                    for raw in container.get("Names", []):
                        with self._lock:
                            live = self._live.get(str(raw).lstrip("/"))
                        if live is not None:
                            live.stopped.set()
                    self._remove_container(str(container["Id"]))
                    removed += 1
        except EngineError as exc:
            logger.error("Could not list containers to reap: %s", exc)
        try:
            for network in self._list("/networks", {"filters": filters}, "networks"):
                if _expired(network.get("Labels"), moment):
                    self.close_network(str(network["Name"]))
        except EngineError as exc:
            logger.error("Could not list networks to reap: %s", exc)
        self._reap_caches(moment)
        return removed

    def _reap_caches(self, moment: float) -> None:
        """Remove shared caches past the age limit and private ones past their deadline.

        A volume in use answers 409 and stays. A private cache outlives its build only when the
        launcher died during it.

        Parameters
        ----------
        moment
            Epoch seconds to measure against.
        """
        base = [f"{LABEL_STACK}={self.settings.stack}", f"{LABEL_KIND}=cache"]
        for shared in ("1", "0"):
            wanted = [*base, f"{LABEL_SHARED}={shared}"]
            try:
                status, body = self.engine.request(
                    "GET", "/volumes", query={"filters": json.dumps({"label": wanted})}
                )
            except EngineError as exc:
                logger.error("Could not list caches to reap: %s", exc)
                continue
            volumes = body.get("Volumes") if status == 200 and isinstance(body, dict) else None
            for volume in volumes or []:
                if shared == "0":
                    stale = _expired(volume.get("Labels"), moment)
                else:
                    try:
                        created = datetime.fromisoformat(str(volume["CreatedAt"])).timestamp()
                    except (KeyError, ValueError):
                        continue
                    stale = moment - created > _CACHE_MAX_AGE
                if stale:
                    self._remove_volume(str(volume["Name"]))

    def start_reaper(self, interval: float = 60.0) -> threading.Thread:
        """Run `reap` every `interval` seconds on a daemon thread until `stop_reaper`.

        Parameters
        ----------
        interval
            Seconds between passes.

        Returns
        -------
        threading.Thread
            The started thread.
        """
        self._reaper_stop.clear()

        def loop() -> None:
            """Reap until stopped; an error in one pass is logged and the loop goes on."""
            while not self._reaper_stop.wait(interval):
                try:
                    self.reap()
                except Exception:
                    logger.exception("The reaper failed")

        thread = threading.Thread(target=loop, name="lakana-launcher-reaper", daemon=True)
        thread.start()
        return thread

    def stop_reaper(self) -> None:
        """Ask the reaper thread to end."""
        self._reaper_stop.set()

    def shutdown(self) -> None:
        """Stop every live run and remove everything of this stack, for SIGTERM and SIGINT.

        Without it a task would keep running, untimed, after the launcher service stopped.
        """
        self.stop_reaper()
        with self._lock:
            runs = list(self._live.values())
        for live in runs:
            live.stopped.set()
        try:
            self._remove_labelled({LABEL_STACK: self.settings.stack}, spare_live=False)
        except EngineError as exc:
            logger.error(
                "Could not remove everything the launcher created: %s; the reaper of the next "
                "launcher removes it after its deadline.",
                exc,
            )

    # --- networks -----------------------------------------------------------------------

    def _open_network(
        self,
        kind: str,
        ident: str,
        owner: str,
        services: Sequence[tuple[str, str]],
        deadline: int,
    ) -> str:
        """Create a task's or build's network and attach services to it, in order.

        Parameters
        ----------
        kind
            ``task`` or ``build``.
        ident
            Its id.
        owner
            The worker.
        services
            Compose service and alias pairs to attach (the server first for a task).
        deadline
            Epoch seconds after which the reaper removes the network.

        Returns
        -------
        str
            The network's name.

        Raises
        ------
        EngineError
            If Docker refuses it, no subnet is free, too many networks are in use, or a
            service is not running; nothing is left behind.
        """
        name = container_name(self.settings, kind, ident)
        self._create_network(name, kind, ident, owner, deadline)
        try:
            for service, alias in services:
                cid = self._service_container(service)
                status, body = self.engine.request(
                    "POST",
                    f"/networks/{quote(name, safe='')}/connect",
                    {"Container": cid, "EndpointConfig": {"Aliases": [alias]}},
                )
                if status != 200:
                    raise EngineError(f"Docker could not attach {service}: {_message(body)}")
        except EngineError:
            self.close_network(name)
            raise
        return name

    def _running(self, service: str) -> bool:
        """Say whether a Compose service of this stack is running.

        Parameters
        ----------
        service
            The service.

        Returns
        -------
        bool
            Whether a container of it runs.
        """
        try:
            self._service_container(service)
        except EngineError:
            return False
        return True

    def open_build_network(
        self, build: str, owner: str, targets: Sequence[tuple[str, int]], seconds: int
    ) -> tuple[str, str | None]:
        """Create a build's network with the egress proxy as its only way out.

        The token binds the build's host:port pairs to the network's own subnet and to the
        build's time plus a minute: copied into another container, it is refused.

        Parameters
        ----------
        build
            The build's id.
        owner
            The worker.
        targets
            The host and port pairs the build may reach (`requests.build_targets`).
        seconds
            The build's timeout.

        Returns
        -------
        tuple
            The network, and the proxy URL with the build's token; `None` when the egress
            service is not running (the build then reaches nothing).

        Raises
        ------
        EngineError
            If Docker refuses the network or cannot attach the proxy; nothing is left behind.
        Refused
            If the pairs do not fit in a token; nothing is left behind.
        RuntimeError
            If the launcher has not started, so it has no egress key.
        """
        if self._egress_key is None:
            raise RuntimeError("the egress key is only available after start()")
        egress = self._running(self.settings.egress_service)
        services = [(self.settings.egress_service, "egress")] if egress else []
        network = self._open_network("build", build, owner, services, _deadline(seconds))
        if not egress:
            return network, None
        expires = int(time.time()) + seconds + 60
        with self._lock:
            subnet = self._subnets[network]
            self._token_expiry[network] = expires
        try:
            token = mint(
                self._egress_key,
                targets,
                expires,
                purpose="build",
                network=ipaddress.ip_network(subnet),
            )
        except ValueError as exc:
            self.close_network(network)
            raise Refused(f"hosts: {exc}") from None
        return network, f"http://lakana:{token}@egress:{EGRESS_PORT}"

    def _create_network(self, name: str, kind: str, ident: str, owner: str, deadline: int) -> None:
        """Create a network on a /29 of the launcher's own range.

        Parameters
        ----------
        name
            The network's name.
        kind
            ``task`` or ``build``.
        ident
            Its id.
        owner
            The worker.
        deadline
            Epoch seconds after which the reaper removes it.

        Raises
        ------
        EngineError
            If Docker refuses it, or no subnet is free.
        """
        for _ in range(_SUBNET_TRIES):
            subnet = self._allocate_subnet(name)
            spec = network_spec(self.settings, kind, ident, owner, subnet=subnet, deadline=deadline)
            status, body = self.engine.request("POST", "/networks/create", spec)
            if status == 201:
                return
            with self._lock:
                self._subnets.pop(name, None)
                if status == 403 and "overlap" in _message(body).lower():
                    continue  # something else holds it: keep it out of use, take the next one
                self._used_subnets.discard(subnet)
            raise EngineError(f"Docker refused the {kind} network: {_message(body)}")
        raise EngineError(
            f"Docker kept refusing the subnets of {self.settings.network_range} as overlapping "
            "another network; choose another range in LAKANA_LAUNCHER__NETWORK_RANGE"
        )

    def _allocate_subnet(self, name: str) -> str:
        """Take the first free /29 of the range for a network.

        Parameters
        ----------
        name
            The network's name.

        Returns
        -------
        str
            The subnet in CIDR form.

        Raises
        ------
        EngineError
            If as many networks as the bound allows are in use, or the range is used up.
        """
        with self._lock:
            now = time.time()
            self._resting = {k: v for k, v in self._resting.items() if v > now}
            if len(self._subnets) + len(self._resting) >= self._max_networks:
                raise EngineError(
                    "too many task networks in use "
                    f"({len(self._subnets) + len(self._resting)} of {self._max_networks}: "
                    f"{len(self._subnets)} running, {len(self._resting)} resting until the "
                    "egress tokens of ended builds expire); wait for runs to end, or enlarge "
                    "LAKANA_LAUNCHER__NETWORK_RANGE"
                )
            for candidate in self._pool.subnets(new_prefix=29):
                text = str(candidate)
                if text not in self._used_subnets and text not in self._resting:
                    self._used_subnets.add(text)
                    self._subnets[name] = text
                    return text
        raise EngineError(
            f"no free address range is left in {self.settings.network_range}; enlarge "
            "LAKANA_LAUNCHER__NETWORK_RANGE"
        )

    def _free_subnet(self, name: str) -> None:
        """Give back the subnet of a removed network.

        Parameters
        ----------
        name
            The network's name.
        """
        with self._lock:
            subnet = self._subnets.pop(name, None)
            expiry = self._token_expiry.pop(name, None)
            if subnet is not None:
                self._used_subnets.discard(subnet)
                if expiry is not None:
                    # A token bound to this subnet is valid until `expiry`: no other network gets
                    # the subnet before then, so a leaked token finds no client there.
                    self._resting[subnet] = expiry + _REST_MARGIN

    def _recover_subnets(self) -> None:
        """Mark the subnets of networks that survived the start-up removal as in use."""
        filters = json.dumps({"label": [f"{LABEL_STACK}={self.settings.stack}"]})
        status, networks = self.engine.request("GET", "/networks", query={"filters": filters})
        for network in networks if status == 200 and isinstance(networks, list) else []:
            for config in network.get("IPAM", {}).get("Config") or []:
                try:
                    subnet = ipaddress.IPv4Network(config.get("Subnet", ""))
                except ValueError:
                    continue
                if subnet.subnet_of(self._pool):
                    with self._lock:
                        self._subnets[str(network["Name"])] = str(subnet)
                        self._used_subnets.add(str(subnet))

    def _service_container(self, service: str) -> str:
        """Find the running container of a Compose service of this stack.

        Parameters
        ----------
        service
            The service name.

        Returns
        -------
        str
            Its id.

        Raises
        ------
        EngineError
            If it is not running.
        """
        filters = json.dumps(
            {
                "label": [
                    f"com.docker.compose.project={self.settings.stack}",
                    f"com.docker.compose.service={service}",
                ],
                "status": ["running"],
            }
        )
        status, body = self.engine.request("GET", "/containers/json", query={"filters": filters})
        if status != 200 or not isinstance(body, list) or not body:
            raise EngineError(
                f"the {service} service of this stack is not running; start it "
                f"(docker compose up -d {service})"
            )
        return str(body[0]["Id"])

    def close_network(self, name: str) -> None:
        """Detach everything from a network and remove it.

        Parameters
        ----------
        name
            The network.
        """
        path = f"/networks/{quote(name, safe='')}"
        try:
            status, body = self.engine.request("GET", path)
            if status == 200 and isinstance(body, dict):
                for cid in body.get("Containers") or {}:
                    self.engine.request(
                        "POST", f"{path}/disconnect", {"Container": cid, "Force": True}
                    )
            status, _ = self.engine.request("DELETE", path)
            if status in (204, 404):
                self._free_subnet(name)
        except EngineError as exc:
            logger.error("Could not remove network %s: %s", name, exc)

    # --- environments -------------------------------------------------------------------

    def _create_volume(self, spec: dict[str, Any]) -> None:
        """Create a labelled volume (Docker answers 201 for one that exists already).

        Parameters
        ----------
        spec
            The body from `volume_spec`.

        Raises
        ------
        EngineError
            If Docker refuses it.
        """
        status, body = self.engine.request("POST", "/volumes/create", spec)
        if status != 201:
            raise EngineError(f"Docker refused the volume {spec['Name']}: {_message(body)}")

    def _marker(self, volume: str) -> Path:
        """Locate an environment's completion marker in the launcher's own volume.

        Parameters
        ----------
        volume
            The environment volume.

        Returns
        -------
        Path
            The marker; its mtime is the last use.
        """
        return self.settings.state_dir / "environments" / f"{volume}.complete"

    def _built(self, volume: str) -> bool:
        """Say whether an environment is built: its marker and its volume both exist.

        Parameters
        ----------
        volume
            The volume.

        Returns
        -------
        bool
            Whether a build ended well and its volume is still there. A build container cannot
            write the marker: it lives outside every volume a build mounts. A marker without a
            volume (a ``docker volume prune``) is a build that has to run again.
        """
        if not self._marker(volume).is_file():
            return False
        try:
            status, _ = self.engine.request("GET", f"/volumes/{quote(volume, safe='')}")
        except EngineError:
            return False
        return status == 200

    def _mark_complete(self, volume: str) -> None:
        """Write the marker of a finished build.

        Parameters
        ----------
        volume
            The volume.
        """
        marker = self._marker(volume)
        temporary = marker.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(volume + "\n", encoding="utf-8")
        os.replace(temporary, marker)

    def _touch(self, volume: str) -> None:
        """Record a use of a built environment.

        Parameters
        ----------
        volume
            The volume.
        """
        with contextlib.suppress(FileNotFoundError):
            os.utime(self._marker(volume))

    def _remove_volume(self, volume: str) -> bool:
        """Remove an environment volume unless a container uses it.

        Parameters
        ----------
        volume
            The volume.

        Returns
        -------
        bool
            Whether it is gone.
        """
        try:
            status, _ = self.engine.request("DELETE", f"/volumes/{quote(volume, safe='')}")
        except EngineError as exc:
            logger.error("Could not remove volume %s: %s", volume, exc)
            return False
        return status in (204, 404)

    def _evict(self, *, keep: str) -> None:
        """Remove the least recently used environments beyond `keep_environments`.

        Parameters
        ----------
        keep
            The volume just built, never removed here.
        """
        markers = sorted(
            (self.settings.state_dir / "environments").glob("*.complete"),
            key=_mtime,
            reverse=True,
        )
        for marker in markers[self.settings.keep_environments :]:
            volume = marker.name.removesuffix(".complete")
            if volume != keep and self._remove_volume(volume):
                marker.unlink(missing_ok=True)
