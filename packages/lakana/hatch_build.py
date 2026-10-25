"""Hatchling metadata hook: pin `lakana[server]` to this exact release and add `lakana[assets]`.

hatch-vcs resolves every workspace package's version from the same git tag
(`raw-options.root = "../.."` in each package's `[tool.hatch.version]`), so
`lakana-server`/`lakana-worker` can be pinned to precisely this build's own
version with no hand-edited version anywhere (rule 3): hatchling resolves
`project.version` before any metadata hook's `update()` runs
(`hatchling.metadata.core.ProjectMetadata.core`), so `metadata["version"]`
below is already the real, resolved version.

The hook also declares the `assets` extra, which adds polars for projects with
Python assets (`lakana.runner` needs it; the SDK and CLI do not).
"""

from __future__ import annotations

from typing import Any

from hatchling.metadata.plugin.interface import MetadataHookInterface


class ServerExtraMetadataHook(MetadataHookInterface):
    """Sets `optional-dependencies.server` to this build's own version and adds `assets`."""

    def update(self, metadata: dict[str, Any]) -> None:
        """Pin the `server` extra's two packages to `metadata["version"]`; add `assets`.

        Parameters
        ----------
        metadata
            The `project` table, mutated in place.
        """
        version = metadata["version"]
        metadata["optional-dependencies"] = {
            "server": [f"lakana-server=={version}", f"lakana-worker=={version}"],
            "assets": ["polars>=1.30", "fastexcel>=0.21"],
        }
