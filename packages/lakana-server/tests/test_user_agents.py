"""The browser a session began in, in plain words, from its user agent."""

from __future__ import annotations

import pytest

from lakana_server.user_agents import describe_browser

_CHROME = "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.7.12 Safari/537.36"
_SAFARI = "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 Safari/605.1.15"


@pytest.mark.parametrize(
    ("user_agent", "browser"),
    [
        (
            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0",
            "Firefox 158 on Ubuntu",
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0",
            "Firefox 158 on Linux",
        ),
        (
            "Mozilla/5.0 (X11; Fedora; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0",
            "Firefox 158 on Fedora",
        ),
        (f"Mozilla/5.0 (X11; Linux x86_64) {_CHROME}", "Chrome 155 on Linux"),
        (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {_CHROME}", "Chrome 155 on Windows"),
        (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {_CHROME} Edg/155.0.3.4",
            "Edge 155 on Windows",
        ),
        (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) {_CHROME} OPR/120.0.1",
            "Opera 120 on macOS",
        ),
        (f"Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) {_CHROME}", "Chrome 155 on ChromeOS"),
        (
            "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/155.0.0.0 Mobile Safari/537.36",
            "Chrome 155 on Android",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/27.0 Chrome/125.0.0.0 Mobile Safari/537.36",
            "Samsung Internet 27 on Android",
        ),
        (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) {_SAFARI}", "Safari 26 on macOS"),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Safari/605.1.15",
            "Safari on macOS",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
            "Safari 18 on iOS",
        ),
        (
            "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
            "Safari 18 on iPadOS",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) CriOS/155.0.1 Mobile/15E148 Safari/604.1",
            "Chrome 155 on iOS",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) FxiOS/150.0 Mobile/15E148 Safari/605.1.15",
            "Firefox 150 on iOS",
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chromium/150.0.0.0 Chrome/150.0.0.0 Safari/537.36",
            "Chromium 150 on Linux",
        ),
        ("Mozilla/5.0 (Haiku; rv:158.0) Gecko/20100101 Firefox/158.0", "Firefox 158"),
    ],
)
def test_a_known_browser_is_named_in_plain_words(user_agent: str, browser: str) -> None:
    """The common engines read as "<browser> <major version> on <system>"."""
    assert describe_browser(user_agent) == browser


@pytest.mark.parametrize("user_agent", ["curl/8.5.0", "python-httpx/0.28.1", "pytest", "  x  "])
def test_an_unknown_agent_is_shown_as_it_named_itself(user_agent: str) -> None:
    """Anything else stays the raw string (trimmed), so nothing is guessed."""
    assert describe_browser(user_agent) == user_agent.strip()


def test_no_agent_is_an_empty_name() -> None:
    """A browser that sent no user agent has no name; the interface says "unknown browser"."""
    assert describe_browser("") == ""
