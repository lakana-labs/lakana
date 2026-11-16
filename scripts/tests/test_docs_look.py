"""Tests for the look of the documentation site: Still Water, in light and dark.

The site takes the colour tokens of the web interface (``ui/src/styles/tokens.css``) and maps
them onto Zensical's variables in ``docs/site/assets/stylesheets/still-water.css``. The reading
steps of the type scale and the code colours are those of the project website
(lakana-labs.github.io), so the website and the documentation read as one family.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs" / "site"
STYLESHEET = SITE / "assets" / "stylesheets" / "still-water.css"
TOKENS = ROOT / "ui" / "src" / "styles" / "tokens.css"
SCHEMES = {"light": "default", "dark": "slate"}


def _config() -> dict:
    """Load ``mkdocs.yml``.

    Returns
    -------
    dict
        The parsed configuration.
    """
    return yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))


def _css() -> str:
    """Read the site's stylesheet.

    Returns
    -------
    str
        The text of ``still-water.css``.
    """
    return STYLESHEET.read_text(encoding="utf-8")


def _block(css: str, selector: str) -> str:
    """Return the declarations of the first rule whose selector list holds ``selector``.

    Parameters
    ----------
    css
        A stylesheet.
    selector
        One selector of the rule's list, as written.

    Returns
    -------
    str
        The text between the rule's braces.

    Raises
    ------
    AssertionError
        When no rule lists ``selector``.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [part.strip() for part in selectors.split(",")]:
            return declarations
    raise AssertionError(f"no rule for {selector}")


def _colours(declarations: str) -> dict[str, str]:
    """Return the ``#rrggbb`` custom properties of a rule.

    Parameters
    ----------
    declarations
        The text of a rule's body.

    Returns
    -------
    dict[str, str]
        ``{name: value}`` without the leading ``--``, values in lower case.
    """
    pairs = re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", declarations)
    return {name: value.lower() for name, value in pairs}


def _scheme(theme: str) -> dict[str, str]:
    """Return the colours a scheme of the site defines.

    Parameters
    ----------
    theme
        ``light`` or ``dark``.

    Returns
    -------
    dict[str, str]
        The ``#rrggbb`` custom properties of ``[data-md-color-scheme="…"]``.
    """
    return _colours(_block(_css(), f'[data-md-color-scheme="{SCHEMES[theme]}"]'))


def _contrast(a: str, b: str) -> float:
    """Return the WCAG 2.1 contrast ratio of two ``#rrggbb`` colours.

    Parameters
    ----------
    a, b
        The two colours.

    Returns
    -------
    float
        The ratio, from 1 to 21.
    """

    def luminance(colour: str) -> float:
        channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_site_has_a_light_and_a_dark_scheme_with_a_toggle() -> None:
    """Two palettes, each chosen by the system setting on a first visit and with a toggle.

    Zensical selects the first palette whose ``media`` matches when the reader has not chosen
    one, and stores the reader's choice once the toggle is used.
    """
    palette = _config()["theme"]["palette"]
    assert [entry["scheme"] for entry in palette] == ["default", "slate"]
    assert [entry["media"] for entry in palette] == [
        "(prefers-color-scheme: light)",
        "(prefers-color-scheme: dark)",
    ]
    for entry in palette:
        assert entry["toggle"]["icon"] and entry["toggle"]["name"]
        # "custom" leaves the colours to the stylesheet instead of a Material palette.
        assert entry["primary"] == entry["accent"] == "custom"


def test_the_stylesheet_is_listed_and_local() -> None:
    """The stylesheet ships with the site (the site loads nothing from another host)."""
    assert _config()["extra_css"] == ["assets/stylesheets/still-water.css"]
    assert STYLESHEET.is_file()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_colours_are_the_interfaces_tokens(theme: str) -> None:
    """Every ``--color-*`` of a scheme has the value of the same token in ``tokens.css``.

    When the interface's tokens change, copy the new values into both schemes.
    """
    tokens = TOKENS.read_text(encoding="utf-8")
    source = _colours(_block(tokens, ":root" if theme == "light" else ':root[data-theme="dark"]'))
    ours = {name: value for name, value in _scheme(theme).items() if name.startswith("color-")}
    assert ours
    for name, value in ours.items():
        assert source.get(name) == value, (
            f"{theme}: --{name} is {value}, tokens.css has {source.get(name)}"
        )


def test_both_schemes_define_the_same_colours() -> None:
    """A colour defined for one scheme only would fall back to the other scheme's value."""
    assert set(_scheme("light")) == set(_scheme("dark"))


# (foreground, background, minimum ratio): every text pair the stylesheet draws. Body text
# 7:1, other text, links and code colours 4.5:1, the edge and the focus ring 3:1.
PAIRS = [
    *[("color-text", bg, 7.0) for bg in ("color-ground", "color-surface", "color-sunk")],
    *[
        (fg, bg, 4.5)
        for fg in ("color-text-muted", "color-accent", "color-accent-hover")
        for bg in ("color-ground", "color-surface")
    ],
    ("color-text-muted", "color-sunk", 4.5),
    ("color-text-faint", "color-ground", 4.5),
    ("color-text", "color-accent-bg", 7.0),
    ("color-accent", "color-accent-bg", 4.5),
    ("color-text", "color-ok-bg", 7.0),
    ("color-text", "color-warn-bg", 7.0),
    ("color-text", "color-bad-bg", 7.0),
    ("color-edge", "color-ground", 3.0),
    ("color-accent", "color-ground", 3.0),
    ("color-ok", "color-ok-bg", 3.0),
    ("color-warn", "color-warn-bg", 3.0),
    ("color-bad", "color-bad-bg", 3.0),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_colour_pairs_meet_wcag_aa(theme: str) -> None:
    """Text, links and glyphs keep their contrast on the surfaces they are drawn on."""
    values = _scheme(theme)
    for fg, bg, minimum in PAIRS:
        ratio = _contrast(values[fg], values[bg])
        assert ratio >= minimum, f"{theme}: --{fg} on --{bg} is {ratio:.2f}, needs {minimum}"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_code_colours_meet_aa_on_the_code_background(theme: str) -> None:
    """Every syntax colour (``--tok-*``) reads at 4.5:1 on the code background."""
    values = _scheme(theme)
    tokens = [name for name in values if name.startswith("tok-")]
    assert tokens
    for name in tokens:
        ratio = _contrast(values[name], values["color-sunk"])
        assert ratio >= 4.5, f"{theme}: --{name} {values[name]} is {ratio:.2f} on --color-sunk"


CODE_COLOURS = {
    "tok-keyword": "color-accent",
    "tok-builtin": "color-accent-hover",
    "tok-function": "color-text",
    "tok-string": "color-ok",
    "tok-number": "color-text-muted",
    "tok-comment": "color-text-faint",
    "tok-deleted": "color-bad",
    "tok-inserted": "color-ok",
}
"""The interface token each syntax colour stands for."""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_each_code_colour_is_the_token_it_stands_for(theme: str) -> None:
    """A ``--tok-*`` keeps the value of its interface token, so a token change reaches the code."""
    values = _scheme(theme)
    assert sorted(name for name in values if name.startswith("tok-")) == sorted(CODE_COLOURS)
    for name, token in CODE_COLOURS.items():
        assert values[name] == values[token], (
            f"{theme}: --{name} is {values[name]}, --{token} is {values[token]}; copy the token"
        )


def test_every_variable_the_stylesheet_uses_is_defined() -> None:
    """A ``var(--…)`` of our own that is not defined would silently fall back.

    Zensical's own variables (``--md-*``) are defined by its stylesheet.
    """
    css = _css()
    defined = set(re.findall(r"--([\w-]+)\s*:", css))
    used = set(re.findall(r"var\(--([\w-]+)", css))
    missing = {name for name in used - defined if not name.startswith("md-")}
    assert not missing, f"still-water.css uses undefined {sorted(missing)}"


def test_every_font_is_bundled_with_its_licence() -> None:
    """IBM Plex is served by the site: the complete, unmodified files and the OFL beside them."""
    css = _css()
    files = re.findall(r'url\("\.\./fonts/([^"]+)"\)', css)
    assert files
    fonts = SITE / "assets" / "fonts"
    for name in files:
        assert (fonts / name).is_file(), name
    licence = (fonts / "OFL.txt").read_text(encoding="utf-8")
    assert licence.startswith('Copyright © 2017 IBM Corp. with Reserved Font Name "Plex"')
    weights = set(re.findall(r"font-weight:\s*(\d+)", css))
    assert weights <= {"400", "500"}, "only the regular and medium weights are bundled"


def test_the_header_shows_the_logo_and_the_tab_the_favicon() -> None:
    """The brand logo in the header, the interface's favicon in the browser tab."""
    theme = _config()["theme"]
    assert (SITE / theme["logo"]).is_file()
    assert (SITE / theme["favicon"]).is_file()
