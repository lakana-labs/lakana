"""The environment an in-process CLI test runs in, shared by the CLI tests of two packages.

`invoke` in `packages/lakana/tests/cli/conftest.py` and `run_cli` in
`packages/lakana-server/tests/cli/conftest.py` both hand this to Typer's runner, so a developer's
or a CI runner's own settings (a profile, a token, GitHub Actions' OIDC request variables) never
reach a test, and the output is plain whatever the terminal.

Standard library only, so every package's tests can import it.
"""

from __future__ import annotations

CLEARED = (
    "LAKANA_OUTPUT",
    "LAKANA_PROFILE",
    "LAKANA_TOKEN",
    "LAKANA_URL",
    "LAKANA_OIDC",
    "LAKANA_OIDC_TOKEN",
    "LAKANA_OIDC_AUDIENCE",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
)
"""Variables the CLI reads, removed for the test: its settings come from the test alone."""


def clean_cli_env(extra: dict[str, str] | None = None) -> dict[str, str | None]:
    """Build the environment for one in-process CLI run.

    Parameters
    ----------
    extra
        Variables to set on top, e.g. ``{"LAKANA_PROFILE": "ci"}``.

    Returns
    -------
    dict
        Names to values; `None` removes the variable for the run.
    """
    env: dict[str, str | None] = dict.fromkeys(CLEARED)
    env["COLUMNS"] = "200"
    # Plain output regardless of the caller's terminal settings (CI, FORCE_COLOR). `NO_COLOR`
    # removes colours but not bold; a dumb terminal removes every style, and GITHUB_ACTIONS
    # would otherwise make the help renderer treat a self-hosted runner's TERM as a terminal.
    env.update(
        {
            "FORCE_COLOR": None,
            "PY_COLORS": None,
            "GITHUB_ACTIONS": None,
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    env.update(extra or {})
    return env
