"""A container task's peak memory and processor time come from its cgroup, on a real Engine.

The launcher runs on the test host, so its default cgroup root (`/sys/fs/cgroup`) is the tree the
container lives in. Skipped without Docker, failed then when `LAKANA_TEST_DOCKER=1`; skipped with
the reason where the host's cgroup is not readable (cgroup v1, or a kernel before 5.19).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from docker_stack import Stack, build_task_image, docker_client, stack

pytestmark = [pytest.mark.integration, pytest.mark.docker]

_WORK = """
import time
block = bytearray(200 * 1024 * 1024)
for offset in range(0, len(block), 4096):
    block[offset] = 1
end = time.process_time() + 1.0
while time.process_time() < end:
    pass
"""


@pytest.fixture(scope="module")
def running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """One stack for the module.

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
    with stack(client, image, tmp_path_factory.mktemp("stack")) as current:
        yield current


def test_a_task_reports_its_peak_memory_and_processor_time(running: Stack) -> None:
    """About 200 MiB held and about a second of processor time show in the launcher's result."""
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        pytest.skip("the host has no cgroup v2 tree at /sys/fs/cgroup")

    result, _ = running.run(running.attempt(_WORK), memory_mb=512, timeout=120)

    peak, cpu = result["peak_memory_bytes"], result["cpu_seconds"]
    if peak is None and cpu is None:
        pytest.skip(f"the container's cgroup is not readable from this host: {result}")
    assert isinstance(peak, int) and peak >= 150 * 2**20, result
    assert isinstance(cpu, float) and cpu > 0.5, result


_SHORT = """
block = bytearray(150 * 1024 * 1024)
for offset in range(0, len(block), 4096):
    block[offset] = 1
"""


def test_a_task_that_ends_within_a_fraction_of_a_second_still_has_figures(running: Stack) -> None:
    """The first read and the one as the output ends catch a short task."""
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        pytest.skip("the host has no cgroup v2 tree at /sys/fs/cgroup")

    result, _ = running.run(running.attempt(_SHORT), memory_mb=512, timeout=120)

    assert isinstance(result["peak_memory_bytes"], int), result
    assert isinstance(result["cpu_seconds"], float), result
