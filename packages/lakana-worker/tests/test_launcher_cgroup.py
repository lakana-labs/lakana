"""The launcher's reading of a container's cgroup: peak memory and processor time."""

from __future__ import annotations

import threading
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from lakana_worker.launcher.cgroup import (
    CgroupReading,
    CgroupSampler,
    cgroup_directory,
    read_cgroup,
)

ID = "ab" * 32


def _tree(directory: Path, *, peak: str | None = "536870912\n", cpu: str | None = None) -> Path:
    """Write a fake cgroup directory.

    Parameters
    ----------
    directory
        Where to make it.
    peak
        Content of ``memory.peak``, or `None` for no such file.
    cpu
        Content of ``cpu.stat``, or `None` for ``usage_usec 3100000``.

    Returns
    -------
    Path
        The directory.
    """
    directory.mkdir(parents=True)
    if peak is not None:
        (directory / "memory.peak").write_text(peak)
    text = "user_usec 1\nsystem_usec 2\nusage_usec 3100000\n" if cpu is None else cpu
    (directory / "cpu.stat").write_text(text)
    return directory


def test_both_figures_are_read(tmp_path: Path) -> None:
    """Bytes from `memory.peak`, seconds from the `usage_usec` line of `cpu.stat`."""
    assert read_cgroup(_tree(tmp_path / "c")) == CgroupReading(536870912, 3.1)


def test_a_kernel_without_memory_peak_still_gives_processor_time(tmp_path: Path) -> None:
    """Cgroup v1 and kernels before 5.19 have no `memory.peak`: no guess for memory."""
    assert read_cgroup(_tree(tmp_path / "c", peak=None)) == CgroupReading(None, 3.1)


def test_a_missing_directory_gives_nothing(tmp_path: Path) -> None:
    """The cgroup is gone or was never visible."""
    assert read_cgroup(tmp_path / "nope") == CgroupReading(None, None)
    assert cgroup_directory(ID, tmp_path) is None


@pytest.mark.parametrize("text", ["abc", "-5", "max", "", "1.5", "9" * 40])
def test_garbage_is_not_a_figure(tmp_path: Path, text: str) -> None:
    """`max` and negative or non-integer text are dropped, never guessed."""
    reading = read_cgroup(
        _tree(tmp_path / "c", peak=text, cpu=f"usage_usec {text}\nusage_usec 1\n")
    )
    assert reading.peak_memory_bytes is None
    assert reading.cpu_seconds is None


def test_a_cpu_stat_without_usage_gives_no_seconds(tmp_path: Path) -> None:
    """Only the `usage_usec` line counts."""
    assert read_cgroup(_tree(tmp_path / "c", cpu="user_usec 5\n")).cpu_seconds is None


@pytest.mark.parametrize(
    "relative",
    [f"system.slice/docker-{ID}.scope", f"docker/{ID}", ID],
    ids=["systemd", "cgroupfs", "bare"],
)
def test_the_directory_is_found_under_each_layout(tmp_path: Path, relative: str) -> None:
    """Docker's systemd driver, its cgroupfs driver and a flat tree."""
    expected = _tree(tmp_path / relative)
    assert cgroup_directory(ID, tmp_path) == expected


@pytest.mark.parametrize(
    "container_id", ["", "../etc", ID.upper(), ID[:-1], ID + "0", "a/" + ID[2:], f"{ID[:-1]}g"]
)
def test_only_a_full_lower_case_hex_id_builds_a_path(tmp_path: Path, container_id: str) -> None:
    """An id from the engine is untrusted text; nothing else becomes a path."""
    (tmp_path / container_id.replace("/", "_")).mkdir(exist_ok=True)
    assert cgroup_directory(container_id, tmp_path) is None
    sampler = CgroupSampler(container_id, root=tmp_path, sleep=lambda seconds: True)
    sampler.start()
    assert sampler.stop() == CgroupReading(None, None)


def _sampler(root: Path, sleeps: list[float], on_sleep: Callable[[int], bool]) -> CgroupSampler:
    """Build a sampler whose sleep runs `on_sleep(n)` and returns without waiting.

    Parameters
    ----------
    root
        The cgroup tree.
    sleeps
        Receives each requested interval.
    on_sleep
        Called with the number of sleeps so far; returns `True` to stop the sampler.

    Returns
    -------
    CgroupSampler
        The sampler.
    """

    def sleep(seconds: float) -> bool:
        sleeps.append(seconds)
        return on_sleep(len(sleeps))

    return CgroupSampler(ID, root=root, interval=1.0, sleep=sleep)


def test_the_peak_is_the_maximum_and_survives_the_cgroup_vanishing(tmp_path: Path) -> None:
    """100 then 300 then the directory is removed: 300 and the last processor time stand."""
    directory = _tree(tmp_path / f"docker/{ID}", peak="100\n", cpu="usage_usec 1000000\n")
    sleeps: list[float] = []

    def on_sleep(count: int) -> bool:
        if count == 1:
            (directory / "memory.peak").write_text("300\n")
            (directory / "cpu.stat").write_text("usage_usec 2000000\n")
        elif count == 2:
            (directory / "memory.peak").write_text("50\n")
        elif count == 3:
            (directory / "memory.peak").unlink()
            (directory / "cpu.stat").unlink()
            directory.rmdir()
        else:
            return True
        return False

    sampler = _sampler(tmp_path, sleeps, on_sleep)
    sampler.start()
    sampler._thread.join(5)  # ty: ignore[unresolved-attribute]
    assert sampler.stop() == CgroupReading(300, 2.0)
    assert sleeps[0] == pytest.approx(1.0)


def test_stopping_before_the_first_sample_gives_nothing(tmp_path: Path) -> None:
    """A container whose cgroup was never visible leaves no reading."""
    sampler = CgroupSampler(ID, root=tmp_path, sleep=lambda seconds: True)
    sampler.start()
    assert sampler.stop() == CgroupReading(None, None)


def test_stop_reads_once_more_so_a_short_task_has_figures(tmp_path: Path) -> None:
    """The cgroup is read after the container ended and before its removal."""
    _tree(tmp_path / f"docker/{ID}")
    sampler = CgroupSampler(ID, root=tmp_path, sleep=lambda seconds: True)
    sampler.start()
    assert sampler.stop() == CgroupReading(536870912, 3.1)


def test_a_sampler_never_raises_into_the_launcher(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `PermissionError` is swallowed and logged once at debug."""
    _tree(tmp_path / f"docker/{ID}")

    def refuse(directory: Path) -> CgroupReading:
        raise PermissionError(directory)

    monkeypatch.setattr("lakana_worker.launcher.cgroup.read_cgroup", refuse)
    sampler = _sampler(tmp_path, [], lambda count: count >= 3)
    with caplog.at_level("DEBUG", logger="lakana_worker.launcher"):
        sampler.start()
        sampler._thread.join(5)  # ty: ignore[unresolved-attribute]
        assert sampler.stop() == CgroupReading(None, None)
    assert len([r for r in caplog.records if "cgroup" in r.getMessage()]) == 1


def test_stop_without_start_reads_once(tmp_path: Path) -> None:
    """A sampler that never started still gives the final reading."""
    _tree(tmp_path / f"docker/{ID}")
    assert CgroupSampler(ID, root=tmp_path).stop() == CgroupReading(536870912, 3.1)


def test_a_sampler_keeps_each_figure_it_can_read(tmp_path: Path) -> None:
    """No `memory.peak` leaves memory unknown, keeping the time; no `usage_usec` the reverse."""
    _tree(tmp_path / f"docker/{ID}", peak=None)
    assert CgroupSampler(ID, root=tmp_path).stop() == CgroupReading(None, 3.1)
    other = tmp_path / "other"
    _tree(other / f"docker/{ID}", cpu="user_usec 1\n")
    assert CgroupSampler(ID, root=other).stop() == CgroupReading(536870912, None)


def test_the_first_read_comes_before_the_first_sleep(tmp_path: Path) -> None:
    """A task that ends within the first interval still has a reading."""
    _tree(tmp_path / f"docker/{ID}")
    sampler = CgroupSampler(ID, root=tmp_path, sleep=lambda seconds: True)
    sampler.start()
    sampler._thread.join(5)  # ty: ignore[unresolved-attribute]
    (tmp_path / f"docker/{ID}/memory.peak").unlink()
    (tmp_path / f"docker/{ID}/cpu.stat").unlink()
    (tmp_path / f"docker/{ID}").rmdir()
    assert sampler.stop() == CgroupReading(536870912, 3.1)


def test_sample_reads_on_demand(tmp_path: Path) -> None:
    """`sample` is the read the launcher makes as the output stream ends."""
    _tree(tmp_path / f"docker/{ID}")
    sampler = CgroupSampler(ID, root=tmp_path)
    sampler.sample()
    assert sampler.stop() == CgroupReading(536870912, 3.1)


def test_a_thread_that_cannot_start_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` and `stop` survive thread pressure; the final read still works."""
    _tree(tmp_path / f"docker/{ID}")

    def refuse() -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(
        "lakana_worker.launcher.cgroup.threading",
        types.SimpleNamespace(
            Thread=lambda **kwargs: types.SimpleNamespace(start=refuse), Event=threading.Event
        ),
    )
    sampler = CgroupSampler(ID, root=tmp_path)
    sampler.start()
    assert sampler.stop() == CgroupReading(536870912, 3.1)
