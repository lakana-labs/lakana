"""The worker's heartbeat: register, refresh, and know when it has been silent too long.

A thread of its own, not the scheduler loop's: that one works only while the worker leads and
backs off for up to 30 s after a failed step, and a long tick must not make the leader look
dead. Whether the worker is *lost* is never decided here: only the database's clock decides
that, in `lakana_core.runs.workers`. What this class decides is local and cautious: after a
lease without a successful write the worker claims nothing and stops the query it runs. That
saves work; it protects no table (a frozen process measures no time). The write fence does.
The time since the last successful write is measured on this process's monotonic clock, never
compared with a clock of another machine.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from sqlalchemy.orm import Session

from lakana_core.log_events import LogEvent, log_event
from lakana_core.runs import workers
from lakana_core.runs.workers import CacheSizes, MemoryUse

logger = logging.getLogger(__name__)

RETRY_SECONDS = 5.0
"""How long the thread waits after a failed write before it tries again."""

MEMORY_STEP_BYTES = 1024 * 1024
"""How far the worker's memory must move from the figure last written before a beat writes it."""


def _moved(current: MemoryUse, written: MemoryUse | None) -> bool:
    """Say whether a memory use differs enough from the one last written to write it.

    Parameters
    ----------
    current
        The use measured now.
    written
        The use last written; `None` when none was.

    Returns
    -------
    bool
        `True` for a first figure, a changed limit, or a use that moved `MEMORY_STEP_BYTES` or more.
    """
    return (
        written is None
        or current.limit_bytes != written.limit_bytes
        or abs(current.used_bytes - written.used_bytes) >= MEMORY_STEP_BYTES
    )


class WorkerHeartbeat:
    """Keep this worker's row fresh, and say when it could not for a lease.

    Parameters
    ----------
    session_factory
        Called for a fresh session per write; used as a context manager.
    worker_id
        The worker's id.
    host
        The host name shown for the worker.
    version
        The worker's version.
    renew_seconds
        The wait between two writes (`LAKANA_RUN__RENEW_SECONDS`).
    lease_seconds
        The silence after which the worker claims nothing and stops its query
        (`LAKANA_RUN__LEASE_SECONDS`).
    takes
        The work the worker takes (`LAKANA_WORKER__TAKES`); written with every registration.
    monotonic
        The clock the silence is measured on; a test replaces it.
    caches
        Returns the sizes of the worker's caches it last measured (`CacheKeeper.latest`); new
        figures are written with the next beat, and with every registration. `None` writes none.
    memory
        Returns the worker's memory use as its watchdog last measured it
        (`MemoryWatchdog.use`); a beat writes it when it moved `MEMORY_STEP_BYTES` or more
        since the figure last written, and every registration writes it. `None` writes none.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]],
        worker_id: str,
        *,
        host: str,
        version: str,
        renew_seconds: float,
        lease_seconds: float,
        takes: str = "all",
        monotonic: Callable[[], float] = time.monotonic,
        caches: Callable[[], CacheSizes | None] | None = None,
        memory: Callable[[], MemoryUse | None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._host = host
        self._version = version
        self._renew_seconds = renew_seconds
        self._lease_seconds = lease_seconds
        self._takes = takes
        self._monotonic = monotonic
        self._caches = caches
        self._written: CacheSizes | None = None
        self._memory = memory
        self._memory_written: MemoryUse | None = None
        self._started_at: datetime | None = None
        self._registered = False
        self._failures = 0
        self._last_success: float | None = None
        self._stale_reported = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def beat_once(self) -> bool:
        """Register, or refresh the heartbeat.

        A failed write is logged (with its cause the first time of an outage, in one line after
        that) and never raised.

        Returns
        -------
        bool
            `True` when the write reached the database.
        """
        # Read before the write is sent and kept only if it succeeds: the worker's view of its
        # freshness is then never younger than the database's, whatever the write took.
        started = self._monotonic()
        current = None if self._caches is None else self._caches()
        fresh = None if current == self._written else current
        memory = None if self._memory is None else self._memory()
        moved = None if memory is None or not _moved(memory, self._memory_written) else memory
        sent = moved
        try:
            with self._session_factory() as session:
                if not self._registered or not workers.beat(
                    session, self._worker_id, caches=fresh, memory=moved
                ):
                    sent = memory
                    self._started_at = workers.register(
                        session,
                        self._worker_id,
                        host=self._host,
                        version=self._version,
                        started_at=self._started_at,
                        takes=self._takes,
                        caches=current,
                        memory=memory,
                    )
                session.commit()
        except Exception as error:
            self._failures += 1
            if self._failures == 1:
                with self._lock:
                    never = self._last_success is None
                logger.exception(
                    "The worker's heartbeat could not be written; it is tried again every %g s. "
                    "%s If it keeps failing for %g s (LAKANA_RUN__LEASE_SECONDS), this worker "
                    "stops the query it runs, and its running queries and deploys are ended as "
                    "lost.",
                    RETRY_SECONDS,
                    (
                        "This worker claims nothing until its first heartbeat is written."
                        if never
                        else "Until then it still claims work."
                    ),
                    self._lease_seconds,
                    extra=log_event(LogEvent.DATABASE_UNREACHABLE),
                )
            else:
                logger.warning(
                    "The worker's heartbeat is still failing (%d in a row): %s",
                    self._failures,
                    error,
                    extra=log_event(LogEvent.DATABASE_UNREACHABLE),
                )
            return False
        if self._failures:
            logger.info(
                "The worker's heartbeat was written again after %d failed attempts.",
                self._failures,
            )
        self._failures = 0
        self._written = current
        if sent is not None:
            self._memory_written = sent
        with self._lock:
            self._registered = True
            self._last_success = started
            recovered, self._stale_reported = self._stale_reported, False
        if recovered:
            logger.info(
                "The worker's heartbeat is fresh again; it claims work again.",
                extra=log_event(LogEvent.HEARTBEAT_RECOVERED),
            )
        return True

    def stale(self) -> bool:
        """Say whether no heartbeat has succeeded for a lease, or none ever has.

        Returns
        -------
        bool
            `True` when the worker must claim nothing and stop its query.
        """
        with self._lock:
            last = self._last_success
            stale = last is None or self._monotonic() - last >= self._lease_seconds
            turned = stale and last is not None and not self._stale_reported
            if turned:
                self._stale_reported = True
        if turned:
            logger.warning(
                "No heartbeat was written for %g s (LAKANA_RUN__LEASE_SECONDS): this worker "
                "claims nothing and stops the query it runs until one is written. Check that "
                "the database is reachable.",
                self._lease_seconds,
                extra=log_event(LogEvent.HEARTBEAT_MISSED),
            )
        return stale

    def may_claim(self) -> bool:
        """Say whether the dequeue loop may claim work.

        Returns
        -------
        bool
            The opposite of `stale`.
        """
        return not self.stale()

    def run(self) -> None:
        """Beat until `stop`: every `renew_seconds`, and every `RETRY_SECONDS` after a failure."""
        while not self._stop.is_set():
            self._stop.wait(self._renew_seconds if self.beat_once() else RETRY_SECONDS)

    def start(self) -> threading.Thread:
        """Run `run` in a daemon thread.

        Returns
        -------
        threading.Thread
            The started thread, for the caller to join after `stop`.
        """
        thread = threading.Thread(target=self.run, name="lakana-heartbeat", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Ask the thread to end after its current write."""
        self._stop.set()

    def unregister(self) -> None:
        """Delete this worker's row after a clean stop; a failure is logged, never raised."""
        try:
            with self._session_factory() as session:
                workers.remove(session, self._worker_id)
                session.commit()
        except Exception:
            logger.warning(
                "The worker's row could not be removed at shutdown; it is shown as lost for an "
                "hour and then removed.",
                exc_info=True,
            )
