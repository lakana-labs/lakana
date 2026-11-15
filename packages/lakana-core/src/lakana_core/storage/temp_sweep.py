"""The temporary file sweep: remove what a write killed before its rename left in local storage.

The local backend writes an object to a hidden temporary file next to it
(``.<name>.<16 hex>.part``, `lakana_core.storage.fsspec_storage.is_temp_name`) and renames it over
the name when the write is done. A process killed between those two steps leaves the temporary
file, and nothing else removes it: the artifact sweep handles the ones under ``blobs/`` and
``manifests/`` of an artifact, but code archives, task logs, query results and the drop marker
leave theirs. Run by the worker that holds the scheduler role, with the other sweeps.

It deletes files unattended, so the rules are strict. In every live environment's storage root
that is a local directory (a root on S3 has no temporary files: a write there uploads parts to
its final key), an entry is removed only if

1. its name has the exact shape of a temporary name: a dot, a name, a dot, 16 lower-case hex
   digits and ``.part``; a look-alike is left alone;
2. it is a regular file or a symbolic link; a link is removed itself and what it points to is
   never touched, read or used to judge its age, and the walk never goes through a link;
3. its own modification time is older than `min_age`, never less than `TEMP_FILE_MIN_AGE` (one
   hour): a writer that is still writing keeps moving it.

A write in progress is never older than that unless it has not written a byte for an hour; the
worker passes the artifact upload lifetime when it is longer, because an upload may wait that
long between two pieces. Environments that share a root share one walk.

Bounds: one run has a deadline (`max_seconds`) shared between the roots and stops at the next
decision once it is past it, or at shutdown. The walk visits a root's own directories in an order
that turns with every run, so a root too large for one run's share is still covered over several
runs. A root whose walk fails is logged and counted, and the others go on.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.identifiers import shown
from lakana_core.models import LIVE_ENVIRONMENT, Environment
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger(__name__)

TEMP_FILE_MIN_AGE = timedelta(hours=1)
"""How long nobody may have written to a temporary file before it is removed, at the least."""
TEMP_SWEEP_MAX_SECONDS = 60.0
"""Default length of one run; shutdown ends it earlier."""


@dataclass(frozen=True)
class TempSweepReport:
    """What one run did.

    Parameters
    ----------
    removed
        Temporary files (and links with such a name) removed.
    freed_bytes
        The size of the regular files among them.
    kept_fresh
        Temporary files kept because they were written to too recently.
    left_alone
        Entries with a temporary name that are not a file or a link; never touched.
    failed
        Removals that failed.
    failed_roots
        Storage roots whose walk failed.
    stopped_early
        Whether the deadline or a shutdown ended the run before it was done.
    """

    removed: int = 0
    freed_bytes: int = 0
    kept_fresh: int = 0
    left_alone: int = 0
    failed: int = 0
    failed_roots: int = 0
    stopped_early: bool = False


def sweep_temp_files(
    session: Session,
    storage: StorageResolver,
    *,
    now: datetime,
    min_age: timedelta = TEMP_FILE_MIN_AGE,
    max_seconds: float = TEMP_SWEEP_MAX_SECONDS,
    monotonic: Callable[[], float] | None = None,
    rotation: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> TempSweepReport:
    """Remove the stale temporary files in every local storage root, once.

    Parameters
    ----------
    session
        Database session; the sweep leaves it without a transaction open.
    storage
        Resolves each environment's storage.
    now
        The run's time.
    min_age
        How long a temporary file must have gone unwritten; raised to `TEMP_FILE_MIN_AGE` when
        shorter.
    max_seconds
        How long the run may take, shared between the roots, before it stops at the next
        decision.
    monotonic
        The clock the deadline is read from; `time.monotonic` when `None`.
    rotation
        A number that grows by one with each run; it moves the starting root and the order of
        each root's directories.
    should_stop
        Called before every decision; the run ends when it returns `True` (shutdown).

    Returns
    -------
    TempSweepReport
        What was removed. A root that failed is counted and skipped; the others are still swept.
    """
    clock = monotonic or time.monotonic
    stop = should_stop or (lambda: False)
    final = clock() + max_seconds
    older_than = now - max(min_age, TEMP_FILE_MIN_AGE)
    rows = session.execute(
        select(Environment.name, Environment.storage_root)
        .where(LIVE_ENVIRONMENT)
        .order_by(Environment.name)
    ).all()
    session.rollback()
    groups: dict[str, str] = {}
    for name, root in rows:
        if root.startswith("file://"):
            groups.setdefault(root.rstrip("/"), name)
    roots = sorted(groups)
    if roots:
        start = rotation % len(roots)
        roots = roots[start:] + roots[:start]
    totals = {"removed": 0, "freed_bytes": 0, "kept_fresh": 0, "left_alone": 0, "failed": 0}
    failed_roots = 0
    stopped = False
    for position, root in enumerate(roots):
        moment = clock()
        if stop() or moment >= final:
            stopped = True
            break
        share = moment + (final - moment) / (len(roots) - position)

        def spent(limit: float = share) -> bool:
            return stop() or clock() >= limit

        try:
            backend = storage.for_environment(groups[root]).storage
            if not isinstance(backend, FsspecStorage):
                continue
            counts = backend.remove_stale_temp_files(
                older_than=older_than, should_stop=spent, rotation=rotation
            )
        except Exception as exc:
            failed_roots += 1
            session.rollback()
            logger.warning(
                "The temporary file sweep skipped the storage of environment %s (%s).",
                shown(groups[root]),
                type(exc).__name__,
            )
            continue
        for key in totals:
            totals[key] += getattr(counts, key)
        stopped = stopped or counts.stopped  # this root's share is spent; the next gets its own
    report = TempSweepReport(**totals, failed_roots=failed_roots, stopped_early=stopped)
    if report.removed or report.failed or report.failed_roots:
        logger.info(
            "Temporary file sweep: %d leftover temporary file(s) removed (%d bytes); %d kept as "
            "too recent, %d failed, %d storage root(s) failed%s.",
            report.removed,
            report.freed_bytes,
            report.kept_fresh,
            report.failed,
            report.failed_roots,
            "; stopped at its time limit" if stopped else "",
        )
    return report
