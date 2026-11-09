"""The browser a session began in, in plain words: "Firefox 158 on Ubuntu".

A browser names itself in its `User-Agent` header in a form made for compatibility, not for
people ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0").
The sessions list shows the common browsers and systems in words, computed here once for the
interface and the CLI alike, and anything it does not recognise as the browser sent it. Nothing
here decides anything: the name is for people to recognise their own sessions, and a browser
can claim any name.
"""

from __future__ import annotations

import re

_BROWSERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Order matters: Edge, Opera, Samsung Internet and Chromium also carry "Chrome/", Chrome and
    # every browser on iOS also carry "Safari/".
    (re.compile(r"\bEdg(?:e|A|iOS)?/(\d+)"), "Edge"),
    (re.compile(r"\bOPR/(\d+)"), "Opera"),
    (re.compile(r"\bSamsungBrowser/(\d+)"), "Samsung Internet"),
    (re.compile(r"\bFxiOS/(\d+)"), "Firefox"),
    (re.compile(r"\bFirefox/(\d+)"), "Firefox"),
    (re.compile(r"\bCriOS/(\d+)"), "Chrome"),
    (re.compile(r"\bChromium/(\d+)"), "Chromium"),
    (re.compile(r"\bChrome/(\d+)"), "Chrome"),
    (re.compile(r"\bVersion/(\d+)[^ ]* (?:Mobile/[^ ]+ )?Safari/"), "Safari"),
    (re.compile(r"\bSafari/()"), "Safari"),
)
"""Each browser's mark in the user agent, with its major version as the group (empty: none)."""

_SYSTEMS: tuple[tuple[str, str], ...] = (
    # Order matters: Android and ChromeOS also say "Linux", iOS says "like Mac OS X".
    ("iPhone", "iOS"),
    ("iPod", "iOS"),
    ("iPad", "iPadOS"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Windows", "Windows"),
    ("Macintosh", "macOS"),
    ("Ubuntu", "Ubuntu"),
    ("Fedora", "Fedora"),
    ("Linux", "Linux"),
)
"""Each operating system's mark in the user agent, and its name."""


def describe_browser(user_agent: str) -> str:
    """Name the browser of a user agent in plain words.

    Parameters
    ----------
    user_agent
        The `User-Agent` header as the browser sent it (at most 255 characters are stored).

    Returns
    -------
    str
        "<browser> <major version> on <system>" for the common browsers ("Firefox 158 on
        Ubuntu", "Safari on macOS" when it names no version, "Firefox 158" on a system not
        listed); the user agent itself, trimmed, for anything else; "" for none.
    """
    raw = user_agent.strip()
    for mark, browser in _BROWSERS:
        found = mark.search(raw)
        if found is not None:
            name = f"{browser} {found.group(1)}" if found.group(1) else browser
            break
    else:
        return raw
    system = next((words for token, words in _SYSTEMS if token in raw), None)
    return name if system is None else f"{name} on {system}"
