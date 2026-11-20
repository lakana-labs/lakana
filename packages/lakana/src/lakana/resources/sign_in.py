"""The sign-in resources: links, invites, recovery links, sessions, passkeys and settings.

`Client.sign_in` issues the setup link (the server operator's bootstrap token only) and a local
sign-in link (your own token, on a server started with `lakana server start --local`), and
carries what `lakana login` asks before it has a token: whether the server signs people in
through an identity provider, and the exchanges of a sign-in confirmed in a browser. Those send
no credential, so a client built with an empty token can call them.
`Client.invites` and `Client.recovery_links` issue, list and withdraw the links an administrator
sends to people. `Client.sessions` and `Client.passkeys` list and end a person's browser
sessions and passkeys. `Client.sign_in_settings` reads and changes how long sessions last, in
minutes. Every route answers 409 while the web interface is off (`LAKANA_UI__ENABLED`).

The WebAuthn ceremonies (signing in with a passkey, adding one, a step-up touch) and using a
link happen in a browser only, so the SDK has no methods for them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import (
    BrowserSession,
    DeviceSignIn,
    DeviceSignInPoll,
    Invite,
    IssuedLink,
    IssuedToken,
    OidcInfo,
    OpenInvite,
    OpenRecoveryLink,
    Passkey,
    RecoveryLink,
    SignInBounds,
    SignInProvider,
    SignInSettings,
)
from lakana._routes import ROUTES
from lakana.errors import LakanaError


def _when(value: str) -> datetime:
    """Parse a timestamp of the platform API.

    Parameters
    ----------
    value
        An ISO 8601 timestamp.

    Returns
    -------
    datetime
        The parsed value.
    """
    return datetime.fromisoformat(value)


def _seconds(value: str | None) -> float | None:
    """Read a `Retry-After` that names seconds.

    Parameters
    ----------
    value
        The header as sent, or `None`.

    Returns
    -------
    float or None
        The seconds; `None` without the header or for an HTTP date.
    """
    try:
        return None if value is None else float(value)
    except ValueError:
        return None


def _issued_link(data: dict[str, Any]) -> IssuedLink:
    """Build an `IssuedLink` from an `IssuedLinkOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    IssuedLink
        The link.
    """
    return IssuedLink(url=data["url"], expires_at=_when(data["expires_at"]))


class SignIn:
    """Sign-in links a program asks for: `Client.sign_in`.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def setup_link(self) -> IssuedLink:
        """Issue the setup link that signs the first administrator in to the web interface.

        Only the server operator's bootstrap token may ask, and only while the first
        administrator has no passkey. The link works once, for 10 minutes; earlier open setup
        links are withdrawn.

        Returns
        -------
        IssuedLink
            The link, shown only here.

        Raises
        ------
        lakana.errors.PermissionDenied
            If the token is not the operator's bootstrap token.
        lakana.errors.Conflict
            If the first administrator has a passkey already (use a recovery link), or the
            interface is off.
        """
        return _issued_link(self._transport.request(ROUTES["sign_in.setup_link"]).json())

    def local_link(self) -> IssuedLink:
        """Issue a sign-in link for yourself, with your own personal access token.

        Only on a server with local sign-in on (`lakana server start --local` sets it). The
        link works once, for 60 seconds, and signs in without a passkey touch.

        Returns
        -------
        IssuedLink
            The link, shown only here.

        Raises
        ------
        lakana.errors.NotFound
            If local sign-in is off on the server.
        lakana.errors.PermissionDenied
            If the credential is not a person's own personal access token.
        """
        return _issued_link(self._transport.request(ROUTES["sign_in.local_link"]).json())

    def oidc_info(self) -> OidcInfo:
        """Say whether the server signs people in through an identity provider.

        Sends no credential.

        Returns
        -------
        OidcInfo
            Whether it is on, and the provider's name.

        Raises
        ------
        lakana.errors.NotFound
            From a server older than sign-in through a provider.
        lakana.errors.Conflict
            If the server's web interface is off.
        """
        data = self._transport.request(ROUTES["sign_in.oidc_info"], authenticated=False).json()
        return OidcInfo(enabled=data["enabled"], display_name=data["display_name"])

    def exchange_code(self, code: str, code_verifier: str) -> IssuedToken:
        """Exchange the one-time code of a sign-in confirmed in a browser for a new token.

        Sends no credential: the code and the PKCE verifier it was confirmed for are the proof.
        The code works once, for 2 minutes; a wrong verifier uses it up too.

        Parameters
        ----------
        code
            The code the browser brought back to the command line.
        code_verifier
            The PKCE verifier whose S256 challenge the browser confirmed.

        Returns
        -------
        IssuedToken
            The confirming person's new token, named ``lakana login on <host>``, for 90 days.

        Raises
        ------
        lakana.errors.LakanaError
            With status 400 for an unknown, used, expired or wrong code or verifier; the
            message says to sign in again.
        """
        data = self._transport.request(
            ROUTES["sign_in.exchange_code"],
            json={"code": code, "code_verifier": code_verifier},
            authenticated=False,
        ).json()
        return IssuedToken(key_id=data["key_id"], name=data["name"], token=data["token"])

    def start_device(self, host: str) -> DeviceSignIn:
        """Start a sign-in of the command line to confirm in a browser elsewhere.

        Sends no credential. Show `DeviceSignIn.user_code` and `DeviceSignIn.verification_url`
        to the person, then call `poll_device` every `DeviceSignIn.interval` seconds.

        Parameters
        ----------
        host
            This computer's name, for the confirmation and the token's name.

        Returns
        -------
        DeviceSignIn
            The codes, the page and the interval.
        """
        data = self._transport.request(
            ROUTES["sign_in.start_device"], json={"host": host}, authenticated=False
        ).json()
        return DeviceSignIn(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_url=data["verification_url"],
            expires_in=data["expires_in"],
            interval=data["interval"],
        )

    def poll_device(self, device_code: str) -> DeviceSignInPoll:
        """Ask whether a device sign-in was confirmed; an approved one answers its token once.

        Sends no credential.

        Parameters
        ----------
        device_code
            `DeviceSignIn.device_code`.

        Returns
        -------
        DeviceSignInPoll
            The state, with the token once approved; ``slow_down`` for any 429: asked too soon,
            or past the server's rate.

        Raises
        ------
        lakana.errors.LakanaError
            With status 400 for a device code the server does not know.
        """
        try:
            data = self._transport.request(
                ROUTES["sign_in.poll_device"],
                json={"device_code": device_code},
                authenticated=False,
            ).json()
        except LakanaError as exc:
            if exc.status == 429:
                return DeviceSignInPoll(state="slow_down", retry_after=_seconds(exc.retry_after))
            raise
        token = data["token"]
        return DeviceSignInPoll(
            state=data["state"],
            token=None
            if token is None
            else IssuedToken(key_id=token["key_id"], name=token["name"], token=token["token"]),
        )


_OpenInvites = list[OpenInvite]
"""`list[OpenInvite]`, resolved before `Invites.list` shadows the builtin."""


class Invites:
    """Invites of new people: `Client.invites`. Needs the ADMIN role.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, name: str, display_name: str | None = None) -> Invite:
        """Invite a new person by a link that creates their account when used.

        Parameters
        ----------
        name
            The new person's principal name; refused when any principal or open invite has it
            in any letter case.
        display_name
            The person's name as the interface shows it; optional.

        Returns
        -------
        Invite
            The invite and its link, shown only here: it works once, for 7 days, and creates
            an account with no rights. Confirm who signed up before granting roles.

        Raises
        ------
        lakana.errors.Conflict
            If the name is taken in any letter case.
        """
        body: dict[str, str] = {"name": name}
        if display_name is not None:
            body["display_name"] = display_name
        data = self._transport.request(ROUTES["invites.create"], json=body).json()
        return Invite(
            id=data["id"],
            name=data["name"],
            display_name=data["display_name"],
            url=data["url"],
            expires_at=_when(data["expires_at"]),
        )

    def list(self) -> _OpenInvites:
        """List the open invites, oldest first, without their links.

        Returns
        -------
        list of OpenInvite
            Invites neither used, withdrawn nor expired.
        """
        return [
            OpenInvite(
                id=row["id"],
                name=row["name"],
                display_name=row["display_name"],
                created_at=_when(row["created_at"]),
                expires_at=_when(row["expires_at"]),
            )
            for row in self._transport.request(ROUTES["invites.list"]).json()
        ]

    def withdraw(self, invite_id: str) -> None:
        """Withdraw an open invite: its link stops working and its name is free again.

        Parameters
        ----------
        invite_id
            The invite's id, from `create` or `list`.

        Raises
        ------
        lakana.errors.NotFound
            If no invite has this id.
        lakana.errors.Conflict
            If it was used or withdrawn already.
        """
        self._transport.request(ROUTES["invites.withdraw"], path_params={"invite_id": invite_id})


_OpenRecoveryLinks = list[OpenRecoveryLink]
"""`list[OpenRecoveryLink]`, resolved before `RecoveryLinks.list` shadows the builtin."""


class RecoveryLinks:
    """Recovery links for people who lost their passkeys: `Client.recovery_links`.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, principal: str) -> RecoveryLink:
        """Issue a recovery link for a person who lost their passkeys.

        Using it removes their passkeys, ends their sessions and tells them and every
        administrator. Needs the ADMIN role. Recovering anyone who holds or has ever held the
        ADMIN role, directly or through a group, needs the server operator's bootstrap token
        (the profile `lakana bundle init` or `lakana server start --local` saved). A new link
        withdraws the person's previous open one.

        Parameters
        ----------
        principal
            The person's principal name.

        Returns
        -------
        RecoveryLink
            The link, shown only here: it works once, for 1 hour.

        Raises
        ------
        lakana.errors.PermissionDenied
            For an administrator, unless the token is the operator's bootstrap token.
        lakana.errors.Conflict
            For yourself, a service account or a deactivated account.
        """
        data = self._transport.request(
            ROUTES["recovery_links.create"], path_params={"name": principal}
        ).json()
        return RecoveryLink(
            id=data["id"],
            name=data["name"],
            url=data["url"],
            expires_at=_when(data["expires_at"]),
        )

    def list(self) -> _OpenRecoveryLinks:
        """List the open recovery links, oldest first, without the links themselves.

        Returns
        -------
        list of OpenRecoveryLink
            Links neither used, withdrawn nor expired.
        """
        return [
            OpenRecoveryLink(
                id=row["id"],
                name=row["name"],
                created_at=_when(row["created_at"]),
                expires_at=_when(row["expires_at"]),
            )
            for row in self._transport.request(ROUTES["recovery_links.list"]).json()
        ]

    def withdraw(self, link_id: str) -> None:
        """Withdraw an open recovery link; it stops working at once.

        Parameters
        ----------
        link_id
            The link's id, from `create` or `list`.

        Raises
        ------
        lakana.errors.NotFound
            If no recovery link has this id.
        lakana.errors.Conflict
            If it was used or withdrawn already.
        """
        self._transport.request(ROUTES["recovery_links.withdraw"], path_params={"link_id": link_id})


_BrowserSessions = list[BrowserSession]
"""`list[BrowserSession]`, resolved before `Sessions.list` shadows the builtin."""


class Sessions:
    """A person's browser sessions: `Client.sessions`.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(self, principal: str) -> _BrowserSessions:
        """List a person's open browser sessions, oldest first.

        Your own, or anyone's with the ADMIN role.

        Parameters
        ----------
        principal
            The person's principal name.

        Returns
        -------
        list of BrowserSession
            The open sessions.

        Raises
        ------
        lakana.errors.PermissionDenied
            For another person's sessions without the ADMIN role.
        """
        return [
            BrowserSession(
                id=row["id"],
                via=row["via"],
                created_at=_when(row["created_at"]),
                last_seen_at=_when(row["last_seen_at"]),
                absolute_expires_at=_when(row["absolute_expires_at"]),
                user_agent=row["user_agent"],
                browser=row.get("browser", row["user_agent"]),
                peer=row["peer"],
                current=row["current"],
            )
            for row in self._transport.request(
                ROUTES["sessions.list"], path_params={"name": principal}
            ).json()
        ]

    def end_all(self, principal: str) -> None:
        """End every browser session of a person; each is recorded in the audit log.

        Your own (sign out everywhere), or anyone's with the ADMIN role.

        Parameters
        ----------
        principal
            The person's principal name.

        Raises
        ------
        lakana.errors.PermissionDenied
            For another person's sessions without the ADMIN role.
        """
        self._transport.request(ROUTES["sessions.end_all"], path_params={"name": principal})


_Passkeys = list[Passkey]
"""`list[Passkey]`, resolved before `Passkeys.list` shadows the builtin."""


class Passkeys:
    """A person's passkeys: `Client.passkeys`. Adding one happens in the web interface.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(self, principal: str) -> _Passkeys:
        """List a person's passkeys, oldest first.

        Your own, or anyone's with the ADMIN role.

        Parameters
        ----------
        principal
            The person's principal name.

        Returns
        -------
        list of Passkey
            The passkeys, disabled ones included.

        Raises
        ------
        lakana.errors.PermissionDenied
            For another person's passkeys without the ADMIN role.
        """
        return [
            Passkey(
                id=row["id"],
                name=row["name"],
                created_at=_when(row["created_at"]),
                last_used_at=None if row["last_used_at"] is None else _when(row["last_used_at"]),
                backup_eligible=row["backup_eligible"],
                disabled_reason=row["disabled_reason"],
                usable_here=row["usable_here"],
            )
            for row in self._transport.request(
                ROUTES["passkeys.list"], path_params={"name": principal}
            ).json()
        ]

    def remove(self, principal: str, passkey_id: str) -> None:
        """Remove a passkey.

        Your own, or anyone's with the ADMIN role; removing another person's also ends their
        browser sessions. Your own last working passkey cannot be removed.

        Parameters
        ----------
        principal
            The person's principal name.
        passkey_id
            The passkey's id, from `list`.

        Raises
        ------
        lakana.errors.Conflict
            If it is your own last working passkey (add another first).
        """
        self._transport.request(
            ROUTES["passkeys.remove"], path_params={"name": principal, "passkey_id": passkey_id}
        )


def _settings(data: dict[str, Any]) -> SignInSettings:
    """Build `SignInSettings` from a `SignInSettingsOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    SignInSettings
        The settings.
    """
    bounds = data["bounds"]
    provider = data.get("provider")
    return SignInSettings(
        idle_timeout_minutes=data["idle_timeout_minutes"],
        absolute_lifetime_minutes=data["absolute_lifetime_minutes"],
        bounds=SignInBounds(
            idle_timeout_minutes_min=bounds["idle_timeout_minutes_min"],
            idle_timeout_minutes_max=bounds["idle_timeout_minutes_max"],
            absolute_lifetime_minutes_min=bounds["absolute_lifetime_minutes_min"],
            absolute_lifetime_minutes_max=bounds["absolute_lifetime_minutes_max"],
            token_provider_window_days_min=bounds.get("token_provider_window_days_min", 1),
            token_provider_window_days_max=bounds.get("token_provider_window_days_max", 365),
        ),
        token_provider_window_days=data.get("token_provider_window_days", 30),
        provider=None
        if provider is None
        else SignInProvider(
            display_name=provider["display_name"],
            issuer=provider["issuer"],
            linked_people=provider["linked_people"],
            unlinked_admins=provider["unlinked_admins"],
        ),
    )


class SignInSettingsResource:
    """How long browser sessions last: `Client.sign_in_settings`.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def get(self) -> SignInSettings:
        """Read the session lengths, in minutes, with the bounds they may be set between.

        Returns
        -------
        SignInSettings
            The lengths in force for new sessions.
        """
        return _settings(self._transport.request(ROUTES["sign_in_settings.get"]).json())

    def update(
        self,
        idle_timeout_minutes: int | None = None,
        absolute_lifetime_minutes: int | None = None,
        token_provider_window_days: int | None = None,
    ) -> SignInSettings:
        """Change how long new browser sessions last, or the token window of linked people.

        Open sessions keep their lengths; a new token window applies to the next request with a
        token. Needs the ADMIN role. Recorded in the audit log.

        Parameters
        ----------
        idle_timeout_minutes
            The idle timeout, 15 to 1440 minutes (24 hours); `None` keeps it.
        absolute_lifetime_minutes
            The absolute lifetime, 60 to 43200 minutes (30 days), not shorter than the idle
            timeout; `None` keeps it.
        token_provider_window_days
            How long a person linked to the identity provider may use their personal tokens
            after their last sign-in there, 1 to 365 days; `None` keeps it.

        Returns
        -------
        SignInSettings
            The lengths now in force for new sessions.

        Raises
        ------
        lakana.errors.ValidationError
            If a value is outside its bounds; the message names the bound.
        """
        body: dict[str, int] = {}
        if idle_timeout_minutes is not None:
            body["idle_timeout_minutes"] = idle_timeout_minutes
        if absolute_lifetime_minutes is not None:
            body["absolute_lifetime_minutes"] = absolute_lifetime_minutes
        if token_provider_window_days is not None:
            body["token_provider_window_days"] = token_provider_window_days
        return _settings(
            self._transport.request(ROUTES["sign_in_settings.update"], json=body).json()
        )
