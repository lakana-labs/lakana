"""Single-use sign-in links: setup, invite, recovery and local.

A link is ``<public address>/sign-in#l=<public id>&s=<secret>``. Both halves travel after the
``#``, so a browser never sends them in a request line, an access log or a ``Referer``. The
interface looks the link up by its public id to show "Sign in as X" (`LinkService.describe`,
which never uses the secret and never consumes), and uses it with a POST of both halves
(`LinkService.redeem`).

Rules (the spec's "Decided rules", and the security reviews S3, S4, S9, N4 to N8):

- Only the secret's SHA-256 digest is stored; the presented secret is compared in constant time.
  A wrong secret answers exactly like an unknown public id (`LinkUnusableError("unknown")`).
- Lifetimes (`LINK_LIFETIMES`): setup 10 minutes, local 60 seconds (the browser opens it at
  once), recovery 1 hour, invite 7 days.
  A link is expired from `expires_at` on (``expires_at <= now``).
- Use is one conditional ``UPDATE … WHERE used_at IS NULL AND withdrawn_at IS NULL AND
  expires_at > now RETURNING`` in the transaction that also creates the account (invite) and
  the session, so of two concurrent uses exactly one wins, and of a use and a withdrawal, too.
- Links are for people: a task, CI or service-account token never issues one
  (`refuse_automation`).
- The setup link and recovering an administrator are the server operator's: the holder of the
  bootstrap PAT `bootstrap_if_needed` minted (`is_operator`), never any other token, even one
  named ``bootstrap`` for the bootstrap principal.
- An open invite reserves its name, compared in any letter case (`name_unavailable`), so no
  principal and no second invite can take it meanwhile; using it fails if the name was taken
  anyway.
- An invite or recovery link stands only while its issuer does: an active account holding
  ADMIN on the instance, the server operator included. Losing either withdraws the issuer's
  open links (`lakana_core.grants.service.withdraw_links_of_lapsed_issuers`), and using one
  checks again (`LinkService._check_issuer`).
- A local link (only with `LAKANA_UI__LOCAL_SIGN_IN`) is minted with the person's own PAT and
  signs in without a passkey touch; it is never used over a session the browser already has.
"""

from __future__ import annotations

import secrets
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import AuthorizationService, PrincipalRef
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    LinkUnusableError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.admin import holds_instance_admin
from lakana_core.grants.service import (
    ISSUED_BY_ADMINS,
    active_instance_admin_ids,
    lapsed_issuer_reason,
)
from lakana_core.identity.notices import NoticeService
from lakana_core.identity.sessions import (
    SessionService,
    StartedSession,
    require_principal_admin,
    require_step_up,
    utc_now,
)
from lakana_core.identity.tokens import hash_secret, verify_secret
from lakana_core.models import (
    AuditEventType,
    Passkey,
    PersonalAccessToken,
    Principal,
    PrincipalType,
    SignInLink,
)

LinkKind = Literal["setup", "invite", "recovery", "local"]
"""The four kinds of sign-in link."""

LinkState = Literal["open", "used", "expired", "withdrawn"]
"""What a link can be: usable, or why not."""

LINK_LIFETIMES: dict[str, timedelta] = {
    "setup": timedelta(minutes=10),
    "local": timedelta(seconds=60),
    "recovery": timedelta(hours=1),
    "invite": timedelta(days=7),
}
"""How long each kind of link can be used after it was issued."""

AUTOMATION_MESSAGE = (
    "Links are for people: a task, CI or service-account token cannot issue one. Use your own "
    "token or the interface."
)
"""The refusal of `refuse_automation`."""

NOT_OPERATOR_SETUP = "Only the server operator's bootstrap token issues the setup link."
"""The refusal of a setup link to anyone but the operator."""


def not_operator_recovery(name: str) -> str:
    """Word the refusal of an administrator's recovery to anyone but the server operator.

    Parameters
    ----------
    name
        The person's principal name.

    Returns
    -------
    str
        The cause (they hold or once held ADMIN) and the operator's command.
    """
    return (
        f"{name!r} holds or has held the administrator role, so only the server operator can "
        f"recover the account: run `lakana recovery create {name}` with the bootstrap token "
        "where the server runs."
    )


def already_set_up(bootstrap_name: str) -> str:
    """Word the refusal of a setup link once the first administrator has a passkey.

    Parameters
    ----------
    bootstrap_name
        The bootstrap principal's name (`LAKANA_BOOTSTRAP_ADMIN`).

    Returns
    -------
    str
        The cause (a passkey exists, perhaps made for an earlier public address) and the server
        operator's command.
    """
    return (
        "Already set up: the first administrator has a passkey, even if it stopped working "
        "after a change of the public address. The server operator recovers the account: run "
        f"`lakana recovery create {bootstrap_name}` with the bootstrap token where the server "
        "runs."
    )


LOCAL_OFF = (
    "Local sign-in is off on this server; it is only for `lakana server start --local` on one "
    "person's own machine. Sign in with your passkey or ask an administrator for a recovery link."
)
"""The refusal (404) of a local link while `LAKANA_UI__LOCAL_SIGN_IN` is off."""

NAME_TAKEN_MEANWHILE = (
    "That name was taken meanwhile, so this invite was closed. Ask whoever invited you for a new "
    "invite under another name (an administrator runs `lakana invite create NAME`)."
)
"""The refusal of an invite whose name another principal took after it was issued."""

_NAME_LIMIT = 255


class _CloseLink(Exception):
    """A claimed link whose rules no longer hold: roll back, withdraw it, raise `error`.

    Parameters
    ----------
    error
        The error the caller sees.
    reason
        Why the link is closed, for the audit event.
    """

    def __init__(self, error: Exception, reason: str) -> None:
        super().__init__(reason)
        self.error = error
        self.reason = reason


@dataclass(frozen=True)
class IssuedLink:
    """A link just issued; its URL (with the secret) is shown here and nowhere else.

    Parameters
    ----------
    id
        The link's row id: what an administrator withdraws it by.
    kind
        ``setup``, ``invite``, ``recovery`` or ``local``.
    url
        ``<public address>/sign-in#l=<public id>&s=<secret>``. Left out of the `repr`.
    expires_at
        When it stops working.
    """

    id: uuid.UUID
    kind: str
    url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class LinkDescription:
    """What the sign-in page shows for a link before it is used.

    Parameters
    ----------
    kind
        The link's kind.
    name
        The principal name it signs in as (an invite: the name it creates).
    display_name
        The person's display name, if one is known.
    state
        ``open``, ``used``, ``expired`` or ``withdrawn``.
    """

    kind: str
    name: str
    display_name: str | None
    state: LinkState


@dataclass(frozen=True)
class Redeemed:
    """What using a link started.

    Parameters
    ----------
    started
        The new browser session.
    needs_passkey
        Whether the person must register a passkey now: after a setup, invite or recovery link
        on an account without a usable passkey.
    offer_passkey
        Whether the interface offers (never forces) to register one: after a local link on an
        account without a usable passkey.
    kind
        The link's kind.
    """

    started: StartedSession
    needs_passkey: bool
    offer_passkey: bool
    kind: str


def is_operator(session: Session, p: PrincipalRef, *, bootstrap_name: str) -> bool:
    """Say whether the caller is the server operator: the bootstrap PAT itself.

    That is the unrevoked, unexpired PAT `lakana_core.bootstrap.bootstrap_if_needed` minted
    (`PersonalAccessToken.bootstrap`), held by the bootstrap principal. Any other PAT of the
    bootstrap principal, even one an administrator named ``bootstrap``, a session and a task or
    CI token are not.

    Parameters
    ----------
    session
        Database session.
    p
        The caller.
    bootstrap_name
        The bootstrap principal's name (`LAKANA_BOOTSTRAP_ADMIN`).

    Returns
    -------
    bool
        Whether `p` authenticated with the operator's token.
    """
    if p.credential != "pat" or p.token_id is None or p.name != bootstrap_name:
        return False
    pat = session.get(PersonalAccessToken, p.token_id)
    if pat is None or not pat.bootstrap or pat.revoked_at is not None:
        return False
    return pat.principal_id == p.id and (pat.expires_at is None or pat.expires_at > utc_now())


def refuse_automation(p: PrincipalRef) -> None:
    """Refuse a link to anything but a person's own credential.

    Parameters
    ----------
    p
        The caller.

    Raises
    ------
    PermissionDenied
        With `AUTOMATION_MESSAGE` for a task or CI token and for a service account.
    """
    if (
        p.credential in {"task", "ci"}
        or p.task_scope is not None
        or p.ci_scope is not None
        or p.type is PrincipalType.SERVICE_ACCOUNT
    ):
        raise PermissionDenied(AUTOMATION_MESSAGE)


def has_passkey(
    session: Session, principal_id: uuid.UUID, *, usable: bool = True, rp_id: str | None = None
) -> bool:
    """Say whether a person has a passkey.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The person.
    usable
        Count only passkeys that are not disabled (a disabled clone cannot sign in).
    rp_id
        Count only passkeys registered for this relying-party id (the host of the public
        address): one registered for an earlier address cannot sign in here.

    Returns
    -------
    bool
        Whether at least one such passkey is registered.
    """
    condition = Passkey.principal_id == principal_id
    if usable:
        condition = condition & Passkey.disabled_at.is_(None)
    if rp_id is not None:
        condition = condition & (Passkey.rp_id == rp_id)
    return bool(session.execute(select(exists().where(condition))).scalar())


def name_key(name: str) -> str:
    """Reduce a principal name to what two look-alike names share.

    NFKC normalisation (fullwidth ``U+FF41`` becomes ``a``, the ligature ``U+FB01`` becomes
    ``fi``), then case folding (``ß`` becomes ``ss``), then NFKC again, as Unicode's
    ``NFKC_Casefold`` recommends for identifiers. Two names with the same key are one name.

    Parameters
    ----------
    name
        A principal name.

    Returns
    -------
    str
        Its comparison key; also what an open invite reserves.
    """
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


NAME_LIMIT = 255
"""The most characters a principal name, and its `name_key`, may have (the columns' length)."""


def name_length_problem(name: str) -> str | None:
    """Say why a new principal name is too long, if it is.

    Both the name and its `name_key` must fit `NAME_LIMIT`: normalising can lengthen a name
    (the ligature ``U+FB03`` becomes ``ffi``), and an open invite stores the key.

    Parameters
    ----------
    name
        The candidate name.

    Returns
    -------
    str or None
        The refusal, naming the cause and the next step; `None` when the name fits.
    """
    if len(name) > NAME_LIMIT:
        return (
            f"name: at most {NAME_LIMIT} characters; this one has {len(name)}. Choose a "
            "shorter name."
        )
    key = len(name_key(name))
    if key > NAME_LIMIT:
        return (
            f"name: compared as {key} characters once its letters are normalised (ligatures "
            f"such as U+FB03 count as several), and at most {NAME_LIMIT} fit. Choose a shorter "
            "name, or write it with plain letters."
        )
    return None


def invisible_character_problem(name: str) -> str | None:
    """Say why a new principal name is refused for its characters, if it is.

    Refused: control characters (Unicode category ``Cc``, such as a line break), format
    characters (``Cf``: zero-width space, zero-width joiner, soft hyphen, direction marks) and
    a combining mark (``Mn``, ``Mc``, ``Me``) as the first character. Each can make two names
    look the same, or a name look like another.

    Parameters
    ----------
    name
        The candidate name.

    Returns
    -------
    str or None
        The refusal, naming the cause and the next step; `None` when the characters are fine.
    """
    for position, character in enumerate(name):
        category = unicodedata.category(character)
        if category in {"Cc", "Cf"} or (position == 0 and category.startswith("M")):
            return (
                f"name: character {position + 1} (U+{ord(character):04X}) is invisible or "
                "combines with the one before it, so the name could look like another. Type the "
                "name again with letters, digits and visible punctuation only."
            )
    return None


def _clashing_principal(session: Session, name: str) -> str | None:
    """Find a principal whose name has the same `name_key` as `name`.

    Compared in Python rather than with SQL ``lower``, which folds only ASCII on SQLite and
    knows no NFKC: the list of principals of one instance is small.

    Parameters
    ----------
    session
        Database session.
    name
        The candidate name.

    Returns
    -------
    str or None
        The existing principal's name, active or not; `None` when no name clashes.
    """
    key = name_key(name)
    for existing in session.execute(select(Principal.name)).scalars():
        if name_key(existing) == key:
            return existing
    return None


def _reserving_invite(session: Session, name: str, now: datetime) -> SignInLink | None:
    """Find the open invite that reserves a name, in any letter case.

    Parameters
    ----------
    session
        Database session.
    name
        The candidate name.
    now
        The time to judge "open" at.

    Returns
    -------
    SignInLink or None
        The open invite; `None` when none reserves the name (an expired one does not).
    """
    row = session.execute(
        select(SignInLink).where(SignInLink.reserved_name == name_key(name))
    ).scalar_one_or_none()
    if row is None or _state(row, now) != "open":
        return None
    return row


def name_unavailable(session: Session, name: str, *, now: datetime) -> str | None:
    """Say why a new principal cannot have a name, if it cannot (F25).

    The exact name and the characters (`invisible_character_problem`) are checked by the
    caller; this refuses a name whose `name_key` equals an existing principal's
    (active or deactivated: it differs only in letter case or in look-alike forms such as
    fullwidth letters), and one an open invite reserves.

    Parameters
    ----------
    session
        Database session.
    name
        The candidate name.
    now
        The time to judge an invite's reservation at.

    Returns
    -------
    str or None
        The refusal, naming the cause and the next step; `None` when the name is free.
    """
    existing = _clashing_principal(session, name)
    if existing is not None:
        return (
            f"The name {name!r} differs only in letter case or look-alike characters (such "
            f"as fullwidth letters) from the existing principal "
            f"{existing!r}, and names that look alike are refused. Choose another name, or use "
            f"{existing!r}."
        )
    if _reserving_invite(session, name, now) is not None:
        return (
            f"The name {name!r} is reserved by an open invite (in any letter case). Withdraw "
            "the invite first (`lakana invite withdraw` or `DELETE /api/v1/invites/{id}`), or "
            "choose another name."
        )
    return None


def withdraw_open_links(
    session: Session, principal_id: uuid.UUID, *, now: datetime, reason: str
) -> int:
    """Withdraw every open link bound to a person, each with one audit event; the caller commits.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The person.
    now
        The withdrawal time.
    reason
        Why, for the audit log (``deactivated``, ``reactivated``).

    Returns
    -------
    int
        How many links this call withdrew.
    """
    withdrawn = session.execute(
        update(SignInLink)
        .where(
            SignInLink.principal_id == principal_id,
            SignInLink.used_at.is_(None),
            SignInLink.withdrawn_at.is_(None),
        )
        .values(withdrawn_at=now, reserved_name=None)
        .returning(SignInLink.id, SignInLink.kind)
        .execution_options(synchronize_session="fetch")
    ).all()
    audit = AuditService(session)
    for link_id, kind in sorted(withdrawn, key=lambda r: str(r[0])):
        audit.record(
            principal_id,
            AuditEventType.SIGN_IN_LINK_WITHDRAWN,
            detail={"kind": kind, "link_id": str(link_id), "reason": reason},
        )
    return len(withdrawn)


def _state(row: SignInLink, now: datetime) -> LinkState:
    """Say what a link is at a time.

    Parameters
    ----------
    row
        The link.
    now
        The time.

    Returns
    -------
    str
        ``used``, ``withdrawn``, ``expired`` (from `expires_at` on) or ``open``.
    """
    if row.used_at is not None:
        return "used"
    if row.withdrawn_at is not None:
        return "withdrawn"
    if row.expires_at <= now:
        return "expired"
    return "open"


def open_links(
    session: Session, kind: Literal["invite", "recovery"], now: datetime
) -> list[SignInLink]:
    """List the open links of one kind, oldest first, with no authorization.

    The caller decides who may see them: `LinkService.list_open` for an administrator, Home's
    Needs your attention (`lakana_core.home.attention`) for one.

    Parameters
    ----------
    session
        Database session.
    kind
        ``invite`` or ``recovery``.
    now
        The time a link must expire after.

    Returns
    -------
    list of SignInLink
        Links neither used, withdrawn nor expired.
    """
    return list(
        session.execute(
            select(SignInLink)
            .where(
                SignInLink.kind == kind,
                SignInLink.used_at.is_(None),
                SignInLink.withdrawn_at.is_(None),
                SignInLink.expires_at > now,
            )
            .order_by(SignInLink.created_at, SignInLink.id)
        ).scalars()
    )


class LinkService:
    """Issue, describe, use, list and withdraw sign-in links.

    Parameters
    ----------
    session
        Database session. Every mutating method commits, except `withdraw_for_principal` by
        default.
    authz
        Authorization service, asked before an administrator issues, lists or withdraws links.
    sessions
        The session service a used link starts its session with.
    origin
        The interface's public address (`Settings.public_origin().origin`); every link is
        built from it, never from a request.
    local_mode
        Whether local links may be issued (`LAKANA_UI__LOCAL_SIGN_IN`).
    bootstrap_name
        The bootstrap principal's name (`LAKANA_BOOTSTRAP_ADMIN`), for `is_operator`.
    clock
        The clock; `utc_now` by default.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        sessions: SessionService,
        *,
        origin: str,
        local_mode: bool,
        bootstrap_name: str,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._authz = authz
        self._sessions = sessions
        self._origin = origin.rstrip("/")
        self._local_mode = local_mode
        self._bootstrap_name = bootstrap_name
        self._clock = clock

    def _require_admin(self, p: PrincipalRef) -> None:
        """Refuse unless `p` is an instance administrator (`require_principal_admin`).

        Parameters
        ----------
        p
            The acting principal.
        """
        require_principal_admin(
            self._session,
            self._authz,
            p,
            doing="issue, list or withdraw invites and recovery links",
        )

    def _issue(
        self,
        p: PrincipalRef,
        kind: LinkKind,
        *,
        principal_id: uuid.UUID | None,
        name: str,
        source_pat_id: uuid.UUID | None = None,
        invite_display_name: str | None = None,
    ) -> IssuedLink:
        """Store a new link, audit it and commit.

        Parameters
        ----------
        p
            The issuer.
        kind
            The link's kind.
        principal_id
            The person it signs in; `None` for an invite.
        name
            The principal name it signs in as (an invite: the name it creates).
        source_pat_id
            The PAT a local link is minted with, or the operator's bootstrap PAT a recovery
            link is issued with.
        invite_display_name
            An invite's display name.

        Returns
        -------
        IssuedLink
            The link with its URL.

        Raises
        ------
        AlreadyExistsError
            If another invite reserved the same name at the same moment.
        """
        now = self._clock()
        public_id = secrets.token_urlsafe(16)
        secret = secrets.token_urlsafe(32)
        row = SignInLink(
            id=uuid.uuid4(),
            public_id=public_id,
            secret_hash=hash_secret(secret),
            kind=kind,
            principal_id=principal_id,
            invite_name=name if kind == "invite" else None,
            invite_display_name=invite_display_name,
            reserved_name=name_key(name) if kind == "invite" else None,
            issued_by=p.id,
            source_pat_id=source_pat_id,
            created_at=now,
            expires_at=now + LINK_LIFETIMES[kind],
        )
        self._session.add(row)
        AuditService(self._session).record(
            p.id,
            AuditEventType.SIGN_IN_LINK_ISSUED,
            detail={"kind": kind, "link_id": str(row.id), "principal": name, "actor": p.name},
        )
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            raise AlreadyExistsError(
                f"Another invite reserved the name {name!r} at the same moment. List the open "
                "invites, or choose another name."
            ) from None
        return IssuedLink(
            id=row.id,
            kind=kind,
            url=f"{self._origin}/sign-in#l={public_id}&s={secret}",
            expires_at=row.expires_at,
        )

    def _withdraw_open(self, principal_id: uuid.UUID, kind: LinkKind, p: PrincipalRef) -> None:
        """Withdraw a person's earlier open links of one kind; the caller commits.

        Parameters
        ----------
        principal_id
            The person.
        kind
            The kind.
        p
            The issuer of the new link, named in the audit event.
        """
        withdrawn = self._session.execute(
            update(SignInLink)
            .where(
                SignInLink.principal_id == principal_id,
                SignInLink.kind == kind,
                SignInLink.used_at.is_(None),
                SignInLink.withdrawn_at.is_(None),
            )
            .values(withdrawn_at=self._clock())
            .returning(SignInLink.id)
            .execution_options(synchronize_session="fetch")
        ).all()
        for (link_id,) in withdrawn:
            AuditService(self._session).record(
                p.id,
                AuditEventType.SIGN_IN_LINK_WITHDRAWN,
                detail={
                    "kind": kind,
                    "link_id": str(link_id),
                    "actor": p.name,
                    "reason": "replaced",
                },
            )

    def issue_setup(self, p: PrincipalRef) -> IssuedLink:
        """Issue the setup link that signs the first administrator in.

        Parameters
        ----------
        p
            The caller: the server operator.

        Returns
        -------
        IssuedLink
            A link for the bootstrap principal, valid for 10 minutes. Earlier open setup
            links are withdrawn.

        Raises
        ------
        PermissionDenied
            For automation, and for anyone but the operator (`is_operator`).
        ConflictError
            Once the bootstrap principal has a passkey.
        """
        refuse_automation(p)
        if not is_operator(self._session, p, bootstrap_name=self._bootstrap_name):
            raise PermissionDenied(NOT_OPERATOR_SETUP)
        if has_passkey(self._session, p.id, usable=False):
            raise ConflictError(already_set_up(self._bootstrap_name))
        self._withdraw_open(p.id, "setup", p)
        return self._issue(p, "setup", principal_id=p.id, name=p.name)

    def issue_local(self, p: PrincipalRef) -> IssuedLink:
        """Issue a local sign-in link for the caller themself.

        Parameters
        ----------
        p
            The caller, with their own personal access token.

        Returns
        -------
        IssuedLink
            A link for the caller, valid for 60 seconds; its session ends when the token is
            revoked.

        Raises
        ------
        PermissionDenied
            For automation, and for any credential but a person's PAT.
        NotFoundError
            While local sign-in is off (`LOCAL_OFF`).
        """
        refuse_automation(p)
        if not self._local_mode:
            raise NotFoundError(LOCAL_OFF)
        if p.credential != "pat" or p.token_id is None:
            raise PermissionDenied(
                "A local sign-in link is minted with your own personal access token "
                "(`lakana server start --local` does this for you); a browser session cannot "
                "mint one, and this browser is signed in already."
            )
        return self._issue(p, "local", principal_id=p.id, name=p.name, source_pat_id=p.token_id)

    def issue_invite(self, p: PrincipalRef, *, name: str, display_name: str | None) -> IssuedLink:
        """Invite a new person: a link that creates their account when used.

        Parameters
        ----------
        p
            The caller: an administrator, with a fresh passkey touch in a browser.
        name
            The new principal's name. Reserved, in any letter case, while the invite is open.
        display_name
            The person's name as the interface shows it; optional.

        Returns
        -------
        IssuedLink
            The invite, valid for 7 days.

        Raises
        ------
        PermissionDenied
            For automation and non-administrators; `StepUpRequired` without a fresh touch.
        ValidationError
            For an empty name, or a name or display name over 255 characters.
        AlreadyExistsError
            For the name of any principal (any state, any letter case), the bootstrap
            principal's name, and a name another open invite reserves.
        """
        refuse_automation(p)
        self._require_admin(p)
        now = self._clock()
        require_step_up(p, now=now)
        if not name or name != name.strip() or len(name) > _NAME_LIMIT:
            raise ValidationError(
                f"name: give the new person's principal name, 1 to {_NAME_LIMIT} characters "
                "without leading or trailing spaces."
            )
        problem = name_length_problem(name) or invisible_character_problem(name)
        if problem is not None:
            raise ValidationError(problem)
        shown = display_name.strip() if display_name else None
        if shown is not None and len(shown) > _NAME_LIMIT:
            raise ValidationError(f"display_name: at most {_NAME_LIMIT} characters.")
        if name_key(name) == name_key(self._bootstrap_name):
            raise AlreadyExistsError(
                f"{name!r} is the server's first administrator (LAKANA_BOOTSTRAP_ADMIN) and "
                "cannot be invited. Choose another name."
            )
        existing = _clashing_principal(self._session, name)
        if existing is not None:
            raise AlreadyExistsError(
                f"A principal named {existing!r} exists already (names are compared in any "
                "letter case). Choose another name; to let that person in again, send them a "
                "recovery link."
            )
        # An expired invite still holds its reservation; free it before taking the name.
        self._session.execute(
            update(SignInLink)
            .where(SignInLink.reserved_name == name_key(name), SignInLink.expires_at <= now)
            .values(reserved_name=None)
            .execution_options(synchronize_session="fetch")
        )
        if _reserving_invite(self._session, name, now) is not None:
            raise AlreadyExistsError(
                f"An open invite reserves the name {name!r} already (in any letter case). List "
                "the open invites, or withdraw that one first."
            )
        return self._issue(
            p, "invite", principal_id=None, name=name, invite_display_name=shown or None
        )

    def issue_recovery(self, p: PrincipalRef, *, principal_name: str) -> IssuedLink:
        """Issue a recovery link for a person who lost their passkeys.

        Using it removes the person's passkeys, ends their sessions and notifies them and every
        administrator. Recovering an administrator is for the server operator only; recovering
        oneself is refused, except for the operator, who has no other way back in.

        Parameters
        ----------
        p
            The caller: an administrator, with a fresh passkey touch in a browser.
        principal_name
            The person to recover.

        Returns
        -------
        IssuedLink
            The link, valid for 1 hour. The person's earlier open recovery link is withdrawn.

        Raises
        ------
        PermissionDenied
            For automation and non-administrators; `StepUpRequired` without a fresh touch; for
            the recovery of anyone who holds or has held ADMIN, by anyone but the operator.
        NotFoundError
            If no principal has the name.
        ConflictError
            For a service account, a deactivated person, and oneself.
        """
        refuse_automation(p)
        self._require_admin(p)
        require_step_up(p, now=self._clock())
        target = self._session.execute(
            select(Principal).where(Principal.name == principal_name)
        ).scalar_one_or_none()
        if target is None:
            raise NotFoundError(
                f"No principal named {principal_name!r}. Check the name and try again."
            )
        if target.type is not PrincipalType.USER:
            raise ConflictError(
                f"{principal_name!r} is a service account: it signs in with its API token, not "
                "a passkey. Issue it a new token instead."
            )
        if not target.is_active:
            raise ConflictError(
                f"{principal_name!r} is deactivated. Reactivate the account before recovering it."
            )
        operator = is_operator(self._session, p, bootstrap_name=self._bootstrap_name)
        if target.id == p.id and not operator:
            raise ConflictError(
                "You cannot recover your own account with a link. Use Add passkey in the "
                "interface while you are signed in, or ask another administrator."
            )
        if self._is_or_was_admin(target) and not operator:
            raise PermissionDenied(not_operator_recovery(target.name))
        self._withdraw_open(target.id, "recovery", p)
        return self._issue(
            p,
            "recovery",
            principal_id=target.id,
            name=target.name,
            # The operator's token is recorded, so the link may still recover the person if
            # they become an administrator before it is used.
            source_pat_id=p.token_id if operator else None,
        )

    def _by_public_id(self, public_id: str) -> SignInLink | None:
        """Look a link up by its public id.

        Parameters
        ----------
        public_id
            The public id.

        Returns
        -------
        SignInLink or None
            The row, in any state.
        """
        return self._session.execute(
            select(SignInLink).where(SignInLink.public_id == public_id)
        ).scalar_one_or_none()

    def describe(self, public_id: str) -> LinkDescription:
        """Describe a link for the sign-in page, without its secret and without using it.

        Parameters
        ----------
        public_id
            The link's public id.

        Returns
        -------
        LinkDescription
            Its kind, the name it signs in as and its state.

        Raises
        ------
        LinkUnusableError
            With ``unknown`` if no link has that public id.
        """
        row = self._by_public_id(public_id)
        if row is None:
            raise LinkUnusableError("unknown")
        principal = self._session.get(Principal, row.principal_id) if row.principal_id else None
        if row.kind == "invite":
            name = row.invite_name or ""
            display_name = row.invite_display_name
        else:
            name = principal.name if principal is not None else ""
            display_name = principal.display_name if principal is not None else None
        return LinkDescription(
            kind=row.kind, name=name, display_name=display_name, state=_state(row, self._clock())
        )

    def redeem(
        self,
        public_id: str,
        secret: str,
        *,
        replacing: str | None,
        peer: str,
        user_agent: str,
        has_session: bool,
    ) -> Redeemed:
        """Use a link: one conditional update, the account (invite) and the session, one commit.

        Parameters
        ----------
        public_id
            The link's public id.
        secret
            The link's secret.
        replacing
            The session cookie the browser presented, if any; it is ended as ``replaced``.
        peer
            The client's address.
        user_agent
            The browser's `User-Agent`.
        has_session
            Whether the browser has an open session: a local link is then not used.

        Returns
        -------
        Redeemed
            The new session and whether a passkey must or may be registered now.

        Raises
        ------
        LinkUnusableError
            ``unknown`` for an unknown public id or a wrong secret (alike); ``used``,
            ``expired`` or ``withdrawn`` otherwise.
        ConflictError
            For a local link over an open session (the link stays usable), and for an invite
            whose name was taken meanwhile.
        PermissionDenied
            If the person cannot sign in (deactivated; a local link's token revoked), and for a
            local link on a server no longer in local mode (the link is closed).
        """
        row = self._by_public_id(public_id)
        if row is None or not verify_secret(secret, row.secret_hash):
            raise LinkUnusableError("unknown")
        if row.kind == "local" and has_session:
            raise ConflictError(
                "This browser is already signed in, so the local sign-in link was not used. "
                "Keep working as you are, or sign out first and open the link again."
            )
        now = self._clock()
        claimed = self._session.execute(
            update(SignInLink)
            .where(
                SignInLink.id == row.id,
                SignInLink.used_at.is_(None),
                SignInLink.withdrawn_at.is_(None),
                SignInLink.expires_at > now,
            )
            .values(
                used_at=now,
                reserved_name=None,
                used_peer=peer[:64],
                used_user_agent=user_agent[:255],
            )
            .returning(SignInLink.id)
            .execution_options(synchronize_session=False)
        ).all()
        if not claimed:
            self._session.rollback()
            self._session.refresh(row)
            state = _state(row, now)
            # Open again only while another use is in flight; that one wins.
            raise LinkUnusableError("used" if state == "open" else state)
        try:
            redeemed = self._complete(
                row, now, replacing=replacing, peer=peer, user_agent=user_agent
            )
            self._session.commit()
        except _CloseLink as close:
            self._session.rollback()
            self._close(row.id, row.kind, close.reason)
            raise close.error from None
        except BaseException:
            self._session.rollback()
            raise
        return redeemed

    def _withdraw_if_open(self, link_id: uuid.UUID) -> bool:
        """Withdraw a link unless it was used or withdrawn already; the caller audits and commits.

        Shares the ``used_at IS NULL`` guard with `redeem`, so a link is either used or
        withdrawn, never both.

        Parameters
        ----------
        link_id
            The link.

        Returns
        -------
        bool
            Whether this call withdrew it.
        """
        withdrawn = self._session.execute(
            update(SignInLink)
            .where(
                SignInLink.id == link_id,
                SignInLink.used_at.is_(None),
                SignInLink.withdrawn_at.is_(None),
            )
            .values(withdrawn_at=self._clock(), reserved_name=None)
            .returning(SignInLink.id)
            .execution_options(synchronize_session=False)
        ).all()
        return bool(withdrawn)

    def _close(self, link_id: uuid.UUID, kind: str, reason: str) -> None:
        """Withdraw a link whose rules no longer hold at its use, audited, and commit.

        Parameters
        ----------
        link_id
            The link.
        kind
            Its kind, for the audit event.
        reason
            Why, for the audit event.
        """
        if self._withdraw_if_open(link_id):
            AuditService(self._session).record(
                None,
                AuditEventType.SIGN_IN_LINK_WITHDRAWN,
                detail={"kind": kind, "link_id": str(link_id), "reason": reason},
            )
        self._session.commit()

    def _is_or_was_admin(self, principal: Principal) -> bool:
        """Say whether a person holds instance ADMIN now or has ever held it.

        Parameters
        ----------
        principal
            The person.

        Returns
        -------
        bool
            `Principal.held_admin`, or ADMIN covering them now (a grant made before the mark
            existed is backfilled by migration 0070; this is the second net).
        """
        return principal.held_admin or principal.id in active_instance_admin_ids(self._session)

    def _issued_by_operator(self, row: SignInLink) -> bool:
        """Say whether a recovery link was issued with the operator's bootstrap token.

        Parameters
        ----------
        row
            The link.

        Returns
        -------
        bool
            Whether `source_pat_id` names a bootstrap PAT that is still unrevoked and
            unexpired, as `is_operator` asks at issue.
        """
        if row.source_pat_id is None:
            return False
        pat = self._session.get(PersonalAccessToken, row.source_pat_id)
        if pat is None or not pat.bootstrap or pat.revoked_at is not None:
            return False
        return pat.expires_at is None or pat.expires_at > self._clock()

    def _check_issuer(self, row: SignInLink) -> None:
        """Refuse an invite or recovery link whose issuer no longer stands behind it.

        The issuer needed ``ADMIN`` on the instance to issue it (`_require_admin`), the server
        operator included, so they still need it, and an active account, when it is used.
        Deactivating an administrator or taking their ADMIN away withdraws their open links at
        once (`lakana_core.grants.service.withdraw_links_of_lapsed_issuers`); this is the check
        that holds even where that did not run.

        Parameters
        ----------
        row
            The claimed link.

        Raises
        ------
        _CloseLink
            With `PermissionDenied` naming the cause and the next step, and the reason
            ``issuer_gone``, ``issuer_deactivated`` or ``issuer_not_admin``.
        """
        issuer = self._session.get(Principal, row.issued_by) if row.issued_by else None
        is_admin = issuer is not None and holds_instance_admin(self._session, issuer.id)
        reason = lapsed_issuer_reason(issuer, is_admin=is_admin)
        if reason is None:
            return
        if issuer is None:
            cause = "The account that issued this link no longer exists"
        elif reason == "issuer_deactivated":
            cause = f"{issuer.name!r}, who issued this link, has been deactivated"
        else:
            cause = f"{issuer.name!r}, who issued this link, is no longer an administrator"
        raise _CloseLink(
            PermissionDenied(
                f"{cause}, so the link was closed. Ask an administrator for a new link."
            ),
            reason,
        )

    def _complete(
        self, row: SignInLink, now: datetime, *, replacing: str | None, peer: str, user_agent: str
    ) -> Redeemed:
        """Do what a claimed link does, in the claiming transaction; the caller commits.

        Parameters
        ----------
        row
            The link, claimed by this transaction.
        now
            The time of use.
        replacing
            The session cookie the browser presented, if any.
        peer
            The client's address.
        user_agent
            The browser's `User-Agent`.

        Returns
        -------
        Redeemed
            The new session and the passkey flags.

        Raises
        ------
        _CloseLink
            When a rule checked at issue no longer holds: the issuer of an invite or recovery
            link lost the right to issue it (`_check_issuer`), an invite's name was taken meanwhile,
            a local link is used on a server no longer in local mode, the bootstrap account of a
            setup link has a passkey now, or the person of a recovery
            link holds or has held ADMIN (`Principal.held_admin`) and the link was not the
            operator's. The caller withdraws the link and raises the carried error.
        """
        kind = row.kind
        if kind in ISSUED_BY_ADMINS:
            self._check_issuer(row)
        if kind == "invite":
            principal_id = self._create_invitee(row)
        else:
            assert row.principal_id is not None  # nosec B101 -- only invites have none
            principal_id = row.principal_id
        principal = self._session.get(Principal, principal_id)
        assert principal is not None  # nosec B101 -- a link's principal cascades with it
        if kind == "local" and not self._local_mode:
            raise _CloseLink(
                PermissionDenied(
                    "This local sign-in link was made while the server ran in local mode, and "
                    "it no longer does, so the link was closed. Start it again with `lakana "
                    "server start --local`, or sign in with your passkey."
                ),
                "not_local",
            )
        if kind == "setup" and has_passkey(self._session, principal_id, usable=False):
            raise _CloseLink(ConflictError(already_set_up(self._bootstrap_name)), "already_set_up")
        if (
            kind == "recovery"
            and self._is_or_was_admin(principal)
            and not self._issued_by_operator(row)
        ):
            raise _CloseLink(
                PermissionDenied(f"{not_operator_recovery(principal.name)} This link was closed."),
                "became_admin",
            )
        if kind == "recovery":
            self._recover(principal, row)
        started = self._sessions.start(
            principal_id,
            via=kind,
            link_id=row.id,
            source_pat_id=row.source_pat_id if kind == "local" else None,
            replacing=replacing,
            peer=peer,
            user_agent=user_agent,
            touched=False,
        )
        context = started.principal.session
        assert context is not None  # nosec B101 -- a started session always has its context
        self._session.execute(
            update(SignInLink)
            .where(SignInLink.id == row.id)
            .values(principal_id=principal_id, session_id=context.session_id)
            .execution_options(synchronize_session=False)
        )
        AuditService(self._session).record(
            principal_id,
            AuditEventType.SIGN_IN_LINK_USED,
            detail={
                "kind": kind,
                "link_id": str(row.id),
                "principal": principal.name,
                "peer": peer[:64],
                "user_agent": user_agent[:255],
            },
        )
        usable = has_passkey(
            self._session, principal_id, rp_id=urlsplit(self._origin).hostname or ""
        )
        local = kind == "local"
        return Redeemed(
            started=started,
            needs_passkey=not usable and not local,
            offer_passkey=not usable and local,
            kind=kind,
        )

    def _create_invitee(self, row: SignInLink) -> uuid.UUID:
        """Create the account an invite promised: a person with no rights.

        Parameters
        ----------
        row
            The claimed invite.

        Returns
        -------
        uuid.UUID
            The new principal's id.

        Raises
        ------
        _CloseLink
            If a principal took the name meanwhile (`NAME_TAKEN_MEANWHILE`).
        """
        name = row.invite_name or ""
        if _clashing_principal(self._session, name) is not None:
            raise _CloseLink(ConflictError(NAME_TAKEN_MEANWHILE), "name_taken")
        principal = Principal(
            id=uuid.uuid4(),
            type=PrincipalType.USER,
            name=name,
            display_name=row.invite_display_name,
            is_active=True,
        )
        self._session.add(principal)
        try:
            with self._session.begin_nested():
                self._session.flush()
        except IntegrityError:
            raise _CloseLink(ConflictError(NAME_TAKEN_MEANWHILE), "name_taken") from None
        AuditService(self._session).record(
            principal.id,
            AuditEventType.PRINCIPAL_CREATED,
            detail={
                "principal": name,
                "type": PrincipalType.USER.value,
                "via": "invite",
                "link_id": str(row.id),
            },
        )
        return principal.id

    def _recover(self, principal: Principal, row: SignInLink) -> None:
        """Remove a person's passkeys, end their sessions and tell them and every administrator.

        Parameters
        ----------
        principal
            The person recovered.
        row
            The recovery link.
        """
        self._session.execute(delete(Passkey).where(Passkey.principal_id == principal.id))
        self._sessions.end_for_principal(principal.id, "recovery", commit=False)
        notices = NoticeService(self._session, clock=self._clock)
        detail = {"name": principal.name, "link_id": str(row.id)}
        for recipient in sorted({principal.id, *active_instance_admin_ids(self._session)}, key=str):
            notices.add(recipient, "recovery_used", principal.id, detail)

    def withdraw(
        self, p: PrincipalRef, link_id: uuid.UUID, *, kind: LinkKind | None = None
    ) -> None:
        """Withdraw an open link.

        Shares the ``used_at IS NULL`` guard with `redeem`, so a link is either used or
        withdrawn, never both.

        Parameters
        ----------
        p
            The caller: an administrator.
        link_id
            The link's row id.
        kind
            The kind the caller expects (the route it came through); a link of another kind
            is not found.

        Raises
        ------
        PermissionDenied
            For non-administrators.
        NotFoundError
            If no link (of `kind`) has that id.
        ConflictError
            If the link was used or withdrawn already.
        """
        self._require_admin(p)
        row = self._session.get(SignInLink, link_id)
        if row is None or (kind is not None and row.kind != kind):
            raise NotFoundError(
                f"No {kind or 'sign-in'} link has this id. List the open ones to find it."
            )
        if not self._withdraw_if_open(link_id):
            self._session.rollback()
            self._session.refresh(row)
            raise ConflictError(
                f"This link was {_state(row, self._clock())} already, so there is nothing to "
                "withdraw."
            )
        AuditService(self._session).record(
            p.id,
            AuditEventType.SIGN_IN_LINK_WITHDRAWN,
            detail={"kind": row.kind, "link_id": str(link_id), "actor": p.name},
        )
        self._session.commit()

    def list_open(self, p: PrincipalRef, kind: Literal["invite", "recovery"]) -> list[SignInLink]:
        """List the open links of one kind, oldest first.

        Parameters
        ----------
        p
            The caller: an administrator.
        kind
            ``invite`` or ``recovery``.

        Returns
        -------
        list of SignInLink
            Links neither used, withdrawn nor expired.

        Raises
        ------
        PermissionDenied
            For non-administrators.
        """
        self._require_admin(p)
        return open_links(self._session, kind, self._clock())

    def name_of(self, row: SignInLink) -> str:
        """Name the principal a link signs in as.

        Parameters
        ----------
        row
            The link.

        Returns
        -------
        str
            An invite's name, or the bound principal's name.
        """
        if row.kind == "invite" or row.principal_id is None:
            return row.invite_name or ""
        principal = self._session.get(Principal, row.principal_id)
        return principal.name if principal is not None else ""

    def withdraw_for_principal(
        self, principal_id: uuid.UUID, *, reason: str = "deactivated", commit: bool = False
    ) -> int:
        """Withdraw every open link bound to a person.

        Parameters
        ----------
        principal_id
            The person.
        reason
            Why, for the audit log.
        commit
            Whether to commit; by default the caller does, with its own change.

        Returns
        -------
        int
            How many links this call withdrew.
        """
        count = withdraw_open_links(self._session, principal_id, now=self._clock(), reason=reason)
        if commit:
            self._session.commit()
        return count
