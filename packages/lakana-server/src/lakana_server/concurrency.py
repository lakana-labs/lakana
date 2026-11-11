"""A non-blocking concurrency limiter for the governed append route.

`asyncio.Semaphore.acquire()` *waits* for a slot to free up rather than
refusing outright, which is the wrong behaviour for an HTTP request that
should fail fast with `503 Retry-After: 1` rather than queue indefinitely
behind other slow uploads — queuing would just move the pool-exhaustion and
memory-amplification risk `AppendConcurrencyLimiter` exists to bound
elsewhere. `AppendConcurrencyLimiter.try_acquire` never waits.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator

from lakana_server.errors import AppendConcurrencyLimitError


class AppendConcurrencyLimiter:
    """A non-blocking counting limiter for concurrent governed appends.

    Safe with no internal lock: the only caller,
    `lakana_server.api.platform.tables.append_table_data`, is `async def`
    code running cooperatively on asyncio's single-threaded event loop, and
    neither `try_acquire` nor `release` ever `await` — so neither method
    can be interrupted mid-call by another coroutine checking or changing
    `_available` concurrently.

    Parameters
    ----------
    max_concurrent
        The most callers allowed to hold a slot at once.
    """

    def __init__(self, max_concurrent: int) -> None:
        self._available = max_concurrent

    def try_acquire(self) -> bool:
        """Attempt to claim one slot, without waiting for one to free up.

        Returns
        -------
        bool
            `True` if a slot was claimed — the caller must call `release`
            exactly once when it is done. `False` if none was available,
            leaving the count unchanged.
        """
        if self._available <= 0:
            return False
        self._available -= 1
        return True

    def release(self) -> None:
        """Return a slot previously claimed by a successful `try_acquire`."""
        self._available += 1


@contextlib.asynccontextmanager
async def hold_one_per_principal(
    slots: asyncio.Semaphore,
    holders: set[uuid.UUID],
    principal_id: uuid.UUID,
    *,
    own_busy: str,
    all_busy: str,
) -> AsyncGenerator[None]:
    """Hold one of a few slots and the caller's place in line, refusing instead of waiting.

    A principal may have one such request at a time, and the request takes a slot before it
    reads its body: otherwise requests that send a few bytes and stall would hold every slot,
    a body each and a database connection each for the whole read timeout. The checks and the
    take have no suspension point between them, so no other request can slip in. The
    principal's id leaves the set when the block ends, however it ends (a normal end, an
    exception, a disconnect, cancellation), so the set never holds more ids than there are
    slots.

    Parameters
    ----------
    slots
        The slots; never waited on.
    holders
        The principals holding a slot now.
    principal_id
        The authenticated caller.
    own_busy
        The message when the caller already holds a slot.
    all_busy
        The message when every slot is taken.

    Yields
    ------
    None
        While both holds are taken.

    Raises
    ------
    AppendConcurrencyLimitError
        With `own_busy` or `all_busy` (503, `Retry-After: 1`).
    """
    # `locked()` and the acquire below have no suspension point between them, so no other
    # request can take the slot this one just saw free.
    if principal_id in holders:
        raise AppendConcurrencyLimitError(own_busy)
    if slots.locked():
        raise AppendConcurrencyLimitError(all_busy)
    holders.add(principal_id)
    try:
        async with slots:
            yield
    finally:
        holders.discard(principal_id)
