"""`lakana sign-in *`: the setup link and how long browser sessions last."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from lakana._models import SignInSettings
from lakana.cli import _output
from lakana.cli._state import CliState, state_of

sign_in_app = typer.Typer(
    help="Sign in to the web interface: the setup link and session lengths.",
    no_args_is_help=True,
)


def print_link(state: CliState, url: str, value: object) -> None:
    """Print a newly issued sign-in link: alone on stdout, or the whole answer as JSON.

    Parameters
    ----------
    state
        The CLI state.
    url
        The link.
    value
        The SDK result, printed in JSON mode.
    """
    if state.output == "json":
        _output.print_json(value)
    else:
        sys.stdout.write(_output.safe_text(url) + "\n")


def send_advice(state: CliState, name: str, lifetime: str) -> None:
    """Tell the administrator how to hand a link over, on stderr.

    Parameters
    ----------
    state
        The CLI state.
    name
        The person the link is for.
    lifetime
        How long it works, e.g. `"7 days"`.
    """
    _output.next_step(
        state,
        f"Send this link to {_output.inline(name)} yourself; it works once, for {lifetime}. "
        "Confirm with them before granting roles.",
    )


@sign_in_app.command("setup-link")
def setup_link(ctx: typer.Context) -> None:
    """Print the link that signs the first administrator in to the web interface.

    Run it as the server operator, with the profile 'lakana bundle init' saved (it holds the
    bootstrap token). The link works once, for 10 minutes, and only until the first
    administrator has a passkey; after that, use 'lakana recovery create'. Only the link goes
    to stdout.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    issued = state.client().sign_in.setup_link()
    print_link(state, issued.url, issued)
    _output.next_step(
        state,
        "Open it within 10 minutes in the browser you will use; it works once and then asks "
        "you to register a passkey.",
    )


def _settings_text(settings: SignInSettings) -> str:
    """Describe the session lengths and their bounds, in minutes.

    Parameters
    ----------
    settings
        The settings.

    Returns
    -------
    str
        Three lines, and a fourth with the identity provider for an administrator.
    """
    bounds = settings.bounds
    text = (
        f"Idle timeout: {settings.idle_timeout_minutes} minutes "
        f"({bounds.idle_timeout_minutes_min} to {bounds.idle_timeout_minutes_max})\n"
        f"Absolute lifetime: {settings.absolute_lifetime_minutes} minutes "
        f"({bounds.absolute_lifetime_minutes_min} to {bounds.absolute_lifetime_minutes_max})\n"
        f"Token window: {settings.token_provider_window_days} days "
        f"({bounds.token_provider_window_days_min} to {bounds.token_provider_window_days_max})"
    )
    provider = settings.provider
    if provider is not None:
        admins = "administrator" if provider.unlinked_admins == 1 else "administrators"
        text += (
            f"\nIdentity provider: {_output.inline(provider.display_name)} "
            f"({_output.inline(provider.issuer)}): {provider.linked_people} linked people, "
            f"{provider.unlinked_admins} {admins} who signs in with a passkey"
        )
    return text


@sign_in_app.command("settings")
def settings(
    ctx: typer.Context,
    idle_timeout: Annotated[
        int | None,
        typer.Option(
            "--idle-timeout",
            min=1,
            metavar="MINUTES",
            help="End a session after this many minutes without use (15 to 1440).",
        ),
    ] = None,
    absolute_lifetime: Annotated[
        int | None,
        typer.Option(
            "--absolute-lifetime",
            min=1,
            metavar="MINUTES",
            help="End a session this many minutes after sign-in at the latest (60 to 43200).",
        ),
    ] = None,
    token_window: Annotated[
        int | None,
        typer.Option(
            "--token-window-days",
            min=1,
            metavar="DAYS",
            help=(
                "Let a person linked to the identity provider use their personal tokens this "
                "many days after their last sign-in there (1 to 365)."
            ),
        ),
    ] = None,
) -> None:
    """Show or change how long browser sessions last, and the token window of linked people.

    Without options it shows the idle timeout and the absolute lifetime in minutes and the
    token window in days, with their bounds, and to an administrator the identity provider with
    its counts. Changing them needs the ADMIN role; new lengths apply to new sessions (open ones
    keep theirs), a new token window to the next request with a token.

    Parameters
    ----------
    ctx
        The Typer context.
    idle_timeout
        `--idle-timeout`: the new idle timeout in minutes.
    absolute_lifetime
        `--absolute-lifetime`: the new absolute lifetime in minutes.
    token_window
        `--token-window-days`: the new token window in days.
    """
    state = state_of(ctx)
    resource = state.client().sign_in_settings
    lengths = idle_timeout is not None or absolute_lifetime is not None
    changing = lengths or token_window is not None
    if changing:
        current = resource.update(
            idle_timeout_minutes=idle_timeout,
            absolute_lifetime_minutes=absolute_lifetime,
            token_provider_window_days=token_window,
        )
    else:
        current = resource.get()
    if state.output == "json":
        _output.print_json(current)
    else:
        sys.stdout.write(_settings_text(current) + "\n")
    if lengths:
        _output.next_step(state, "The new lengths apply to new sessions; open ones keep theirs.")
    if token_window is not None:
        _output.next_step(state, "The new token window applies to the next request with a token.")


def register(app: typer.Typer) -> None:
    """Add the `sign-in` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(sign_in_app, name="sign-in")
