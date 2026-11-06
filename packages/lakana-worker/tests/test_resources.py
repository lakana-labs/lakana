"""Tests for the resource figures of a child process (`lakana_worker.resources`)."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from lakana_core.runs.resources import AttemptResources
from lakana_worker.own_peak import own_peak_bytes, read_peak_bytes
from lakana_worker.resources import (
    current_collector,
    from_rusage,
    maxrss_bytes,
    peak_memory,
    usage_collector,
)


def test_maxrss_linux_is_kilobytes() -> None:
    assert maxrss_bytes(2048, "linux") == 2048 * 1024


def test_maxrss_macos_is_bytes() -> None:
    assert maxrss_bytes(2048, "darwin") == 2048


def test_from_rusage_adds_user_and_system() -> None:
    usage = SimpleNamespace(ru_maxrss=100, ru_utime=1.25, ru_stime=0.5)
    assert from_rusage(usage, "linux") == AttemptResources(102400, 1.75, None)


def test_the_peak_is_read_from_the_vmhwm_line(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\tx\nVmPeak:\t 9 kB\nVmHWM:\t    2048 kB\nVmRSS:\t 5 kB\n")
    assert read_peak_bytes(status) == 2048 * 1024


@pytest.mark.parametrize("text", ["", "VmHWM:\tmany kB\n", "VmHWM:\t5 MB\n", "VmHWM:\t-5 kB\n"])
def test_a_status_without_a_usable_vmhwm_gives_none(tmp_path: Path, text: str) -> None:
    status = tmp_path / "status"
    status.write_text(text)
    assert read_peak_bytes(status) is None


def test_a_missing_status_file_gives_none(tmp_path: Path) -> None:
    assert read_peak_bytes(tmp_path / "absent") is None


def test_this_process_reports_a_peak_on_linux_and_none_elsewhere() -> None:
    peak = own_peak_bytes()
    if sys.platform.startswith("linux"):
        assert peak is not None and peak > 0
    else:
        assert peak is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="VmHWM is Linux only")
def test_a_child_of_a_big_parent_reports_its_own_peak_not_the_parents() -> None:
    held = bytearray(256 * 2**20)
    for offset in range(0, len(held), 4096):
        held[offset] = 1  # touch every page: the parent's high-water mark is now above 256 MiB
    code = "from lakana_worker.own_peak import own_peak_bytes; print(own_peak_bytes())"
    out = subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert 0 < int(out.stdout) < len(held)


def test_macos_has_no_peak_until_its_rusage_is_known_not_to_be_inherited() -> None:
    usage = SimpleNamespace(ru_maxrss=100, ru_utime=1.25, ru_stime=0.5)
    assert from_rusage(usage, "darwin") == AttemptResources(None, 1.75, None)
    assert peak_memory("darwin", reported=5, watched=3) is None


def test_a_linux_child_that_reported_nothing_falls_back_to_the_watchdogs_reading() -> None:
    assert peak_memory("linux", reported=None, watched=3) == 3
    assert peak_memory("linux", reported=None, watched=None) is None
    assert peak_memory("linux", reported=5, watched=9) == 9


def test_there_is_no_collector_outside_a_usage_collector() -> None:
    assert current_collector() is None


def test_a_collector_merges_what_is_added_and_is_gone_after_the_block() -> None:
    with usage_collector() as collector:
        assert current_collector() is collector
        assert not collector.resources.known
        collector.add(AttemptResources(10, 1.0, 5))
        collector.add(AttemptResources(30, 0.5, None))
        assert collector.resources == AttemptResources(30, 1.5, 5)
    assert current_collector() is None


def test_a_collector_names_the_unit_of_work_it_belongs_to() -> None:
    unit = uuid.uuid4()
    with usage_collector(unit) as collector:
        assert collector.unit == unit
    with usage_collector() as bare:
        assert bare.unit is None


def test_a_collector_is_fresh_in_a_nested_block() -> None:
    with usage_collector() as outer:
        with usage_collector() as inner:
            inner.add(AttemptResources(1, 1.0, 1))
        assert current_collector() is outer
        assert not outer.resources.known


@pytest.mark.skipif(sys.platform != "darwin", reason="settles whether macOS inherits ru_maxrss")
def test_a_macos_child_of_a_big_parent_reports_a_peak_below_the_parents_buffer() -> None:
    """If this fails, macOS folds the parent's mark into the child's, so its peak stays unknown."""
    held = bytearray(64 * 2**20)
    for offset in range(0, len(held), 4096):
        held[offset] = 1
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"])  # nosec B603 -- fixed argv
    _, status, usage = os.wait4(child.pid, 0)
    child.returncode = os.waitstatus_to_exitcode(status)  # as `RunningTask.reap` does; no warning
    assert 0 < usage.ru_maxrss < len(held)  # bytes on macOS
