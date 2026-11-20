"""The OIDC sign-in's flow state: opened by ``start``, called back once, finished once.

- A flow is bound to one browser by a secret in a short-lived cookie (``browser_secret``) and to
  one authorization request by ``state``. The callback matches both, so a ``state`` from another
  browser's flow, or a callback without the flow cookie, matches nothing (login CSRF).
- ``called_back`` and ``finish`` are each one conditional ``UPDATE``: of two requests at once,
  exactly one wins, and a flow is used at most once. A flow expires `FLOW_LIFETIME` after it was
  opened, called back or not.
- Only the SHA-256 of ``state`` and of the browser secret are stored. The ``nonce`` and the PKCE
  ``code_verifier`` are stored as they are: the server sends them to the provider.
- ``return_to`` is kept only when it is a path inside the app (`safe_return_to`); a redirect
  never carries it.

The store never commits: its caller commits, so that a finished flow and the session it starts
are one transaction.
"""

from __future__ import annotations

import secrets
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import unquote

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.orm import Session

from lakana_core.errors import OidcRefusal
from lakana_core.identity.sessions import utc_now
from lakana_core.identity.tokens import hash_secret
from lakana_core.models import OidcFlow, Principal, PrincipalType

FLOW_LIFETIME = timedelta(minutes=10)
"""How long a flow may be called back and finished after it was opened."""

PURGE_AFTER = timedelta(days=1)
"""How long a flow's row is kept past its expiry, for a late callback's audit."""

MAX_RETURN_TO = 2048
"""The longest app path kept."""

FlowPurpose = Literal["sign_in"]
"""What a flow is for."""


@dataclass(frozen=True)
class OpenedFlow:
    """A flow just opened; its secrets are shown here and nowhere else.

    Parameters
    ----------
    browser_secret
        The flow cookie's value: 32 random bytes, URL-safe.
    state
        The ``state`` parameter of the authorization request.
    nonce
        The ``nonce`` parameter; the ID token must carry it.
    code_verifier
        The PKCE verifier; its S256 challenge goes into the authorization request.
    """

    browser_secret: str = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)


@dataclass(frozen=True)
class ProviderIdentity:
    """Who the provider says signed in: a verified ID token's issuer and subject.

    Parameters
    ----------
    issuer
        The ``iss``, exactly as configured.
    subject
        The ``sub``.
    email
        The ``email``, for showing only; `None` when the token had none.
    name
        The ``name``, for showing only; `None` when the token had none.
    auth_time
        When the person authenticated at the provider (``auth_time``), or `None`.
    """

    issuer: str
    subject: str
    email: str | None
    name: str | None
    auth_time: datetime | None


_REASONS: frozenset[str] = frozenset(
    {"not_linked", "deactivated", "token_invalid", "provider_unreachable", "provider_refused"}
)


def _text(outcome: Mapping[str, Any], key: str) -> str | None:
    """Read an optional text of an outcome.

    Parameters
    ----------
    outcome
        The outcome.
    key
        The member.

    Returns
    -------
    str or None
        The text, or `None` when it is missing or not a text.
    """
    value = outcome.get(key)
    return value if isinstance(value, str) else None


def sign_in_outcome(outcome: Mapping[str, Any] | None) -> ProviderIdentity | OidcRefusal:
    """Read what the callback wrote on a flow.

    Parameters
    ----------
    outcome
        The flow's `outcome`, or `None` when the callback wrote none.

    Returns
    -------
    ProviderIdentity or str
        The identity of a verified ID token; else the refusal's reason, ``flow_expired`` when
        there is no usable outcome.
    """
    if not outcome:
        return "flow_expired"
    if outcome.get("ok") is not True:
        reason = outcome.get("reason")
        if reason in _REASONS:
            return reason
        return "flow_expired"
    issuer, subject = _text(outcome, "iss"), _text(outcome, "sub")
    if not issuer or not subject:
        return "flow_expired"
    auth_time = outcome.get("auth_time")
    return ProviderIdentity(
        issuer=issuer,
        subject=subject,
        email=_text(outcome, "email"),
        name=_text(outcome, "name"),
        auth_time=(
            datetime.fromtimestamp(auth_time, UTC)
            if isinstance(auth_time, int) and not isinstance(auth_time, bool)
            else None
        ),
    )


def principal_for(session: Session, identity: ProviderIdentity) -> Principal | None:
    """Find the person linked to a provider identity.

    By ``(oidc_issuer, oidc_subject)`` only: never by e-mail address or name, which a provider
    may let people change.

    Parameters
    ----------
    session
        Database session.
    identity
        The verified identity.

    Returns
    -------
    Principal or None
        The linked person (active or not); `None` when nobody is linked to it.
    """
    return session.execute(
        select(Principal).where(
            Principal.oidc_issuer == identity.issuer,
            Principal.oidc_subject == identity.subject,
            Principal.type == PrincipalType.USER,
        )
    ).scalar_one_or_none()


def safe_return_to(value: str | None) -> str:
    """Keep a page to come back to only when it is a path inside the app.

    Parameters
    ----------
    value
        What the browser asked for.

    Returns
    -------
    str
        `value` when it starts with exactly one ``/``, has no ``//`` or backslash anywhere (also
        percent-encoded), no control or format character, is at most `MAX_RETURN_TO`
        characters and is not a sign-in page; ``/`` otherwise.
    """
    if not value or len(value) > MAX_RETURN_TO or not value.startswith("/"):
        return "/"
    spellings = [value]
    for _ in range(3):
        spellings.append(unquote(spellings[-1]))
    for spelling in spellings:
        if (
            "//" in spelling
            or "\\" in spelling
            or any(unicodedata.category(char) in ("Cc", "Cf") for char in spelling)
            or spelling.lower().startswith("/sign-in")
        ):
            return "/"
    return value


class OidcFlowStore:
    """Open, call back, record and finish OIDC sign-in flows.

    Parameters
    ----------
    session
        Database session; the caller commits.
    clock
        The clock; `lakana_core.identity.sessions.utc_now` by default.
    """

    def __init__(self, session: Session, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._session = session
        self._clock = clock

    def open(
        self,
        purpose: FlowPurpose,
        return_to: str,
        *,
        principal_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        link_id: uuid.UUID | None = None,
    ) -> OpenedFlow:
        """Open a flow, purging old ones first.

        Parameters
        ----------
        purpose
            What the flow is for.
        return_to
            The app path to come back to; checked again here (`safe_return_to`).
        principal_id
            The person the flow is for, when known.
        session_id
            The browser session the flow belongs to, when there is one.
        link_id
            The sign-in link the flow completes, when there is one.

        Returns
        -------
        OpenedFlow
            The secrets for the cookie and the authorization request.
        """
        now = self._clock()
        self.purge(now)
        opened = OpenedFlow(
            browser_secret=secrets.token_urlsafe(32),
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
        )
        self._session.add(
            OidcFlow(
                id=uuid.uuid4(),
                state_hash=hash_secret(opened.state),
                browser_hash=hash_secret(opened.browser_secret),
                nonce=opened.nonce,
                code_verifier=opened.code_verifier,
                purpose=purpose,
                return_to=safe_return_to(return_to),
                principal_id=principal_id,
                session_id=session_id,
                link_id=link_id,
                created_at=now,
                expires_at=now + FLOW_LIFETIME,
            )
        )
        self._session.flush()
        return opened

    def _claim(self, conditions: list[ColumnElement[bool]], column: str) -> OidcFlow | None:
        """Mark one open flow with one conditional ``UPDATE``.

        Parameters
        ----------
        conditions
            Conditions on `OidcFlow`, besides being unexpired.
        column
            The time column to set to now: ``called_back_at`` or ``finished_at``.

        Returns
        -------
        OidcFlow or None
            The flow this call marked, or `None` when none matched.
        """
        claimed = self._session.execute(
            update(OidcFlow)
            .where(*conditions, OidcFlow.expires_at > self._clock())
            .values({column: self._clock()})
            .returning(OidcFlow.id)
            .execution_options(synchronize_session="fetch")
        ).scalar_one_or_none()
        if claimed is None:
            return None
        return self._session.get(OidcFlow, claimed)

    def called_back(self, state: str, browser_secret: str) -> OidcFlow | None:
        """Take the flow a callback names, once.

        Parameters
        ----------
        state
            The callback's ``state``.
        browser_secret
            The flow cookie the browser presented.

        Returns
        -------
        OidcFlow or None
            The flow, now marked called back; `None` when no open, unexpired flow matches both,
            or it was called back before.
        """
        if not state or not browser_secret:
            return None
        return self._claim(
            [
                OidcFlow.state_hash == hash_secret(state),
                OidcFlow.browser_hash == hash_secret(browser_secret),
                OidcFlow.called_back_at.is_(None),
                OidcFlow.finished_at.is_(None),
            ],
            "called_back_at",
        )

    def record(self, flow_id: uuid.UUID, outcome: dict[str, Any]) -> None:
        """Write what the callback found on its flow.

        Parameters
        ----------
        flow_id
            The flow.
        outcome
            ``{"ok": true, "iss", "sub", ...}`` or ``{"ok": false, "reason", ...}``.
        """
        self._session.execute(
            update(OidcFlow)
            .where(OidcFlow.id == flow_id)
            .values(outcome=outcome)
            .execution_options(synchronize_session="fetch")
        )

    def finish(self, browser_secret: str) -> OidcFlow | None:
        """Take the flow of this browser that was called back, once.

        Parameters
        ----------
        browser_secret
            The flow cookie the browser presented.

        Returns
        -------
        OidcFlow or None
            The flow, now marked finished, with its outcome; `None` when this browser has no
            called-back, unfinished, unexpired flow.
        """
        if not browser_secret:
            return None
        return self._claim(
            [
                OidcFlow.browser_hash == hash_secret(browser_secret),
                OidcFlow.called_back_at.is_not(None),
                OidcFlow.finished_at.is_(None),
            ],
            "finished_at",
        )

    def purge(self, now: datetime) -> int:
        """Delete the flows more than `PURGE_AFTER` past their expiry.

        Parameters
        ----------
        now
            The time to count from.

        Returns
        -------
        int
            How many rows were deleted.
        """
        result = self._session.execute(
            delete(OidcFlow)
            .where(OidcFlow.expires_at < now - PURGE_AFTER)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)
