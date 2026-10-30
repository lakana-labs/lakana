"""The project version manifest: everything M5c needs to run a commit, as JSON."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from lakana_core.connections.kinds import NAME
from lakana_core.identifiers import NOTIFY_FORMATS
from lakana_core.projects.asset_model import (
    CHECK_FAMILIES,
    CHECK_MODES,
    DEFAULT_DECIMALS,
    KEY_NONE,
    MAX_KEEP_VERSIONS,
    MODEL_KINDS,
    ResolvedReference,
    key_columns,
    normalized_write,
)
from lakana_core.projects.dag import ProjectGraph
from lakana_core.projects.measures import MeasureEntry, measure_entries, read_measure_entries
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import (
    SUPPORTED_TASK_PYTHONS,
    JobConfig,
    ResolvedProjectConfig,
)
from lakana_core.projects.run_policy import RunCaps, resolve_run_policy

if TYPE_CHECKING:
    from lakana_core.projects.checks import ParsedCheck
    from lakana_core.projects.frontmatter import AssetMetadata

MANIFEST_VERSION = 10
"""Bumped when the manifest's shape changes. Version 2 adds `kind` per asset and
`lock_sha256`; a version-1 manifest is read as all-SQL. Version 3 adds `output`,
`keep_versions` and `input_kinds` per Python asset; versions 1 and 2 are read as all-table.
Version 4 adds the `checks` list, and is written only for a project that has checks
(`VERSION_WITHOUT_CHECKS` otherwise), so that a worker of the previous release refuses a manifest
whose checks it would skip. Version 5 adds `jobs` and is written only for a project that
declares jobs, so that a worker of an earlier release refuses a manifest whose jobs it would
ignore; it then always carries a `checks` list, empty if need be, so that reading "version 5
without `checks`" stays a damaged manifest. A project with checks and no jobs stays version 4.
Version 6 is written only when a job declares `notify` in the environment the manifest is built
for, so that a worker of an earlier release refuses a manifest whose notifications it would
never send; any other project keeps its version.
Version 7 is written only when an asset declares connections (`CONNECTIONS_VERSION`), so that a
worker of an earlier release refuses it; every Python entry carries `connections`, empty if need
be, and a version 7 manifest always carries a `checks` list, as versions 5 and 6 do.
Version 8 is written only when an asset uses `write: append` or `merge` in the new spelling
(`WRITES_VERSION`), so that a worker of an earlier release refuses it instead of running a
merge or an append as a replace; it also carries a `checks` list. Every entry carries `write`,
`key` and `watermark` (read with `entry_write`), beside the older `materialize`, `write_mode`,
`first_write_mode` and `incremental_key`; an entry without them reads from those.
Version 9 is written only when an asset declares a model field (`kind`, `grain`, `key`,
`references` or `checks`; `MODEL_VERSION`), so that a worker of an earlier release refuses it; such
an entry carries `model` (read with `entry_model`), and the manifest a `checks` list.
Version 10 is written only when an asset declares measures (`MEASURES_VERSION`); such an entry's
`model` carries `measures`, and its `measures` check entry carries `not_over` and `aggregations`.
The `decimals` key is optional and read with a default (`entry_decimals`), so it needed
no bump: an entry without it means ``float``. The `package_indexes` key is optional in
the same way: a manifest without it uses no registered package index. The `python` key is
optional too: a manifest without it runs on the worker's Python, and a worker of an earlier
release ignores it and does the same."""

VERSION_WITHOUT_CHECKS = 3
"""The version of a manifest that has no `checks` list: a project without `checks/` is unchanged."""

CHECKS_VERSION = 4
"""The version of a manifest that has checks but no jobs."""

JOBS_VERSION = 5
"""The version of a manifest that has jobs and no notification."""

NOTIFY_VERSION = 6
"""The version of a manifest in which a job notifies."""

CONNECTIONS_VERSION = 7
"""The version of a manifest in which an asset declares connections and none uses the new write
spelling; a worker of an earlier release refuses it instead of running the asset without its
connections."""

WRITES_VERSION = 8
"""The version of a manifest in which an asset uses `write: append` or `merge` in the new
spelling (with or without connections); a worker of an earlier release refuses it instead of
running the asset as a replace."""

MODEL_VERSION = 9
"""The version of a manifest in which an asset declares a model field: `kind`, `grain`, `key`
(``none`` too), `references` or `checks`. `key:` alone counts, so every project with a merge asset
is version 9. Written only then, so that a worker of an earlier release refuses it instead of
running the project without the checks its model generates."""

MEASURES_VERSION = 10
"""The version of a manifest in which an asset declares measures; a worker of an earlier release
refuses it instead of reading its `measures` check as damaged."""

LISTS_CHECKS = (4, 5, 6, 7, 8, 9, 10)
"""The versions whose manifest always carries a `checks` list (version 4 a non-empty one). A
tuple, not a set: a stored version may be any JSON value, a list too, and must not raise."""

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

WRITE_MODES: dict[str, tuple[str, str]] = {
    "table": ("replace", "create"),
    "view": ("view", "view"),
    "incremental": ("append", "create"),
    "merge": ("merge", "create"),
}
"""Materialization (or ``merge``) → (write mode, write mode when the table does not exist yet).

An incremental asset's first run creates its table with every row its SELECT returns;
the runs after it append the rows past the watermark. A merge asset's first run creates its
table too, with the same key checks as every later merge.
"""


def _policy(asset: ParsedAsset, caps: RunCaps) -> dict[str, int]:
    """Serialize an asset's resolved run policy.

    Parameters
    ----------
    asset
        The parsed asset.
    caps
        Supplies the default timeout.

    Returns
    -------
    dict
        ``retries``, ``retry_delay_seconds``, ``timeout_seconds``.
    """
    policy = resolve_run_policy(asset.metadata, caps)
    return {
        "retries": policy.retries,
        "retry_delay_seconds": policy.retry_delay_seconds,
        "timeout_seconds": policy.timeout_seconds,
    }


def declares_model(metadata: AssetMetadata) -> bool:
    """Say whether an asset declares a model field.

    Parameters
    ----------
    metadata
        The asset's merged metadata.

    Returns
    -------
    bool
        `True` for a `kind`, `grain`, `references`, `checks`, `measures`, or a `key` (columns
        or ``none``).
    """
    return bool(
        metadata.kind
        or metadata.grain
        or metadata.references
        or metadata.checks
        or metadata.measures
        or key_columns(metadata.key)
        or metadata.key == KEY_NONE
    )


def _entry(
    asset: ParsedAsset,
    caps: RunCaps,
    artifacts: frozenset[str],
    references: Sequence[ResolvedReference] = (),
) -> dict[str, Any]:
    """Serialize one asset.

    Parameters
    ----------
    asset
        The parsed asset.
    caps
        Supplies the default timeout.
    artifacts
        Names of every artifact the asset may read, for `input_kinds`.
    references
        The asset's resolved references, recorded in `model`.

    Returns
    -------
    dict
        The manifest entry.
    """
    write, key, watermark, _ = normalized_write(asset.metadata)
    materialize = asset.metadata.materialize or "table"
    if write == "append":
        materialize = "incremental"
    write_mode, first_write_mode = WRITE_MODES["merge" if write == "merge" else materialize]
    entry: dict[str, Any] = {
        "table": asset.table,
        "path": asset.path,
        "kind": asset.kind,
        "materialize": materialize,
        "write_mode": write_mode,
        "first_write_mode": first_write_mode,
        "incremental_key": watermark,
        "evolve_schema": bool(asset.metadata.evolve_schema),
        "write": write,
        "key": list(key),
        "watermark": watermark,
        **_policy(asset, caps),
        "metadata": {
            "description": asset.metadata.description,
            "owner": asset.metadata.owner,
            "tags": list(asset.metadata.tags),
            "columns": asset.metadata.column_descriptions(),
        },
        "sql": asset.sql,
        "normalized_sql": asset.normalized_sql,
        "references": list(asset.references),
        "columns": list(asset.columns) if asset.columns is not None else None,
        "lineage": asset.lineage,
    }
    if asset.metadata.label is not None:
        entry["metadata"]["label"] = asset.metadata.label
    labels = asset.metadata.column_labels()
    if labels:
        entry["metadata"]["column_labels"] = labels
    if asset.kind == "python":
        entry["inputs"] = dict(asset.inputs)
        entry["output"] = asset.output
        entry["keep_versions"] = asset.keep_versions if asset.output == "artifact" else None
        entry["decimals"] = asset.decimals
        entry["connections"] = list(asset.connections)
        entry["input_kinds"] = {
            parameter: "artifact" if table in artifacts else "table"
            for parameter, table in asset.inputs
        }
        if asset.output == "artifact":
            entry["materialize"] = entry["write_mode"] = entry["first_write_mode"] = "artifact"
    if declares_model(asset.metadata):
        model: dict[str, Any] = {
            "kind": asset.metadata.kind,
            "grain": asset.metadata.grain,
            "key_none": asset.metadata.key == KEY_NONE,
            "references": [
                {"column": r.column, "target": r.target, "target_column": r.target_column}
                for r in references
            ],
            "checks": dict(sorted(asset.metadata.checks.items())),
        }
        if asset.metadata.measures:
            model["measures"] = measure_entries(asset.metadata.measures)
        entry["model"] = model
    return entry


def _unset_fields(job: JobConfig) -> set[str]:
    """Name the job fields a manifest entry leaves out.

    Parameters
    ----------
    job
        The job.

    Returns
    -------
    set of str
        ``notify`` when the job has none, so that a manifest of a project without notifications
        keeps the shape of version 5, and ``on_deploy`` when it is off, so that a project that does
        not use it keeps its manifest as it was.
    """
    unset = set() if job.notify is not None else {"notify"}
    return unset if job.on_deploy else unset | {"on_deploy"}


def _version(
    config: ResolvedProjectConfig,
    *,
    has_checks: bool,
    uses_connections: bool,
    uses_new_writes: bool = False,
    uses_model: bool = False,
    uses_measures: bool = False,
) -> int:
    """Pick the lowest manifest version that carries everything the project declares.

    Parameters
    ----------
    config
        `lakana.yaml` resolved for the target environment.
    has_checks
        Whether the project has checks.
    uses_connections
        Whether an asset declares a connection.
    uses_new_writes
        Whether an asset spells `write: append` or `merge`.
    uses_model
        Whether an asset declares a model field.
    uses_measures
        Whether an asset declares measures.

    Returns
    -------
    int
        `MEASURES_VERSION` when an asset declares measures, else `MODEL_VERSION` when an asset
        declares a model field, else `WRITES_VERSION` when an
        asset uses the new write spelling, else `CONNECTIONS_VERSION`
        when an asset declares a connection, `NOTIFY_VERSION` when a job notifies,
        `JOBS_VERSION` with jobs, `CHECKS_VERSION` with checks, `VERSION_WITHOUT_CHECKS`
        otherwise.
    """
    if uses_measures:
        return MEASURES_VERSION
    if uses_model:
        return MODEL_VERSION
    if uses_new_writes:
        return WRITES_VERSION
    if uses_connections:
        return CONNECTIONS_VERSION
    if any(job.notify is not None for job in config.jobs.values()):
        return NOTIFY_VERSION
    if config.jobs:
        return JOBS_VERSION
    return CHECKS_VERSION if has_checks else VERSION_WITHOUT_CHECKS


def _check_entry(check: ParsedCheck) -> dict[str, Any]:
    """Write one check's manifest entry.

    Parameters
    ----------
    check
        The validated check, hand-written or generated.

    Returns
    -------
    dict
        Its entry. A generated check also has ``generated``, the header field it comes from;
        a hand-written one keeps the shape older readers know.
    """
    entry: dict[str, Any] = {
        "name": check.name,
        "table": check.table,
        "check": check.check,
        "path": check.path,
        "on_failure": check.on_failure,
        "description": check.description,
        "sql": check.sql,
        "references": list(check.references),
    }
    if check.generated is not None:
        entry["generated"] = {
            "family": check.generated.family,
            "columns": list(check.generated.columns),
            "target": check.generated.target,
            "target_column": check.generated.target_column,
        }
        if check.generated.family == "measures":
            entry["generated"]["not_over"] = list(check.generated.not_over)
            entry["generated"]["aggregations"] = list(check.generated.aggregations)
    return entry


def build_manifest(
    assets: Sequence[ParsedAsset],
    graph: ProjectGraph,
    config: ResolvedProjectConfig,
    *,
    caps: RunCaps | None = None,
    lock_sha256: str | None = None,
    artifacts: frozenset[str] = frozenset(),
    package_indexes: Sequence[str] = (),
    checks: Sequence[ParsedCheck] = (),
    python: str | None = None,
    references: Mapping[str, Sequence[ResolvedReference]] | None = None,
) -> dict[str, Any]:
    """Build the manifest stored on a project version.

    Parameters
    ----------
    assets
        The validated assets.
    graph
        Their DAG.
    config
        `lakana.yaml` resolved for the target environment.
    caps
        Operator maximums; `RunCaps()` when `None`.
    lock_sha256
        SHA-256 of `uv.lock`; given exactly when a Python asset exists.
    artifacts
        Inputs from the catalog that are artifacts; the project's own artifact
        assets are added to it.
    package_indexes
        Names of the registered package indexes the lock takes packages from;
        recorded so that a project version shows what it depends on.
    checks
        The validated checks; the manifest has a `checks` list, sorted by name, and version
        `CHECKS_VERSION` (`JOBS_VERSION` with jobs), only when there are any. A manifest with
        jobs, connections, the new write spelling or a model carries the list even when it is
        empty.
    python
        `lakana.yaml`'s `python:`; recorded as `python` only when set, so a project without it
        keeps its manifest.
    references
        Table → its resolved references, recorded in the `model` of an asset that declares a
        model field; `None` when there are none.

    Returns
    -------
    dict
        JSON-serializable manifest, assets sorted by table.
    """
    caps = caps or RunCaps()
    resolved = references or {}
    known = artifacts | {asset.table for asset in assets if asset.output == "artifact"}
    version = _version(
        config,
        has_checks=bool(checks),
        uses_connections=any(asset.connections for asset in assets),
        uses_new_writes=any(asset.metadata.write in ("append", "merge") for asset in assets),
        uses_model=any(declares_model(asset.metadata) for asset in assets),
        uses_measures=any(asset.metadata.measures for asset in assets),
    )
    manifest: dict[str, Any] = {
        "version": version,
        "assets": [
            _entry(asset, caps, known, resolved.get(asset.table, ()))
            for asset in sorted(assets, key=lambda a: a.table)
        ],
        "edges": [list(edge) for edge in graph.edges],
        "external_inputs": list(graph.external_inputs),
        "config": config.model_dump(mode="json", exclude={"jobs"}),
    }
    if config.jobs:
        manifest["jobs"] = {
            # `notify` and `on_deploy` only when set, so a project without them keeps its shape
            name: job.model_dump(mode="json", exclude=_unset_fields(job))
            for name, job in sorted(config.jobs.items())
        }
    if lock_sha256 is not None:
        manifest["lock_sha256"] = lock_sha256
    if python is not None:
        manifest["python"] = python
    if package_indexes:
        manifest["package_indexes"] = sorted(package_indexes)
    lists_empty = (CONNECTIONS_VERSION, WRITES_VERSION, MODEL_VERSION, MEASURES_VERSION)
    if checks or config.jobs or version in lists_empty:
        manifest["checks"] = [_check_entry(check) for check in sorted(checks, key=lambda c: c.name)]
    return manifest


logger = logging.getLogger(__name__)


def manifest_assets(manifest: object) -> dict[str, dict[str, Any]]:
    """Index a manifest's assets by table name.

    A stored manifest is untrusted: another repository's damaged one must never
    break this deploy, so whatever is not a well-formed entry is skipped; one
    WARNING per call says how many were.

    Parameters
    ----------
    manifest
        A manifest.

    Returns
    -------
    dict
        Table → entry, for each entry that is a mapping with a string `table`;
        empty when the manifest is not a mapping or its `assets` is not a list.
    """
    assets = manifest.get("assets") if isinstance(manifest, Mapping) else None
    entries = assets if isinstance(assets, list) else []
    well_formed = [
        entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("table"), str)
    ]
    if len(well_formed) < len(entries):
        logger.warning(
            "A stored deploy manifest has %d malformed asset entries, which are skipped; deploy "
            "the repository again to replace it.",
            len(entries) - len(well_formed),
        )
    return {entry["table"]: entry for entry in well_formed}


def manifest_jobs(manifest: object) -> dict[str, dict[str, Any]]:
    """Index a manifest's jobs by name.

    Parameters
    ----------
    manifest
        A manifest; untrusted, like a stored one.

    Returns
    -------
    dict
        Name → entry, for each entry that is a mapping under a string name; empty for a manifest
        from before jobs or one whose `jobs` is not a mapping.
    """
    jobs = manifest.get("jobs") if isinstance(manifest, Mapping) else None
    if not isinstance(jobs, dict):
        return {}
    return {
        name: job for name, job in jobs.items() if isinstance(name, str) and isinstance(job, dict)
    }


def job_schedule(entry: Mapping[str, Any]) -> tuple[str, str] | None:
    """Read the cron expression and zone of a manifest's job entry.

    Parameters
    ----------
    entry
        The job's entry.

    Returns
    -------
    tuple of (str, str) or None
        The expression and the zone (``UTC`` when the zone is missing or not a string); `None`
        for a job without a schedule.
    """
    schedule = entry.get("schedule")
    if isinstance(schedule, dict) and isinstance(schedule.get("cron"), str):
        zone = schedule.get("timezone")
        return schedule["cron"], zone if isinstance(zone, str) else "UTC"
    return None


def job_on_deploy(entry: Mapping[str, Any]) -> bool:
    """Say whether a manifest's job entry starts after each deploy.

    Parameters
    ----------
    entry
        The job's entry; untrusted, like a stored one.

    Returns
    -------
    bool
        `True` only for the boolean ``on_deploy: true``; anything else, a damaged value included,
        is off.
    """
    return entry.get("on_deploy") is True


@dataclass(frozen=True)
class JobNotify:
    """Where a job of a deployed manifest reports a failed or recovered run.

    Parameters
    ----------
    webhook
        The name of the secret of the environment that holds the URL.
    format
        ``json``, ``slack`` or ``teams``.
    """

    webhook: str
    format: str


def job_notify(manifest: object, job: str) -> JobNotify | None:
    """Read the notification of one job from a stored manifest.

    Parameters
    ----------
    manifest
        A manifest; untrusted, like a stored one.
    job
        The job's name.

    Returns
    -------
    JobNotify or None
        The notification; `None` for a job that is not in the manifest, has none, or has one
        that is damaged (a webhook that is not text, or a format Lakana does not send).
    """
    entry = manifest_jobs(manifest).get(job)
    raw = entry.get("notify") if entry is not None else None
    if not isinstance(raw, dict):
        return None
    webhook, notify_format = raw.get("webhook"), raw.get("format")
    if not isinstance(webhook, str) or notify_format not in NOTIFY_FORMATS:
        return None
    return JobNotify(webhook, cast("str", notify_format))


def manifest_tables(manifest: object) -> set[str]:
    """List the tables a manifest produces.

    Parameters
    ----------
    manifest
        A manifest.

    Returns
    -------
    set of str
        Dotted table names.
    """
    return set(manifest_assets(manifest))


def manifest_artifacts(manifest: object) -> set[str]:
    """List the artifacts a manifest produces.

    Parameters
    ----------
    manifest
        A manifest; untrusted.

    Returns
    -------
    set of str
        Dotted names of its artifact assets; empty for a manifest from an older release.
    """
    return {
        table
        for table, entry in manifest_assets(manifest).items()
        if entry_output(entry) == "artifact"
    }


def asset_kind(entry: object) -> str:
    """Read an asset's kind from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; anything that is not a mapping
        reads as ``sql``.

    Returns
    -------
    str
        ``python`` for a Python asset; ``sql`` otherwise, including for an
        entry deployed by an older release, which has no `kind`.
    """
    return "python" if isinstance(entry, Mapping) and entry.get("kind") == "python" else "sql"


def entry_inputs(entry: object) -> dict[str, str]:
    """Read a Python asset's declared inputs from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; anything that is not a mapping
        reads as having none.

    Returns
    -------
    dict
        Parameter to table; empty when the entry has none or they are malformed.
    """
    raw = entry.get("inputs") if isinstance(entry, Mapping) else None
    if not isinstance(raw, dict):
        return {}
    inputs: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return {}
        inputs[key] = value
    return inputs


def python_entry(manifest: object, asset: str) -> Mapping[str, object] | None:
    """Find a Python asset's entry in a stored manifest.

    Parameters
    ----------
    manifest
        the `manifest_json` of a project version; untrusted.
    asset
        The dotted table name.

    Returns
    -------
    Mapping or None
        The first entry whose `table` is `asset`, if it is a mapping of kind
        ``python``; `None` for no such entry, a malformed manifest or an
        entry of another kind.
    """
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    for entry in assets if isinstance(assets, list) else []:
        if isinstance(entry, dict) and entry.get("table") == asset:
            return entry if entry.get("kind") == "python" else None
    return None


def entry_output(entry: object) -> str:
    """Read what an asset produces from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; untrusted.

    Returns
    -------
    str
        ``artifact`` for a Python asset declared with ``output="artifact"``;
        ``table`` otherwise, including for an entry deployed by an older release.
    """
    is_artifact = (
        asset_kind(entry) == "python"
        and isinstance(entry, Mapping)
        and entry.get("output") == "artifact"
    )
    return "artifact" if is_artifact else "table"


def entry_keep_versions(entry: object) -> int | None:
    """Read an artifact asset's retention from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; untrusted.

    Returns
    -------
    int or None
        How many live versions to keep; `None` (keep all) unless the entry is
        an artifact asset whose `keep_versions` is an integer from 1 to
        `MAX_KEEP_VERSIONS` (2**31 - 1). A larger value, like any other
        malformed one, reads as "keep all".
    """
    value = entry.get("keep_versions") if isinstance(entry, Mapping) else None
    if entry_output(entry) != "artifact" or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_KEEP_VERSIONS else None


def entry_decimals(entry: object) -> str:
    """Read how SQL decimals reach a Python asset from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; untrusted.

    Returns
    -------
    str
        ``exact`` for a Python entry that says so; ``float`` for every other
        entry, including one deployed before the option existed and one whose
        value is anything but ``exact``.
    """
    value = entry.get("decimals") if isinstance(entry, Mapping) else None
    return "exact" if asset_kind(entry) == "python" and value == "exact" else DEFAULT_DECIMALS


def entry_write(entry: object) -> tuple[str, tuple[str, ...], str | None]:
    """Read how a run writes an asset's table from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; untrusted.

    Returns
    -------
    tuple
        ``(write, key, watermark)``. An entry with a string `write` returns it as stored, also
        one that is not ``replace``, ``append`` or ``merge``, so that a reader refuses what it
        does not know rather than replace the table; `key` is the strings of a list of strings
        (empty otherwise) and `watermark` a string or `None`. An entry from before `write`
        reads ``("append", (), incremental_key)`` for ``materialize: incremental`` and
        ``("replace", (), None)`` otherwise.
    """
    if not isinstance(entry, Mapping):
        return "replace", (), None
    write = entry.get("write")
    if not isinstance(write, str):
        if entry.get("materialize") != "incremental":
            return "replace", (), None
        column = entry.get("incremental_key")
        return "append", (), column if isinstance(column, str) else None
    raw_key = entry.get("key")
    key = (
        tuple(raw_key)
        if isinstance(raw_key, list) and all(isinstance(column, str) for column in raw_key)
        else ()
    )
    watermark = entry.get("watermark")
    return write, key, watermark if isinstance(watermark, str) else None


@dataclass(frozen=True)
class EntryModel:
    """What a stored manifest entry says about the asset's model.

    Parameters
    ----------
    kind
        One of `MODEL_KINDS`, or `None`.
    grain
        The grain, or `None`.
    key
        The key columns (`entry_write`'s key).
    key_none
        Whether the asset declares `key: none`.
    references
        The resolved references.
    checks
        Check family → failure mode.
    measures
        The readable measures, sorted by column; empty for an entry without them.
    """

    kind: str | None
    grain: str | None
    key: tuple[str, ...]
    key_none: bool
    references: tuple[ResolvedReference, ...]
    checks: dict[str, str]
    measures: tuple[MeasureEntry, ...] = ()


def entry_model(entry: object) -> EntryModel | None:
    """Read an asset's model from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; untrusted.

    Returns
    -------
    EntryModel or None
        `None` for an entry without a `model` mapping. Otherwise each value that is not what a
        deploy writes reads as absent: a `kind` outside `MODEL_KINDS`, a `grain` that is not a
        string, a `key_none` other than `true`, a reference that is not a mapping of three
        strings, a check family or mode Lakana does not know, and a measure
        `read_measure_entries` cannot read.
    """
    raw = entry.get("model") if isinstance(entry, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    kind, grain = raw.get("kind"), raw.get("grain")
    items = raw.get("references")
    references = tuple(
        ResolvedReference(item["column"], item["target"], item["target_column"])
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, Mapping)
        and all(isinstance(item.get(k), str) for k in ("column", "target", "target_column"))
    )
    checks = raw.get("checks")
    return EntryModel(
        kind=kind if isinstance(kind, str) and kind in MODEL_KINDS else None,
        grain=grain if isinstance(grain, str) else None,
        key=entry_write(entry)[1],
        key_none=raw.get("key_none") is True,
        references=references,
        checks={
            family: mode
            for family, mode in (checks.items() if isinstance(checks, Mapping) else ())
            if family in CHECK_FAMILIES and isinstance(mode, str) and mode in CHECK_MODES
        },
        measures=read_measure_entries(raw.get("measures")),
    )


@dataclass(frozen=True)
class EntryConnections:
    """What a stored manifest entry says about connections.

    Parameters
    ----------
    names
        Every distinct string the entry names as a connection, in order; for a malformed value
        too (a bare string counts as one name), so that a reader that must fail safe can treat
        them as declared.
    readable
        Whether the value is what a deploy writes: absent, or a list of distinct valid names.
    """

    names: tuple[str, ...]
    readable: bool


def read_entry_connections(entry: object) -> EntryConnections:
    """Read the connections a Python asset declares from a stored manifest entry, failing safe.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; untrusted.

    Returns
    -------
    EntryConnections
        Nothing (and readable) for an entry that is not a Python asset or has no
        `connections`; otherwise the string names and whether the value is well-formed.
    """
    raw = (
        entry.get("connections")
        if asset_kind(entry) == "python" and isinstance(entry, Mapping)
        else None
    )
    if raw is None:
        return EntryConnections((), readable=True)
    if isinstance(raw, str):
        return EntryConnections((raw,), readable=False)
    if not isinstance(raw, list):
        return EntryConnections((), readable=False)
    names = tuple(dict.fromkeys(name for name in raw if isinstance(name, str)))
    readable = len(names) == len(raw) and all(NAME.match(name) for name in names)
    return EntryConnections(names, readable)


def entry_connections(entry: object) -> tuple[str, ...]:
    """Read the connections a Python asset declares from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; untrusted.

    Returns
    -------
    tuple of str
        The names; empty for an entry that is not a Python asset, one deployed before
        connections, and one whose value is not a list of distinct valid connection names
        (logged once per such entry, without its content). A reader that must fail safe uses
        `read_entry_connections`.
    """
    read = read_entry_connections(entry)
    if read.readable:
        return read.names
    logger.warning(
        "A stored deploy manifest has an asset entry whose connections cannot be read; it is "
        "read as declaring none. Deploy the repository again to replace it."
    )
    return ()


def entry_input_kinds(entry: object) -> dict[str, str]:
    """Read the kind of each declared input from a stored manifest entry.

    Parameters
    ----------
    entry
        One element of `manifest_json["assets"]`; untrusted.

    Returns
    -------
    dict
        Parameter to ``table`` or ``artifact``, for exactly the parameters
        of `entry_inputs`. Anything but the string ``artifact`` reads as
        ``table``.
    """
    raw = entry.get("input_kinds") if isinstance(entry, Mapping) else None
    kinds = raw if isinstance(raw, dict) else {}
    return {
        parameter: "artifact" if kinds.get(parameter) == "artifact" else "table"
        for parameter in entry_inputs(entry)
    }


def manifest_lock_sha256(manifest: Mapping[str, object]) -> str | None:
    """Read the lock digest from a stored manifest.

    Parameters
    ----------
    manifest
        A manifest.

    Returns
    -------
    str or None
        The digest, or `None` when absent or not a lower-case SHA-256 hex
        string (it becomes a directory name in the venv cache).
    """
    value = manifest.get("lock_sha256")
    return value if isinstance(value, str) and _SHA256.match(value) else None


def manifest_python(manifest: Mapping[str, object]) -> str | None:
    """Read the Python a project's tasks run on from a stored manifest.

    Parameters
    ----------
    manifest
        A manifest.

    Returns
    -------
    str or None
        One of `SUPPORTED_TASK_PYTHONS`; `None` when absent or anything else (no release writes
        another value), which means the worker's own Python.
    """
    value = manifest.get("python")
    return value if isinstance(value, str) and value in SUPPORTED_TASK_PYTHONS else None
