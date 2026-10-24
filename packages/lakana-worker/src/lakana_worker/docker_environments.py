"""Environments built by the launcher: one volume per project, lock, Python and index set.

The counterpart of `lakana_worker.venvs.acquire` for DockerRunner: the same errors, so a task
fails the same way whichever runner built its environment. The build runs in a container with
the code read-only, its own volume, the cache of its index set and the credentials of the
indexes its lock uses; the worker decides which credentials those are (`resolve_index_access`
refuses a secret outside its use) and the launcher only builds.

The key holds the project, unlike the local venv cache's. A lock pins every package from an
index by hash, but not the files of a path source (a vendored package directory in the
repository): two projects with the same `uv.lock` and `pyproject.toml` could otherwise share an
environment built from one project's vendored code, and the other project's tasks would run
that code with their own token. Environments are therefore never shared between projects; the
cost is one build per project for a lock several projects use.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from lakana_core.projects.package_sources import RegisteredIndex
from lakana_worker.git import proxy_secrets
from lakana_worker.index_credentials import (
    IndexAccess,
    missing_wheel,
    no_wheel_message,
    redaction_values,
    refusal_message,
    refused_index,
    wheels_only,
)
from lakana_worker.launcher.task_build import NO_INTERPRETER, NO_SDK, TOO_LARGE
from lakana_worker.launcher_client import LauncherClient, LauncherRefused, LauncherUnavailable
from lakana_worker.processes import OutputRedactor
from lakana_worker.runners import NO_SDK_MESSAGE, stop_retry_gap
from lakana_worker.trust import certificate_hint, install_trust
from lakana_worker.venvs import (
    BuildStop,
    IndexResolver,
    Tee,
    VenvBuildError,
    VenvBuildRefused,
    VenvBuildStopped,
    VenvLease,
)

PUBLIC_INDEX_HOSTS = ("pypi.org", "files.pythonhosted.org")
"""The hosts of the public package index, which a build without `LAKANA_RUN__INDEX_URL` uses."""

_FAILED = "environment build failed"
_STOPPED = (
    f"{_FAILED}: stopped before it ended (the task was cancelled, lost its lease, or the worker "
    "is stopping); the launcher removed what it had built"
)
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def _without_userinfo(url: str) -> str:
    """Drop a user name and password from a URL.

    Parameters
    ----------
    url
        The URL.

    Returns
    -------
    str
        Scheme, lower-case host, port and path.
    """
    parts = urlsplit(url)
    netloc = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def index_set(used: Sequence[RegisteredIndex], operator_index: str | None) -> str:
    """Describe the package indexes a build may use, without credentials.

    Parameters
    ----------
    used
        The registered indexes the lock uses.
    operator_index
        `LAKANA_RUN__INDEX_URL`, or `None` for the public index.

    Returns
    -------
    str
        One line per index, sorted after the default index.
    """
    default = _without_userinfo(operator_index) if operator_index else "public"
    return "\n".join([default, *sorted(f"{index.name}={index.url}" for index in used)])


def environment_key(
    project_id: uuid.UUID, lock_sha256: str, pyproject_sha256: str, python: str, indexes: str
) -> str:
    """Name one environment.

    Parameters
    ----------
    project_id
        The project whose tasks use it; environments are never shared between projects.
    lock_sha256
        The lock's digest.
    pyproject_sha256
        `pyproject.toml`'s digest.
    python
        The Python version.
    indexes
        `index_set`'s answer.

    Returns
    -------
    str
        64 hex digits; the launcher adds the image to the volume's name.
    """
    material = f"project:{project_id}:{lock_sha256}:{pyproject_sha256}:py{python}:{indexes}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _endpoint(url: str) -> str:
    """Name the host and port a URL connects to.

    Parameters
    ----------
    url
        An index URL.

    Returns
    -------
    str
        ``host:port``, the host in lower case, the port the scheme's own (443 for https, 80 for
        http) when the URL names none; empty for a URL without a host.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        return ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.hostname.lower()}:{port}"


def build_targets(used: Sequence[RegisteredIndex], operator_index: str | None) -> list[str]:
    """List the host:port pairs a build may reach through the egress proxy.

    Every index is sent with its port, whether it has a credential or not: the launcher's token
    grants exactly these pairs (#301).

    Parameters
    ----------
    used
        The registered indexes the lock uses.
    operator_index
        `LAKANA_RUN__INDEX_URL`, or `None`.

    Returns
    -------
    list of str
        The default index's pairs (port 443 of the public index's hosts), then each registered
        index's, without repeats.
    """
    if operator_index:
        targets = [_endpoint(operator_index)]
    else:
        targets = [f"{host}:443" for host in PUBLIC_INDEX_HOSTS]
    targets += [_endpoint(index.url) for index in used]
    return list(dict.fromkeys(target for target in targets if target))


@dataclass
class DockerEnvironments:
    """Gets a task's environment from the launcher, building it on first use.

    Parameters
    ----------
    client
        The launcher.
    owner
        This worker, for the launcher's labels.
    memory_mb
        A build container's memory (`LAKANA_RUN__TASK_MEMORY`).
    cpus
        Its CPUs.
    pids
        Its process count.
    build_timeout
        `LAKANA_RUN__VENV_BUILD_TIMEOUT`.
    max_bytes
        `LAKANA_RUN__VENV_MAX_BYTES`.
    operator_index
        `LAKANA_RUN__INDEX_URL`; may carry a password, left out of `repr`.
    poll_seconds
        How often a running build's stop is checked.
    stop_wait
        Seconds a stopped build's result is waited for once the launcher confirmed the stop;
        then the task ends without it (an image pull the launcher cannot interrupt), and the
        launcher removes the build when the pull ends.
    trust
        The worker's trust bundle as bytes (`lakana_worker.trust`), written into the attempt and
        mounted at ``/trust``; `None` without `LAKANA_RUN__CA_BUNDLE`.
    """

    client: LauncherClient
    owner: str
    memory_mb: int
    cpus: float
    pids: int
    build_timeout: float
    max_bytes: int
    operator_index: str | None = field(default=None, repr=False)
    poll_seconds: float = 0.2
    stop_wait: float = 30.0
    trust: bytes | None = field(default=None, repr=False)

    def acquire(
        self,
        lock_sha256: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None,
        *,
        python: str,
        attempt: str,
        used: Sequence[RegisteredIndex],
        project_id: uuid.UUID,
        stop: BuildStop | None = None,
    ) -> VenvLease:
        """Return the environment of a lock, building it in a container on first use.

        Parameters
        ----------
        lock_sha256
            The project version's lock digest.
        project
            The attempt's unpacked code, for `pyproject.toml`.
        on_log
            Receives the build's redacted output.
        indexes
            Returns the used indexes with their credentials; called only if a build runs.
        python
            The Python version.
        attempt
            The attempt directory's name, whose ``code`` the build mounts.
        used
            The registered indexes the lock uses.
        project_id
            The project the task belongs to; part of the key.
        stop
            Says when the task is to stop (#291): the launcher is asked to stop the build, which
            kills its container and keeps nothing of it.

        Returns
        -------
        VenvLease
            A lease whose `key` names the environment; nothing to release.

        Raises
        ------
        VenvBuildStopped
            If `stop` said so before the environment was built.
        VenvBuildRefused
            If a credential cannot be used, the launcher refused the build, an index refused the
            credential, a wheel is missing while a credential is in play, or the environment has
            no `lakana` harness.
        VenvBuildError
            If the build failed otherwise, or the launcher is unavailable (its text).
        """
        if _SHA256.match(lock_sha256) is None:
            raise VenvBuildError(f"{_FAILED}: the project version has no valid lock digest")
        try:
            pyproject = hashlib.sha256((project / "pyproject.toml").read_bytes()).hexdigest()
        except OSError as exc:
            raise VenvBuildError(
                f"{_FAILED}: the deployed code has no readable pyproject.toml; redeploy"
            ) from exc
        key = environment_key(
            project_id, lock_sha256, pyproject, python, index_set(used, self.operator_index)
        )
        lease = VenvLease(path=Path("/env"), python=Path("/env/bin/python"), key=key)
        accesses: list[IndexAccess] = []
        tee: Tee | None = None
        try:
            if self.client.status(python, key):
                return lease
            if stop is not None and stop():
                raise VenvBuildStopped(_STOPPED)
            if self.trust is not None:
                try:
                    install_trust(self.trust, project.parent)
                except OSError as exc:
                    raise VenvBuildError(
                        f"{_FAILED}: could not copy the trust bundle into the attempt: "
                        f"{exc.strerror or exc}"
                    ) from None
            accesses = list(indexes()) if indexes is not None else []
            tee = Tee(
                OutputRedactor(
                    [*proxy_secrets(self.operator_index), *redaction_values(accesses)], on_log
                )
            )
            request: dict[str, object] = {
                "op": "build",
                "build": uuid.uuid4().hex,
                "owner": self.owner,
                "python": python,
                "environment": key,
                "attempt": attempt,
                "indexes": [
                    {"url": a.url, "username": a.username, "password": a.password}
                    for a in accesses
                    if a.password is not None
                ],
                "operator_index": self.operator_index,
                "targets": build_targets(used, self.operator_index),
                "no_build": wheels_only(accesses),
                "memory_mb": self.memory_mb,
                "cpus": self.cpus,
                "pids": self.pids,
                "timeout_seconds": max(1, math.ceil(self.build_timeout)),
                "max_bytes": self.max_bytes,
                "trust": self.trust is not None,
            }
            try:
                result = self._call(request, tee.feed, stop)
            finally:
                tee.close()
        except LauncherRefused as exc:
            raise VenvBuildRefused(f"{_FAILED}: the launcher refused the build: {exc}") from None
        except LauncherUnavailable as exc:
            raise VenvBuildError(str(exc)) from None
        if result.get("complete") is True:
            return lease
        if stop is not None and stop() and result.get("stopped") is True:
            raise VenvBuildStopped(_STOPPED)
        raise self._failure(result, tee.text(), accesses)

    def _call(
        self,
        request: dict[str, object],
        on_log: Callable[[bytes], None],
        stop: BuildStop | None,
    ) -> dict[str, object]:
        """Send the build and wait for its result, asking the launcher to stop it on `stop`.

        Parameters
        ----------
        request
            The build request.
        on_log
            Receives the build's output.
        stop
            Says when to stop the build; `None` waits for its end.

        Returns
        -------
        dict
            The launcher's result.

        Raises
        ------
        VenvBuildStopped
            If the launcher confirmed the stop and sent no result within `stop_wait`.
        Exception
            Whatever the launcher client raised.
        """
        if stop is None:
            return self.client.call(request, on_log)
        box: dict[str, object] = {}
        given_up = threading.Event()

        def feed(chunk: bytes) -> None:
            """Pass output on until the task stopped waiting for the build.

            Parameters
            ----------
            chunk
                A piece of the build's output.
            """
            if not given_up.is_set():
                on_log(chunk)

        def call() -> None:
            """Run the launcher call on its own thread, keeping what it returned or raised."""
            try:
                box["result"] = self.client.call(request, feed)
            except Exception as exc:  # handed back to the waiting thread below
                box["error"] = exc

        thread = threading.Thread(target=call, name="lakana-docker-build", daemon=True)
        thread.start()
        ident = str(request["build"])
        confirmed: float | None = None
        gap = 0.0
        next_stop = 0.0
        while thread.is_alive():
            thread.join(self.poll_seconds)
            if not thread.is_alive() or not stop():
                continue
            now = time.monotonic()
            if confirmed is None and now >= next_stop:
                # False until the launcher registered the build, and when Docker did not stop
                # the container: asked again, less often each time.
                if self._stop(ident):
                    confirmed = now
                next_stop = now + gap
                gap = stop_retry_gap(gap, self.poll_seconds)
            elif confirmed is not None and now - confirmed >= self.stop_wait:
                given_up.set()
                raise VenvBuildStopped(_STOPPED)
        error = box.get("error")
        if isinstance(error, BaseException):
            raise error
        result = box.get("result")
        assert isinstance(result, dict)  # nosec B101 -- set by `call` when nothing was raised
        return result

    def _stop(self, ident: str) -> bool:
        """Ask the launcher to stop a build; a launcher that cannot is not an error here.

        Parameters
        ----------
        ident
            The build's id.

        Returns
        -------
        bool
            Whether the launcher stopped it; `False` also before it registered the build.
        """
        with contextlib.suppress(LauncherUnavailable, LauncherRefused):
            return self.client.stop(ident, self.owner)
        return False

    def _failure(
        self, result: dict[str, object], output: str, accesses: Sequence[IndexAccess]
    ) -> VenvBuildError:
        """Say why a build did not complete, as the local venv cache would.

        Parameters
        ----------
        result
            The launcher's result.
        output
            The last of the build's unredacted output, held in memory only.
        accesses
            The indexes the build used.

        Returns
        -------
        VenvBuildError
            `VenvBuildRefused` when a retry cannot help.
        """
        code = result.get("exit")
        if result.get("timed_out") is True:
            return VenvBuildError(f"{_FAILED}: timed out after {int(self.build_timeout)}s")
        if result.get("oom_killed") is True:
            return VenvBuildError(
                f"{_FAILED}: the build used more than its memory ({self.memory_mb} MB, "
                "LAKANA_RUN__TASK_MEMORY)"
            )
        if code == NO_SDK:
            return VenvBuildRefused(f"{_FAILED}: {NO_SDK_MESSAGE}")
        if code == NO_INTERPRETER:
            return VenvBuildError(f"{_FAILED}: uv did not create an interpreter")
        if code == TOO_LARGE:
            return VenvBuildError(f"{_FAILED}: the venv exceeded {self.max_bytes} bytes")
        refused = refused_index(output, accesses)
        if refused is not None:
            return VenvBuildRefused(f"{_FAILED}: {refusal_message(refused)}")
        package = missing_wheel(output) if wheels_only(accesses) else None
        if package is not None:
            return VenvBuildRefused(f"{_FAILED}: {no_wheel_message(package)}")
        hint = certificate_hint(output)
        return VenvBuildError(
            f"{_FAILED}: uv exited with status {code}" + (f": {hint}" if hint else "")
        )
