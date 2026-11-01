"""The deploy diff against the current project version."""

from __future__ import annotations

from dataclasses import replace

from test_projects_dag import make_asset

from lakana_core.projects.asset_model import ResolvedReference
from lakana_core.projects.dag import build_graph
from lakana_core.projects.diff import DeployDiff, diff_manifests
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.manifest import build_manifest
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import ResolvedProjectConfig


def _manifest(*assets: ParsedAsset) -> dict:
    """Build a manifest from assets.

    Parameters
    ----------
    *assets
        The assets.

    Returns
    -------
    dict
        The manifest.
    """
    return build_manifest(list(assets), build_graph(list(assets)), ResolvedProjectConfig())


def test_first_deploy_adds_or_adopts() -> None:
    """With no previous project version, a table that exists unowned is adopted, the rest added."""
    current = _manifest(make_asset("s.new"), make_asset("s.old"))
    diff = diff_manifests(None, current, existing_unowned={"s.old"})
    assert diff.added == ("s.new",)
    assert diff.adopted == ("s.old",)
    assert diff.removed == ()


def test_each_change_category() -> None:
    """Removed, materialization, SQL and metadata changes are reported separately."""
    previous = _manifest(
        make_asset("s.gone"),
        make_asset("s.mat"),
        make_asset("s.sql", ("bronze.a",)),
        make_asset("s.meta"),
    )
    current = _manifest(
        make_asset("s.mat", metadata=AssetMetadata(materialize="view")),
        make_asset("s.sql", ("bronze.b",)),
        make_asset("s.meta", metadata=AssetMetadata(materialize="table", owner="new")),
    )
    diff = diff_manifests(previous, current, existing_unowned=set())
    assert diff.removed == ("s.gone",)
    assert diff.materialization_changed == ("s.mat",)
    assert diff.sql_changed == ("s.sql",)
    assert diff.metadata_changed == ("s.meta",)
    assert diff.added == ()
    assert diff.counts() == {
        "added": 0,
        "removed": 1,
        "adopted": 0,
        "materialization_changed": 1,
        "sql_changed": 1,
        "metadata_changed": 1,
        "model_changed": 0,
        "jobs_added": 0,
        "jobs_removed": 0,
        "jobs_changed": 0,
    }


def test_formatting_only_change_is_no_diff() -> None:
    """Normalized SQL decides: a reformatted query is unchanged."""
    first = make_asset("s.t")
    second = replace(first, sql="select a\nfrom bronze.src -- note")
    diff = diff_manifests(_manifest(first), _manifest(second), existing_unowned=set())
    assert diff == DeployDiff()


def test_json_round_trip() -> None:
    """`diff_json` stores lists and loads back to the same diff."""
    diff = DeployDiff(added=("a.b",), removed=("c.d",))
    assert diff.to_json()["added"] == ["a.b"]
    assert DeployDiff.from_json(diff.to_json()) == diff


def test_adopted_with_a_previous_manifest() -> None:
    """A new asset of a later deploy is adopted when its table exists unowned."""
    previous = _manifest(make_asset("s.kept"))
    current = _manifest(make_asset("s.kept"), make_asset("s.existing"), make_asset("s.fresh"))
    diff = diff_manifests(previous, current, existing_unowned={"s.existing"})
    assert diff.adopted == ("s.existing",)
    assert diff.added == ("s.fresh",)
    assert diff.removed == ()


def test_incremental_key_alone_is_a_materialization_change() -> None:
    """Changing only the watermark column is reported, with nothing else changing."""
    before = make_asset(
        "s.inc", metadata=AssetMetadata(materialize="incremental", incremental_key="ts")
    )
    after = make_asset(
        "s.inc", metadata=AssetMetadata(materialize="incremental", incremental_key="updated_at")
    )
    diff = diff_manifests(_manifest(before), _manifest(after), existing_unowned=set())
    assert diff.materialization_changed == ("s.inc",)
    assert diff.sql_changed == () and diff.metadata_changed == ()


def test_evolve_schema_alone_is_a_materialization_change() -> None:
    """Flipping only `evolve_schema` is reported, with nothing else changing."""
    before = make_asset(
        "s.inc",
        metadata=AssetMetadata(
            materialize="incremental", incremental_key="ts", evolve_schema=False
        ),
    )
    after = make_asset(
        "s.inc",
        metadata=AssetMetadata(materialize="incremental", incremental_key="ts", evolve_schema=True),
    )
    diff = diff_manifests(_manifest(before), _manifest(after), existing_unowned=set())
    assert diff.materialization_changed == ("s.inc",)
    assert diff.sql_changed == () and diff.metadata_changed == ()


def test_a_changed_output_or_retention_is_a_materialization_change() -> None:
    def manifest(output: str, keep: int | None) -> dict[str, object]:
        return {
            "assets": [
                {
                    "table": "models.churn",
                    "kind": "python",
                    "materialize": "artifact" if output == "artifact" else "table",
                    "write_mode": "artifact" if output == "artifact" else "replace",
                    "incremental_key": None,
                    "evolve_schema": False,
                    "output": output,
                    "keep_versions": keep,
                    "normalized_sql": "python:sha256:x",
                    "metadata": {},
                }
            ]
        }

    for before, after in (
        (manifest("table", None), manifest("artifact", None)),
        (manifest("artifact", None), manifest("artifact", 2)),
    ):
        diff = diff_manifests(before, after, existing_unowned=set())
        assert diff.materialization_changed == ("models.churn",)
    same = diff_manifests(manifest("artifact", 2), manifest("artifact", 2), existing_unowned=set())
    assert same.materialization_changed == ()


def test_a_version_2_entry_against_a_version_3_table_entry_is_no_change() -> None:
    """An asset deployed by an older release has no `output`; the same asset now has `table`."""
    old = {
        "table": "gold.t",
        "kind": "python",
        "materialize": "table",
        "write_mode": "replace",
        "incremental_key": None,
        "evolve_schema": False,
        "normalized_sql": "python:sha256:x",
        "metadata": {},
    }
    new = {**old, "output": "table", "keep_versions": None}
    diff = diff_manifests({"assets": [old]}, {"assets": [new]}, existing_unowned=set())
    assert diff.materialization_changed == ()


def test_a_previous_entry_without_sql_or_metadata_does_not_break_the_diff() -> None:
    """m5: a damaged stored manifest of the same repository must never fail a deploy."""
    current = _manifest(make_asset("s.t"))
    diff = diff_manifests({"assets": [{"table": "s.t"}]}, current, existing_unowned=set())
    assert diff.sql_changed == ("s.t",)
    assert diff.metadata_changed == ("s.t",)
    assert (diff.added, diff.removed) == ((), ())


def _python_entry(**extra: object) -> dict[str, object]:
    """Build a Python table entry of the current manifest version.

    Parameters
    ----------
    **extra
        Keys to add.

    Returns
    -------
    dict
        The entry.
    """
    return {
        "table": "gold.t",
        "kind": "python",
        "materialize": "table",
        "write_mode": "replace",
        "incremental_key": None,
        "evolve_schema": False,
        "output": "table",
        "keep_versions": None,
        "normalized_sql": "python:sha256:x",
        "metadata": {},
        **extra,
    }


def test_a_changed_decimals_option_is_a_materialization_change() -> None:
    before, after = _python_entry(decimals="float"), _python_entry(decimals="exact")
    diff = diff_manifests({"assets": [before]}, {"assets": [after]}, existing_unowned=set())
    assert diff.materialization_changed == ("gold.t",)
    same = diff_manifests({"assets": [after]}, {"assets": [after]}, existing_unowned=set())
    assert same.materialization_changed == ()


def test_an_entry_without_decimals_reads_as_float_so_upgrading_alone_is_no_change() -> None:
    old = _python_entry()
    new = _python_entry(decimals="float")
    diff = diff_manifests({"assets": [old]}, {"assets": [new]}, existing_unowned=set())
    assert diff.materialization_changed == ()


def _jobs_manifest(jobs: dict[str, dict] | None) -> dict:
    manifest = _manifest(make_asset("s.a"))
    if jobs is not None:
        manifest["jobs"] = jobs
    return manifest


def _job(
    select: tuple[str, ...] = ("s.*",),
    cron: str | None = "0 6 * * *",
    tz: str = "UTC",
    *,
    upstream: bool = False,
    notify: dict | None = None,
) -> dict:
    return {
        "select": list(select),
        "upstream": upstream,
        "downstream": False,
        "schedule": None if cron is None else {"cron": cron, "timezone": tz},
        "notify": notify,
    }


def test_jobs_added_removed_and_changed_are_reported() -> None:
    previous = _jobs_manifest(
        {
            "gone": _job(),
            "same": _job(),
            "sel": _job(),
            "cron": _job(),
            "zone": _job(),
            "both": _job(),
            "closure": _job(),
            "unsched": _job(),
        }
    )
    current = _jobs_manifest(
        {
            "new": _job(),
            "same": _job(),
            "sel": _job(select=("t.*",)),
            "cron": _job(cron="0 7 * * *"),
            "zone": _job(tz="Europe/Berlin"),
            "both": _job(select=("t.*",), cron="1 1 * * *"),
            "closure": _job(upstream=True),
            "unsched": _job(cron=None),
        }
    )
    diff = diff_manifests(previous, current, existing_unowned=set())
    assert diff.jobs_added == ("new",)
    assert diff.jobs_removed == ("gone",)
    assert diff.jobs_changed == (
        "both (selection, cron)",
        "closure (selection)",
        "cron (cron)",
        "sel (selection)",
        "unsched (cron)",
        "zone (time zone)",
    )


def _notify(webhook: str = "alerts", fmt: str = "json") -> dict:
    return {"webhook": webhook, "format": fmt}


def test_notification_changes_are_named_per_job() -> None:
    previous = _jobs_manifest(
        {
            "same": _job(notify=_notify()),
            "gain": _job(),
            "lose": _job(notify=_notify()),
            "secret": _job(notify=_notify()),
            "format": _job(notify=_notify()),
            "old": _job(),  # a manifest of version 5 has no `notify` key at all
        }
    )
    del previous["jobs"]["old"]["notify"]
    current = _jobs_manifest(
        {
            "same": _job(notify=_notify()),
            "gain": _job(notify=_notify()),
            "lose": _job(),
            "secret": _job(notify=_notify("other")),
            "format": _job(notify=_notify(fmt="slack")),
            "old": _job(),
        }
    )
    diff = diff_manifests(previous, current, existing_unowned=set())
    assert diff.jobs_changed == (
        "format (notify)",
        "gain (notify)",
        "lose (notify)",
        "secret (notify)",
    )


def test_a_job_that_changes_its_notification_and_its_cron_names_both() -> None:
    previous = _jobs_manifest({"a": _job()})
    current = _jobs_manifest({"a": _job(cron="1 1 * * *", notify=_notify())})
    diff = diff_manifests(previous, current, existing_unowned=set())
    assert diff.jobs_changed == ("a (cron, notify)",)


def test_a_manifest_without_jobs_has_no_job_changes() -> None:
    diff = diff_manifests(_jobs_manifest(None), _jobs_manifest(None), existing_unowned=set())
    assert (diff.jobs_added, diff.jobs_removed, diff.jobs_changed) == ((), (), ())


def test_the_first_deploy_adds_its_jobs() -> None:
    diff = diff_manifests(None, _jobs_manifest({"b": _job(), "a": _job()}), existing_unowned=set())
    assert diff.jobs_added == ("a", "b")


def test_job_categories_round_trip_through_json_and_old_diffs_load() -> None:
    diff = DeployDiff(jobs_added=("a",), jobs_changed=("b (cron)",))
    assert DeployDiff.from_json(diff.to_json()) == diff
    assert diff.counts()["jobs_added"] == 1
    old = {"added": ["s.a"], "removed": []}
    assert DeployDiff.from_json(old).jobs_added == ()


def _written(write: str, key: tuple[str, ...] = (), watermark: str | None = None) -> ParsedAsset:
    """Build an asset with a write model.

    Parameters
    ----------
    write
        ``replace``, ``append`` or ``merge``.
    key
        The merge key.
    watermark
        The watermark column.

    Returns
    -------
    ParsedAsset
        The asset `s.customers`.
    """
    metadata = AssetMetadata(materialize="table", write=write, key=key, watermark=watermark)
    return make_asset("s.customers", metadata=metadata)


def test_a_changed_merge_key_is_a_materialization_change() -> None:
    before = _manifest(_written("merge", ("id",)))
    after = _manifest(_written("merge", ("id", "region")))
    diff = diff_manifests(before, after, existing_unowned=set())
    assert diff.materialization_changed == ("s.customers",)
    assert diff.sql_changed == () and diff.metadata_changed == ()
    assert diff.model_changed == ()


def test_a_changed_write_mode_is_a_materialization_change() -> None:
    before = _manifest(_written("append", watermark="ts"))
    after = _manifest(_written("merge", ("id",), watermark="ts"))
    diff = diff_manifests(before, after, existing_unowned=set())
    assert diff.materialization_changed == ("s.customers",)


def test_moving_to_the_new_spelling_or_upgrading_alone_is_no_change() -> None:
    """`materialize: incremental` and `write: append` with the same column mean the same."""
    old_spelling = make_asset(
        "s.customers", metadata=AssetMetadata(materialize="incremental", incremental_key="ts")
    )
    new_spelling = _written("append", watermark="ts")
    assert (
        diff_manifests(
            _manifest(old_spelling), _manifest(new_spelling), existing_unowned=set()
        ).materialization_changed
        == ()
    )
    stored = _manifest(old_spelling)
    for entry in stored["assets"]:
        for key in ("write", "key", "watermark"):
            del entry[key]  # what a release before write modes stored
    diff = diff_manifests(stored, _manifest(new_spelling), existing_unowned=set())
    assert diff.materialization_changed == ()


def test_hints_round_trip_and_are_not_counted_as_changes() -> None:
    diff = DeployDiff(hints=("s.customers: a hint",))
    assert DeployDiff.from_json(diff.to_json()) == diff
    assert diff.to_json()["hints"] == ["s.customers: a hint"]
    assert "hints" not in diff.counts()


def _model(**fields: object) -> ParsedAsset:
    """Build a fact asset with a model.

    Parameters
    ----------
    **fields
        Metadata fields over a fact keyed on `id`.

    Returns
    -------
    ParsedAsset
        The asset `g.fact`.
    """
    values: dict[str, object] = {
        "materialize": "table",
        "kind": "fact",
        "grain": "One row per order.",
        "key": ("id",),
        **fields,
    }
    return make_asset("g.fact", metadata=AssetMetadata.model_validate(values))


def test_a_changed_grain_or_reference_is_a_model_change_only() -> None:
    before = _manifest(_model())
    for after in (
        _manifest(_model(grain="One row per order line.")),
        build_manifest(
            [_model()],
            build_graph([_model()]),
            ResolvedProjectConfig(),
            references={"g.fact": (ResolvedReference("store_id", "g.dim", "id"),)},
        ),
        _manifest(_model(kind="wide")),
        _manifest(_model(checks={"key": "warn"})),
    ):
        diff = diff_manifests(before, after, existing_unowned=set())
        assert diff.model_changed == ("g.fact",)
        assert diff.materialization_changed == ()
        assert diff.metadata_changed == () and diff.sql_changed == ()
    assert diff_manifests(before, _manifest(_model()), existing_unowned=set()) == DeployDiff()


def test_a_changed_key_of_a_replace_asset_is_a_model_change() -> None:
    diff = diff_manifests(
        _manifest(_model()), _manifest(_model(key=("id", "line"))), existing_unowned=set()
    )
    assert diff.model_changed == ("g.fact",)
    assert diff.materialization_changed == ()


def test_model_changed_round_trips_and_old_diffs_load_without_it() -> None:
    diff = DeployDiff(model_changed=("g.fact",))
    assert DeployDiff.from_json(diff.to_json()) == diff
    assert diff.counts()["model_changed"] == 1
    assert DeployDiff.from_json({"added": []}).model_changed == ()


def test_a_merge_asset_deployed_before_models_is_no_model_change() -> None:
    """A version 8 merge entry has no `model`; the same asset now records one with only a key."""
    stored = _manifest(_written("merge", ("id",)))
    del stored["assets"][0]["model"]
    diff = diff_manifests(stored, _manifest(_written("merge", ("id",))), existing_unowned=set())
    assert diff == DeployDiff()


def test_a_history_assets_key_valid_from_and_close_missing_are_materialization() -> None:
    def history(key: tuple[str, ...], **fields: object) -> ParsedAsset:
        metadata = AssetMetadata(
            materialize="table",
            write="history",
            key=key,
            **fields,  # ty: ignore[invalid-argument-type]
        )
        return make_asset("s.customers", metadata=metadata)

    before = _manifest(history(("id",)))
    for after in (
        history(("id", "region")),
        history(("id",), valid_from="changed_at"),
        history(("id",), close_missing=True),
    ):
        diff = diff_manifests(before, _manifest(after), existing_unowned=set())
        assert diff.materialization_changed == ("s.customers",)
        assert diff.model_changed == ()
    same = diff_manifests(before, _manifest(history(("id",))), existing_unowned=set())
    assert same.materialization_changed == ()
