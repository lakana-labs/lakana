"""`lakana login` through a browser: the loopback and device grants (#32, PR 3).

The browser's routes, under `/api/v1/sign-in/cli`, need a browser session with its CSRF token,
which the interface's confirmation pages send; a token is refused (403):

- `POST /approvals` confirms a loopback sign-in (step-up) and answers its one-time code;
- `POST /device/describe` names the computer a user code would sign in;
- `POST /device/approve` (step-up) and `POST /device/deny` decide a device grant.

Past `WRONG_CODES` user codes that open no grant in `WRONG_CODE_WINDOW`, a person's device
routes answer 429 until the window passes (counted per person, so more sessions buy no more
guesses).

The command line's routes, under `/api/v1/auth/cli` beside the CI token exchange, take no
credential: the grant is the proof. They live outside `/api/v1/sign-in` because a program sends
no `Origin` (`lakana_server.browser_guard`), and they read at most 16 KiB of body:

- `POST /token` exchanges a loopback code and its PKCE verifier for the token;
- `POST /device` starts a device grant;
- `POST /device/token` polls it (429 ``slow_down`` faster than every 5 seconds).

The exchange and the start together answer at most
`LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE` calls per client address and
`LAKANA_UI__PASSKEY_OPTIONS_PER_NETWORK_PER_MINUTE` per network and minute; the polls have
buckets of their own, at `POLL_RATE` times those rates. Every
route answers 409 while the interface is off. No route logs a code, a verifier or a token; the
request bodies never reach the access log.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, Request, Response

from lakana_core.auth import PrincipalRef
from lakana_core.errors import NotFoundError
from lakana_core.identity.cli_grants import DEVICE_LIFETIME, POLL_INTERVAL, CliGrantService
from lakana_core.identity.passkeys import OPEN_BODY_LIMIT
from lakana_core.identity.pats import IssuedPat
from lakana_server.api.platform.schemas import (
    CliApprovalIn,
    CliApprovalOut,
    CliDeviceHostOut,
    CliDeviceIn,
    CliDeviceOut,
    CliDevicePollIn,
    CliDevicePollOut,
    CliTokenIn,
    CliUserCodeIn,
    TokenIssueOut,
)
from lakana_server.api.platform.sign_in import OpenSignInRoute, client_bucket, network_bucket
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import require_interface
from lakana_server.deps import get_cli_grant_service, get_principal
from lakana_server.errors import TooManyAttemptsError

WRONG_CODES = 5
"""How many user codes that open nothing a person may type in `WRONG_CODE_WINDOW`."""

WRONG_CODE_WINDOW = 600.0
"""The window of `WRONG_CODES`, in seconds: 10 minutes."""

TOO_MANY_WRONG_CODES = (
    "Too many codes that open no sign-in were typed for your account; wait 10 minutes, then "
    "type the code lakana login --device shows."
)
"""The 429 detail of the device routes past `WRONG_CODES`."""

POLL_RATE = int(DEVICE_LIFETIME / POLL_INTERVAL)
"""How many times the start rate the device polls may use: 120. A grant waits up to 10 minutes and
is polled every 5 seconds, so the sign-ins started at the full start rate over 10 minutes may all
poll at once: at the default rates, 600 waiting sign-ins per address and 2,400 per network."""

TOO_MANY_REQUESTS = "Too many command-line sign-in requests from this address; wait a minute."
"""The 429 detail of the command line's routes past their rate."""

_MAX_KEYS = 10_000

_T = TypeVar("_T")


class WrongCodeLimit:
    """Count the user codes a person typed that opened no grant.

    Parameters
    ----------
    limit
        How many wrong codes a key may have within `window`.
    window
        The window, in seconds.
    clock
        Monotonic time source; injectable for tests.
    """

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._misses: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, key: str, now: float) -> deque[float]:
        """Read a key's wrong codes within the window, dropping older ones.

        Parameters
        ----------
        key
            The person.
        now
            The time.

        Returns
        -------
        deque of float
            When each recent wrong code was typed.
        """
        misses = self._misses.setdefault(key, deque())
        self._misses.move_to_end(key)
        while misses and now - misses[0] >= self._window:
            misses.popleft()
        if len(self._misses) > _MAX_KEYS:
            self._misses.popitem(last=False)
        return misses

    def check(self, key: str) -> None:
        """Refuse a key that typed `limit` wrong codes within the window.

        Parameters
        ----------
        key
            The person.

        Raises
        ------
        TooManyAttemptsError
            429, with `TOO_MANY_WRONG_CODES`.
        """
        with self._lock:
            if len(self._recent(key, self._clock())) >= self._limit:
                raise TooManyAttemptsError(TOO_MANY_WRONG_CODES)

    def miss(self, key: str) -> None:
        """Count a wrong code.

        Parameters
        ----------
        key
            The person.
        """
        with self._lock:
            now = self._clock()
            self._recent(key, now).append(now)


class CliOpenRoute(OpenSignInRoute):
    """`OpenSignInRoute` with the command-line sign-in's 413 detail."""

    too_large = (
        f"A command-line sign-in request is at most {OPEN_BODY_LIMIT} bytes; send only the "
        "fields of the route."
    )
    """The 413 detail."""


router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/sign-in/cli",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(401, 403, 409),
)

open_router = APIRouter(
    route_class=CliOpenRoute,
    prefix="/api/v1/auth/cli",
    tags=["auth"],
    dependencies=[Depends(require_interface)],
    responses=errors(400, 409, 413, 429),
)


def _admit(request: Request, *, poll: bool = False) -> None:
    """Refuse a command line's request past the per-address or per-network rate.

    Parameters
    ----------
    request
        The request, for the client's address and the limiters.
    poll
        Whether it is a device poll, counted in buckets of its own (`POLL_RATE` times the
        rate), so that sign-ins waiting behind one address do not starve each other.

    Raises
    ------
    TooManyAttemptsError
        429 past either rate.
    """
    state = request.app.state
    client = request.client.host if request.client else "unknown"
    limiter = state.cli_poll_limiter if poll else state.cli_sign_in_limiter
    admitted, _ = limiter.admit(client_bucket(client))
    if admitted:
        network = state.cli_poll_network_limiter if poll else state.cli_sign_in_network_limiter
        admitted, _ = network.admit(network_bucket(client))
    if not admitted:
        raise TooManyAttemptsError(TOO_MANY_REQUESTS)


def _issued(token: IssuedPat) -> TokenIssueOut:
    """Shape an issued token for the answer.

    Parameters
    ----------
    token
        The token.

    Returns
    -------
    TokenIssueOut
        Its key id, name and value.
    """
    return TokenIssueOut(key_id=token.key_id, name=token.name, token=token.token)


@router.post("/approvals", response_model=CliApprovalOut)
def approve_cli_sign_in(
    body: CliApprovalIn,
    principal: PrincipalRef = Depends(get_principal),
    grants: CliGrantService = Depends(get_cli_grant_service),
) -> CliApprovalOut:
    """Confirm signing `lakana login` in on a computer, and answer its one-time code.

    The interface's page `/sign-in/cli` calls this after the person pressed Create token. Needs
    a browser session with a passkey or provider touch from the last 15 minutes (403
    `StepUpRequired`); a token is refused (403): it needs no command-line sign-in. The code
    works once, for 2 minutes, and only with the PKCE verifier of `code_challenge`: send the
    browser to `http://127.0.0.1:<port>/?code=<code>&state=<state>`, with the answer's port.

    Parameters
    ----------
    body
        ``{"port", "code_challenge", "host"}``.
    principal
        The caller.
    grants
        The grant service.

    Returns
    -------
    CliApprovalOut
        The code and the port.
    """
    code = grants.approve_loopback(
        principal, port=body.port, code_challenge=body.code_challenge, host=body.host
    )
    return CliApprovalOut(code=code, port=body.port)


def _counted(request: Request, principal: PrincipalRef, call: Callable[[], _T]) -> _T:
    """Run a device route's service call, counting a user code that opens no grant.

    Parameters
    ----------
    request
        The request, for the wrong-code limit.
    principal
        The caller.
    call
        The service call.

    Returns
    -------
    _T
        What the call answered.

    Raises
    ------
    NotFoundError
        404, from the call, for a code that opens no grant; it is counted.
    """
    limit: WrongCodeLimit = request.app.state.cli_user_code_limit
    key = str(principal.id)
    limit.check(key)
    try:
        return call()
    except NotFoundError:
        limit.miss(key)
        raise


@router.post("/device/describe", response_model=CliDeviceHostOut, responses=errors(404, 429))
def describe_cli_device(
    body: CliUserCodeIn,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    grants: CliGrantService = Depends(get_cli_grant_service),
) -> CliDeviceHostOut:
    """Describe the sign-in a code from `lakana login --device` would approve.

    Answers the name the command line gave its computer, the address it asked from and when,
    so the person can tell their own request from one someone sent them.

    Needs a browser session; a token is refused (403). 404 when no sign-in waits for the code
    (it expired, or was used, approved or denied); 429 past 5 such codes in 10 minutes for one
    person.

    Parameters
    ----------
    body
        ``{"user_code"}``, in any letter case, with or without the dash.
    request
        The request, for the wrong-code limit.
    principal
        The caller.
    grants
        The grant service.

    Returns
    -------
    CliDeviceHostOut
        The computer's name, the requesting address and the time.
    """
    described = _counted(
        request, principal, lambda: grants.describe_device(principal, body.user_code)
    )
    return CliDeviceHostOut(
        host=described.host,
        requested_from=described.requested_from,
        requested_at=described.requested_at,
        age_seconds=described.age_seconds,
    )


@router.post("/device/approve", status_code=204, responses=errors(404, 429))
def approve_cli_device(
    body: CliUserCodeIn,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    grants: CliGrantService = Depends(get_cli_grant_service),
) -> Response:
    """Approve a sign-in of `lakana login --device`: its command line receives the token.

    Needs a browser session with a passkey or provider touch from the last 15 minutes (403
    `StepUpRequired`); a token is refused (403). 404 and 429 as `/device/describe`.

    Parameters
    ----------
    body
        ``{"user_code"}``.
    request
        The request, for the wrong-code limit.
    principal
        The caller.
    grants
        The grant service.

    Returns
    -------
    Response
        204.
    """
    _counted(request, principal, lambda: grants.approve_device(principal, body.user_code))
    return Response(status_code=204)


@router.post("/device/deny", status_code=204, responses=errors(404, 429))
def deny_cli_device(
    body: CliUserCodeIn,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    grants: CliGrantService = Depends(get_cli_grant_service),
) -> Response:
    """Refuse a sign-in of `lakana login --device`: its command line is told so and stops.

    Needs a browser session; a token is refused (403). A refusal needs no touch: it gives no
    access. 404 and 429 as `/device/describe`.

    Parameters
    ----------
    body
        The code of the sign-in to refuse.
    request
        The request; a code that opens nothing counts against its person.
    principal
        The person refusing.
    grants
        Refuses the grant.

    Returns
    -------
    Response
        204, once the command line will be told at its next poll.
    """
    _counted(request, principal, lambda: grants.deny_device(principal, body.user_code))
    return Response(status_code=204)


@open_router.post("/token", response_model=TokenIssueOut)
def exchange_cli_code(
    body: CliTokenIn,
    request: Request,
    grants: CliGrantService = Depends(get_cli_grant_service),
) -> TokenIssueOut:
    """Exchange the one-time code of `lakana login` and its PKCE verifier for the new token.

    Needs no credential: the code and the verifier are the proof. The code is used up by the
    first exchange, right or wrong (400, one sentence for an unknown, used, expired or wrong one,
    or when the browser session that confirmed it has ended; each but the unknown one is audited
    as `SIGN_IN_CLI_REFUSED`). The token is the
    confirming person's, named `lakana login on <host>`, and works for 90 days. 429 past the
    rate.

    Parameters
    ----------
    body
        ``{"code", "code_verifier"}``.
    request
        The request, for the rate.
    grants
        The grant service.

    Returns
    -------
    TokenIssueOut
        The token, shown this once.
    """
    _admit(request)
    client = request.client.host if request.client else "unknown"
    return _issued(grants.exchange_loopback(body.code, body.code_verifier, peer=client))


@open_router.post("/device", response_model=CliDeviceOut)
def start_cli_device(
    body: CliDeviceIn,
    request: Request,
    grants: CliGrantService = Depends(get_cli_grant_service),
) -> CliDeviceOut:
    """Start a sign-in of `lakana login --device`, for a computer without a browser.

    Needs no credential. `host` is 1 to 64 characters of `A-Z a-z 0-9 . _ -` (422 with a sentence
    otherwise). Show `user_code` and `verification_url` to the person, who confirms
    in a browser where they are signed in; poll `POST /api/v1/auth/cli/device/token` every
    `interval` seconds. The codes work for 10 minutes. 429 past the rate.

    Parameters
    ----------
    body
        ``{"host"}``: the computer's name, for the confirmation and the token's name.
    request
        The request, for the rate and the public address.
    grants
        The grant service.

    Returns
    -------
    CliDeviceOut
        The codes, the page and the interval.
    """
    _admit(request)
    client = request.client.host if request.client else "unknown"
    started = grants.start_device(body.host, peer=client)
    origin = request.app.state.settings.public_origin().origin
    return CliDeviceOut(
        device_code=started.device_code,
        user_code=started.user_code,
        verification_url=f"{origin}/sign-in/device",
        expires_in=int(DEVICE_LIFETIME.total_seconds()),
        interval=int(POLL_INTERVAL.total_seconds()),
    )


@open_router.post("/device/token", response_model=CliDevicePollOut)
def poll_cli_device(
    body: CliDevicePollIn,
    request: Request,
    grants: CliGrantService = Depends(get_cli_grant_service),
) -> CliDevicePollOut:
    """Ask whether a sign-in of `lakana login --device` was approved.

    Needs no credential: the device code is the proof. Answers ``pending`` until someone
    decides, then ``approved`` with the token once, or ``denied``; ``expired`` after 10 minutes
    and after the token was handed out. 429 with ``error: slow_down`` when asked again sooner
    than 5 seconds; 400 for an unknown device code.

    Parameters
    ----------
    body
        ``{"device_code"}``.
    request
        The request, for the rate.
    grants
        The grant service.

    Returns
    -------
    CliDevicePollOut
        The state, with the token once approved.
    """
    _admit(request, poll=True)
    client = request.client.host if request.client else "unknown"
    polled = grants.poll_device(body.device_code, peer=client)
    return CliDevicePollOut(
        state=polled.state, token=None if polled.token is None else _issued(polled.token)
    )
