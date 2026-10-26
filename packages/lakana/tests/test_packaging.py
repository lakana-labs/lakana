"""Tests for `hatch_build.py`'s metadata hook."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

_HATCH_BUILD_PATH = Path(__file__).resolve().parents[1] / "hatch_build.py"


def _load_hatch_build() -> ModuleType:
    """Load `packages/lakana/hatch_build.py` as a standalone module.

    It sits outside the `lakana` package on purpose — hatchling's custom
    metadata hook loader (`[tool.hatch.metadata.hooks.custom]`) looks for
    `hatch_build.py` at the project root, not inside `src/` — so it's loaded
    here by file path rather than by package import.

    Returns
    -------
    ModuleType
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location("lakana_hatch_build", _HATCH_BUILD_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_server_extra_is_pinned_to_the_resolved_version() -> None:
    """The hook pins `lakana-server`/`lakana-worker` to `metadata["version"]` exactly.

    This is a unit test of the hook class alone,
    not a full `uv build` -- `uv build --package lakana` and inspecting the
    built wheel's `METADATA` is exercised manually and documented in the
    fix report, since that path is slower and this test already proves the
    hook's own logic.
    """
    hatch_build = _load_hatch_build()
    hook = hatch_build.ServerExtraMetadataHook(root=str(_HATCH_BUILD_PATH.parent), config={})
    metadata = {"version": "1.2.3"}

    hook.update(metadata)

    assert metadata["optional-dependencies"] == {
        "server": ["lakana-server==1.2.3", "lakana-worker==1.2.3"],
        "assets": [
            "polars>=1.30",
            "fastexcel>=0.21",
            "obstore>=0.12,<0.13",
            "adbc-driver-manager>=1.13",
            "adbc-driver-postgresql>=1.13",
            "adbc-driver-sqlite>=1.13",
            "PyMySQL>=1.2.3",
        ],
    }


def test_the_compose_template_ships_in_the_wheel(tmp_path: Path) -> None:
    """`lakana bundle init` reads its template from package data."""
    subprocess.run(
        ["uv", "build", "--package", "lakana", "--wheel", "-o", str(tmp_path)], check=True
    )
    wheel = next(tmp_path.glob("lakana-*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        assert "lakana/cli/deploy/compose.yaml" in zf.namelist()


def test_lakana_requires_pyyaml() -> None:
    """M5b reads `lakana.yaml` in the CLI, so PyYAML is a runtime dependency of `lakana`."""
    from importlib.metadata import requires

    reqs = requires("lakana") or []
    assert any(r.split(";")[0].replace(" ", "").lower().startswith("pyyaml>=") for r in reqs), reqs
