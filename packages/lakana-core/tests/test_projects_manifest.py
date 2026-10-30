"""The manifest stored in `job_versions.manifest_json`."""

from __future__ import annotations

import dataclasses
import logging

import pytest
from test_projects_dag import make_asset

from lakana_core.projects.asset_model import ResolvedReference, deploy_hints
from lakana_core.projects.checks import GeneratedFrom, ParsedCheck, read_manifest_checks
from lakana_core.projects.dag import build_graph
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.manifest import (
    CHECKS_VERSION,
    CONNECTIONS_VERSION,
    JOBS_VERSION,
    MANIFEST_VERSION,
    MEASURES_VERSION,
    MODEL_VERSION,
    NOTIFY_VERSION,
    VERSION_WITHOUT_CHECKS,
    WRITES_VERSION,
    EntryConnections,
    EntryModel,
    JobNotify,
    asset_kind,
    build_manifest,
    entry_connections,
    entry_decimals,
    entry_input_kinds,
    entry_inputs,
    entry_keep_versions,
    entry_model,
    entry_output,
    entry_write,
    job_notify,
    manifest_artifacts,
    manifest_assets,
    manifest_jobs,
    manifest_lock_sha256,
    manifest_python,
    manifest_tables,
    python_entry,
    read_entry_connections,
)
from lakana_core.projects.model_checks import generated_checks
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import ResolvedProjectConfig, parse_project_config


def test_manifest_records_assets_edges_inputs_and_config() -> None:
    """Every asset with its write mode, the DAG, external inputs and the resolved config."""
    assets = [
        make_asset("bronze.raw", ("landing.events",), metadata=AssetMetadata(materialize="view")),
        make_asset(
            "silver.clean",
            ("bronze.raw",),
            metadata=AssetMetadata(materialize="incremental", incremental_key="ts", owner="me"),
        ),
        make_asset("gold.daily", ("silver.clean",)),
    ]
    config = ResolvedProjectConfig(repo="analytics", properties={"team": "x"})
    manifest = build_manifest(assets, build_graph(assets), config)
    assert manifest["version"] == VERSION_WITHOUT_CHECKS
    assert [a["table"] for a in manifest["assets"]] == ["bronze.raw", "gold.daily", "silver.clean"]
    entries = manifest_assets(manifest)
    assert (entries["bronze.raw"]["write_mode"], entries["bronze.raw"]["first_write_mode"]) == (
        "view",
        "view",
    )
    assert (entries["gold.daily"]["write_mode"], entries["gold.daily"]["first_write_mode"]) == (
        "replace",
        "create",
    )
    # An incremental asset's first run creates its table; later runs append.
    assert (entries["silver.clean"]["write_mode"], entries["silver.clean"]["first_write_mode"]) == (
        "append",
        "create",
    )
    assert entries["silver.clean"]["incremental_key"] == "ts"
    assert entries["silver.clean"]["metadata"]["owner"] == "me"
    assert manifest["edges"] == [["bronze.raw", "silver.clean"], ["silver.clean", "gold.daily"]]
    assert manifest["external_inputs"] == ["landing.events"]
    assert manifest["config"]["repo"] == "analytics"
    assert manifest["config"]["properties"] == {"team": "x"}
    assert manifest_tables(manifest) == {"bronze.raw", "silver.clean", "gold.daily"}


def test_manifest_records_each_assets_run_policy() -> None:
    """Retries, delay and timeout are resolved at deploy time, against the caps."""
    from lakana_core.projects.run_policy import RunCaps

    assets = [
        make_asset("gold.a", metadata=AssetMetadata(materialize="table", retries=1)),
        make_asset("gold.b"),
    ]
    manifest = build_manifest(
        assets,
        build_graph(assets),
        ResolvedProjectConfig(),
        caps=RunCaps(max_retries=5, max_timeout_seconds=300),
    )
    entries = manifest_assets(manifest)
    assert (
        entries["gold.a"]["retries"],
        entries["gold.a"]["retry_delay_seconds"],
        entries["gold.a"]["timeout_seconds"],
    ) == (1, 30, 300)
    assert entries["gold.b"]["retries"] == 2


def test_manifest_pins_every_field() -> None:
    """The whole shape M5c reads: a new or renamed key must change this test on purpose."""
    from lakana_core.projects.run_policy import RunCaps

    asset = make_asset(
        "silver.clean",
        ("bronze.raw",),
        metadata=AssetMetadata(
            materialize="incremental",
            incremental_key="ts",
            evolve_schema=True,
            description="d",
            owner="me",
            tags=("a", "b"),
            columns={"a": "the a"},
            retries=1,
            retry_delay=10,
            timeout=60,
        ),
    )
    config = ResolvedProjectConfig(repo="analytics")
    manifest = build_manifest([asset], build_graph([asset]), config, caps=RunCaps())
    assert manifest == {
        "version": VERSION_WITHOUT_CHECKS,
        "assets": [
            {
                "table": "silver.clean",
                "path": "assets/silver/clean.sql",
                "kind": "sql",
                "materialize": "incremental",
                "write_mode": "append",
                "first_write_mode": "create",
                "incremental_key": "ts",
                "evolve_schema": True,
                "write": "append",
                "key": [],
                "watermark": "ts",
                "retries": 1,
                "retry_delay_seconds": 10,
                "timeout_seconds": 60,
                "metadata": {
                    "description": "d",
                    "owner": "me",
                    "tags": ["a", "b"],
                    "columns": {"a": "the a"},
                },
                "sql": asset.sql,
                "normalized_sql": asset.normalized_sql,
                "references": ["bronze.raw"],
                "columns": ["a"],
                "lineage": {},
            }
        ],
        "edges": [],
        "external_inputs": ["bronze.raw"],
        "config": config.model_dump(mode="json", exclude={"jobs"}),
    }


def _python_asset() -> ParsedAsset:
    """Build a parsed Python asset.

    Returns
    -------
    ParsedAsset
        `gold.scores`, reading `silver.clean` as `obs`.
    """
    return ParsedAsset(
        table="gold.scores",
        path="assets/gold/scores.py",
        block_line=4,
        body_line=5,
        metadata=AssetMetadata(materialize="table", columns={"score": "0-1"}),
        sql="",
        normalized_sql="python:sha256:" + "a" * 64,
        references=("silver.clean",),
        columns=None,
        lineage={},
        kind="python",
        inputs=(("obs", "silver.clean"),),
    )


def test_a_python_entry_records_kind_inputs_and_the_lock() -> None:
    assets = [_python_asset()]
    manifest = build_manifest(
        assets, build_graph(assets), ResolvedProjectConfig(), lock_sha256="b" * 64
    )
    assert manifest["version"] == 3
    assert manifest["lock_sha256"] == "b" * 64
    (entry,) = manifest["assets"]
    assert entry["kind"] == "python"
    assert entry["inputs"] == {"obs": "silver.clean"}
    assert (entry["write_mode"], entry["first_write_mode"]) == ("replace", "create")
    assert entry["sql"] == ""
    assert entry["metadata"]["columns"] == {"score": "0-1"}
    assert asset_kind(entry) == "python"
    assert entry_inputs(entry) == {"obs": "silver.clean"}
    assert manifest_lock_sha256(manifest) == "b" * 64


def test_the_manifest_names_the_registered_indexes_the_lock_uses() -> None:
    assets = [_python_asset()]
    manifest = build_manifest(
        assets,
        build_graph(assets),
        ResolvedProjectConfig(),
        lock_sha256="b" * 64,
        package_indexes=["team", "alpha"],
    )
    assert manifest["version"] == 3
    assert manifest["package_indexes"] == ["alpha", "team"]
    plain = build_manifest(
        assets, build_graph(assets), ResolvedProjectConfig(), lock_sha256="b" * 64
    )
    assert "package_indexes" not in plain


def test_a_sql_only_manifest_has_no_lock_key() -> None:
    assets = [make_asset("silver.a")]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    assert "lock_sha256" not in manifest
    assert "inputs" not in manifest["assets"][0]
    assert manifest["assets"][0]["kind"] == "sql"
    assert manifest_lock_sha256(manifest) is None


def test_an_entry_without_kind_reads_as_sql() -> None:
    """A project version deployed by an older release."""
    assert asset_kind({"table": "silver.a", "sql": "SELECT 1"}) == "sql"
    assert asset_kind({"kind": "wasm"}) == "sql"
    assert asset_kind({"kind": 7}) == "sql"


@pytest.mark.parametrize(
    "inputs", [None, "x", ["a"], {"a": 1}, {1: "a.b"}, {"a": "a.b", "b": None}]
)
def test_malformed_inputs_read_back_as_empty(inputs: object) -> None:
    assert entry_inputs({"inputs": inputs}) == {}


@pytest.mark.parametrize("value", [None, 7, "short", "G" * 64, "A" * 64, "a" * 64 + "\n"])
def test_a_malformed_lock_digest_reads_back_as_none(value: object) -> None:
    assert manifest_lock_sha256({"lock_sha256": value}) is None


@pytest.mark.parametrize(
    ("entry", "output", "keep", "kinds"),
    [
        ({"kind": "python", "inputs": {"a": "x.a"}}, "table", None, {"a": "table"}),
        (
            {
                "kind": "python",
                "output": "artifact",
                "keep_versions": 3,
                "inputs": {"m": "models.m", "t": "gold.t"},
                "input_kinds": {"m": "artifact", "t": "table", "ghost": "artifact"},
            },
            "artifact",
            3,
            {"m": "artifact", "t": "table"},
        ),
        ({"kind": "sql", "output": "artifact", "keep_versions": 3}, "table", None, {}),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": ["artifact"]},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": "blob"}},
            "table",
            None,
            {"m": "table"},
        ),
        # Untrusted shapes beyond the above: each reads as the safe default.
        (None, "table", None, {}),
        ([], "table", None, {}),
        ("x", "table", None, {}),
        (3, "table", None, {}),
        ({"output": "artifact", "keep_versions": 3}, "table", None, {}),
        ({"kind": "Python", "output": "artifact"}, "table", None, {}),
        ({"kind": "python ", "output": "artifact"}, "table", None, {}),
        ({"kind": ["python"], "output": "artifact"}, "table", None, {}),
        ({"kind": "python", "output": "Artifact"}, "table", None, {}),
        ({"kind": "python", "output": "artifact "}, "table", None, {}),
        ({"kind": "python", "output": None}, "table", None, {}),
        ({"kind": "python", "output": True}, "table", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": 0}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": -1}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": True}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": 2.0}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": "2"}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": None}, "artifact", None, {}),
        ({"kind": "python", "output": "artifact", "keep_versions": 10**30}, "artifact", None, {}),
        (
            {"kind": "python", "output": "artifact", "keep_versions": 2**31 - 1},
            "artifact",
            2**31 - 1,
            {},
        ),
        (
            {"kind": "python", "output": "artifact", "keep_versions": 2**31},
            "artifact",
            None,
            {},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": "Artifact"}},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": " artifact"}},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": ["artifact"]}},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": {"m": True}},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": None},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": "models.m"}, "input_kinds": "artifact"},
            "table",
            None,
            {"m": "table"},
        ),
        (
            {"kind": "python", "inputs": {"m": 7}, "input_kinds": {"m": "artifact"}},
            "table",
            None,
            {},
        ),
        (
            {"kind": "python", "inputs": ["m"], "input_kinds": {"m": "artifact"}},
            "table",
            None,
            {},
        ),
    ],
)
def test_a_stored_entry_is_read_defensively(
    entry: object, output: str, keep: int | None, kinds: dict[str, str]
) -> None:
    assert entry_output(entry) == output
    assert entry_keep_versions(entry) == keep
    assert entry_input_kinds(entry) == kinds


def test_python_entry_reads_only_the_first_entry_of_a_name() -> None:
    sql = {"table": "m.c", "kind": "sql"}
    python = {"table": "m.c", "kind": "python", "output": "artifact"}
    assert python_entry({"assets": [sql, python]}, "m.c") is None
    assert python_entry({"assets": [python, sql]}, "m.c") == python
    assert python_entry({"assets": [3, python]}, "m.c") == python
    assert python_entry({"assets": [python]}, "m.other") is None
    for manifest in (None, [], "x", {}, {"assets": "x"}):
        assert python_entry(manifest, "m.c") is None


def _artifact_asset() -> ParsedAsset:
    """Build a parsed artifact asset.

    Returns
    -------
    ParsedAsset
        `models.churn`, reading `gold.scores` and `models.base`.
    """
    return dataclasses.replace(
        _python_asset(),
        table="models.churn",
        path="assets/models/churn.py",
        metadata=AssetMetadata(materialize="table"),
        references=("gold.scores", "models.base"),
        inputs=(("scores", "gold.scores"), ("base", "models.base")),
        output="artifact",
        keep_versions=2,
    )


def test_manifest_version_3_records_output_retention_and_input_kinds() -> None:
    consumer = dataclasses.replace(
        _python_asset(),
        table="gold.predictions",
        references=("models.churn", "silver.clean"),
        inputs=(("model", "models.churn"), ("rows", "silver.clean")),
    )
    assets = [_python_asset(), _artifact_asset(), consumer]
    manifest = build_manifest(
        assets,
        build_graph(assets),
        ResolvedProjectConfig(),
        artifacts=frozenset({"models.base"}),
    )
    assert manifest["version"] == 3
    entries = manifest_assets(manifest)
    churn = entries["models.churn"]
    assert (churn["output"], churn["keep_versions"]) == ("artifact", 2)
    assert (churn["materialize"], churn["write_mode"], churn["first_write_mode"]) == (
        "artifact",
        "artifact",
        "artifact",
    )
    assert churn["input_kinds"] == {"scores": "table", "base": "artifact"}
    assert entries["gold.predictions"]["input_kinds"] == {"model": "artifact", "rows": "table"}
    scores = entries["gold.scores"]
    assert (scores["output"], scores["keep_versions"], scores["input_kinds"]) == (
        "table",
        None,
        {"obs": "table"},
    )
    assert (scores["write_mode"], scores["first_write_mode"]) == ("replace", "create")
    assert manifest_artifacts(manifest) == {"models.churn"}
    assert manifest_tables(manifest) == {"gold.scores", "models.churn", "gold.predictions"}
    assert entry_output(churn) == "artifact"
    assert entry_keep_versions(churn) == 2
    assert entry_input_kinds(churn) == {"scores": "table", "base": "artifact"}
    assert ("models.churn", "gold.predictions") in [tuple(edge) for edge in manifest["edges"]]


def test_a_table_asset_never_records_retention() -> None:
    asset = dataclasses.replace(_python_asset(), keep_versions=4)
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["assets"][0]["keep_versions"] is None


def test_a_sql_entry_has_no_artifact_fields_and_older_manifests_have_no_artifacts() -> None:
    assets = [make_asset("silver.a", ("bronze.events",))]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    (entry,) = manifest["assets"]
    assert not {"output", "keep_versions", "input_kinds"} & set(entry)
    assert manifest_artifacts(manifest) == set()
    assert (
        manifest_artifacts({"version": 2, "assets": [{"table": "gold.t", "kind": "python"}]})
        == set()
    )
    assert manifest_artifacts({}) == set()
    assert manifest_artifacts({"assets": "x"}) == set()
    assert manifest_artifacts({"assets": [3, {"table": 4, "output": "artifact"}]}) == set()


@pytest.mark.parametrize(
    "damaged",
    [{"assets": [3]}, {"assets": 3}, {"assets": None}, {"assets": "x"}, {}, [1], None, 7],
)
def test_a_damaged_manifest_has_no_assets(damaged: object) -> None:
    """A stored manifest is untrusted: it indexes to nothing and never raises."""
    assert manifest_assets(damaged) == {}
    assert manifest_tables(damaged) == set()
    assert manifest_artifacts(damaged) == set()


def test_damaged_entries_are_skipped_and_the_rest_is_read() -> None:
    good = {"table": "m.a", "kind": "python", "output": "artifact"}
    manifest = {"assets": [3, good, {"kind": "python"}, {"table": 4}, "s", None, ["m.b"]]}
    assert manifest_assets(manifest) == {"m.a": good}
    assert manifest_tables(manifest) == {"m.a"}
    assert manifest_artifacts(manifest) == {"m.a"}


def test_skipped_entries_of_a_stored_manifest_are_counted_in_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """m5: a damaged entry is skipped, and a person can learn that it was, once per manifest."""
    manifest = {"assets": [3, "x", {"kind": "python"}, {"table": "a.b"}, None]}
    with caplog.at_level(logging.WARNING, logger="lakana_core.projects.manifest"):
        assert manifest_assets(manifest) == {"a.b": {"table": "a.b"}}
    (record,) = caplog.records
    assert record.getMessage() == (
        "A stored deploy manifest has 4 malformed asset entries, which are skipped; deploy the "
        "repository again to replace it."
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="lakana_core.projects.manifest"):
        assert manifest_assets({"assets": [{"table": "a.b"}]}) == {"a.b": {"table": "a.b"}}
        assert manifest_assets(None) == {}
    assert caplog.records == []


@pytest.mark.parametrize(
    ("entry", "decimals"),
    [
        ({"kind": "python", "decimals": "exact"}, "exact"),
        ({"kind": "python", "decimals": "float"}, "float"),
        # an entry deployed before the option existed means the default
        ({"kind": "python"}, "float"),
        ({"kind": "python", "decimals": None}, "float"),
        ({"kind": "python", "decimals": "EXACT"}, "float"),
        ({"kind": "python", "decimals": ["exact"]}, "float"),
        ({"kind": "python", "decimals": True}, "float"),
        ({"kind": "sql", "decimals": "exact"}, "float"),
        (None, "float"),
        ("exact", "float"),
    ],
)
def test_a_stored_entry_reads_decimals_defensively(entry: object, decimals: str) -> None:
    assert entry_decimals(entry) == decimals


@pytest.mark.parametrize("decimals", ["float", "exact"])
def test_a_python_entry_records_decimals_and_the_manifest_version_is_unchanged(
    decimals: str,
) -> None:
    """The key is optional and read with a default, so version 3 stays version 3."""
    asset = dataclasses.replace(_python_asset(), decimals=decimals)
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["version"] == VERSION_WITHOUT_CHECKS == 3
    assert MANIFEST_VERSION == 10
    assert manifest["assets"][0]["decimals"] == decimals


def test_a_sql_entry_has_no_decimals_key() -> None:
    assets = [make_asset("silver.a", ("bronze.events",))]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    assert "decimals" not in manifest["assets"][0]


_JOBS_YAML = """\
jobs:
  daily:
    select: ["silver.*"]
    upstream: true
    schedule: { cron: "0 6 * * *", timezone: Europe/Berlin }
  by_hand:
    select: ["gold.*"]
environments:
  dev:
    jobs:
      daily: { schedule: null }
"""


def _jobs_manifest(environment: str) -> dict:
    config = parse_project_config(_JOBS_YAML).resolved_for(environment)
    assets = [make_asset("silver.a"), make_asset("gold.b", ("silver.a",))]
    return build_manifest(assets, build_graph(assets), config)


def test_a_manifest_with_jobs_and_no_notify_is_version_5_and_resolved_per_environment() -> None:
    prod = _jobs_manifest("prod")
    assert prod["version"] == JOBS_VERSION == 5
    assert prod["jobs"] == {
        "by_hand": {
            "select": ["gold.*"],
            "upstream": False,
            "downstream": False,
            "schedule": None,
        },
        "daily": {
            "select": ["silver.*"],
            "upstream": True,
            "downstream": False,
            "schedule": {"cron": "0 6 * * *", "timezone": "Europe/Berlin"},
        },
    }
    assert _jobs_manifest("dev")["jobs"]["daily"]["schedule"] is None
    assert "jobs" not in prod["config"]


def test_a_manifest_without_jobs_has_no_jobs_key_and_keeps_its_version() -> None:
    assets = [make_asset("silver.a")]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    assert "jobs" not in manifest
    assert manifest["version"] == VERSION_WITHOUT_CHECKS


def test_manifest_jobs_reads_a_damaged_value_as_no_jobs() -> None:
    assert manifest_jobs(_jobs_manifest("prod"))["by_hand"]["select"] == ["gold.*"]
    for damaged in (None, [], "x", {"jobs": []}, {"jobs": {"a": 1}}, {"jobs": {1: {}}}):
        assert manifest_jobs(damaged) == {}


_NOTIFY_YAML = """\
jobs:
  daily:
    select: ["silver.*"]
    notify: { webhook: team-alerts, format: slack }
  by_hand:
    select: ["gold.*"]
environments:
  dev:
    jobs:
      daily: { notify: null }
"""


def _notify_manifest(environment: str) -> dict:
    config = parse_project_config(_NOTIFY_YAML).resolved_for(environment)
    assets = [make_asset("silver.a"), make_asset("gold.b", ("silver.a",))]
    return build_manifest(assets, build_graph(assets), config)


def test_a_manifest_with_notify_is_version_6_and_has_it_resolved_for_the_environment() -> None:
    prod = _notify_manifest("prod")
    assert prod["version"] == NOTIFY_VERSION == 6
    assert prod["jobs"]["daily"]["notify"] == {"webhook": "team-alerts", "format": "slack"}
    assert "notify" not in prod["jobs"]["by_hand"]
    dev = _notify_manifest("dev")
    assert dev["version"] == 5  # no job notifies there, so an older worker can read it
    assert "notify" not in dev["jobs"]["daily"]


def test_job_notify_reads_a_manifest_entry_and_a_damaged_one_as_none() -> None:
    manifest = _notify_manifest("prod")
    assert job_notify(manifest, "daily") == JobNotify("team-alerts", "slack")
    assert job_notify(manifest, "by_hand") is None
    assert job_notify(manifest, "missing") is None
    for damaged in (None, [], {"jobs": {"a": 1}}, {"jobs": {"a": {"notify": "x"}}}):
        assert job_notify(damaged, "a") is None
    for bad in (
        {"webhook": 1, "format": "json"},
        {"webhook": "w", "format": "email"},
        {"webhook": "w"},
        {"format": "json"},
    ):
        assert job_notify({"jobs": {"a": {"notify": bad}}}, "a") is None


def test_the_manifest_records_python_only_when_the_project_names_one() -> None:
    """An older worker reads no `python` key and runs on its own interpreter, as before."""
    config = ResolvedProjectConfig()
    graph = build_graph([])

    assert "python" not in build_manifest([], graph, config)
    named = build_manifest([], graph, config, python="3.12")
    assert named["python"] == "3.12"
    assert manifest_python(named) == "3.12"
    assert manifest_python({"python": "3.9"}) is None
    assert manifest_python({}) is None


_CHECK = ParsedCheck(
    name="scores_not_null",
    table="gold.scores",
    check="not_null",
    path="checks/scores_not_null.sql",
    on_failure="block",
    description=None,
    sql="SELECT * FROM gold.scores WHERE score IS NULL",
    references=("gold.scores",),
    body_line=1,
)


def _versioned(yaml: str, *, checks: bool, connections: tuple[str, ...] = ()) -> dict:
    """Build the manifest of `gold.scores` under a `lakana.yaml`, with or without a check.

    Parameters
    ----------
    yaml
        The `lakana.yaml` text, resolved for ``prod``.
    checks
        Whether the project has `_CHECK`.
    connections
        What the Python asset declares.

    Returns
    -------
    dict
        The manifest.
    """
    asset = dataclasses.replace(_python_asset(), connections=connections)
    config = parse_project_config(yaml).resolved_for("prod")
    return build_manifest([asset], build_graph([asset]), config, checks=[_CHECK] if checks else ())


_PLAIN_JOB = "jobs:\n  daily:\n    select: ['gold.*']\n"
_NOTIFY_JOB = _PLAIN_JOB + "    notify: { webhook: team-alerts, format: slack }\n"


@pytest.mark.parametrize(
    ("yaml", "checks", "version"),
    [
        ("", False, VERSION_WITHOUT_CHECKS),
        ("", True, CHECKS_VERSION),
        (_PLAIN_JOB, False, JOBS_VERSION),
        (_NOTIFY_JOB, True, NOTIFY_VERSION),
    ],
    ids=["3", "4", "5", "6"],
)
def test_a_project_without_connections_keeps_its_version_and_one_with_any_is_version_7(
    yaml: str, checks: bool, version: int
) -> None:
    plain = _versioned(yaml, checks=checks)
    assert plain["version"] == version
    assert plain["assets"][0]["connections"] == []
    declared = _versioned(yaml, checks=checks, connections=("crm-db", "files"))
    assert declared["version"] == CONNECTIONS_VERSION == 7
    assert declared["assets"][0]["connections"] == ["crm-db", "files"]
    assert isinstance(declared["checks"], list)
    assert len(declared["checks"]) == (1 if checks else 0)


def test_a_version_7_manifest_without_checks_still_reads_as_having_none() -> None:
    """Version 7 always carries the list, so a missing one reads as a damaged manifest."""
    manifest = _versioned("", checks=False, connections=("crm-db",))
    assert manifest["checks"] == []
    read = read_manifest_checks(manifest)
    assert (read.specs, read.unplaced) == ((), False)
    damaged = {key: value for key, value in manifest.items() if key != "checks"}
    assert read_manifest_checks(damaged).unplaced is True


def test_a_sql_entry_has_no_connections_key() -> None:
    assets = [make_asset("silver.a", ("bronze.events",))]
    manifest = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    assert "connections" not in manifest["assets"][0]


@pytest.mark.parametrize(
    ("entry", "connections"),
    [
        ({"kind": "python", "connections": ["crm-db", "files"]}, ("crm-db", "files")),
        ({"kind": "python", "connections": []}, ()),
        ({"kind": "python", "output": "table"}, ()),  # a version 6 entry
        ({"kind": "sql", "connections": ["crm-db"]}, ()),
        ({"kind": "python", "connections": "crm-db"}, ()),
        ({"kind": "python", "connections": ["crm-db", 3]}, ()),
        ({"kind": "python", "connections": ["CRM"]}, ()),
        ({"kind": "python", "connections": None}, ()),
        (None, ()),
        ("crm-db", ()),
    ],
)
def test_a_stored_entry_reads_connections_defensively(
    entry: object, connections: tuple[str, ...]
) -> None:
    assert entry_connections(entry) == connections


def test_a_malformed_connections_entry_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="lakana_core.projects.manifest"):
        assert entry_connections({"kind": "python", "connections": ["ok", "Not OK", 3]}) == ()
        assert entry_connections({"kind": "python", "connections": ["ok"]}) == ("ok",)
        assert entry_connections({"kind": "python"}) == ()
    (record,) = caplog.records
    assert "connections" in record.getMessage()
    assert "Not OK" not in record.getMessage()


@pytest.mark.parametrize(
    ("entry", "names", "readable"),
    [
        ({"kind": "python", "connections": ["crm-db", "files"]}, ("crm-db", "files"), True),
        ({"kind": "python"}, (), True),
        ({"kind": "sql", "connections": ["crm-db"]}, (), True),
        ({"kind": "python", "connections": "crm-db"}, ("crm-db",), False),
        ({"kind": "python", "connections": ["crm-db", 3, "CRM"]}, ("crm-db", "CRM"), False),
        ({"kind": "python", "connections": ["crm-db", "crm-db"]}, ("crm-db",), False),
        ({"kind": "python", "connections": {"crm-db": 1}}, (), False),
    ],
)
def test_read_entry_connections_names_every_string_and_says_whether_it_is_readable(
    entry: dict, names: tuple[str, ...], readable: bool
) -> None:
    assert read_entry_connections(entry) == EntryConnections(names, readable)
    assert entry_connections(entry) == (names if readable else ())


_OLD_SPELLING = AssetMetadata(materialize="incremental", incremental_key="ts")


def test_the_old_incremental_spelling_is_recorded_as_append_and_keeps_its_version() -> None:
    asset = make_asset("silver.customers", ("bronze.raw",), metadata=_OLD_SPELLING)
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["version"] == VERSION_WITHOUT_CHECKS
    (entry,) = manifest["assets"]
    assert {key: entry[key] for key in _WRITE_KEYS} == {
        "write": "append",
        "key": [],
        "watermark": "ts",
        "materialize": "incremental",
        "write_mode": "append",
        "first_write_mode": "create",
        "incremental_key": "ts",
    }
    assert deploy_hints([asset]) == (
        "silver.customers: `materialize: incremental` is now written `write: append` with "
        "`watermark: ts`; both work",
    )


_WRITE_KEYS = (
    "write",
    "key",
    "watermark",
    "materialize",
    "write_mode",
    "first_write_mode",
    "incremental_key",
)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            AssetMetadata(materialize="table", write="merge", key=("id",)),
            {
                "write": "merge",
                "key": ["id"],
                "watermark": None,
                "materialize": "table",
                "write_mode": "merge",
                "first_write_mode": "create",
                "incremental_key": None,
            },
        ),
        (
            AssetMetadata(materialize="table", write="merge", key=("id", "region"), watermark="u"),
            {
                "write": "merge",
                "key": ["id", "region"],
                "watermark": "u",
                "materialize": "table",
                "write_mode": "merge",
                "first_write_mode": "create",
                "incremental_key": "u",
            },
        ),
        (
            AssetMetadata(materialize="table", write="append", watermark="ts"),
            {
                "write": "append",
                "key": [],
                "watermark": "ts",
                "materialize": "incremental",
                "write_mode": "append",
                "first_write_mode": "create",
                "incremental_key": "ts",
            },
        ),
    ],
    ids=["merge", "merge-with-watermark", "append"],
)
def test_the_new_write_spelling_keeps_the_keys_older_readers_know(
    metadata: AssetMetadata, expected: dict[str, object]
) -> None:
    asset = make_asset("silver.customers", ("bronze.raw",), metadata=metadata)
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["version"] == (MODEL_VERSION if expected["key"] else WRITES_VERSION)
    assert manifest["checks"] == []
    (entry,) = manifest["assets"]
    assert {key: entry[key] for key in _WRITE_KEYS} == expected
    assert deploy_hints([asset]) == ()


def test_a_replace_asset_records_replace_and_keeps_its_version() -> None:
    asset = make_asset("gold.daily", ("bronze.raw",))
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["version"] == VERSION_WITHOUT_CHECKS
    (entry,) = manifest["assets"]
    assert (entry["write"], entry["key"], entry["watermark"]) == ("replace", [], None)
    explicit = make_asset(
        "gold.daily", ("bronze.raw",), metadata=AssetMetadata(materialize="table", write="replace")
    )
    assert build_manifest([explicit], build_graph([explicit]), ResolvedProjectConfig()) == manifest


def test_a_project_with_connections_and_new_writes_is_version_8() -> None:
    asset = dataclasses.replace(
        _python_asset(),
        connections=("crm-db",),
        metadata=AssetMetadata(materialize="table", write="merge", key=("id",)),
    )
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["version"] == MODEL_VERSION == 9
    assert manifest["assets"][0]["connections"] == ["crm-db"]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"write": "merge", "key": ["id"], "watermark": "u"}, ("merge", ("id",), "u")),
        ({"write": "append", "key": [], "watermark": "ts"}, ("append", (), "ts")),
        ({"materialize": "incremental", "incremental_key": "ts"}, ("append", (), "ts")),
        ({"materialize": "table", "write_mode": "replace"}, ("replace", (), None)),
        ({"materialize": "view"}, ("replace", (), None)),
        ({"write": "merge", "key": "id"}, ("merge", (), None)),
        ({"write": "merge", "key": [1, "id"], "watermark": 3}, ("merge", (), None)),
        ({"write": "upsert"}, ("upsert", (), None)),
        ("not an entry", ("replace", (), None)),
    ],
)
def test_entry_write_reads_new_and_old_entries(
    entry: object, expected: tuple[str, tuple[str, ...], str | None]
) -> None:
    """An unknown `write` is returned as stored, so a reader refuses it instead of replacing."""
    assert entry_write(entry) == expected


def test_a_version_8_manifest_without_checks_reads_as_damaged() -> None:
    """Version 8 always carries the list, so a missing one reads as a damaged manifest."""
    asset = make_asset(
        "silver.customers", metadata=AssetMetadata(materialize="table", write="merge", key=("id",))
    )
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    read = read_manifest_checks(manifest)
    assert (read.specs, read.unplaced) == ((), False)
    damaged = {key: value for key, value in manifest.items() if key != "checks"}
    assert read_manifest_checks(damaged).unplaced is True


def _modelled() -> ParsedAsset:
    """Build a replace asset that declares a model.

    Returns
    -------
    ParsedAsset
        `gold.fact_orders`, a fact keyed on `id`.
    """
    return make_asset(
        "gold.fact_orders",
        ("gold.dim_store",),
        columns=("id", "store_id"),
        metadata=AssetMetadata(
            materialize="table", kind="fact", grain="One row per order.", key=("id",)
        ),
    )


def test_an_asset_with_a_model_is_version_9_and_records_it() -> None:
    asset = _modelled()
    references = {"gold.fact_orders": (ResolvedReference("store_id", "gold.dim_store", "id"),)}
    manifest = build_manifest(
        [asset], build_graph([asset]), ResolvedProjectConfig(), references=references
    )
    assert manifest["version"] == MODEL_VERSION == 9
    assert manifest["checks"] == []
    (entry,) = manifest["assets"]
    assert entry["write"] == "replace"
    assert entry["key"] == ["id"]
    assert entry["model"] == {
        "kind": "fact",
        "grain": "One row per order.",
        "key_none": False,
        "references": [{"column": "store_id", "target": "gold.dim_store", "target_column": "id"}],
        "checks": {},
    }


def test_key_none_and_checks_are_recorded_sorted() -> None:
    asset = make_asset(
        "gold.readings",
        metadata=AssetMetadata(
            materialize="table",
            kind="fact",
            grain="One reading; identical rows are valid.",
            key="none",
            references={"meter_id": "gold.dim_meter.id"},
            checks={"references": "warn", "key": "block"},
        ),
    )
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    (entry,) = manifest["assets"]
    assert entry["key"] == []
    assert entry["model"]["key_none"] is True
    assert entry["model"]["references"] == []
    assert list(entry["model"]["checks"]) == ["key", "references"]


def test_a_project_without_model_fields_keeps_its_version_and_has_no_model() -> None:
    asset = make_asset("gold.daily", ("bronze.raw",))
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())
    assert manifest["version"] == VERSION_WITHOUT_CHECKS
    assert "model" not in manifest["assets"][0]


def test_entry_model_reads_a_stored_entry_back() -> None:
    asset = _modelled()
    references = {"gold.fact_orders": (ResolvedReference("store_id", "gold.dim_store", "id"),)}
    manifest = build_manifest(
        [asset], build_graph([asset]), ResolvedProjectConfig(), references=references
    )
    assert entry_model(manifest["assets"][0]) == EntryModel(
        kind="fact",
        grain="One row per order.",
        key=("id",),
        key_none=False,
        references=(ResolvedReference("store_id", "gold.dim_store", "id"),),
        checks={},
    )


@pytest.mark.parametrize(
    "entry", [None, "x", {"table": "t"}, {"table": "t", "model": ["kind"]}], ids=repr
)
def test_entry_model_is_none_without_a_model(entry: object) -> None:
    assert entry_model(entry) is None


def test_entry_model_drops_what_it_cannot_read() -> None:
    """A stored manifest is untrusted: malformed values read as absent."""
    entry = {
        "write": "replace",
        "key": ["id", 3],
        "model": {
            "kind": "cube",
            "grain": 7,
            "key_none": "yes",
            "references": [
                {"column": "a", "target": "n.t", "target_column": "id"},
                {"column": "b", "target": "n.t"},
                {"column": "c", "target": 1, "target_column": "id"},
                "d",
            ],
            "checks": {"key": "warn", "references": "explode", "other": "block"},
        },
    }
    assert entry_model(entry) == EntryModel(
        kind=None,
        grain=None,
        key=(),
        key_none=False,
        references=(ResolvedReference("a", "n.t", "id"),),
        checks={"key": "warn"},
    )
    assert entry_model({"model": {"references": "x", "checks": []}}) == EntryModel(
        None, None, (), False, (), {}
    )


def test_measures_raise_the_version_to_10_and_are_recorded() -> None:
    metadata = AssetMetadata.model_validate(
        {
            "materialize": "table",
            "kind": "fact",
            "grain": "One row per order.",
            "key": ["id"],
            "measures": {"amount": {"aggregation": "sum", "currency": "EUR"}},
        }
    )
    asset = make_asset("gold.fact_orders", columns=("id", "amount"), metadata=metadata)
    checks = generated_checks([asset], {}, default_on_failure="block")
    manifest = build_manifest([asset], build_graph([asset]), ResolvedProjectConfig(), checks=checks)
    assert manifest["version"] == MEASURES_VERSION == MANIFEST_VERSION == 10
    (entry,) = manifest["assets"]
    assert entry["model"]["measures"] == [
        {
            "column": "amount",
            "aggregation": "sum",
            "additivity": "additive",
            "not_over": [],
            "unit": None,
            "currency": "EUR",
            "label": None,
            "description": None,
        }
    ]
    model = entry_model(entry)
    assert model is not None and [m.column for m in model.measures] == ["amount"]
    measures_check = next(c for c in manifest["checks"] if c["check"] == "measures")
    assert measures_check["generated"]["not_over"] == []
    assert measures_check["generated"]["aggregations"] == ["sum"]
    read = read_manifest_checks(manifest)
    assert [s.generated for s in read.specs if s.check == "measures"] == [
        GeneratedFrom("measures", ("amount",), not_over=(), aggregations=("sum",))
    ]


def test_a_model_without_measures_stays_version_9_without_a_measures_key() -> None:
    manifest = build_manifest([_modelled()], build_graph([_modelled()]), ResolvedProjectConfig())
    assert manifest["version"] == MODEL_VERSION == 9
    assert "measures" not in manifest["assets"][0]["model"]
