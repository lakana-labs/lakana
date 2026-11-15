"""Tests that CI proves the offline install with the container image and no network (#493).

The test itself needs Docker and a built image, so `compose-smoke` runs it; these tests keep the
script, the job that calls it and the page that says it is tested in step.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "offline_install_test.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PAGE = ROOT / "docs" / "site" / "how-to" / "install-without-internet-access.md"


def test_the_script_runs_the_image_without_a_network_and_without_the_extension_download() -> None:
    """The container has no network, the worker may not download, and a query runs."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--network none" in text
    assert "LAKANA_WORKER__EXTENSION_SOURCE=none" in text
    assert "lakana server start --local" in text
    assert "SELECT 42" in text


def test_the_script_is_valid_bash() -> None:
    """`bash -n` parses it."""
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_compose_smoke_runs_the_script_on_the_image_it_built() -> None:
    """The script is a step of `compose-smoke` and a change to it starts the job."""
    text = CI.read_text(encoding="utf-8")
    job = text[text.index("\n  compose-smoke:") : text.index("\n  budgets:")]
    assert "bash scripts/offline_install_test.sh lakana:ci" in job
    assert "offline_install_test" in re.search(r"grep -qE '([^']*)'", job).group(1)  # ty: ignore[possibly-missing-attribute]


def test_the_page_says_that_ci_tests_it() -> None:
    """The page names the script and no longer says no job cuts the network off."""
    text = PAGE.read_text(encoding="utf-8")
    assert "scripts/offline_install_test.sh" in text
    assert "We run no CI job with the network cut off" not in text
