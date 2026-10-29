"""The deploy diff: what a new manifest changes against the current one."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any

from lakana_core.projects.manifest import (
    EntryModel,
    entry_decimals,
    entry_keep_versions,
    entry_model,
    entry_output,
    entry_write,
    job_on_deploy,
    manifest_assets,
    manifest_jobs,
)

_SELECTION_KEYS = ("select", "upstream", "downstream")
_MATERIALIZATION_KEYS = ("materialize", "write_mode", "incremental_key", "evolve_schema")
_NO_MODEL = EntryModel(kind=None, grain=None, key=(), key_none=False, references=(), checks={})
"""The model of an entry whose only model field is a key."""


def _materialization(entry: dict[str, Any]) -> tuple[object, ...]:
    """Collect what decides how an asset is materialized.

    `output`, `keep_versions`, `decimals` and the write model (`write`, `key`, `watermark`) are
    read through the manifest readers, not with `get`: an entry deployed before manifest
    version 3 lacks the keys and reads as "table, keep all, float decimals", exactly like a
    version 3 table entry, and one from before `write` reads its write model from
    `materialize`, so upgrading the platform alone is not a change. A changed write mode, or the
    changed key of a merge, is a change of how the table is written; the key of any other write
    only identifies a row, a model change (`_model`).

    Parameters
    ----------
    entry
        One manifest entry.

    Returns
    -------
    tuple
        The values to compare.
    """
    write, key, watermark = entry_write(entry)
    return (
        *(entry.get(key) for key in _MATERIALIZATION_KEYS),
        entry_output(entry),
        entry_keep_versions(entry),
        entry_decimals(entry),
        (write, key if write == "merge" else (), watermark),
    )


def _model(entry: dict[str, Any]) -> tuple[object, ...]:
    """Collect what the asset declares about its model.

    Parameters
    ----------
    entry
        One manifest entry.

    Returns
    -------
    tuple
        The model as `entry_model` reads it, without its key (`None` when it declares nothing
        else, as for a merge asset deployed before models), and the key unless the asset merges
        on it (that is a materialization change).
    """
    model = entry_model(entry)
    declared = replace(model, key=()) if model is not None else None
    write, key, _ = entry_write(entry)
    return (None if declared == _NO_MODEL else declared, key if write != "merge" else ())


def _job_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Name what differs between two versions of a job.

    Parameters
    ----------
    before
        The job in the current manifest.
    after
        The job in the new manifest.

    Returns
    -------
    list of str
        ``selection`` (patterns or closure flags), ``cron`` (also a schedule added or removed)
        and ``time zone`` (when both versions have a schedule), ``notify`` (a notification
        added, removed or changed in its secret or format) and ``on deploy`` (the job starts after
        each deploy, or no longer does), for each that changed.
    """
    schedule_before = before.get("schedule") or {}
    schedule_after = after.get("schedule") or {}
    changes = []
    if tuple(before.get(key) for key in _SELECTION_KEYS) != tuple(
        after.get(key) for key in _SELECTION_KEYS
    ):
        changes.append("selection")
    if schedule_before.get("cron") != schedule_after.get("cron"):
        changes.append("cron")
    if (
        schedule_before
        and schedule_after
        and schedule_before.get("timezone") != schedule_after.get("timezone")
    ):
        changes.append("time zone")
    if before.get("notify") != after.get("notify"):
        changes.append("notify")
    if job_on_deploy(before) != job_on_deploy(after):
        changes.append("on deploy")
    return changes


@dataclass(frozen=True)
class DeployDiff:
    """Table names per change category, each sorted.

    Parameters
    ----------
    added
        New in this manifest, and no such table exists yet.
    removed
        In the current project version, not in this manifest — now orphaned.
    adopted
        New in this manifest; the table exists but no project version produces it.
    materialization_changed
        Materialization, write mode, the key of a merge, watermark or evolve flag changed.
    sql_changed
        Normalized SQL changed.
    metadata_changed
        Description, owner, tags or column docs changed.
    model_changed
        Kind, grain, references, the checks overrides, or the key of an asset that does not
        merge, changed.
    jobs_added
        Names of jobs this manifest declares and the current one does not.
    jobs_removed
        Names of jobs the current manifest declares and this one does not.
    jobs_changed
        Jobs in both whose selection, cron, time zone or notification changed, each as
        ``name (what, ...)``.
    hints
        Advice about how the new manifest's assets are written (an older spelling), each as
        ``table: text``; not a change, so not counted. The deploy adds them, not
        `diff_manifests`.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    adopted: tuple[str, ...] = ()
    materialization_changed: tuple[str, ...] = ()
    sql_changed: tuple[str, ...] = ()
    metadata_changed: tuple[str, ...] = ()
    model_changed: tuple[str, ...] = ()
    jobs_added: tuple[str, ...] = ()
    jobs_removed: tuple[str, ...] = ()
    jobs_changed: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()

    def to_json(self) -> dict[str, list[str]]:
        """Serialize for `deploy_runs.diff_json`.

        Returns
        -------
        dict
            Category → list of table names.
        """
        return {field.name: list(getattr(self, field.name)) for field in fields(self)}

    def counts(self) -> dict[str, int]:
        """Count each category, for the audit event.

        Returns
        -------
        dict
            Category → count; `hints` are advice, not a change, and are left out.
        """
        return {
            field.name: len(getattr(self, field.name))
            for field in fields(self)
            if field.name != "hints"
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DeployDiff:
        """Load a stored diff.

        Parameters
        ----------
        data
            `to_json`'s output.

        Returns
        -------
        DeployDiff
            The diff.
        """
        return cls(**{field.name: tuple(data.get(field.name, ())) for field in fields(cls)})


def diff_manifests(
    previous: dict[str, Any] | None, current: dict[str, Any], *, existing_unowned: set[str]
) -> DeployDiff:
    """Compare a new manifest with the environment's current one for the repository.

    Parameters
    ----------
    previous
        The current project version's manifest, or `None` on a first deploy.
    current
        The new manifest.
    existing_unowned
        Tables that exist in the environment while no current project version
        (of any repository) produces them.

    Returns
    -------
    DeployDiff
        Every category, sorted.
    """
    before = manifest_assets(previous) if previous is not None else {}
    after = manifest_assets(current)
    jobs_before = manifest_jobs(previous) if previous is not None else {}
    jobs_after = manifest_jobs(current)
    job_changes = {
        name: _job_changes(jobs_before[name], jobs_after[name])
        for name in sorted(set(jobs_before) & set(jobs_after))
    }
    new = sorted(set(after) - set(before))
    common = sorted(set(after) & set(before))
    return DeployDiff(
        added=tuple(t for t in new if t not in existing_unowned),
        removed=tuple(sorted(set(before) - set(after))),
        adopted=tuple(t for t in new if t in existing_unowned),
        materialization_changed=tuple(
            t for t in common if _materialization(before[t]) != _materialization(after[t])
        ),
        sql_changed=tuple(
            t for t in common if before[t].get("normalized_sql") != after[t].get("normalized_sql")
        ),
        metadata_changed=tuple(
            t for t in common if before[t].get("metadata") != after[t].get("metadata")
        ),
        model_changed=tuple(t for t in common if _model(before[t]) != _model(after[t])),
        jobs_added=tuple(sorted(set(jobs_after) - set(jobs_before))),
        jobs_removed=tuple(sorted(set(jobs_before) - set(jobs_after))),
        jobs_changed=tuple(
            f"{name} ({', '.join(what)})" for name, what in job_changes.items() if what
        ),
    )
