"""The thread-safe limiter of JSON result pages: a few per process, one per principal."""

from __future__ import annotations

import threading
import uuid
from types import TracebackType
from typing import Literal

from lakana_server.concurrency import (
    JSON_PAGES_PER_PRINCIPAL,
    JSON_PAGES_PER_PROCESS,
    ThreadSafeConcurrencyLimiter,
)

_A, _B, _C, _D, _E = (uuid.uuid4() for _ in range(5))


def race(
    lim: ThreadSafeConcurrencyLimiter, keys: list[uuid.UUID]
) -> list[Literal["own", "all"] | None]:
    """Let one thread per key ask for a slot at the same moment.

    Parameters
    ----------
    lim
        The limiter.
    keys
        One key per thread.

    Returns
    -------
    list
        Each thread's answer, in the order of `keys`.
    """
    barrier = threading.Barrier(len(keys))
    results: list[Literal["own", "all"] | None] = [None] * len(keys)

    def ask(index: int) -> None:
        barrier.wait()
        results[index] = lim.try_acquire(keys[index])

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(len(keys))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


class RecordingLock:
    """A `threading.Lock` that counts how often it is entered."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.entered = 0

    def __enter__(self) -> bool:
        """Take the lock and count it.

        Returns
        -------
        bool
            What the wrapped lock's `__enter__` returns.
        """
        self.entered += 1
        return self._lock.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release the lock.

        Parameters
        ----------
        exc_type
            The exception's type, if one is propagating.
        exc
            The exception, if one is propagating.
        tb
            Its traceback.
        """
        self._lock.__exit__(exc_type, exc, tb)


def test_the_json_page_limits_are_four_per_process_and_one_per_principal() -> None:
    assert (JSON_PAGES_PER_PROCESS, JSON_PAGES_PER_PRINCIPAL) == (4, 1)


def test_one_principal_cannot_hold_every_json_slot() -> None:
    lim = ThreadSafeConcurrencyLimiter(4, 1)
    assert lim.try_acquire(_A) is None
    assert lim.try_acquire(_A) == "own"
    assert [lim.try_acquire(k) for k in (_B, _C, _D)] == [None, None, None]
    assert lim.try_acquire(_E) == "all"
    lim.release(_A)
    assert lim.try_acquire(_E) is None


def test_a_released_key_leaves_no_count_behind() -> None:
    lim = ThreadSafeConcurrencyLimiter(4, 2)
    assert [lim.try_acquire(_A), lim.try_acquire(_A)] == [None, None]
    lim.release(_A)
    lim.release(_A)
    assert lim._per_key == {}
    assert [lim.try_acquire(_A), lim.try_acquire(_A), lim.try_acquire(_A)] == [None, None, "own"]


def test_eight_threads_at_one_barrier_get_exactly_four_slots() -> None:
    lim = ThreadSafeConcurrencyLimiter(4, 1)
    keys = [uuid.uuid4() for _ in range(8)]
    for _ in range(200):
        results = race(lim, keys)
        assert sorted(map(str, results)) == ["None"] * 4 + ["all"] * 4
        for key, result in zip(keys, results, strict=True):
            if result is None:
                lim.release(key)
    assert [lim.try_acquire(k) for k in keys[:4]] == [None] * 4


def test_every_acquire_and_release_holds_the_lock() -> None:
    lim = ThreadSafeConcurrencyLimiter(4, 1)
    lock = RecordingLock()
    lim._lock = lock
    lim.try_acquire(_A)
    lim.release(_A)
    assert lock.entered == 2
