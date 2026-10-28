"""`docker_stack` without Docker: its listing skips a removed object, its waits are bounded."""

from __future__ import annotations

from typing import Any

import pytest
from docker_stack import labelled_names, wait_until_listening

docker_errors = pytest.importorskip("docker.errors")


class _RacingApi:
    """A low-level client whose objects are gone by the time anyone inspects them."""

    def __init__(self) -> None:
        self.filters: list[dict[str, Any]] = []

    def containers(self, *, all: bool, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """List two containers, as Docker answers the list call.

        Parameters
        ----------
        all
            Stopped ones too.
        filters
            Recorded.

        Returns
        -------
        list of dict
            Their list entries.
        """
        self.filters.append(filters)
        return [{"Id": "c1", "Names": ["/lt1-task-a"]}, {"Id": "c2", "Names": ["/lt1-task-b"]}]

    def networks(self, *, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """List one network.

        Parameters
        ----------
        filters
            Recorded.

        Returns
        -------
        list of dict
            Its list entry.
        """
        self.filters.append(filters)
        return [{"Id": "n1", "Name": "lt1-task-a"}]

    def inspect_container(self, ident: str) -> dict[str, Any]:
        """Fail as for a container the launcher removed after the list call.

        Parameters
        ----------
        ident
            The container.

        Raises
        ------
        docker.errors.NotFound
            Always.
        """
        raise docker_errors.NotFound(f"No such container: {ident}")

    inspect_network = inspect_container


def test_labelled_names_come_from_the_list_answers_and_inspect_nothing() -> None:
    """CI's NotFound: `containers.list` inspected a container `stop` had just removed."""
    api = _RacingApi()

    assert labelled_names(api, "lakana.launcher.stack=lt1") == [
        "lt1-task-a",
        "lt1-task-b",
        "lt1-task-a",
    ]
    assert api.filters == [{"label": ["lakana.launcher.stack=lt1"]}] * 2


class _Proxy:
    """A container whose port starts answering after some probes, or that has exited."""

    def __init__(self, refusals: int, status: str = "running") -> None:
        self.refusals = refusals
        self.status = status
        self.probes: list[list[str]] = []

    def exec_run(self, command: list[str]) -> tuple[int, bytes]:
        """Probe the port: refused until `refusals` probes were made.

        Parameters
        ----------
        command
            Recorded.

        Returns
        -------
        tuple
            The exit code and the output, as `docker` returns them.
        """
        self.probes.append(command)
        if len(self.probes) <= self.refusals:
            return 1, b"ConnectionRefusedError: [Errno 111] Connection refused"
        return 0, b""

    def reload(self) -> None:
        """Read the status again (it does not change here)."""

    def logs(self) -> bytes:
        """Return what the proxy printed.

        Returns
        -------
        bytes
            One line.
        """
        return b"egress: started"


class _Clock:
    """A clock that a wait advances instead of sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        """Return the time.

        Returns
        -------
        float
            Seconds.
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the time.

        Parameters
        ----------
        seconds
            How far.
        """
        self.now += seconds


def test_the_wait_returns_once_the_port_answers() -> None:
    """#319: the first build connected before the proxy listened and got connection refused."""
    proxy, clock = _Proxy(refusals=2), _Clock()

    wait_until_listening(proxy, 3128, seconds=30, clock=clock, sleep=clock.sleep)

    assert len(proxy.probes) == 3
    assert "3128" in " ".join(proxy.probes[-1])
    assert clock.now < 1


def test_the_wait_fails_with_the_port_and_the_log_when_nothing_listens_in_time() -> None:
    proxy, clock = _Proxy(refusals=10**6), _Clock()

    with pytest.raises(AssertionError, match=r"(?s)port 3128 within 30 s.*egress: started"):
        wait_until_listening(proxy, 3128, seconds=30, clock=clock, sleep=clock.sleep)

    assert 30 <= clock.now < 31


def test_the_wait_fails_at_once_when_the_container_has_exited() -> None:
    proxy, clock = _Proxy(refusals=10**6, status="exited"), _Clock()

    with pytest.raises(AssertionError, match=r"(?s)exited.*egress: started"):
        wait_until_listening(proxy, 3128, seconds=30, clock=clock, sleep=clock.sleep)

    assert len(proxy.probes) == 1
