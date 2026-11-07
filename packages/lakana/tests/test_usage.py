"""The bytes the SDK's readers count, and the figures a task reports about itself."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lakana import _usage


def test_nothing_is_counted_until_a_reader_reports() -> None:
    assert _usage.bytes_read() is None
    assert "bytes_read" not in _usage.record_fields()


def test_a_negative_count_is_ignored_and_does_not_start_the_total() -> None:
    _usage.add_bytes_read(-3)
    assert _usage.bytes_read() is None


def test_a_zero_count_starts_the_total_at_zero() -> None:
    """A reader that read an empty file counted 0 bytes, which is not the same as never counting."""
    _usage.add_bytes_read(0)
    assert _usage.bytes_read() == 0
    assert _usage.record_fields()["bytes_read"] == 0


def test_counts_add_up_and_a_negative_one_changes_nothing() -> None:
    _usage.add_bytes_read(5)
    _usage.add_bytes_read(-3)
    _usage.add_bytes_read(0)
    assert _usage.bytes_read() == 5


def test_threads_add_exactly() -> None:
    def work() -> None:
        for _ in range(1000):
            _usage.add_bytes_read(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert _usage.bytes_read() == 8000


def test_reset_forgets_the_total() -> None:
    _usage.add_bytes_read(7)
    _usage.reset()
    assert _usage.bytes_read() is None


def test_read_peak_reads_the_high_water_mark_line(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmHWM:\t    2048 kB\nVmRSS:\t 1 kB\n")
    assert _usage.read_peak_bytes(status) == 2048 * 1024


@pytest.mark.parametrize("text", ["Name:\tpython\n", "VmHWM: lots\n"])
def test_read_peak_gives_none_without_the_line(tmp_path: Path, text: str) -> None:
    status = tmp_path / "status"
    status.write_text(text)
    assert _usage.read_peak_bytes(status) is None


def test_read_peak_gives_none_without_the_file(tmp_path: Path) -> None:
    assert _usage.read_peak_bytes(tmp_path / "missing") is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_a_task_reports_its_own_peak_on_linux() -> None:
    peak = _usage.record_fields()["peak_memory_bytes"]
    assert peak > 0


def test_a_task_on_macos_reports_its_rusage_peak_in_bytes() -> None:
    fake = SimpleNamespace(ru_maxrss=4096)
    assert _usage.own_peak_bytes("darwin", getrusage=lambda _who: fake) == 4096
    assert (
        _usage.own_peak_bytes("darwin", getrusage=lambda _who: SimpleNamespace(ru_maxrss=0)) is None
    )


def test_a_task_on_another_platform_reports_no_peak() -> None:
    assert _usage.own_peak_bytes("win32") is None


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS reads its peak from getrusage")
def test_a_task_without_proc_reports_no_peak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``/proc`` (macOS) the record carries no peak, never a made-up one."""
    monkeypatch.setattr(_usage, "_STATUS", tmp_path / "missing")
    assert "peak_memory_bytes" not in _usage.record_fields()
