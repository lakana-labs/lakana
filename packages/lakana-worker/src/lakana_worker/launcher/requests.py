"""What the launcher accepts, and the container and network specs it builds from it.

A request is one JSON object with exactly the fields of its operation (`FIELDS`), and every
value is checked against a pattern or a bound before anything else happens. Every Docker field
that matters is fixed here: the image comes from the operator's allow-list, the mounts from the
work volume's code, task and trust subpaths, the environment volume and the allowed
ingestion roots; user, capabilities, network and command are constants. Nothing the caller
sends reaches Docker as-is: identifiers become names only through the patterns, the stdin text
becomes the container's input stream, and an index credential becomes one entry of
`UV_INDEX`.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, NoReturn
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from lakana_worker.launcher.egress_token import MAX_TARGETS as MAX_TOKEN_TARGETS
from lakana_worker.launcher.egress_token import valid_host
from lakana_worker.launcher.settings import LauncherSettings

MAX_REQUEST_BYTES = 512 * 1024
MAX_STDIN_BYTES = 256 * 1024
MAX_INDEXES = 16
MAX_TARGETS = 32
"""The most host:port pairs one build may reach."""
BUILD_DOWNLOADS = 16
"""uv's parallel downloads in a build (`UV_CONCURRENT_DOWNLOADS`, 50 by default): half the egress
proxy's default cap of 32 connections per build network, which leaves room for the connections
uv keeps open to an index's other host and for a build backend's own."""
MAX_ROOTS = 16
MAX_EGRESS = MAX_TOKEN_TARGETS
"""The most host:port pairs one task may reach through the egress proxy: what a token holds."""
MAX_RELAY_ROUTES = 16
"""The most database connections one task's relay forwards."""
RELAY_PORTS = range(15001, 15065)
"""The ports a relay listens on, one per route."""
RELAY_MAX_PER_PORT = 16
"""The most connections one route of a relay carries at once."""
RELAY_PIDS = MAX_RELAY_ROUTES * (RELAY_MAX_PER_PORT + 1) + 2 + 16
"""The relay container's process limit: a thread per connection and an accepting thread per
route at their maximum, the main thread and Docker's init, and 16 to spare."""
EGRESS_MISSING = (
    "tasks with connections need the `egress` service; start it (Compose bundle) or remove the "
    "connection from the asset."
)
"""The refusal of a run with egress while the egress service is not running."""
RELAY_ALIAS = "lakana-relay"
"""The name a task resolves to its relay's address."""
RELAY_USER = "65534:65534"
"""The relay runs as nobody: it owns no file and mounts nothing."""
RELAY_COMMAND = (
    "/usr/local/bin/python",
    "-I",
    "-S",
    "-c",
    "import sys; sys.path.insert(0, '/opt/lakana-launcher'); "
    "from lakana_worker.launcher.relay import main; main()",
)
"""The relay's entry point in the egress service's image: the base interpreter on the
launcher's own copy of its package, as the egress proxy runs (no site-packages, no `.pth`)."""
_EGRESS_RULE = f"egress: a list of at most {MAX_EGRESS} distinct [host, port] pairs"
_RELAY_RULE = (
    f"relay: at most {MAX_RELAY_ROUTES} routes, each a listen port from {RELAY_PORTS.start} to "
    f"{RELAY_PORTS.stop - 1} (distinct) and a host and port"
)
MAX_CREDENTIAL_CHARS = 4096
MAX_URL_CHARS = 2048
MAX_KILL_GRACE = 300
MAX_ENVIRONMENT_BYTES = 1 << 40
MIN_MEMORY_MB = 64
TRUST_FILE = "trust.pem"
"""The trust bundle's file name, in an attempt's ``trust`` directory and in the container."""
TRUST_MOUNT = "/trust"
"""Where a container sees its attempt's ``trust`` directory, read-only."""
TRUST_PATH = f"{TRUST_MOUNT}/{TRUST_FILE}"
"""What `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` name in a container that trusts the bundle."""
INDEX_NAME_PREFIX = "lakana-registered-"
"""Prefix of the index names in `UV_INDEX`; the credential is bound to the URL, not the name."""

LABEL_STACK = "lakana.launcher.stack"
LABEL_KIND = "lakana.launcher.kind"
LABEL_ID = "lakana.launcher.id"
LABEL_OWNER = "lakana.launcher.owner"
LABEL_DEADLINE = "lakana.launcher.deadline"
"""Epoch seconds after which a container or network is the reaper's to remove."""
LABEL_SHARED = "lakana.launcher.shared"
"""``1`` on a volume that several environments may mount (the wheels-only index cache)."""
DEADLINE_MARGIN = 120
"""Seconds past a run's timeout and kill grace that its deadline label allows."""

_ID = re.compile(r"\A[a-z0-9][a-z0-9-]{0,62}\Z")
_OWNER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_KEY = re.compile(r"\A[0-9a-f]{64}\Z")
_ATTEMPT = re.compile(r"\Aattempt-[a-z0-9_]{1,32}\Z")
_HOST = re.compile(
    r"\A(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
_URL_CHARS = re.compile(r"[A-Za-z0-9._~:/@!$&'()*+,;=%\[\]-]+")
_NUMERIC_LABEL = re.compile(r"[0-9]")
MIN_CPUS = 0.01
"""Least CPU share Docker accepts; below it a share rounds to no limit at all."""

_ID_RULE = "lower-case letters, digits and '-', at most 63"
_OWNER_RULE = "letters, digits, '.', '_', ':' and '-', at most 128"
_KEY_RULE = "64 lower-case hex digits"
_ATTEMPT_RULE = "an attempt directory's name"

_LIMITS = frozenset({"memory_mb", "cpus", "pids", "timeout_seconds"})
FIELDS: dict[str, frozenset[str]] = {
    "run": frozenset(
        {
            "token",
            "op",
            "task",
            "owner",
            "python",
            "environment",
            "attempt",
            "kill_grace_seconds",
            "stdin",
            "roots",
            "trust",
            "egress",
            "relay",
        }
    )
    | _LIMITS,
    "build": frozenset(
        {
            "token",
            "op",
            "build",
            "owner",
            "python",
            "environment",
            "attempt",
            "indexes",
            "operator_index",
            "targets",
            "no_build",
            "max_bytes",
            "trust",
        }
    )
    | _LIMITS,
    "stop": frozenset({"token", "op", "id", "owner"}),
    "status": frozenset({"token", "op", "python", "environment"}),
    "cleanup": frozenset({"token", "op", "owner"}),
}
"""The exact fields of each operation."""


class Refused(Exception):
    """A request the launcher does not run; the text names the field and the rule."""


@dataclass(frozen=True)
class RelayRoute:
    """One port of a task's relay and the target it forwards to.

    Parameters
    ----------
    listen
        The port the relay listens on, in `RELAY_PORTS`.
    host
        The target's host, as `valid_host` spells it.
    port
        The target's port.
    """

    listen: int
    host: str
    port: int


@dataclass(frozen=True)
class Limits:
    """A container's limits, each within the operator's ceiling.

    Parameters
    ----------
    memory_mb
        Memory and memory plus swap, in MiB.
    cpus
        CPUs.
    pids
        Most processes.
    timeout_seconds
        Seconds before the launcher stops the container.
    """

    memory_mb: int
    cpus: float
    pids: int
    timeout_seconds: int


@dataclass(frozen=True)
class IndexCredential:
    """A registered package index a build may present a credential to.

    Parameters
    ----------
    url
        ``https://host[:port]/path``, without userinfo.
    host
        Its host, lower case.
    username
        The user name; may be empty. Left out of `repr`.
    password
        The password. Left out of `repr`.
    """

    url: str
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class RunRequest:
    """Run one attempt of a Python task.

    Parameters
    ----------
    task
        The attempt's id; names the container and its network.
    owner
        The worker that asked: only it may stop the run (`stop`, a closed connection), and
        `cleanup` removes by it.
    python
        The Python version; selects the image.
    environment
        The environment key (hex SHA-256).
    attempt
        The attempt directory's name in the work volume.
    limits
        Memory, CPUs, processes, timeout.
    kill_grace_seconds
        Seconds between SIGTERM and SIGKILL when the container is stopped.
    stdin
        The task description, written to the container's stdin. Left out of `repr`.
    roots
        Allowed ingestion roots to mount read-only.
    trust
        Whether the container mounts its attempt's trust bundle at ``/trust``.
    egress
        The host and port pairs the task may reach through the egress proxy; empty for a task
        without such connections, whose network then holds the server alone.
    relay
        The routes of the task's relay; empty for a task without a database connection.
    """

    task: str
    owner: str
    python: str
    environment: str
    attempt: str
    limits: Limits
    kill_grace_seconds: int
    stdin: bytes = field(repr=False)
    roots: tuple[str, ...] = ()
    trust: bool = False
    egress: tuple[tuple[str, int], ...] = ()
    relay: tuple[RelayRoute, ...] = ()


@dataclass(frozen=True)
class BuildRequest:
    """Build one environment.

    Parameters
    ----------
    build
        The build's id; names the container and its network.
    owner
        The worker that asked: only it may stop the build.
    python
        The Python version; selects the image.
    environment
        The environment key.
    attempt
        The attempt directory whose ``code`` holds `pyproject.toml` and `uv.lock`.
    indexes
        Registered indexes with a credential.
    operator_index
        `LAKANA_RUN__INDEX_URL`, which may carry its own credential. Left out of `repr`.
    targets
        Every host and port the build may reach through the egress proxy.
    no_build
        Whether uv may install wheels only (a credential is in play).
    limits
        Memory, CPUs, processes, timeout.
    max_bytes
        Largest size of the built environment.
    trust
        Whether the container mounts its attempt's trust bundle at ``/trust``.
    """

    build: str
    owner: str
    python: str
    environment: str
    attempt: str
    indexes: tuple[IndexCredential, ...]
    operator_index: str | None = field(repr=False)
    targets: tuple[tuple[str, int], ...]
    no_build: bool
    limits: Limits
    max_bytes: int
    trust: bool = False


@dataclass(frozen=True)
class StopRequest:
    """Stop a running task or build of the worker that asks.

    Parameters
    ----------
    id
        The task's or build's id.
    owner
        The worker that asks; only a run it started is stopped.
    """

    id: str
    owner: str


@dataclass(frozen=True)
class StatusRequest:
    """Ask whether an environment is built.

    Parameters
    ----------
    python
        The Python version.
    environment
        The environment key.
    """

    python: str
    environment: str


@dataclass(frozen=True)
class CleanupRequest:
    """Remove a worker's containers that nothing supervises any more.

    Parameters
    ----------
    owner
        The worker.
    """

    owner: str


Request = RunRequest | BuildRequest | StopRequest | StatusRequest | CleanupRequest


def _encodes(text: str) -> bool:
    """Say whether a string can be written as UTF-8.

    JSON accepts a lone surrogate escape, which no UTF-8 encoder takes.

    Parameters
    ----------
    text
        The string.

    Returns
    -------
    bool
        ``False`` for a string with a lone surrogate.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a request object, refusing a field named twice.

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
    Refused
        If a key occurs twice: the last one would silently win.
    """
    result = dict(pairs)
    if len(result) != len(pairs):
        raise Refused("a request repeats the field of a JSON object; send each field once")
    return result


def _no_constant(name: str) -> NoReturn:
    """Refuse `NaN`, `Infinity` and `-Infinity`, which JSON does not have.

    Parameters
    ----------
    name
        The constant.

    Raises
    ------
    Refused
        Always.
    """
    raise Refused(f"a request is one JSON object without {name}")


def _pattern(data: dict[str, Any], key: str, pattern: re.Pattern[str], rule: str) -> str:
    """Read a text field that must match a pattern.

    Parameters
    ----------
    data
        The request.
    key
        The field.
    pattern
        The pattern.
    rule
        The rule in words.

    Returns
    -------
    str
        The value.

    Raises
    ------
    Refused
        If it is not text or does not match.
    """
    value = data[key]
    if not isinstance(value, str) or pattern.match(value) is None:
        raise Refused(f"{key}: {rule}")
    return value


def _raise_with(setting: str | None) -> str:
    """Name the setting that raises a ceiling.

    Parameters
    ----------
    setting
        The variable's name after the prefix, or `None` for a fixed ceiling.

    Returns
    -------
    str
        A parenthesis for a refusal's text, or nothing.
    """
    return f" (LAKANA_LAUNCHER__{setting}; the operator raises it)" if setting else ""


def _integer(
    data: dict[str, Any], key: str, low: int, high: int, setting: str | None = None
) -> int:
    """Read a whole number within bounds; `True` is not a number here.

    Parameters
    ----------
    data
        The request.
    key
        The field.
    low
        The least value.
    high
        The greatest value.
    setting
        The launcher setting that raises `high`, named in the refusal.

    Returns
    -------
    int
        The value.

    Raises
    ------
    Refused
        If it is not an `int` in range.
    """
    value = data[key]
    if type(value) is not int or not low <= value <= high:
        raise Refused(f"{key}: an integer from {low} to {high}" + _raise_with(setting))
    return value


def _limits(data: dict[str, Any], settings: LauncherSettings) -> Limits:
    """Read the four limits.

    Parameters
    ----------
    data
        The request.
    settings
        The ceilings.

    Returns
    -------
    Limits
        The limits.

    Raises
    ------
    Refused
        If one is not a number within its ceiling.
    """
    cpus = data["cpus"]
    if (
        type(cpus) not in (int, float)
        or not math.isfinite(cpus)
        or not MIN_CPUS <= cpus <= settings.max_cpus
    ):
        raise Refused(
            f"cpus: a number from {MIN_CPUS:g} to {settings.max_cpus:g}" + _raise_with("MAX_CPUS")
        )
    return Limits(
        memory_mb=_integer(
            data, "memory_mb", MIN_MEMORY_MB, settings.max_memory_mb, "MAX_MEMORY_MB"
        ),
        cpus=float(cpus),
        pids=_integer(data, "pids", 1, settings.max_pids, "MAX_PIDS"),
        timeout_seconds=_integer(data, "timeout_seconds", 1, settings.max_seconds),
    )


def _python(data: dict[str, Any], settings: LauncherSettings) -> str:
    """Read the Python version, which must name an allowed image.

    Parameters
    ----------
    data
        The request.
    settings
        The allow-list.

    Returns
    -------
    str
        The version.

    Raises
    ------
    Refused
        If no allowed image has it.
    """
    value = data["python"]
    if not isinstance(value, str) or value not in settings.images:
        raise Refused(
            f"python must be one of {', '.join(sorted(settings.images))} (LAKANA_LAUNCHER__IMAGES)"
        )
    return value


def _allowed_host(host: str) -> bool:
    """Say whether a host name may be reached by a build.

    A host name is allowed. A host whose last label starts with a digit is a number, and is
    allowed only as a global unicast IP address: loopback, link-local (the cloud metadata
    address), private, shared (100.64.0.0/10), multicast and unspecified addresses, and the
    short or hex spellings a resolver would turn into one, are not.

    Parameters
    ----------
    host
        A lower-case host from a request.

    Returns
    -------
    bool
        ``True`` if the build may be told to reach it.
    """
    if _HOST.match(host) is None:
        return False
    if _NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]) is None:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_global and not address.is_multicast


def _port(text: str) -> int | None:
    """Read a port in its one spelling.

    Parameters
    ----------
    text
        The text after a host's colon.

    Returns
    -------
    int or None
        1 to 65535, written in ASCII digits without a sign or a leading zero; `None` otherwise.
    """
    if not text.isascii() or not text.isdigit() or text.startswith("0") or len(text) > 5:
        return None
    number = int(text)
    return number if number < 65536 else None


def _target(item: object) -> tuple[str, int] | None:
    """Read one ``host:port`` of a build's targets.

    Parameters
    ----------
    item
        An element of ``targets``.

    Returns
    -------
    tuple or None
        The host and the port; `None` unless the host is one a build may reach
        (`_allowed_host`) and the port is in its one spelling.
    """
    if not isinstance(item, str):
        return None
    host, colon, port_text = item.rpartition(":")
    port = _port(port_text)
    if not colon or port is None or not _allowed_host(host):
        return None
    return host, port


def _endpoint(url: str) -> tuple[str, int]:
    """Name the host and port a URL that `_host_of` accepted connects to.

    Parameters
    ----------
    url
        The URL.

    Returns
    -------
    tuple
        The lower-case host and the port, the scheme's own (443 for https, 80 for http) when the
        URL names none.
    """
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _host_of(url: str, *, schemes: tuple[str, ...], userinfo: bool) -> str | None:
    """Return the host of a URL the launcher will hand to uv, or `None` for a bad one.

    Parameters
    ----------
    url
        The URL.
    schemes
        The schemes allowed.
    userinfo
        Whether a user name and password may be in it.

    Returns
    -------
    str or None
        The lower-case host.
    """
    if len(url) > MAX_URL_CHARS or _CONTROL.search(url) or _URL_CHARS.fullmatch(url) is None:
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if (
        parts.scheme not in schemes
        or host is None
        or not _allowed_host(host)
        or parts.query
        or parts.fragment
        or (not userinfo and "@" in parts.netloc)
    ):
        return None
    return host


def _credential(item: object) -> IndexCredential:
    """Read one index credential.

    Parameters
    ----------
    item
        An element of ``indexes``.

    Returns
    -------
    IndexCredential
        The credential.

    Raises
    ------
    Refused
        If it is not exactly ``url``, ``username``, ``password`` of the right shapes.
    """
    rule = (
        "a list of at most 16 {url, username, password}: an https URL without userinfo, a user "
        "name and a non-empty password of at most 4096 characters each, no NUL"
    )
    if not isinstance(item, dict) or set(item) != {"url", "username", "password"}:
        raise Refused(f"indexes: {rule}")
    url, username, password = item["url"], item["username"], item["password"]
    host = _host_of(url, schemes=("https",), userinfo=False) if isinstance(url, str) else None
    texts = isinstance(username, str) and isinstance(password, str)
    if host is None or not texts or not password:
        raise Refused(f"indexes: {rule}")
    if any(
        len(v) > MAX_CREDENTIAL_CHARS or "\x00" in v or not _encodes(v)
        for v in (username, password)
    ):
        raise Refused(f"indexes: {rule}")
    return IndexCredential(url=url, host=host, username=username, password=password)


def _pair(item: object) -> tuple[str, int] | None:
    """Read one ``[host, port]`` of a run's egress or relay.

    Parameters
    ----------
    item
        The JSON value.

    Returns
    -------
    tuple or None
        The pair; `None` unless the host is lower case as `valid_host` spells it and the port is
        a JSON integer from 1 to 65535. Private addresses pass: a source is often on one. The
        proxy and the relay refuse loopback, link-local, metadata and the stack's own networks
        when they resolve the host, whatever this says.
    """
    if not isinstance(item, list) or len(item) != 2:
        return None
    host, port = item
    if not isinstance(host, str) or not valid_host(host):
        return None
    if type(port) is not int or not 0 < port < 65536:
        return None
    return host, port


def _egress(data: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    """Read ``egress``.

    Parameters
    ----------
    data
        The request.

    Returns
    -------
    tuple
        The pairs, in request order.

    Raises
    ------
    Refused
        With `_EGRESS_RULE`.
    """
    raw = data["egress"]
    if not isinstance(raw, list) or len(raw) > MAX_EGRESS:
        raise Refused(_EGRESS_RULE)
    pairs = [_pair(item) for item in raw]
    found = [pair for pair in pairs if pair is not None]
    if len(found) != len(pairs) or len(set(found)) != len(found):
        raise Refused(_EGRESS_RULE)
    return tuple(found)


def _relay_routes(data: dict[str, Any]) -> tuple[RelayRoute, ...]:
    """Read ``relay``.

    Parameters
    ----------
    data
        The request.

    Returns
    -------
    tuple of RelayRoute
        The routes, in request order.

    Raises
    ------
    Refused
        With `_RELAY_RULE`.
    """
    raw = data["relay"]
    if not isinstance(raw, list) or len(raw) > MAX_RELAY_ROUTES:
        raise Refused(_RELAY_RULE)
    routes: list[RelayRoute] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"listen", "host", "port"}:
            raise Refused(_RELAY_RULE)
        listen = item["listen"]
        pair = _pair([item["host"], item["port"]])
        if type(listen) is not int or listen not in RELAY_PORTS or pair is None:
            raise Refused(_RELAY_RULE)
        routes.append(RelayRoute(listen, *pair))
    if len({route.listen for route in routes}) != len(routes):
        raise Refused(_RELAY_RULE)
    return tuple(routes)


def _trust(data: dict[str, Any]) -> bool:
    """Read the ``trust`` field of a run or a build.

    Parameters
    ----------
    data
        The request.

    Returns
    -------
    bool
        Whether the container mounts its attempt's trust bundle.

    Raises
    ------
    Refused
        For anything but a JSON boolean.
    """
    if type(data["trust"]) is not bool:
        raise Refused("trust: true or false")
    return data["trust"]


def _trust_mount(settings: LauncherSettings, attempt: str) -> dict[str, Any]:
    """Describe the read-only mount of an attempt's trust bundle.

    Parameters
    ----------
    settings
        For the work volume.
    attempt
        The attempt directory's name.

    Returns
    -------
    dict
        One entry of ``HostConfig.Mounts``.
    """
    return _volume(settings.work_volume, TRUST_MOUNT, read_only=True, subpath=f"{attempt}/trust")


def _run(data: dict[str, Any], settings: LauncherSettings) -> RunRequest:
    """Read a ``run`` request.

    Parameters
    ----------
    data
        The request.
    settings
        The allow-lists and ceilings.

    Returns
    -------
    RunRequest
        The request.

    Raises
    ------
    Refused
        For the first field that breaks its rule.
    """
    stdin = data["stdin"]
    if (
        not isinstance(stdin, str)
        or not _encodes(stdin)
        or len(stdin.encode("utf-8")) > MAX_STDIN_BYTES
    ):
        raise Refused("stdin: a string of at most 256 KiB")
    roots = data["roots"]
    names = ", ".join(sorted(settings.ingestion_roots)) or "none is set"
    root_rule = f"roots: a list of distinct names from LAKANA_LAUNCHER__INGESTION_ROOTS ({names})"
    if (
        not isinstance(roots, list)
        or len(roots) > MAX_ROOTS
        or not all(isinstance(r, str) and r in settings.ingestion_roots for r in roots)
        or len(set(roots)) != len(roots)
    ):
        raise Refused(root_rule)
    return RunRequest(
        task=_pattern(data, "task", _ID, _ID_RULE),
        owner=_pattern(data, "owner", _OWNER, _OWNER_RULE),
        python=_python(data, settings),
        environment=_pattern(data, "environment", _KEY, _KEY_RULE),
        attempt=_pattern(data, "attempt", _ATTEMPT, _ATTEMPT_RULE),
        limits=_limits(data, settings),
        kill_grace_seconds=_integer(data, "kill_grace_seconds", 0, MAX_KILL_GRACE),
        stdin=stdin.encode("utf-8"),
        roots=tuple(roots),
        trust=_trust(data),
        egress=_egress(data),
        relay=_relay_routes(data),
    )


def _build(data: dict[str, Any], settings: LauncherSettings) -> BuildRequest:
    """Read a ``build`` request.

    Parameters
    ----------
    data
        The request.
    settings
        The allow-lists and ceilings.

    Returns
    -------
    BuildRequest
        The request.

    Raises
    ------
    Refused
        For the first field that breaks its rule.
    """
    indexes = data["indexes"]
    if not isinstance(indexes, list) or len(indexes) > MAX_INDEXES:
        raise Refused("indexes: a list of at most 16 credentials")
    credentials = tuple(_credential(item) for item in indexes)
    sent = data["targets"]
    targets = [_target(item) for item in sent] if isinstance(sent, list) else []
    if not 1 <= len(targets) <= MAX_TARGETS or None in targets or len(set(targets)) != len(targets):
        raise Refused(
            "targets: a list of 1 to 32 distinct host:port, each a lower-case host name or a "
            "public IP address and a port from 1 to 65535"
        )
    allowed = {target for target in targets if target is not None}
    operator = data["operator_index"]
    operator_host = None
    if operator is not None:
        operator_host = (
            _host_of(operator, schemes=("http", "https"), userinfo=True)
            if isinstance(operator, str)
            else None
        )
        if operator_host is None:
            raise Refused("operator_index: null or an http(s) URL without spaces")
    urls = [c.url for c in credentials] + ([operator] if operator_host is not None else [])
    if any(_endpoint(url) not in allowed for url in urls):
        raise Refused("indexes: every index's host and port must be in targets")
    if type(data["no_build"]) is not bool:
        raise Refused("no_build: true or false")
    return BuildRequest(
        build=_pattern(data, "build", _ID, _ID_RULE),
        owner=_pattern(data, "owner", _OWNER, _OWNER_RULE),
        python=_python(data, settings),
        environment=_pattern(data, "environment", _KEY, _KEY_RULE),
        attempt=_pattern(data, "attempt", _ATTEMPT, _ATTEMPT_RULE),
        indexes=credentials,
        operator_index=operator,
        targets=tuple(target for target in targets if target is not None),
        no_build=data["no_build"],
        limits=_limits(data, settings),
        max_bytes=_integer(data, "max_bytes", 1, MAX_ENVIRONMENT_BYTES),
        trust=_trust(data),
    )


def parse_request(raw: bytes, *, token: str, settings: LauncherSettings) -> Request:
    """Read one request, refusing anything outside the schema.

    Parameters
    ----------
    raw
        The request line as read from the socket.
    token
        The launcher's token; the request must carry it.
    settings
        The allow-lists and ceilings.

    Returns
    -------
    Request
        The parsed request.

    Raises
    ------
    Refused
        For an oversized or malformed line, a wrong token, an unknown operation, a field too
        many or too few, or the first field that breaks its rule.
    """
    if len(raw) > MAX_REQUEST_BYTES:
        raise Refused("a request is one JSON line of at most 512 KiB")
    try:
        data = json.loads(
            raw.decode("utf-8"), parse_constant=_no_constant, object_pairs_hook=_unique_pairs
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused("a request is one JSON object") from None
    if not isinstance(data, dict):
        raise Refused("a request is one JSON object")
    sent = data.get("token")
    if (
        not isinstance(sent, str)
        or not _encodes(sent)
        or not hmac.compare_digest(sent.encode("utf-8"), token.encode("utf-8"))
    ):
        raise Refused(
            "the request does not carry the launcher's token; LAKANA_RUN__LAUNCHER_SOCKET "
            "must point into the launcher's own volume"
        )
    op = data.get("op")
    if not isinstance(op, str) or op not in FIELDS:
        raise Refused(f"op must be one of {', '.join(sorted(FIELDS))}")
    if set(data) != FIELDS[op]:
        raise Refused(
            f"{op} takes exactly the fields {', '.join(sorted(FIELDS[op]))}; the worker and "
            "the launcher must be the same release"
        )
    if op == "run":
        return _run(data, settings)
    if op == "build":
        return _build(data, settings)
    if op == "stop":
        return StopRequest(
            _pattern(data, "id", _ID, _ID_RULE), _pattern(data, "owner", _OWNER, _OWNER_RULE)
        )
    if op == "status":
        return StatusRequest(
            python=_python(data, settings),
            environment=_pattern(data, "environment", _KEY, _KEY_RULE),
        )
    return CleanupRequest(_pattern(data, "owner", _OWNER, _OWNER_RULE))


def container_name(settings: LauncherSettings, kind: str, ident: str) -> str:
    """Name a container or network the launcher creates.

    Parameters
    ----------
    settings
        For the stack.
    kind
        ``task``, ``build`` or ``probe``.
    ident
        The id from a request (already checked) or a fixed word.

    Returns
    -------
    str
        ``<stack>-<kind>-<ident>``.
    """
    return f"{settings.stack}-{kind}-{ident}"


def labels(
    settings: LauncherSettings, kind: str, ident: str, owner: str, deadline: int | None = None
) -> dict[str, str]:
    """Label what the launcher creates, so a restart and `cleanup` find it.

    Parameters
    ----------
    settings
        For the stack.
    kind
        ``task``, ``build`` or ``probe``.
    ident
        Its id.
    owner
        The worker that asked.
    deadline
        Epoch seconds after which the reaper removes it; `None` for what lives only a moment.

    Returns
    -------
    dict
        The four labels, and the deadline when there is one.
    """
    found = {LABEL_STACK: settings.stack, LABEL_KIND: kind, LABEL_ID: ident, LABEL_OWNER: owner}
    if deadline is not None:
        found[LABEL_DEADLINE] = str(deadline)
    return found


def _digest(*parts: str) -> str:
    """Hash parts into a name-safe key.

    Parameters
    ----------
    *parts
        Texts, joined by newlines.

    Returns
    -------
    str
        The first 40 hex digits of their SHA-256.
    """
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:40]


def environment_volume(settings: LauncherSettings, python: str, key: str) -> str:
    """Name the volume of one built environment.

    The image is part of the name: a venv's `bin/python` is an absolute link into the image
    it was built in, so a new image needs a new build.

    Parameters
    ----------
    settings
        For the stack and the image.
    python
        An allowed Python version.
    key
        The environment key from the worker.

    Returns
    -------
    str
        ``<stack>-env-<digest>``.
    """
    return f"{settings.stack}-env-{_digest(settings.images[python], key)}"


def _operator_identity(request: BuildRequest, key: bytes) -> tuple[str, str]:
    """Split the operator index into its credential-free URL and a keyed fingerprint of who it is.

    Parameters
    ----------
    request
        The build.
    key
        The launcher's cache key.

    Returns
    -------
    tuple
        The URL without userinfo, and the fingerprint of its user name and password (both empty
        without an operator index).
    """
    if not request.operator_index:
        return "", ""
    parts = urlsplit(request.operator_index)
    url = urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
    return url, _fingerprint(key, url, unquote(parts.username or ""), unquote(parts.password or ""))


def _fingerprint(key: bytes, *parts: str) -> str:
    """Compute a keyed fingerprint, so equal credentials give equal names and nothing else does.

    Parameters
    ----------
    key
        The launcher's cache key.
    *parts
        The texts to bind, joined by newlines.

    Returns
    -------
    str
        An HMAC-SHA-256 in hex. The password cannot be recovered or checked from it without the
        key, which never leaves the launcher.
    """
    return hmac.new(key, "\n".join(parts).encode("utf-8"), "sha256").hexdigest()


def cache_volume(settings: LauncherSettings, request: BuildRequest, *, key: bytes) -> str:
    """Name the uv cache a build mounts.

    A wheels-only build (``no_build``) only downloads and unpacks wheels that the lock pins by
    hash; no package code runs, so builds that reach the same indexes with the same credentials
    share one cache. Any other build runs the code of an sdist or a path package as the build
    user with the cache writable, and could rewrite files that another project's later build
    would install: it gets a cache private to its own environment. The cache is never mounted
    into a task.

    Parameters
    ----------
    settings
        For the stack and the image.
    request
        The build.
    key
        The launcher's cache key (random, made once, kept in its state directory).

    Returns
    -------
    str
        ``<stack>-uvcache-<digest>`` (shared) or ``<stack>-uvcache-private-<digest>``. The digest
        holds the index set and a keyed HMAC of each credential's user name and password, so two
        teams with different tokens never share cache entries, even under one user name; a
        private cache also holds the environment.
    """
    operator, operator_who = _operator_identity(request, key)
    identity = (
        operator,
        operator_who,
        *sorted(f"{host}:{port}" for host, port in request.targets),
        *sorted(_fingerprint(key, c.url, c.username, c.password) for c in request.indexes),
    )
    if request.no_build:
        return f"{settings.stack}-uvcache-{_digest(*identity)}"
    environment = environment_volume(settings, request.python, request.environment)
    return f"{settings.stack}-uvcache-private-{_digest(environment, *identity)}"


def volume_spec(
    settings: LauncherSettings,
    name: str,
    kind: str,
    *,
    shared: bool,
    deadline: int | None = None,
) -> dict[str, Any]:
    """Describe an environment or cache volume, labelled so an uninstall or a cleanup finds it.

    Parameters
    ----------
    settings
        For the stack.
    name
        The volume's name.
    kind
        ``env`` or ``cache``.
    shared
        Whether several environments may mount it.
    deadline
        Epoch seconds after which the reaper removes it (a private cache that outlived its
        build), or `None`.

    Returns
    -------
    dict
        The body of ``POST /volumes/create``.
    """
    found = {
        LABEL_STACK: settings.stack,
        LABEL_KIND: kind,
        LABEL_SHARED: "1" if shared else "0",
    }
    if deadline is not None:
        found[LABEL_DEADLINE] = str(deadline)
    return {"Name": name, "Labels": found}


def network_spec(
    settings: LauncherSettings,
    kind: str,
    ident: str,
    owner: str,
    *,
    subnet: str | None = None,
    deadline: int | None = None,
) -> dict[str, Any]:
    """Describe the network of one task or build.

    Internal, so nothing routes out of it, and with an isolated gateway: a default internal
    bridge still reaches every host service listening on all interfaces through its gateway
    address (measured in the design trial).

    Parameters
    ----------
    settings
        For the stack.
    kind
        ``task`` or ``build``.
    ident
        Its id.
    owner
        The worker that asked.
    subnet
        The /29 the launcher allocated, or `None` for Docker's own pools (the start-up check).
    deadline
        Epoch seconds after which the reaper removes it.

    Returns
    -------
    dict
        The body of ``POST /networks/create``.
    """
    spec: dict[str, Any] = {
        "Name": container_name(settings, kind, ident),
        "Driver": "bridge",
        "Internal": True,
        "Attachable": False,
        "EnableIPv6": False,
        "Labels": labels(settings, kind, ident, owner, deadline),
        "Options": {
            "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
            "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
        },
    }
    if subnet is not None:
        spec["IPAM"] = {"Driver": "default", "Config": [{"Subnet": subnet}]}
    return spec


def _volume(
    source: str, target: str, *, read_only: bool, subpath: str | None = None
) -> dict[str, Any]:
    """Describe one volume mount.

    Parameters
    ----------
    source
        The volume's name.
    target
        Where it appears.
    read_only
        Whether it is read-only.
    subpath
        The directory inside the volume, for the work volume; Docker refuses one that leaves the
        volume through ``..`` or a link.

    Returns
    -------
    dict
        One entry of ``HostConfig.Mounts``. A new volume without a subpath is filled from the
        image's directory at `target` once, which gives it the image's owner.
    """
    options: dict[str, Any] = {"NoCopy": subpath is not None}
    if subpath is not None:
        options["Subpath"] = subpath
    return {
        "Type": "volume",
        "Source": source,
        "Target": target,
        "ReadOnly": read_only,
        "VolumeOptions": options,
    }


def _ingestion_mount(settings: LauncherSettings, name: str) -> dict[str, Any]:
    """Describe the read-only mount of one allowed ingestion root.

    Parameters
    ----------
    settings
        For the root's host path.
    name
        The root's name.

    Returns
    -------
    dict
        One entry of ``HostConfig.Mounts``. Non-recursive: a mount below the directory on the
        host does not appear in the container.
    """
    return {
        "Type": "bind",
        "Source": settings.ingestion_roots[name],
        "Target": f"/ingest/{name}",
        "ReadOnly": True,
        "BindOptions": {"Propagation": "rprivate", "NonRecursive": True},
    }


def _host_config(
    limits: Limits,
    network: str,
    mounts: list[dict[str, Any]],
    tmpfs: str,
    *,
    no_new_privileges: bool,
) -> dict[str, Any]:
    """Describe what every container of the launcher shares.

    Parameters
    ----------
    limits
        The limits.
    network
        Its own network.
    mounts
        Its mounts.
    tmpfs
        Options of the private `/tmp`.
    no_new_privileges
        Whether Docker allows the option here (`Launcher.start` found out).

    Returns
    -------
    dict
        ``HostConfig``.
    """
    memory = limits.memory_mb * 1024 * 1024
    return {
        "Mounts": mounts,
        "Binds": [],
        "Devices": [],
        "Memory": memory,
        "MemorySwap": memory,
        "NanoCpus": round(limits.cpus * 1_000_000_000),
        "PidsLimit": limits.pids,
        "CapDrop": ["ALL"],
        "CapAdd": [],
        "Privileged": False,
        "ReadonlyRootfs": True,
        "Init": True,
        "IpcMode": "private",
        "PidMode": "",
        "UsernsMode": "",
        "CgroupnsMode": "private",
        "NetworkMode": network,
        "PublishAllPorts": False,
        "Tmpfs": {"/tmp": tmpfs},  # nosec B108 -- the container's own tmpfs mount point
        "LogConfig": {"Type": "none"},
        "AutoRemove": False,
        "RestartPolicy": {"Name": "no"},
        "SecurityOpt": ["no-new-privileges"] if no_new_privileges else [],
    }


def run_container_spec(
    request: RunRequest,
    settings: LauncherSettings,
    *,
    no_new_privileges: bool,
    deadline: int | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Describe the container of one task attempt.

    With ``trust`` it also mounts the attempt's trust bundle read-only at ``/trust`` and points
    `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` at it. With a proxy URL it sets
    `LAKANA_EGRESS_PROXY`, never `HTTPS_PROXY` or `HTTP_PROXY`: libraries read those unasked,
    and the ingestion helpers choose per connection whether a request goes through the proxy.

    Parameters
    ----------
    request
        The run.
    settings
        Images, work volume, user and roots.
    no_new_privileges
        Whether to set the option.
    deadline
        Epoch seconds after which the reaper removes the container.
    proxy_url
        ``http://lakana:<token>@egress:3128`` for a run with egress, else `None`.

    Returns
    -------
    dict
        The body of ``POST /containers/create``.
    """
    memory = request.limits.memory_mb * 1024 * 1024
    polars = str(max(memory // 1_000_000, 1))
    mounts = [
        _volume(settings.work_volume, "/code", read_only=True, subpath=f"{request.attempt}/code"),
        _volume(
            environment_volume(settings, request.python, request.environment),
            "/env",
            read_only=True,
        ),
        _volume(settings.work_volume, "/work", read_only=False, subpath=f"{request.attempt}/task"),
        *(_ingestion_mount(settings, name) for name in request.roots),
    ]
    env = [
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/work/home",
        "TMPDIR=/work/tmp",
        "PYTHONUNBUFFERED=1",
        "PYTHONDONTWRITEBYTECODE=1",
        "POLARS_OOC_SPILL_DIR=/work/tmp/polars-spill",
        f"POLARS_OVERRIDE_TOTAL_MEMORY_MB={polars}",
        f"POLARS_OOC_DISK_BUDGET_MB={polars}",
    ]
    if request.trust:
        mounts.append(_trust_mount(settings, request.attempt))
        env += [f"SSL_CERT_FILE={TRUST_PATH}", f"LAKANA_CA_BUNDLE={TRUST_PATH}"]
    if proxy_url is not None:
        env.append(f"LAKANA_EGRESS_PROXY={proxy_url}")
    return {
        "Image": settings.images[request.python],
        "Entrypoint": ["/env/bin/python", "-P", "-m", "lakana.runner"],
        "Cmd": [],
        "User": settings.task_user,
        "WorkingDir": "/code",
        "Env": env,
        "AttachStdin": True,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": True,
        "StdinOnce": True,
        "Tty": False,
        "NetworkDisabled": False,
        "Labels": labels(settings, "task", request.task, request.owner, deadline),
        "HostConfig": _host_config(
            request.limits,
            container_name(settings, "task", request.task),
            mounts,
            "rw,noexec,nosuid,nodev,size=64m",
            no_new_privileges=no_new_privileges,
        ),
    }


def relay_address(subnet: str) -> str:
    """Name the address a task's relay takes in the task's /29.

    Parameters
    ----------
    subnet
        The task network's subnet.

    Returns
    -------
    str
        Its last host address, which Docker hands to no other container: the gateway, the
        server, the egress proxy and the task take the lowest free ones.
    """
    return str(list(ipaddress.IPv4Network(subnet).hosts())[-1])


def relay_container_spec(
    settings: LauncherSettings,
    *,
    kind: str,
    ident: str,
    owner: str,
    image: str,
    upstream: str | None,
    address: str,
    no_new_privileges: bool,
    deadline: int,
) -> dict[str, Any]:
    """Describe the relay container of one task attempt or connection check.

    Parameters
    ----------
    settings
        For the stack's names.
    kind
        ``task`` or ``check``: whose network the relay joins.
    ident
        The run's or check's id; the relay's labels carry it.
    owner
        The worker that asked.
    image
        The egress service's image id, which holds the launcher's package.
    upstream
        The egress service's `LAKANA_EGRESS__UPSTREAM_PROXY`, or `None`. Left in the
        container's environment only, never in a log.
    address
        `relay_address` of the network's subnet.
    no_new_privileges
        Whether to set the option.
    deadline
        The run's deadline label: the reaper removes the relay with it.

    Returns
    -------
    dict
        The body of ``POST /containers/create``; the outward network is connected after.
    """
    network = container_name(settings, kind, ident)
    env = ["PATH=/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1"]
    if upstream:
        env.append(f"LAKANA_EGRESS__UPSTREAM_PROXY={upstream}")
    # No timer of its own: the relay is removed with its run, or by the reaper at the deadline.
    limits = Limits(memory_mb=64, cpus=0.25, pids=RELAY_PIDS, timeout_seconds=0)
    return {
        "Image": image,
        "Entrypoint": list(RELAY_COMMAND),
        "Cmd": [],
        "User": RELAY_USER,
        "WorkingDir": "/",
        "Env": env,
        "AttachStdin": True,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": True,
        "StdinOnce": True,
        "Tty": False,
        "NetworkDisabled": False,
        "Labels": labels(settings, "relay", ident, owner, deadline),
        "HostConfig": _host_config(
            limits,
            network,
            [],
            "rw,noexec,nosuid,nodev,size=1m",
            no_new_privileges=no_new_privileges,
        ),
        "NetworkingConfig": {
            "EndpointsConfig": {
                network: {"Aliases": [RELAY_ALIAS], "IPAMConfig": {"IPv4Address": address}}
            }
        },
    }


def relay_plan(routes: tuple[RelayRoute, ...], settings: LauncherSettings, subnet: str) -> bytes:
    """Write the plan a relay reads on stdin.

    Parameters
    ----------
    routes
        The run's routes.
    settings
        For the launcher's range of task networks, which the relay refuses as a target.
    subnet
        The network's subnet.

    Returns
    -------
    bytes
        One JSON object.
    """
    return json.dumps(
        {
            "address": relay_address(subnet),
            "subnet": subnet,
            "internal": settings.network_range,
            "routes": [{"listen": r.listen, "host": r.host, "port": r.port} for r in routes],
        }
    ).encode("utf-8")


def uv_index_value(request: BuildRequest) -> str | None:
    """Build the build's `UV_INDEX`: the operator's index, then each credentialed index.

    uv keeps a credential written into an index URL by scheme, host and port and sends it to
    nothing else, whatever the project's files say.

    Parameters
    ----------
    request
        The build.

    Returns
    -------
    str or None
        Space-separated entries; `None` when there are none.
    """
    entries = [request.operator_index] if request.operator_index else []
    for number, index in enumerate(request.indexes):
        parts = urlsplit(index.url)
        userinfo = f"{quote(index.username, safe='')}:{quote(index.password, safe='')}"
        url = urlunsplit((parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, "", ""))
        entries.append(f"{INDEX_NAME_PREFIX}{number}={url}")
    return " ".join(entries) or None


def build_targets(request: BuildRequest) -> list[tuple[str, int]]:
    """List the host:port pairs a build's egress token grants: exactly the request's targets.

    The worker sends the host and port of every index the build may use, with a credential or
    without (#301); parsing refused a request whose credential or operator index is on a pair it
    does not list.

    Parameters
    ----------
    request
        The build.

    Returns
    -------
    list of tuple
        Distinct pairs, in the request's order.
    """
    return list(request.targets)


def build_container_spec(
    request: BuildRequest,
    settings: LauncherSettings,
    *,
    no_new_privileges: bool,
    proxy_url: str | None,
    cache_key: bytes,
    deadline: int | None = None,
) -> dict[str, Any]:
    """Describe the container that builds one environment.

    It mounts the attempt's code read-only (the lock may name wheels in the repository), its
    own environment volume and the cache of its index set, and runs the image's build entry. With
    ``trust`` it also mounts the attempt's trust bundle read-only at ``/trust`` and points
    `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` at it.

    Parameters
    ----------
    request
        The build.
    settings
        Images, work volume, user.
    no_new_privileges
        Whether to set the option.
    proxy_url
        The egress proxy with this build's token; `None` leaves the build without a way out.
    cache_key
        The launcher's cache key, for the cache volume's name.
    deadline
        Epoch seconds after which the reaper removes the container.

    Returns
    -------
    dict
        The body of ``POST /containers/create``.
    """
    # uv caches a local wheel or index file under its absolute `file://` URL; a path of its own
    # per build keeps one project's cached local file from matching another project's.
    source = f"/src/{request.build}"
    env = [
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/tmp",
        "TMPDIR=/tmp",
        "UV_PROJECT_ENVIRONMENT=/env",
        "UV_PYTHON_DOWNLOADS=never",
        "UV_CACHE_DIR=/uv-cache",
        "UV_NO_PROGRESS=1",
        f"UV_CONCURRENT_DOWNLOADS={BUILD_DOWNLOADS}",
        f"LAKANA_BUILD_NO_BUILD={1 if request.no_build else 0}",
        f"LAKANA_BUILD_MAX_BYTES={request.max_bytes}",
        f"LAKANA_BUILD_SRC={source}",
    ]
    index = uv_index_value(request)
    if index is not None:
        env.append(f"UV_INDEX={index}")
    if proxy_url is not None:
        env += [
            f"{name}={proxy_url}"
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
        ]
    mounts = [
        _volume(settings.work_volume, source, read_only=True, subpath=f"{request.attempt}/code"),
        _volume(
            environment_volume(settings, request.python, request.environment),
            "/env",
            read_only=False,
        ),
        _volume(cache_volume(settings, request, key=cache_key), "/uv-cache", read_only=False),
    ]
    if request.trust:
        mounts.append(_trust_mount(settings, request.attempt))
        env += [f"SSL_CERT_FILE={TRUST_PATH}", f"LAKANA_CA_BUNDLE={TRUST_PATH}"]
    return {
        "Image": settings.images[request.python],
        "Entrypoint": ["/usr/local/bin/python", "-I", "/opt/lakana-task/build.py"],
        "Cmd": [],
        "User": settings.build_user,
        "WorkingDir": source,
        "Env": env,
        "AttachStdout": True,
        "AttachStderr": True,
        "OpenStdin": False,
        "Tty": False,
        "Labels": labels(settings, "build", request.build, request.owner, deadline),
        "HostConfig": _host_config(
            request.limits,
            container_name(settings, "build", request.build),
            mounts,
            "rw,nosuid,nodev,size=1g",
            no_new_privileges=no_new_privileges,
        ),
    }


def probe_container_spec(
    settings: LauncherSettings, image: str, *, no_new_privileges: bool = True
) -> dict[str, Any]:
    """Describe the container that finds out whether `no-new-privileges` works here.

    Parameters
    ----------
    settings
        For the user and the labels.
    image
        An allowed image.
    no_new_privileges
        ``True`` for the probe, ``False`` for the control that tells a Docker that refuses the
        option from one that cannot start the image at all.

    Returns
    -------
    dict
        The body of ``POST /containers/create``: ``python -c pass``, no network, no mounts.
    """
    host = _host_config(
        Limits(memory_mb=MIN_MEMORY_MB, cpus=0.5, pids=16, timeout_seconds=60),
        "none",
        [],
        "rw,noexec,nosuid,nodev,size=1m",
        no_new_privileges=no_new_privileges,
    )
    return {
        "Image": image,
        "Entrypoint": ["/usr/local/bin/python", "-c", "pass"],
        "Cmd": [],
        "User": settings.task_user,
        "NetworkDisabled": True,
        "Labels": labels(
            settings,
            "probe",
            "no-new-privileges" if no_new_privileges else "no-new-privileges-control",
            "launcher",
        ),
        "HostConfig": host,
    }


INGEST_PROBE_SCRIPT = """\
import collections, os, stat, sys
_SYSTEM = (
    "proc", "sysfs", "devtmpfs", "devpts", "debugfs", "tracefs", "securityfs", "cgroup",
    "cgroup2", "bpf", "efivarfs", "configfs", "pstore", "binfmt_misc", "fusectl", "mqueue",
    "hugetlbfs", "nsfs", "autofs",
)
root, mountinfo, _LIMIT = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(mountinfo, encoding="utf-8", errors="replace") as handle:
    for line in handle:
        front, _, back = line.partition(" - ")
        fields, rest = front.split(), back.split()
        if len(fields) > 4 and rest and rest[0] in _SYSTEM:
            if fields[4] == root or fields[4].startswith(root + "/"):
                sys.exit(3)
try:
    mode = os.lstat(root).st_mode
except OSError:
    sys.exit(6)
if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
    sys.exit(4)
if not stat.S_ISDIR(mode):
    sys.exit(0)
seen = 0
queue = collections.deque([root])
while queue:
    try:
        with os.scandir(queue.popleft()) as entries:
            for entry in entries:
                seen += 1
                if seen > _LIMIT:
                    print("root %r has more than %d entries" % (root, _LIMIT), file=sys.stderr)
                    sys.exit(5)
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
                    sys.exit(4)
                if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                    sys.exit(4)
                if stat.S_ISDIR(mode):
                    queue.append(entry.path)
    except OSError:
        sys.exit(6)
"""
"""The script an ingestion-root probe runs, failing closed. Exit 3: a system mount (proc, sysfs,
cgroup, debugfs and the like) at or below the root. 4: a socket, pipe or device node. 5: more
entries than the cap (its third argument), so part of the root was not looked at. 6: something
that cannot be listed or examined. 0 otherwise. The walk is breadth-first and never follows a
link."""


def ingestion_probe_spec(
    settings: LauncherSettings, image: str, name: str, *, no_new_privileges: bool
) -> dict[str, Any]:
    """Describe the container that looks at one ingestion root the way a task would see it.

    Parameters
    ----------
    settings
        For the user, the labels and the root's host path.
    image
        An allowed image.
    name
        The root's name.
    no_new_privileges
        Whether Docker allows the option here.

    Returns
    -------
    dict
        The body of ``POST /containers/create``: the root mounted read-only, no network, running
        `INGEST_PROBE_SCRIPT`.
    """
    host = _host_config(
        Limits(memory_mb=MIN_MEMORY_MB, cpus=0.5, pids=16, timeout_seconds=300),
        "none",
        [_ingestion_mount(settings, name)],
        "rw,noexec,nosuid,nodev,size=1m",
        no_new_privileges=no_new_privileges,
    )
    return {
        "Image": image,
        "Entrypoint": ["/usr/local/bin/python", "-I", "-c", INGEST_PROBE_SCRIPT],
        "Cmd": [f"/ingest/{name}", "/proc/self/mountinfo", str(settings.probe_max_entries)],
        "User": settings.task_user,
        "NetworkDisabled": True,
        "Labels": labels(settings, "probe", f"ingest-{name}", "launcher"),
        "HostConfig": host,
    }
