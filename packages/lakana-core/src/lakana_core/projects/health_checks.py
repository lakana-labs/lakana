"""The blocking checks that `health: {signal: block}` adds to a task's check phase (#340).

A health check runs no SQL: the worker reads whether the signal fired at the task's own snapshot
(`lakana_worker.task_checks._health_result`) and records the result like any generated check, so
a fired one sets the table back with the same records as a failed blocking check. Only a table
gets one: a view or an artifact has no snapshots to watch.

This module imports `projects.manifest`, which imports `projects.health`; so it is not part of
`projects.health`.
"""

from __future__ import annotations

from collections.abc import Sequence

from lakana_core.projects.checks import CheckSpec, GeneratedFrom
from lakana_core.projects.health import entry_effects
from lakana_core.projects.manifest import asset_health, manifest_assets
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import ResolvedProjectConfig

HEALTH_FAMILY = "health"
"""The `GeneratedFrom.family` of a health check."""

_PREFIX = "health_"
"""How the name of a health check starts, after ``<table>/``."""

_NO_SNAPSHOTS = frozenset({"view", "artifact"})
"""The `materialize` values of an entry that writes no table snapshots."""


def entry_health_specs(entry: object, asset: str) -> tuple[CheckSpec, ...]:
    """Build the blocking health checks of one manifest asset entry.

    Parameters
    ----------
    entry
        The asset's entry of a stored manifest; untrusted, `None` when there is none.
    asset
        The asset's table.

    Returns
    -------
    tuple of CheckSpec
        One per ``block`` entry of the asset's `health`, by name; empty for an asset without
        one, for a view or an artifact, and for ``freshness``, which never blocks.
    """
    if not isinstance(entry, dict) or entry.get("materialize") in _NO_SNAPSHOTS:
        return ()
    stored = entry.get("path")
    path = stored if isinstance(stored, str) else f"assets/{asset.replace('.', '/')}"
    specs = []
    for key, effect in entry_effects(entry).flat().items():
        signal, _, column = key.partition(".")
        if effect != "block" or signal == "freshness":
            continue
        check = f"{_PREFIX}{signal}__{column}" if column else f"{_PREFIX}{signal}"
        specs.append(
            CheckSpec(
                name=f"{asset}/{check}",
                table=asset,
                check=check,
                path=path,
                on_failure="block",
                description="generated from health:",
                sql="",
                generated=GeneratedFrom(HEALTH_FAMILY, (signal, column) if column else (signal,)),
            )
        )
    return tuple(sorted(specs, key=lambda spec: spec.name))


def health_specs(manifest: object, asset: str) -> tuple[CheckSpec, ...]:
    """Build the blocking health checks of one asset of a stored manifest.

    Parameters
    ----------
    manifest
        The run's manifest; untrusted.
    asset
        The asset's table.

    Returns
    -------
    tuple of CheckSpec
        What `entry_health_specs` gives for the asset's entry; empty without one.
    """
    return entry_health_specs(manifest_assets(manifest).get(asset), asset)


def project_health_specs(
    assets: Sequence[ParsedAsset], config: ResolvedProjectConfig
) -> list[CheckSpec]:
    """Build the blocking health checks a deploy's manifest will give its tables' runs.

    What `health_specs` gives each asset once the manifest stores its resolved `health:`
    (`manifest.asset_health`). The deploy refuses a hand-written check that takes one of these
    names (`model_checks.generated_name_issues`): both results would be recorded under one name,
    and the later would replace the earlier.

    Parameters
    ----------
    assets
        The parsed assets.
    config
        `lakana.yaml` resolved for the target environment.

    Returns
    -------
    list of CheckSpec
        Every asset's health checks, in the order of `assets`.
    """
    specs: list[CheckSpec] = []
    for asset in assets:
        effects = asset_health(asset, config)
        if effects:
            specs.extend(entry_health_specs({"path": asset.path, "health": effects}, asset.table))
    return specs


def is_health_check(check_name: str, *, generated: bool) -> bool:
    """Say whether a recorded check result is a health check's.

    Parameters
    ----------
    check_name
        The result's ``<table>/<check>``.
    generated
        Whether the result is of a generated check.

    Returns
    -------
    bool
        Whether it is generated and its name starts with ``health_``: a hand-written check
        may have such a name, a generated check of another family never has.
    """
    return generated and check_name.rpartition("/")[2].startswith(_PREFIX)
