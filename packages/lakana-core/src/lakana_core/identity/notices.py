"""Sign-in notices: what a person should know about sign-in events on their account or others'.

A notice is written in the same transaction as the event it reports (a recovery link used, a
passkey disabled as a clone), so it exists exactly when the event happened. Its recipient sees
it in the interface until they dismiss it; nobody else sees or dismisses it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.errors import NotFoundError
from lakana_core.identity.sessions import utc_now
from lakana_core.models import SignInNotice


class NoticeService:
    """Add, list and dismiss sign-in notices.

    Parameters
    ----------
    session
        Database session. `add` leaves its row for the caller's commit; `dismiss` commits.
    clock
        The clock; `utc_now` by default.
    """

    def __init__(self, session: Session, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._session = session
        self._clock = clock

    def add(
        self, recipient_id: uuid.UUID, kind: str, subject_id: uuid.UUID | None, detail: dict
    ) -> SignInNotice:
        """Add a notice for one person; the caller commits.

        Parameters
        ----------
        recipient_id
            Who sees it.
        kind
            What happened, such as ``recovery_used``.
        subject_id
            The person it is about, if any.
        detail
            What the interface shows with it; never a secret.

        Returns
        -------
        SignInNotice
            The new row.
        """
        row = SignInNotice(
            id=uuid.uuid4(),
            recipient_id=recipient_id,
            kind=kind,
            subject_id=subject_id,
            detail=detail,
            created_at=self._clock(),
        )
        self._session.add(row)
        return row

    def list_for(self, p: PrincipalRef) -> list[SignInNotice]:
        """List the caller's notices that are not dismissed, newest first.

        Parameters
        ----------
        p
            The caller.

        Returns
        -------
        list of SignInNotice
            The caller's own open notices.
        """
        return list(
            self._session.execute(
                select(SignInNotice)
                .where(SignInNotice.recipient_id == p.id, SignInNotice.dismissed_at.is_(None))
                .order_by(SignInNotice.created_at.desc(), SignInNotice.id)
            ).scalars()
        )

    def dismiss(self, p: PrincipalRef, notice_id: uuid.UUID) -> None:
        """Dismiss one of the caller's notices.

        Parameters
        ----------
        p
            The caller.
        notice_id
            The notice.

        Raises
        ------
        NotFoundError
            If the caller has no open notice with that id (another person's notice is not
            found either).
        """
        row = self._session.get(SignInNotice, notice_id)
        if row is None or row.recipient_id != p.id or row.dismissed_at is not None:
            raise NotFoundError(
                "You have no open notice with this id; it may be dismissed already. List your "
                "notices to see the open ones."
            )
        row.dismissed_at = self._clock()
        self._session.commit()
