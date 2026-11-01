"""Handing the memory a large write freed back to the system: `trim_heap`."""

from __future__ import annotations

import pytest

from lakana_core import heap as heap_module


class _Libc:
    """A C library that records `malloc_trim` calls."""

    def __init__(self) -> None:
        """Start with no call."""
        self.calls: list[int] = []

    def malloc_trim(self, pad: int) -> int:
        """Record a call.

        Parameters
        ----------
        pad
            The padding asked for.

        Returns
        -------
        int
            1, as glibc does when it released memory.
        """
        self.calls.append(pad)
        return 1


def test_trim_heap_asks_glibc_to_return_freed_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = _Libc()
    monkeypatch.setattr(heap_module, "_libc", lambda: libc)
    heap_module.trim_heap("linux")
    assert libc.calls == [0]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_trim_heap_does_nothing_off_linux(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    def refuse() -> object:
        """Fail: off Linux the C library is never loaded.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError("loaded the C library off Linux")

    monkeypatch.setattr(heap_module, "_libc", refuse)
    heap_module.trim_heap(platform)


@pytest.mark.parametrize("missing", ["library", "symbol"])
def test_trim_heap_skips_a_c_library_without_malloc_trim(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Musl has no `malloc_trim`; a library that will not load is skipped the same way."""

    def libc() -> object:
        """Load no usable C library.

        Returns
        -------
        object
            An object without `malloc_trim`.

        Raises
        ------
        OSError
            When the library itself is missing.
        """
        if missing == "library":
            raise OSError("no libc")
        return object()

    monkeypatch.setattr(heap_module, "_libc", libc)
    heap_module.trim_heap("linux")


def test_trim_heap_runs_on_this_machine() -> None:
    """The real call returns nothing and raises nothing, glibc or not."""
    assert heap_module.trim_heap() is None
