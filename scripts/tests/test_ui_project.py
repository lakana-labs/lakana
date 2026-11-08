"""Tests for the configuration of the ``ui/`` project (the web interface)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

BROWSERS = [
    "last 2 Chrome versions",
    "last 2 Edge versions",
    "last 2 Firefox versions",
    "Firefox ESR",
    "last 2 Safari versions",
]


def _package() -> dict:
    """Load ``ui/package.json``.

    Returns
    -------
    dict
        The parsed manifest.
    """
    return json.loads((UI / "package.json").read_text())


def _npmrc() -> dict[str, str]:
    """Parse ``ui/.npmrc`` into its settings.

    Returns
    -------
    dict of str to str
        Each ``key=value`` line, comments and blank lines left out.
    """
    settings = {}
    for line in (UI / ".npmrc").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";")):
            key, _, value = line.partition("=")
            settings[key.strip()] = value.strip()
    return settings


def test_every_ui_dependency_is_pinned_exactly() -> None:
    """No range anywhere: each tool upgrades in its own pull request."""
    package = _package()
    pins = {**package.get("dependencies", {}), **package["devDependencies"]}
    assert pins
    loose = {name: v for name, v in pins.items() if not re.fullmatch(r"\d+\.\d+\.\d+", v)}
    assert loose == {}


def test_node_is_the_lts_line_in_both_places() -> None:
    """CI reads ``.nvmrc``; npm refuses another Node through ``engines``."""
    assert (UI / ".nvmrc").read_text().strip() == "26"
    assert _package()["engines"] == {"node": "^26.0.0"}


def test_npmrc_pins_saves_and_refuses_scripts() -> None:
    """Exact saves, a refused wrong Node, and no install script of any package runs."""
    settings = _npmrc()
    assert settings["save-exact"] == "true"
    assert settings["engine-strict"] == "true"
    assert settings["ignore-scripts"] == "true"


def test_browserslist_is_the_supported_browser_list() -> None:
    """The browsers of the build target and the docs' supported list."""
    assert _package()["browserslist"] == BROWSERS


def test_the_lock_records_a_licence_for_every_package() -> None:
    """``scripts/check_licenses.py`` reads the licences from the lock; npm 11 writes them."""
    lock = json.loads((UI / "package-lock.json").read_text())
    assert lock["lockfileVersion"] == 3
    packages = {k: v for k, v in lock["packages"].items() if k.startswith("node_modules/")}
    assert packages
    assert [name for name, entry in packages.items() if "license" not in entry] == []


def test_vite_builds_into_the_server_package() -> None:
    """The build lands where the server wheel picks it up, and is never committed."""
    assert "lakana_server/ui_dist" in (UI / "vite.config.ts").read_text()
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "packages/lakana-server/src/lakana_server/ui_dist/" in ignored


def test_the_ui_package_has_no_version_of_its_own() -> None:
    """Rule 3: the one version comes from git tags, so the manifest carries none."""
    package = _package()
    assert "version" not in package
    assert package["private"] is True


def test_the_only_override_lifts_minimatch_10_past_its_advisory() -> None:
    """One override, on purpose: remove it with #507, once typescript-eslint allows minimatch 11.

    GHSA-g3hv-75wg-m249 affects minimatch below 11.0.1; typescript-eslint 8.72.1 still asks for
    ``^10.2.2``. The override reaches only that range, never ESLint's own minimatch 3.
    """
    assert _package()["overrides"] == {"minimatch@^10": "11.0.5"}  # remove with #507
    lock = json.loads((UI / "package-lock.json").read_text())
    versions = {
        entry["version"]
        for name, entry in lock["packages"].items()
        if name.endswith("node_modules/minimatch")
    }
    assert versions == {"3.1.5", "11.0.5"}


def test_vite_inlines_no_asset() -> None:
    """The content security policy has no ``data:`` source, so no asset becomes one."""
    assert "assetsInlineLimit: 0" in (UI / "vite.config.ts").read_text()


def test_the_ui_sources_are_not_git_ignored() -> None:
    """``build/`` is ignored for Python builds; ``ui/build/`` holds source and must be committed."""
    for path in ("ui/build/targets.ts", "ui/scripts/prove-gates.mjs", "ui/src/App.tsx"):
        done = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
        assert done.returncode == 1, path


def test_the_plex_licence_names_the_reserved_font() -> None:
    """The font licence ships with the interface (Vitest compares it with the package copy)."""
    licence = (UI / "public/licenses/IBM-Plex-OFL-1.1.txt").read_text(encoding="utf-8")
    assert licence.startswith('Copyright © 2017 IBM Corp. with Reserved Font Name "Plex"')


def test_only_complete_plex_files_are_used() -> None:
    """The ``complete`` woff2 files are the ones with every glyph; no other folder is referenced."""
    urls = re.findall(r"url\(([^)]*)\)", (UI / "src/styles/fonts.css").read_text())
    assert len(urls) == 3
    for url in urls:
        assert "fonts/complete/woff2/" in url, url


def test_every_cli_line_in_the_shell_exists() -> None:
    """Each "use the command line" line of the shell names a command the CLI reference has.

    A subcommand (``lakana runs list``) needs its ``###`` heading; a command whose next word is
    an argument (``lakana search <words>``, ``lakana sql "select 1"``) needs its ``##`` heading.
    """
    areas = (UI / "src/shell/areas.ts").read_text()
    lines = [line for _, line in re.findall(r"""cli:\s*(["'])(.*?)\1""", areas) if line]
    assert len(lines) >= 7, lines
    headings = set(
        re.findall(
            r"^#{2,3} `(lakana [a-z -]+)`$", (ROOT / "docs/site/reference/cli.md").read_text(), re.M
        )
    )
    for line in lines:
        words = line.split()
        assert words[0] == "lakana", line
        command = " ".join(
            words[:3] if len(words) > 2 and re.fullmatch(r"[a-z][a-z-]*", words[2]) else words[:2]
        )
        assert command in headings, (
            f"{line!r}: no heading for `{command}` in docs/site/reference/cli.md"
        )


def _csp_from_ts() -> str:
    """Evaluate the policy ``ui/csp.ts`` exports, from its source text.

    Returns
    -------
    str
        The value of ``CONTENT_SECURITY_POLICY`` as the browser receives it.
    """
    source = (UI / "csp.ts").read_text()
    hashes_block = re.search(r"LIBRARY_STYLE_HASHES[^=]*=\s*\[(.*?)\];", source, re.S)
    assert hashes_block, "LIBRARY_STYLE_HASHES not found in ui/csp.ts"
    hashes = re.findall(r'^\s*"([^"]*)",', hashes_block.group(1), re.M)
    assert hashes, "no hash in LIBRARY_STYLE_HASHES"
    policy_block = re.search(
        r"CONTENT_SECURITY_POLICY[^=]*=\s*\[(.*?)\]\.join\(\"; \"\);", source, re.S
    )
    assert policy_block, "CONTENT_SECURITY_POLICY is not a list joined with '; ' in ui/csp.ts"
    parts = []
    for line in policy_block.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        literal = re.fullmatch(r'"([^"]*)",|`([^`]*)`,', line)
        assert literal, f"cannot read this line of ui/csp.ts: {line!r}"
        text = literal.group(1) if literal.group(1) is not None else literal.group(2)
        text = text.replace('${LIBRARY_STYLE_HASHES.join(" ")}', " ".join(hashes))
        assert "${" not in text, f"an expression this test does not know: {line!r}"
        parts.append(text)
    return "; ".join(parts)


def test_the_server_sends_the_ui_s_csp() -> None:
    """The server's header and ``vite preview``'s (the Playwright run) are one policy."""
    from lakana_server.ui import CONTENT_SECURITY_POLICY

    assert _csp_from_ts() == CONTENT_SECURITY_POLICY


def test_no_area_path_collides_with_a_server_path() -> None:
    """An area's URL never starts with a segment the server or the build owns."""
    areas = (UI / "src/shell/areas.ts").read_text()
    paths = re.findall(r"""path:\s*["']([^"']*)["']""", areas)
    assert len(paths) >= 8, paths
    firsts = {path.strip("/").split("/")[0] for path in paths}
    server = {
        "api",
        "v1",
        "healthz",
        "readyz",
        "docs",
        "redoc",
        "openapi.json",
        "assets",
        "licenses",
    }
    assert firsts & server == set()
