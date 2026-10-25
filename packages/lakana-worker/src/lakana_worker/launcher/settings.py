"""The launcher's settings, read once from its environment (`LAKANA_LAUNCHER__*`).

Every error names the variable and says what to write. None echoes a value, except the entry of
`EGRESS_REFUSED` it refuses (an address range, never a secret): the ingestion roots are host
paths an operator may not want in a log.

Only what a person needs to decide is a variable: the stack name, the allowed images, the allowed
ingestion roots, the Unix socket, whether ``no-new-privileges`` is required, the ceilings of a
task's memory, CPUs and processes, and the address ranges nothing Lakana starts may reach.
Everything else (users, service names, the Docker socket's path, the state directory) is a
module constant: a variable that nobody should change is a way to break the isolation.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("lakana_worker.launcher")

PREFIX = "LAKANA_LAUNCHER__"
SUPPORTED_PYTHONS = ("3.11", "3.12", "3.13", "3.14")
"""The Python versions Lakana publishes a task image for."""

DOCKER_SOCKET = Path("/var/run/docker.sock")
"""Where the Compose bundle mounts the Docker Engine's socket into the launcher."""
STATE_DIR = Path("/var/lib/lakana-launcher")
"""The launcher's own volume: one completion marker per built environment."""
EGRESS_KEY = Path("/run/lakana-egress/egress.key")
"""The key that signs the egress proxy's tokens."""
TASK_USER = "10001:10003"
"""``uid:gid`` of every task container: the worker's uid, so the harness owns its work directory,
in a group of its own (the worker's group is 10001)."""
BUILD_USER = "10002:10002"
"""``uid:gid`` of every build container: another one, so a task cannot change its environment."""
SERVER_SERVICE = "server"
"""The Compose service a task network gets attached."""
SERVER_ALIAS = "server"
"""The name a task reaches that service by."""
EGRESS_SERVICE = "egress"
"""The Compose service of the build proxy."""
MAX_SECONDS = 604800
"""Ceiling of a request's timeout: one week."""
NETWORK_RANGE = "10.207.0.0/16"
"""Where task networks get their addresses unless `LAKANA_LAUNCHER__NETWORK_RANGE` says so."""
KEEP_ENVIRONMENTS = 10
"""How many built environments are kept, most recently used first."""
FORBIDDEN_ROOTS = (
    Path("/"),
    Path("/run"),
    Path("/var/run"),
    Path("/var/lib/docker"),
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/etc"),
    Path("/var/lib/containerd"),
    Path("/var/snap/docker"),
    Path("/root"),
    Path("/boot"),
)
"""Host directories no ingestion root may be, hold or sit under.

A read-only bind of a directory that holds a socket still lets a task connect to it, and
``/proc``, ``/sys``, ``/dev``, ``/etc``, ``/root`` and ``/boot`` expose the host itself, and
the data directories of Docker and containerd hold every volume and image. The list is checked
on the literal path. Docker resolves a symlink on the host, which the launcher cannot see, so
symlinked roots are checked at start by a probe container and documented as an operator rule.
"""
KNOWN_SETTINGS = (
    "STACK",
    "IMAGES",
    "INGESTION_ROOTS",
    "SOCKET",
    "NO_NEW_PRIVILEGES",
    "MAX_MEMORY_MB",
    "MAX_CPUS",
    "MAX_PIDS",
    "PROBE_MAX_ENTRIES",
    "NETWORK_RANGE",
    "MAX_TOTAL_MEMORY_MB",
    "EGRESS_REFUSED",
)
"""The names after the prefix that the launcher reads."""
MAX_REFUSED = 64
"""The most ranges `LAKANA_LAUNCHER__EGRESS_REFUSED` lists."""
_CONSTANT_NOW = frozenset(
    {
        "TASK_USER",
        "BUILD_USER",
        "DOCKER_SOCKET",
        "STATE_DIR",
        "EGRESS_KEY",
        "SERVER_SERVICE",
        "SERVER_ALIAS",
        "EGRESS_SERVICE",
        "MAX_SECONDS",
        "KEEP_ENVIRONMENTS",
        "WORK_VOLUME",
    }
)

_STACK = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,62}\Z")
_ROOT_NAME = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,62}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NUMBER = re.compile(r"[0-9]{1,10}")
_IMAGE = re.compile(
    r"\A[a-z0-9][a-z0-9._/:-]{0,200}(?::[A-Za-z0-9._-]{1,128})?(?:@sha256:[0-9a-f]{64})?\Z"
)


class _Repeated(ValueError):
    """A JSON object names one key twice."""


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object from its pairs, refusing a repeated key.

    Parameters
    ----------
    pairs
        The key and value pairs in document order.

    Returns
    -------
    dict
        The object.

    Raises
    ------
    _Repeated
        If a key occurs twice.
    """
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _Repeated
    return result


class SettingsError(Exception):
    """A setting is missing or malformed; the text names it and says what to write."""


@dataclass(frozen=True)
class LauncherSettings:
    """What the launcher is allowed to do, as the operator set it.

    Parameters
    ----------
    stack
        The Compose project name; it prefixes every name the launcher creates and finds the
        `server` and `egress` services (`com.docker.compose.project`).
    images
        Python version to task image: the allow-list of images.
    socket
        Where the launcher listens; the token file sits next to it.
    ingestion_roots
        Name to absolute host directory, mounted read-only at ``/ingest/<name>`` when a run
        names it.
    no_new_privileges
        ``auto`` (on where Docker allows it, detected at start) or ``on`` (refuse to start
        without it).
    max_memory_mb
        Ceiling of a request's memory.
    max_cpus
        Ceiling of a request's CPUs.
    max_pids
        Ceiling of a request's process count.
    network_range
        The private IPv4 range the launcher carves one /29 per task or build network from.
    max_total_memory_mb
        Ceiling of the memory the live runs and builds may ask for together.
    probe_max_entries
        Most entries the start-up probe looks at in one ingestion root; a root with more is
        unusable until the cap is raised.
    egress_refused
        Address ranges in CIDR form (an address as ``/32`` or ``/128``) that the egress proxy
        and the relays refuse, besides Docker's gateways and the built-in refusals: the Docker
        host's own LAN address or subnet, and any other network nothing Lakana starts may reach.
    work_volume
        The named volume of the worker's attempt directories; ``<stack>-task-work`` unless a
        test says otherwise. Not an environment variable.
    docker_socket, state_dir, egress_key, task_user, build_user, server_service
        The module constants of the same name; fields so that tests can point them elsewhere.
    server_alias, egress_service, max_seconds, keep_environments
        The module constants of the same name; fields for the same reason.
    """

    stack: str
    images: Mapping[str, str]
    work_volume: str = ""
    socket: Path = Path("/run/lakana-launcher/launcher.sock")
    ingestion_roots: Mapping[str, str] = field(default_factory=dict)
    no_new_privileges: str = "auto"
    max_memory_mb: int = 16384
    max_cpus: float = 8.0
    max_pids: int = 4096
    probe_max_entries: int = 100000
    network_range: str = NETWORK_RANGE
    max_total_memory_mb: int = 65536
    egress_refused: tuple[str, ...] = ()
    docker_socket: Path = DOCKER_SOCKET
    state_dir: Path = STATE_DIR
    egress_key: Path = EGRESS_KEY
    task_user: str = TASK_USER
    build_user: str = BUILD_USER
    server_service: str = SERVER_SERVICE
    server_alias: str = SERVER_ALIAS
    egress_service: str = EGRESS_SERVICE
    max_seconds: int = MAX_SECONDS
    keep_environments: int = KEEP_ENVIRONMENTS

    def __post_init__(self) -> None:
        """Name the work volume after the stack when none is given."""
        if not self.work_volume:
            object.__setattr__(self, "work_volume", f"{self.stack}-task-work")


def _get(environ: Mapping[str, str], name: str) -> str | None:
    """Read one setting, blank meaning unset.

    Parameters
    ----------
    environ
        The environment.
    name
        The name after the prefix.

    Returns
    -------
    str or None
        The stripped value.
    """
    value = environ.get(PREFIX + name, "").strip()
    return value or None


def _json_object(environ: Mapping[str, str], name: str, message: str) -> dict[str, object] | None:
    """Read a setting that holds a JSON object.

    Parameters
    ----------
    environ
        The environment.
    name
        The name after the prefix.
    message
        The error for anything that is not an object.

    Returns
    -------
    dict or None
        The object; `None` when unset.

    Raises
    ------
    SettingsError
        With `message`.
    """
    text = _get(environ, name)
    if text is None:
        return None
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs)
    except _Repeated:
        raise SettingsError(f"{PREFIX}{name} repeats a key; {message}") from None
    except ValueError:
        raise SettingsError(message) from None
    if not isinstance(value, dict):
        raise SettingsError(message)
    return value


def _number(environ: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    """Read a whole-number setting within bounds.

    Parameters
    ----------
    environ
        The environment.
    name
        The name after the prefix.
    default
        The value when unset.
    low
        The least value.
    high
        The greatest value.

    Returns
    -------
    int
        The value.

    Raises
    ------
    SettingsError
        If it is not a whole number in range.
    """
    text = _get(environ, name)
    if text is None:
        return default
    if _NUMBER.fullmatch(text) is None or not low <= int(text) <= high:
        raise SettingsError(f"{PREFIX}{name} must be a whole number from {low} to {high}.")
    return int(text)


_PRIVATE_BLOCKS = tuple(
    ipaddress.IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _network_range(environ: Mapping[str, str]) -> str:
    """Read the range task networks are carved from.

    Parameters
    ----------
    environ
        The environment.

    Returns
    -------
    str
        The range in CIDR form.

    Raises
    ------
    SettingsError
        If it is not an IPv4 network inside 10/8, 172.16/12 or 192.168/16 with room for at
        least 32 networks of 8 addresses (a /24 or larger).
    """
    text = _get(environ, "NETWORK_RANGE")
    if text is None:
        return NETWORK_RANGE
    rule = (
        f"{PREFIX}NETWORK_RANGE must be a private IPv4 range in CIDR form (inside 10.0.0.0/8, "
        "172.16.0.0/12 or 192.168.0.0/16) of at least 256 addresses, for example 10.207.0.0/16."
    )
    try:
        network = ipaddress.IPv4Network(text, strict=True)
    except ValueError:
        raise SettingsError(rule) from None
    if network.prefixlen > 24 or not any(network.subnet_of(block) for block in _PRIVATE_BLOCKS):
        raise SettingsError(rule)
    return str(network)


def _egress_refused(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Read the address ranges egress refuses besides the built-in ones.

    Parameters
    ----------
    environ
        The environment.

    Returns
    -------
    tuple of str
        Each range in CIDR form, in the order given; an address becomes ``/32`` or ``/128``.
        Empty when unset.

    Raises
    ------
    SettingsError
        For more than `MAX_REFUSED` entries, an entry that is not an address or a range, or a
        range with host bits set; the message names the entry and the format.
    """
    name = f"{PREFIX}EGRESS_REFUSED"
    entries = [entry.strip() for entry in (_get(environ, "EGRESS_REFUSED") or "").split(",")]
    entries = [entry for entry in entries if entry]
    if len(entries) > MAX_REFUSED:
        raise SettingsError(
            f"{name} lists {len(entries)} ranges, more than {MAX_REFUSED}; list wider ranges "
            "instead, such as 192.168.0.0/16."
        )
    rule = "list CIDR ranges or addresses separated by commas, such as 192.168.1.0/24,10.0.0.7."
    ranges: list[str] = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=True)
        except ValueError:
            try:
                meant = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                raise SettingsError(
                    f"{name}: `{entry}` is not an IP address or CIDR range; {rule}"
                ) from None
            raise SettingsError(
                f"{name}: `{entry}` is not a network (host bits set; did you mean {meant}?); {rule}"
            ) from None
        ranges.append(str(_unmapped(network, name)))
    return tuple(ranges)


def _unmapped(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network, name: str
) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Write an IPv4-mapped IPv6 range (``::ffff:192.168.1.10``) as the IPv4 range it means.

    The proxy and the relays judge a mapped address as its IPv4 address, so the IPv6 spelling
    of a range would never match anything.

    Parameters
    ----------
    network
        A range of the setting.
    name
        The setting, for the log.

    Returns
    -------
    IPv4Network or IPv6Network
        The IPv4 range for a range inside ``::ffff:0:0/96``; `network` otherwise.
    """
    mapped = network.network_address.ipv4_mapped if network.version == 6 else None
    if mapped is None or network.prefixlen < 96:
        return network
    ipv4 = ipaddress.IPv4Network(f"{mapped}/{network.prefixlen - 96}")
    logger.info("%s: read the IPv4-mapped range %s as %s.", name, network, ipv4)
    return ipv4


def _path(environ: Mapping[str, str], name: str, default: Path) -> Path:
    """Read an absolute path setting.

    Parameters
    ----------
    environ
        The environment.
    name
        The name after the prefix.
    default
        The value when unset.

    Returns
    -------
    Path
        The path.

    Raises
    ------
    SettingsError
        If it is not absolute.
    """
    text = _get(environ, name)
    if text is None:
        return default
    if not _plain_absolute(text):
        raise SettingsError(
            f"{PREFIX}{name} must be an absolute path without '..', a leading '//' "
            "or control characters."
        )
    return Path(text)


def _plain_absolute(text: str) -> bool:
    """Say whether a path is absolute, normalised and spelled with one leading slash.

    POSIX keeps exactly two leading slashes through normalisation, and the kernel reads
    ``//run`` as ``/run``, so a comparison of the literal text would miss it.

    Parameters
    ----------
    text
        The path.

    Returns
    -------
    bool
        ``True`` for a path such as ``/srv/landing``.
    """
    return (
        os.path.isabs(text)
        and os.path.normpath(text) == text
        and not text.startswith("//")
        and _CONTROL.search(text) is None
    )


def _is_under(path: Path, parent: Path) -> bool:
    """Say whether a path is a directory inside another, or the same one.

    Parameters
    ----------
    path
        The candidate.
    parent
        The directory.

    Returns
    -------
    bool
        ``True`` when `path` is `parent` or below it.
    """
    return path == parent or parent in path.parents


def _unsafe_root(path: Path, socket: Path) -> bool:
    """Say whether a directory must never be an ingestion root.

    Parameters
    ----------
    path
        The root, absolute and normalised.
    socket
        The launcher's own socket.

    Returns
    -------
    bool
        ``True`` when `path` is, sits under or holds a directory of `FORBIDDEN_ROOTS`, the
        directory of the Docker socket or the directory of the launcher's socket.
    """
    if path == Path("/"):
        return True
    sensitive = (*FORBIDDEN_ROOTS[1:], DOCKER_SOCKET.parent, socket.parent)
    return any(_is_under(path, bad) or _is_under(bad, path) for bad in sensitive)


def load_settings(environ: Mapping[str, str]) -> LauncherSettings:
    """Read and check every `LAKANA_LAUNCHER__*` setting.

    Parameters
    ----------
    environ
        The process environment.

    Returns
    -------
    LauncherSettings
        The settings.

    Raises
    ------
    SettingsError
        For the first setting that is missing or malformed.
    """
    for name in sorted(environ):
        if (
            name.startswith(PREFIX)
            and name[len(PREFIX) :] not in KNOWN_SETTINGS
            and environ[name].strip()
        ):
            short = name[len(PREFIX) :]
            reason = " (it is a constant now; remove it)" if short in _CONSTANT_NOW else ""
            raise SettingsError(
                f"{name} is not a setting{reason}; the settings are "
                + ", ".join(PREFIX + known for known in KNOWN_SETTINGS)
                + "."
            )
    stack = _get(environ, "STACK")
    if stack is None:
        raise SettingsError(
            f"{PREFIX}STACK is not set; the Compose bundle sets it to the project name "
            "(${COMPOSE_PROJECT_NAME})."
        )
    if _STACK.match(stack) is None:
        raise SettingsError(
            f"{PREFIX}STACK must be the Compose project name: lower-case letters, digits, '-' "
            "and '_', at most 63."
        )
    images_message = (
        f"{PREFIX}IMAGES must be a JSON object from Python version to task image, such as "
        '{"3.14": "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.14"}; the versions are '
        f"{', '.join(SUPPORTED_PYTHONS)}."
    )
    images = _json_object(environ, "IMAGES", images_message)
    if images is None:
        raise SettingsError(f"{PREFIX}IMAGES is not set. " + images_message)
    if not images or not all(
        key in SUPPORTED_PYTHONS and isinstance(value, str) and _IMAGE.match(value)
        for key, value in images.items()
    ):
        raise SettingsError(images_message)
    roots_message = (
        f"{PREFIX}INGESTION_ROOTS must be a JSON object from a name (lower-case letters, digits, "
        "'-' and '_') to an absolute host directory that is not a system directory, not the "
        "Docker socket's directory and not the launcher's own: / and anything that holds or sits "
        "under /run, /var/run, /var/lib/docker, /proc, /sys, /dev or /etc is refused."
    )
    socket = _path(environ, "SOCKET", LauncherSettings.socket)
    roots = _json_object(environ, "INGESTION_ROOTS", roots_message) or {}
    for key, value in roots.items():
        if (
            _ROOT_NAME.match(key) is None
            or not isinstance(value, str)
            or not _plain_absolute(value)
            or _unsafe_root(Path(value), socket)
        ):
            raise SettingsError(roots_message)
    no_new_privileges = (_get(environ, "NO_NEW_PRIVILEGES") or "auto").lower()
    if no_new_privileges not in ("auto", "on"):
        raise SettingsError(
            f"{PREFIX}NO_NEW_PRIVILEGES must be auto or on (auto leaves it off where Docker "
            "refuses it, as Docker installed as a snap does)."
        )
    max_memory = _number(environ, "MAX_MEMORY_MB", 16384, 64, 1 << 24)
    max_total = _number(environ, "MAX_TOTAL_MEMORY_MB", max(65536, max_memory), 64, 1 << 30)
    if max_total < max_memory:
        raise SettingsError(
            f"{PREFIX}MAX_TOTAL_MEMORY_MB must be at least {PREFIX}MAX_MEMORY_MB, or the biggest "
            "task could never run."
        )
    return LauncherSettings(
        stack=stack,
        images={str(k): str(v) for k, v in images.items()},
        socket=socket,
        ingestion_roots={str(k): str(v) for k, v in roots.items()},
        no_new_privileges=no_new_privileges,
        max_memory_mb=max_memory,
        max_cpus=float(_number(environ, "MAX_CPUS", 8, 1, 1024)),
        max_pids=_number(environ, "MAX_PIDS", 4096, 16, 4_194_304),
        probe_max_entries=_number(environ, "PROBE_MAX_ENTRIES", 100000, 1, 1_000_000_000),
        network_range=_network_range(environ),
        max_total_memory_mb=max_total,
        egress_refused=_egress_refused(environ),
    )
