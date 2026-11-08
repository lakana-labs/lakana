"""A person's passkeys in the platform API: list, add (in the browser) and remove.

`GET /api/v1/principals/{name}/passkeys` lists one's own passkeys, or anyone's for an
administrator. Adding one is a WebAuthn ceremony in the web interface: `POST .../options`
starts it and `POST .../passkeys` finishes it, both only for one's own account and from a
browser session (a token gets 409). Removing one (`DELETE`) needs a fresh passkey touch in a
browser session; nobody removes their own last passkey, and an administrator removing another
person's passkey ends that person's sessions. Every route answers 409 while the interface is
off. No answer carries a passkey's public key or credential id.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Request, Response

from lakana_core.auth import PrincipalRef
from lakana_core.errors import PermissionDenied
from lakana_core.identity.passkeys import PasskeyService
from lakana_core.models import Passkey
from lakana_server.api.platform.schemas import PasskeyOptionsOut, PasskeyOut, PasskeyRegisterIn
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import require_interface
from lakana_server.deps import get_passkey_service, get_principal

OWN_ONLY = (
    "A passkey can be added only to your own account, from your own browser. Ask {name} to "
    "add it, or give them a recovery link."
)
"""The 403 detail of adding a passkey to another person's account."""

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/principals",
    tags=["principals"],
    dependencies=[Depends(require_interface)],
    responses=errors(401, 409),
)


def _out(row: Passkey, rp_id: str) -> PasskeyOut:
    """Describe a passkey without its key.

    Parameters
    ----------
    row
        The passkey.
    rp_id
        The current relying-party id.

    Returns
    -------
    PasskeyOut
        The description.
    """
    return PasskeyOut(
        id=row.id,
        name=row.name,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        backup_eligible=row.backup_eligible,
        disabled_reason=row.disabled_reason,
        usable_here=row.rp_id == rp_id,
    )


def _own(principal: PrincipalRef, name: str) -> None:
    """Refuse adding a passkey to another person's account.

    Parameters
    ----------
    principal
        The caller.
    name
        The account named in the path.

    Raises
    ------
    PermissionDenied
        If `name` is not the caller's own.
    """
    if name != principal.name:
        raise PermissionDenied(OWN_ONLY.format(name=name))


@router.get("/{name}/passkeys", response_model=list[PasskeyOut], responses=errors(403, 404))
def list_passkeys(
    name: str,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    passkeys: PasskeyService = Depends(get_passkey_service),
) -> list[PasskeyOut]:
    """List a person's passkeys.

    Anyone may list their own; another person's needs the ADMIN role on the instance (403
    otherwise). Disabled passkeys are listed with `disabled_reason`; `usable_here` is false for
    a passkey registered for an earlier public address (`LAKANA_UI__PUBLIC_URL`).

    Parameters
    ----------
    name
        The person's principal name.
    request
        The request, for the settings.
    principal
        The authenticated caller.
    passkeys
        The passkey service for this request.

    Returns
    -------
    list of PasskeyOut
        The passkeys, oldest first.
    """
    rp_id = request.app.state.settings.public_origin().host
    return [_out(row, rp_id) for row in passkeys.list_passkeys(principal, name)]


@router.post("/{name}/passkeys/options", response_model=PasskeyOptionsOut, responses=errors(403))
def passkey_registration_options(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    passkeys: PasskeyService = Depends(get_passkey_service),
) -> PasskeyOptionsOut:
    """Start adding a passkey to your own account.

    From the web interface only (a token gets 409). The first passkey may be added within 15
    minutes of signing in with a setup, invite or recovery link; every later one needs a passkey
    touch within the last 15 minutes (403 `StepUpRequired`). A session from a local sign-in link
    may add only the first passkey. The options ask for a discoverable passkey with user
    verification (a PIN, fingerprint or face) and no attestation.

    Parameters
    ----------
    name
        Your own principal name.
    principal
        The authenticated caller.
    passkeys
        The passkey service for this request.

    Returns
    -------
    PasskeyOptionsOut
        The ceremony id and the options for `navigator.credentials.create`.
    """
    _own(principal, name)
    ceremony_id, options = passkeys.registration_options(principal)
    return PasskeyOptionsOut(ceremony_id=ceremony_id, public_key=options)


@router.post(
    "/{name}/passkeys",
    response_model=PasskeyOut,
    status_code=201,
    responses=errors(403),
)
def register_passkey(
    name: str,
    body: PasskeyRegisterIn,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    passkeys: PasskeyService = Depends(get_passkey_service),
) -> PasskeyOut:
    """Finish adding a passkey to your own account.

    Send the browser's `credential.toJSON()` for the ceremony the options route started, and
    optionally a name for the passkey. An answer from another address or without user
    verification, or a used or expired challenge, answers 403 `PasskeyRefused`; a passkey that is
    registered already answers 409. Adding a passkey counts as a passkey touch.

    Parameters
    ----------
    name
        Your own principal name.
    body
        ``{"ceremony_id", "credential", "name"}``.
    request
        The request, for the `User-Agent` and the settings.
    principal
        The authenticated caller.
    passkeys
        The passkey service for this request.

    Returns
    -------
    PasskeyOut
        The new passkey.
    """
    _own(principal, name)
    row = passkeys.register(
        principal,
        body.ceremony_id,
        json.dumps(body.answer),
        name=body.name,
        user_agent=request.headers.get("user-agent", ""),
    )
    return _out(row, request.app.state.settings.public_origin().host)


@router.delete(
    "/{name}/passkeys/{passkey_id}",
    status_code=204,
    response_class=Response,
    responses=errors(403, 404),
)
def remove_passkey(
    name: str,
    passkey_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    passkeys: PasskeyService = Depends(get_passkey_service),
) -> Response:
    """Remove a passkey.

    Your own, or, with the ADMIN role on the instance, anyone's. From the web interface it
    needs a passkey touch within the last 15 minutes (403 `StepUpRequired`). Your own last
    working passkey cannot be removed (409): add another one first, or ask an administrator for
    a recovery link. An administrator removing another person's passkey also ends all of that
    person's browser sessions.

    Parameters
    ----------
    name
        The person's principal name.
    passkey_id
        The passkey's id, from the list.
    principal
        The authenticated caller.
    passkeys
        The passkey service for this request.

    Returns
    -------
    Response
        204.
    """
    passkeys.remove(principal, name, passkey_id)
    return Response(status_code=204)
