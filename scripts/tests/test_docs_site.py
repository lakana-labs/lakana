"""Tests for the documentation site's configuration."""

import re
import subprocess
from pathlib import Path

import yaml

from scripts.docs_blocks import check, find_blocks
from scripts.generate_reference import OPENAPI_PAGE

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs" / "site"
OLD_PATHS = ("docs/projects.md", "docs/deploy/compose.md", "docs/scope.md", "docs/roadmap.md")
EXEMPT = (
    "CHANGELOG.md",
    "scripts/tests/test_docs_site.py",
    "scripts/tests/test_docs_only.py",
)
NOT_TESTED_CEILING = 54
UNCHECKED_OUTPUT_CEILING = 78
"""Blocks marked as not tested; it only goes down, apart from the rises below.

The artifact pages left six when they were written: the `artifact uploads abort` command,
which needs the id of an upload that was begun and not finished, the `chmod -R u+w DIR && rm -r
DIR` that removes a directory of the reader's choice, the delete in a protected environment
(`--break-glass`), the `uv lock` of the scikit-learn page, which needs the public package index,
the downgrade of the upgrade page, which changes a real database, and the tutorial's build of
the `lakana` wheel from a checkout. The first review of the artifact pages rose it from 36 to 41 on
purpose:
the "After a restore" steps of `deploy-with-compose.md` had their commands in running text, which
the site's rules forbid, and each now stands in a block. Four of the five blocks hold a command
with placeholders (`<name>`, `<n>`, `<env>`) that no example can run, and the fifth reads the
log of a running Compose stack. Store, delete and drop of an artifact are run by the example of
the tutorial.

The column-mask page rose it from 41 to 44: its three command blocks need an
administrator's profile and the profiles of two readers on a running instance, which the
one-instance examples do not set up. `test_column_masks_howto.py` in lakana-server runs those
commands against the real app and checks the output and every message the page quotes.

The secrets page rose it by one, to 45: its one command block lists and sets a secret
of a running instance, with placeholders for the name and the environment.

The upgrade page (the format-1 refusal) rose it from 45 to 46 for the one command that set the
bundle's storage secret again from its `.env`; the bundle's `setup` service does that now, and the
block is gone (back to 45).

The private package index page rose it from 45 to 48: three command blocks of
`private-package-index.md` need a package
index that asks for a password (`uv lock`, deploy and run) or the id of a deployed project version
(`project-version show`), which the docs-examples job cannot reach without a new service (item 4).
Six tests of `test_python_assets_integration.py` run the effect of those commands end to
end and are the proof. The page's registering, rotating and clearing of an index run in the
`getting-started` example, because a local instance has a secrets master key and registering never
contacts the index.

The upgrade page's section on a dependency that breaks Lakana rose it from 54 to 55 with one
block: the `pip`, `uv` and constraints-file ways to pin a package name versions that no example
can know are good.

The upgrade note of the following release rose it from 48 to 52 with four blocks. Two need
names or ids of the reader's own instance (`grant list --principal NAME` and `group members
GROUP`; `grant create ... --principal NAME` and `grant revoke ID`). Two are for an instance whose
administrators are locked out and read or change its database: the `SELECT` that records who holds
the `ADMIN` role, and the `UPDATE` that revokes it, which no example can set up. The note's other
commands (`whoami`, `principal list`, `grant list`, `group list`, three `audit list`) are synced
from `getting-started/run.sh` and run there. `test_bootstrap.py` in lakana-core pins the restore of
the bootstrap administrator that follows the `UPDATE`; the statement itself is not run.

The page "Stop a query" rose it from 52 to 53 with one block: `lakana query cancel
QUERY_ID` needs the id of a query that is still running, which a docs example cannot hold: its
queries end in milliseconds. The tests of `lakana query cancel` and of Ctrl-C in
`packages/lakana/tests/cli/test_queries.py` run the command and check every message the page
quotes.

The page "Why is my query or task waiting?" rose it from 53 to 54 with one block:
`lakana query status QUERY_ID` needs the id of a query that is still queued, which a docs example
cannot hold: its queries start within a second. The tests of the wait line in
`packages/lakana/tests/cli/test_queries.py` run the commands that print the reason and assert the
line the page shows.

The scikit-learn page's `uv lock` block fell out of the count (54 to 53): it is synced from the
example `train-and-predict`, which installs scikit-learn and locks with the public package index.

The upgrade note "A secret is bound to one use" rose it from 52 to 53 with one block: it lists
and binds the secrets of an upgraded instance, with placeholders for the names. The commands run
for real in `docs/examples/bind-a-secret/run.sh` and, for the index case, in
`docs/examples/getting-started/run.sh`.

The "Verify a download" block of `deploy-with-compose.md` rose it from 53 to 54 (#130): it runs
`pip download` and `gh attestation verify` against a published, attested release, which needs
the network, the GitHub CLI and a release that exists, none of which the example harness has.

The sorting of the pages (#55) removed the 28 blocks marked "moved as is" and the count fell from
55 to 48. Seven became tested: the `*lakana-env` block of `deploy-with-compose.md` shows its
anchor and is parsed; `uv lock` is synced from `train-and-predict`; the project start, the
two SQL assets of the first example and `lakana undeploy` from the new example `projects`.
Twenty-one others stayed and now say why: Docker (the Compose commands, backups and the
bundle's teardown),
a reverse proxy's own configuration, a protected `prod` environment or a hosted repository's token
that the one-instance examples do not set up, a command with optional parts or an id in brackets,
and the two SQL headers that `test_the_front_matter_in_a_sql_block_is_valid` parses instead.

The operator's guide (#57) rose it from 48 to 51 with the three blocks of its recipe "One instance
per client": generating and starting two Compose stacks on one host, listing the Compose projects,
and upgrading one of the two. Each needs Docker and two complete stacks, which the example runner
does not have. The one backup block of `deploy-with-compose.md` replaced two (a single
procedure now), so the count is 50.
The guide's other command blocks are synced from `give-access` and `getting-started`.

The page "Is my instance healthy?" (#45) rose it from 50 to 53 with three blocks that ask Docker
about a running stack: `docker compose ps --all`, `docker system df -v` with `docker compose exec
worker df`, and `docker compose logs` with the filter that removes secrets. The example runner has
no Docker. The health route, the queue listings and the version run in `getting-started/run.sh`.

The backup block of `deploy-with-compose.md` is synced from `docs/examples/compose-restore.sh`
since #200, which `compose-smoke` runs, so the count fell by one, to 52.

The task-container release rose it by two, to 54: the four `.env` lines of the upgrade (input,
checked by `test_deploy_init.py`) and the launcher's volume removal under "Starting over" (run by
`scripts/compose_smoke_test.sh`).

`UNCHECKED_OUTPUT_CEILING` rose from 77 to 78 with the Model block of `lakana table get` in
"See a table's model" (`describe-a-table.md`): the page shows that block only, not the whole
output, and the example `see-a-tables-model` runs the command and checks the model in its JSON.
"""
SECTIONS = ("tutorials", "how-to", "reference", "explanation")


def _config() -> dict:
    """Load the site configuration.

    Returns
    -------
    dict
        The parsed `mkdocs.yml`.
    """
    return yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))


def _nav_files(nav: list) -> list[str]:
    """Flatten the navigation to its page paths.

    Parameters
    ----------
    nav
        The `nav` value of `mkdocs.yml`, or a nested part of it.

    Returns
    -------
    list of str
        Every page path, in order.
    """
    files: list[str] = []
    for entry in nav:
        value = next(iter(entry.values())) if isinstance(entry, dict) else entry
        files.extend(_nav_files(value) if isinstance(value, list) else [value])
    return files


def test_the_site_is_built_from_docs_site_into_an_ignored_directory() -> None:
    """The published tree is `docs/site` and nothing else."""
    config = _config()
    assert config["docs_dir"] == "docs/site"
    assert config["site_dir"] == "site-build"
    assert "site-build/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_every_page_is_in_the_navigation_and_every_entry_exists() -> None:
    """A page nobody can reach, or an entry with no page, is a mistake."""
    listed = _nav_files(_config()["nav"])
    on_disk = sorted(p.relative_to(SITE).as_posix() for p in SITE.rglob("*.md"))
    assert sorted(listed) == on_disk
    assert len(listed) == len(set(listed))


def test_the_four_sections_exist() -> None:
    """Tutorials, how-to guides, reference and explanation each have a landing page."""
    for section in SECTIONS:
        assert (SITE / section / "index.md").is_file()


def test_no_file_names_a_page_at_its_old_path() -> None:
    """The pages moved into the site; every reference moved with them."""
    patterns = [arg for path in OLD_PATHS for arg in ("-e", path)]
    listed = subprocess.run(
        ["git", "grep", "-n", "-I", "-F", *patterns],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    left = [line for line in listed if not line.startswith(EXEMPT)]
    assert left == []


def test_the_moved_pages_are_on_the_site() -> None:
    """Each of the four existing pages has its place."""
    pages = (
        "how-to/projects.md",
        "how-to/deploy-with-compose.md",
        "explanation/scope.md",
        "explanation/roadmap.md",
    )
    for page in pages:
        assert (SITE / page).is_file()


_CDN_MARKERS = (
    "```mermaid",
    "~~~mermaid",
    'class="mermaid"',
    "pyodide",
    "{.mermaid",
    ".glightbox",
    "api.github.com",
)


def test_the_configuration_enables_no_feature_that_loads_from_a_third_party() -> None:
    """Zensical's script bundle fetches from public CDNs when a page uses certain features.

    The site must work on an internal network, so none of them may be switched on. Each
    check below names the feature it guards: ``repo_url`` makes the header call the GitHub
    API for stars and forks; the ``glightbox`` plugin or extension zooms images with a
    script from unpkg; a superfences custom fence named ``mermaid`` renders diagrams with a
    script from unpkg; the ``markdown-exec`` plugin runs Python in the browser with
    Pyodide from jsDelivr, and its ``.pyodide`` blocks are editable with Ace from unpkg;
    an ``extra_javascript`` or ``extra_css`` entry with a URL loads from that host.
    """
    config = _config()
    assert not config.get("repo_url"), "repo_url makes the header query api.github.com"
    plugins = config.get("plugins") or []
    names = {next(iter(p)) if isinstance(p, dict) else p for p in plugins}
    assert not names & {"glightbox", "markdown-exec"}
    extensions = [
        next(iter(e)) if isinstance(e, dict) else e for e in config.get("markdown_extensions", [])
    ]
    assert "zensical.extensions.glightbox" not in extensions
    for entry in config.get("markdown_extensions", []):
        if isinstance(entry, dict) and "pymdownx.superfences" in entry:
            fences = (entry["pymdownx.superfences"] or {}).get("custom_fences", [])
            assert "mermaid" not in [fence.get("name") for fence in fences]
    features = config.get("theme", {}).get("features", [])
    assert not [f for f in features if "pyodide" in f or "glightbox" in f]
    for key in ("extra_javascript", "extra_css"):
        for entry in config.get(key, []):
            path = entry if isinstance(entry, str) else entry.get("path", "")
            assert "://" not in path and not path.startswith("//")


ALLOWED_PLUGINS: frozenset[str] = frozenset()
"""The plugins `mkdocs.yml` may list. Today none."""


def test_the_configuration_runs_no_code_at_build_time() -> None:
    """A `hooks:` key or a plugin runs Python when the site is built.

    A pull request that changes only documentation (and `mkdocs.yml`, for a new page's
    navigation entry) skips `lint`, `static` and the other code checks, so it must not be
    able to add code the build runs. To add a hook or a plugin, list the plugin in
    `ALLOWED_PLUGINS` here, in a pull request that is not documentation only: this file is
    under `scripts/`, so changing it runs the whole pipeline.
    """
    config = _config()
    assert "hooks" not in config, (
        "mkdocs.yml has a `hooks:` key; hooks run code at build time and a docs-only pull "
        "request skips the code checks. Add it in a pull request that is not docs-only and "
        "say so in scripts/tests/test_docs_site.py"
    )
    plugins = {next(iter(p)) if isinstance(p, dict) else p for p in (config.get("plugins") or [])}
    assert plugins <= ALLOWED_PLUGINS, (
        f"mkdocs.yml lists plugins {sorted(plugins - ALLOWED_PLUGINS)}, which run code at build "
        "time and a docs-only pull request skips the code checks. List them in ALLOWED_PLUGINS "
        "in scripts/tests/test_docs_site.py, in a pull request that is not docs-only"
    )


def test_no_page_uses_a_feature_that_loads_from_a_third_party() -> None:
    """A page can switch a CDN loader on without any configuration.

    A ``mermaid`` fence renders a diagram, an element with the ``pyodide`` class is run and
    edited in the browser, and a ``glightbox`` link opens a lightbox; each loads a script from
    a public CDN. Draw the diagram as text, or leave it out.
    """
    found = [
        f"{page.relative_to(ROOT)}: {marker}"
        for page in sorted(SITE.rglob("*.md"))
        for marker in _CDN_MARKERS
        if marker in page.read_text(encoding="utf-8").lower()
    ]
    assert found == []


def test_the_font_is_off_and_there_is_no_analytics() -> None:
    """Zensical loads Google Fonts unless ``theme.font`` is false, and analytics unless unset.

    Deleting ``font: false`` or adding ``extra.analytics`` makes every built page call a third
    party. ``scripts/check_built_site.py`` catches the result in the built site; this names
    the cause.
    """
    config = _config()
    assert config["theme"]["font"] is False
    assert "analytics" not in (config.get("extra") or {})


def test_only_pages_and_assets_live_under_docs_site() -> None:
    """Every file is a ``.md`` page (checked by the navigation test) or an asset.

    Zensical copies any other file into the site as is: an ``.html`` or ``.markdown`` page
    would be published without the block check or the navigation check. Put images and the
    like under ``docs/site/assets``. The one exception is the OpenAPI document of the REST
    API, which ``scripts/generate_reference.py`` writes next to the other generated references.
    """
    stray = [
        p.relative_to(SITE).as_posix()
        for p in sorted(SITE.rglob("*"))
        if p.is_file()
        and p.suffix != ".md"
        and "assets" not in p.relative_to(SITE).parts[:1]
        and p.relative_to(SITE).as_posix() != OPENAPI_PAGE
    ]
    assert stray == [], f"move these under docs/site/assets or convert them to .md pages: {stray}"


def test_the_number_of_not_tested_blocks_does_not_rise() -> None:
    """A block marked "not tested" is a block that can rot, so the count has a ceiling."""
    problems, not_tested = check(ROOT, SITE)
    assert problems == []
    assert not_tested <= NOT_TESTED_CEILING, (
        f"{not_tested} blocks are marked as not tested, above the ceiling of "
        f"{NOT_TESTED_CEILING}. Test the new block (sync it from docs/examples or make it "
        "parse); if it truly cannot be tested, raise NOT_TESTED_CEILING in "
        "scripts/tests/test_docs_site.py in the same pull request and say why."
    )


def test_the_number_of_unchecked_output_blocks_does_not_rise() -> None:
    """An output block no example prints can rot, so the count has a ceiling.

    Tie a block to the region of an example that prints it with
    `<!-- output: EXAMPLE#REGION -->` (see "The documentation site" in docs/agent/testing.md);
    lower the ceiling in the same pull request. A block shown only in part, or for a case
    no example runs, stays plain `<!-- output -->`.
    """
    counts: dict[str, int] = {}
    problems, _ = check(ROOT, SITE, counts)
    assert problems == []
    assert counts["unchecked"] <= UNCHECKED_OUTPUT_CEILING, (
        f"{counts['unchecked']} output blocks are not tied to an example, above the ceiling of "
        f"{UNCHECKED_OUTPUT_CEILING}. Put `<!-- output: EXAMPLE#REGION -->` above the new "
        "block, naming the region of docs/examples/EXAMPLE/run.sh that prints it; if no "
        "example can, raise UNCHECKED_OUTPUT_CEILING in scripts/tests/test_docs_site.py in the "
        "same pull request and say why."
    )
    assert counts["unchecked"] == UNCHECKED_OUTPUT_CEILING, (
        f"{counts['unchecked']} output blocks are not tied to an example; lower "
        f"UNCHECKED_OUTPUT_CEILING from {UNCHECKED_OUTPUT_CEILING} to that number."
    )


def test_no_block_is_marked_as_moved_as_is() -> None:
    """The placeholder reason of the first sorting pass is gone: every reason is a real one."""
    left = [
        f"{page.relative_to(ROOT).as_posix()}:{block.line}"
        for page in sorted(SITE.rglob("*.md"))
        for block in find_blocks(page)
        if block.marker is not None
        and block.marker.kind == "not-tested"
        and "moved as is" in block.marker.value
    ]
    assert left == [], f"give each of these blocks a real reason, or sync or parse it: {left}"


def test_the_front_matter_in_a_sql_block_is_valid() -> None:
    """A SQL block of an asset page that opens with `/* lakana` carries a header Lakana accepts.

    The block checker does not parse SQL, so this test hands the header to the parser that
    reads the front matter of a real asset. A header with an unknown key or a bad value fails.
    """
    from lakana_core.projects.frontmatter import parse_front_matter

    seen = 0
    for name in (
        "reference/asset-front-matter.md",
        "reference/lakana-yaml.md",
        "how-to/projects.md",
    ):
        page = SITE / name
        for block in find_blocks(page):
            if block.lang == "sql" and block.body.startswith("/* lakana"):
                parse_front_matter(block.body, path=f"{page.name}:{block.line}")
                seen += 1
    assert seen >= 4, "expected the front-matter examples of the asset pages to be found"


MILESTONE = re.compile(r"\bM[0-9]+(?:[a-z][0-9]*)*\b")
MILESTONE_PAGES = ("explanation/scope.md", "explanation/roadmap.md")
"""The pages that define the milestones; every other page says it in plain words."""


def test_a_milestone_name_appears_only_on_the_pages_that_define_it() -> None:
    """A reader does not know "M5c1" or "M7b"; say "in a later release" or what the work is."""
    found = [
        f"{page.relative_to(SITE).as_posix()}:{number}: {match.group(0)}"
        for page in sorted(SITE.rglob("*.md"))
        if page.relative_to(SITE).as_posix() not in MILESTONE_PAGES
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        for match in MILESTONE.finditer(line)
    ]
    assert found == [], (
        "replace each milestone name with plain words (the release that brought the change, or "
        f"'in a later release'); only {', '.join(MILESTONE_PAGES)} may use them: {found}"
    )


def test_the_milestone_pattern_matches_the_names_it_is_meant_to_catch() -> None:
    """M5c1, M7b, M10 and M5c2a match; a word that merely starts with M does not."""
    for name in ("M5c1", "M7b", "M10", "M5c2a", "M8"):
        assert MILESTONE.fullmatch(name), name
    for text in ("Mb", "MB", "M5MB", "Memory", "M"):
        assert MILESTONE.search(text) is None, text
