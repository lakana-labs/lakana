"""Run an access apply in one transaction, and lock it against other access changes (#46).

`applying` links every audit event of the apply's changes to one id and refuses any commit
until `finish_apply`, so a failed apply leaves nothing behind. `lock_access` serializes an
apply with the guarded changes that run beside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from lakana_core.audit.service import ACCESS_APPLY_KEY

ACCESS_LOCK_KEY = int.from_bytes(b"lkaccess", "big", signed=True)
"""The Postgres advisory lock key: apply exclusive, every guarded change shared."""

_FINAL = "lakana.access_apply_final"


@contextmanager
def applying(session: Session, apply_id: uuid.UUID) -> Generator[None]:
    """Run an apply's changes in the session's one transaction, linked to the apply.

    Parameters
    ----------
    session
        The request's session.
    apply_id
        The apply's id; `AuditService.record` adds it to every event as `access_apply_id`.

    Yields
    ------
    None
        While the changes run; a commit other than `finish_apply`'s raises.
    """

    def refuse(_: Session) -> None:
        """Refuse any commit before `finish_apply`.

        Parameters
        ----------
        _
            The committing session.

        Raises
        ------
        RuntimeError
            Always, unless `finish_apply` is committing.
        """
        if not session.info.get(_FINAL):
            raise RuntimeError("an access apply commits once, at its end")

    session.info.pop(_FINAL, None)
    session.info[ACCESS_APPLY_KEY] = apply_id
    event.listen(session, "before_commit", refuse)
    try:
        yield
    finally:
        event.remove(session, "before_commit", refuse)
        session.info.pop(ACCESS_APPLY_KEY, None)
        session.info.pop(_FINAL, None)


def finish_apply(session: Session) -> None:
    """Commit an apply: the one commit `applying` allows.

    Parameters
    ----------
    session
        The session inside `applying`.

    Raises
    ------
    RuntimeError
        Outside `applying`, where the flag would let a later apply commit half-way.
    """
    if ACCESS_APPLY_KEY not in session.info:
        raise RuntimeError("finish_apply commits only inside an access apply")
    session.info[_FINAL] = True
    session.commit()


def lock_access(session: Session, *, shared: bool) -> None:
    """Take the access advisory lock until the transaction ends (Postgres only).

    Parameters
    ----------
    session
        The request's session.
    shared
        `True` for a guarded change, which can run beside others; `False` for an apply,
        which runs alone.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    session.execute(text(f"SELECT {function}(:key)"), {"key": ACCESS_LOCK_KEY})
