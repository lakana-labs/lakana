"""Tests that the offline install page names every container image the Compose bundle needs (#492).

`docker compose config --images` does not list the task images, because the launcher starts them
from `LAKANA_TASK_IMAGE`, so the page's list is the only one; these tests keep it equal to the
bundle's `compose.yaml`.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "packages" / "lakana" / "src" / "lakana" / "cli" / "deploy" / "compose.yaml"
PAGE = ROOT / "docs" / "site" / "how-to" / "install-without-internet-access.md"
SERVER = "ghcr.io/lakana-labs/lakana:<version>"
TASK = "ghcr.io/lakana-labs/lakana-task:<version>"


def _fixed_images() -> set[str]:
    """Return the images the bundle names literally, not through `LAKANA_IMAGE`.

    Returns
    -------
    set of str
        Every `image:` value of `compose.yaml` that is not a variable.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    return set(re.findall(r"^\s+image: ([^$\s]\S*)$", text, flags=re.MULTILINE))


def _task_pythons() -> list[str]:
    """Return the Pythons the bundle's launcher has a task image for.

    Returns
    -------
    list of str
        The keys of `LAKANA_LAUNCHER__IMAGES`, in file order.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    (line,) = [ln for ln in text.splitlines() if "LAKANA_LAUNCHER__IMAGES" in ln]
    return re.findall(r'"(3\.\d+)":', line)


def _expected() -> set[str]:
    """Return every image an offline Compose site must carry.

    Returns
    -------
    set of str
        The server image, one task image per Python and the fixed images.
    """
    return {SERVER, *(f"{TASK}-py{py}" for py in _task_pythons()), *_fixed_images()}


def test_the_bundle_names_postgres_seaweedfs_and_four_task_pythons() -> None:
    """The parsing above finds what the bundle holds, so the tests below compare something."""
    assert {image.split(":")[0] for image in _fixed_images()} == {"postgres", "chrislusf/seaweedfs"}
    assert _task_pythons() == ["3.11", "3.12", "3.13", "3.14"]


def test_the_pull_commands_fetch_every_image_the_bundle_needs() -> None:
    """The page pulls and saves the complete list, task images included."""
    text = PAGE.read_text(encoding="utf-8")
    pulled = set(re.findall(r"^docker pull (\S+)$", text, flags=re.MULTILINE))
    assert pulled == _expected()


def test_the_image_table_lists_every_image_the_bundle_needs() -> None:
    """Each image of the bundle has its row in the table of the page."""
    text = PAGE.read_text(encoding="utf-8")
    start = text.index("| Image | Used for |")
    table = text[start : text.index("\n\n", start)]
    named = set(re.findall(r"^\| `([^`]+)`", table, flags=re.MULTILINE))
    assert named == _expected()
