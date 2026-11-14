"""Watch the file descriptors the calling thread opens, to find a leak without flaking.

Comparing every descriptor of the process before and after a call is flaky: other threads and
finalizers open and close descriptors too. `watch_opened_descriptors` sees only what the code
under test opens with `os.open` or `os.dup`, in the thread that calls it.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

import pytest


@dataclass
class Opened:
    """The descriptors `watch_opened_descriptors` saw opened, with the file each named."""

    ever: list[tuple[int, tuple[int, int]]] = field(default_factory=list)

    @property
    def numbers(self) -> list[int]:
        """The numbers of every descriptor opened.

        Returns
        -------
        list[int]
            In the order they were opened.
        """
        return [number for number, _ in self.ever]

    def leaked(self) -> list[int]:
        """Return the descriptors opened that still name the file they were opened on.

        Closing may happen through `os.close` or a file object, so this asks the system:
        a number that is closed, or names another file now, is not a leak.

        Returns
        -------
        list[int]
            The numbers still open on the same device and inode.
        """
        found = []
        for number, identity in self.ever:
            try:
                info = os.fstat(number)
            except OSError:
                continue
            if (info.st_dev, info.st_ino) == identity:
                found.append(number)
        return found


def watch_opened_descriptors(monkeypatch: pytest.MonkeyPatch) -> Opened:
    """Watch the descriptors the calling thread opens with `os.open` and `os.dup`.

    Parameters
    ----------
    monkeypatch
        Wraps `os.open` and `os.dup` until the test ends.

    Returns
    -------
    Opened
        Every descriptor the calling thread opened; it fills as the test goes on.
    """
    mine = threading.get_ident()
    seen = Opened()
    real_open, real_dup, real_fstat = os.open, os.dup, os.fstat

    def note(descriptor: int) -> int:
        if threading.get_ident() == mine:
            info = real_fstat(descriptor)
            seen.ever.append((descriptor, (info.st_dev, info.st_ino)))
        return descriptor

    def watching_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        return note(real_open(path, flags, mode, dir_fd=dir_fd))

    def watching_dup(descriptor: int) -> int:
        return note(real_dup(descriptor))

    monkeypatch.setattr(os, "open", watching_open)
    monkeypatch.setattr(os, "dup", watching_dup)
    return seen
