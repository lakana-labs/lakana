"""`health:` in the asset header and lakana.yaml: forms, refusals, resolution (#340)."""

from __future__ import annotations

import dataclasses

import pytest
from test_projects_dag import make_asset

from lakana_core.catalog.health_signals import HealthEffects
from lakana_core.projects.checks import GeneratedFrom
from lakana_core.projects.frontmatter import AssetMetadata, parse_front_matter
from lakana_core.projects.health import (
    entry_effects,
    health_issues,
    normalize_health,
    resolve_effects,
)
from lakana_core.projects.health_checks import (
    entry_health_specs,
    health_specs,
    is_health_check,
    project_health_specs,
)
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import ResolvedProjectConfig, parse_project_config

_ALL_OFF = dict.fromkeys(("freshness", "volume", "null_rate", "range", "schema"), "off")


@pytest.mark.parametrize(
    ("value", "flat"),
    [
        (None, {}),
        ("off", _ALL_OFF),
        (False, _ALL_OFF),
        ({"volume": "block", "range": "off"}, {"volume": "block", "range": "off"}),
        (
            {"range": {"price": False}, "null_rate": {"note": "warn"}},
            {"range.price": "off", "null_rate.note": "warn"},
        ),
    ],
)
def test_the_forms_normalize_to_one_flat_map(value: object, flat: dict[str, str]) -> None:
    """Every spelling reads to the same sorted flat map."""
    assert normalize_health(value) == flat


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "loud",
            "takes off, or a mapping of signals to warn, block or off, such as {volume: block}",
        ),
        (
            {"rows": "warn"},
            "rows is not a health signal; use freshness, volume, null_rate, range or schema",
        ),
        ({"volume": "loud"}, "volume must be warn, block or off, not loud"),
        (
            {"volume": True},
            "volume must be warn, block or off; on, yes and true are not effects "
            "(to turn a signal off, write off)",
        ),
        (
            {"freshness": "block"},
            "freshness cannot block: it fires between runs, when there is no write to stop; "
            "use warn or off",
        ),
        (
            {"volume": {"x": "off"}},
            "volume is about the whole table; give it warn, block or off, not columns",
        ),
        ({"range": {"a.b": "off"}}, "column a.b cannot name its check health_range__a.b: "),
        ({"range": {"a": "loud"}}, "range: a must be warn, block or off, not loud"),
    ],
)
def test_bad_values_are_refused_with_the_next_step(value: object, message: str) -> None:
    """Each refusal names the cause and the next step."""
    with pytest.raises(ValueError) as refused:
        normalize_health(value)
    assert str(refused.value).startswith(message)


def test_lakana_yaml_names_no_columns() -> None:
    """A project default is about whole signals; columns belong in an asset's header."""
    with pytest.raises(ValueError, match=r"null_rate takes warn, block or off in lakana\.yaml"):
        normalize_health({"null_rate": {"x": "off"}}, columns=False)


def test_unquoted_off_in_yaml_means_off() -> None:
    """YAML reads an unquoted `off` as False; both places turn it into `off`."""
    header = "/* lakana\nhealth: {range: {price: off}, volume: off}\n*/\nSELECT 1 AS price\n"
    meta = parse_front_matter(header, path="assets/gold/t.sql").metadata
    assert meta.health == {"range.price": "off", "volume": "off"}
    assert parse_project_config("health: off\n").health == _ALL_OFF


def test_on_is_refused_with_the_effects() -> None:
    """`on` is read as True and refused with the three effects."""
    with pytest.raises(ProjectValidationError) as refused:
        parse_front_matter(
            "/* lakana\nhealth: {volume: on}\n*/\nSELECT 1 AS a\n", path="assets/gold/t.sql"
        )
    assert "health: volume must be warn, block or off; on, yes and true are not effects" in str(
        refused.value.issues[0].message
    )


def test_the_header_merges_over_the_project_default() -> None:
    """The asset's entries win; only what differs from the default is kept."""
    assert resolve_effects(
        {"volume": "block", "range": "off"}, {"range.price": "warn", "volume": "warn"}
    ) == {"range": "off", "range.price": "warn"}
    assert resolve_effects({}, {"range.price": "warn"}) == {}
    assert resolve_effects(_ALL_OFF, {}) == _ALL_OFF


def test_a_manifest_entry_gives_its_effects() -> None:
    """A stored entry is untrusted; anything unreadable is the default."""
    assert entry_effects({"health": {"volume": "block"}}).of("volume") == "block"
    assert entry_effects({"health": "damaged"}) == HealthEffects.default()
    assert entry_effects(None) == HealthEffects.default()


def _with(asset: ParsedAsset, **meta: object) -> ParsedAsset:
    """Give an asset metadata fields.

    Parameters
    ----------
    asset
        The asset.
    **meta
        `AssetMetadata` fields.

    Returns
    -------
    ParsedAsset
        A copy with the fields set.
    """
    return dataclasses.replace(asset, metadata=AssetMetadata.model_validate(meta))


def test_a_view_with_health_is_refused() -> None:
    """A view has no snapshots to watch."""
    view = _with(make_asset("gold.v"), materialize="view", health="off")
    [issue] = health_issues([view])
    assert issue.path == "assets/gold/v.sql"
    assert issue.message == (
        "health: does not apply to a view, which has no snapshots to watch; remove it, "
        "or materialize the asset as a table"
    )


def test_a_column_the_table_does_not_have_is_refused() -> None:
    """The message lists the columns the table has."""
    table = _with(
        make_asset("gold.t", columns=("id", "qty")),
        health={"range": {"price": "off", "qty": "off"}, "volume": "block"},
    )
    [issue] = health_issues([table])
    assert issue.message == (
        "health: range names column price, which gold.t does not have (columns: id, qty); "
        "fix the name or remove it"
    )


def test_a_history_asset_has_its_added_columns() -> None:
    """The columns a history write adds count as the table's columns, `valid_from` does not."""
    table = _with(
        make_asset("gold.t", columns=("id", "since")),
        write="history",
        valid_from="since",
        health={"null_rate": {"valid_to": "off", "since": "off"}},
    )
    [issue] = health_issues([table])
    assert "null_rate names column since, which gold.t does not have" in issue.message


def test_nothing_is_checked_when_the_columns_are_not_known() -> None:
    """A star leaves the names to the run; assets without health are skipped."""
    star = _with(make_asset("gold.s", columns=None), health={"range": {"x": "off"}})
    assert health_issues([star, make_asset("gold.p")]) == []


def test_each_block_entry_becomes_one_blocking_check() -> None:
    """One check per `block` entry, sorted by name; no other effect, asset or kind gives one."""
    manifest = {
        "version": 14,
        "assets": [
            {
                "table": "gold.t",
                "path": "assets/gold/t.sql",
                "health": {"volume": "block", "range.price": "block", "schema": "off"},
            },
            {"table": "gold.u", "path": "assets/gold/u.sql"},
            {"table": "gold.n", "health": {"null_rate": "block", "freshness": "block"}},
        ],
    }
    specs = health_specs(manifest, "gold.t")
    assert [(s.name, s.check, s.on_failure, s.sql, s.generated) for s in specs] == [
        (
            "gold.t/health_range__price",
            "health_range__price",
            "block",
            "",
            GeneratedFrom("health", ("range", "price")),
        ),
        (
            "gold.t/health_volume",
            "health_volume",
            "block",
            "",
            GeneratedFrom("health", ("volume",)),
        ),
    ]
    assert {(s.path, s.table, s.description) for s in specs} == {
        ("assets/gold/t.sql", "gold.t", "generated from health:")
    }
    assert health_specs(manifest, "gold.u") == ()
    assert health_specs(manifest, "gold.x") == ()
    assert health_specs({"assets": "damaged"}, "gold.t") == ()
    # Freshness never blocks; an entry without a path names the asset's directory.
    (null_rate,) = health_specs(manifest, "gold.n")
    assert (null_rate.name, null_rate.path) == ("gold.n/health_null_rate", "assets/gold/n")


@pytest.mark.parametrize("materialize", ["view", "artifact"])
def test_a_view_or_an_artifact_never_gains_a_health_check(materialize: str) -> None:
    """Only a table has snapshots to watch, so `health:` on anything else adds no check (F12).

    Parameters
    ----------
    materialize
        The entry's kind of output.
    """
    entry = {"table": "gold.v", "materialize": materialize, "health": {"volume": "block"}}
    assert health_specs({"assets": [entry]}, "gold.v") == ()
    assert entry_health_specs(entry, "gold.v") == ()


def test_a_health_check_is_told_by_its_generated_name() -> None:
    """A generated `health_` check is one; a hand-written check of that name is not."""
    assert is_health_check("gold.t/health_volume", generated=True)
    assert not is_health_check("gold.t/health_volume", generated=False)
    assert not is_health_check("gold.t/key_id", generated=True)


def test_a_deploy_knows_the_health_checks_each_table_will_gain() -> None:
    """The checks a run would add, from `lakana.yaml` and each header, so names can be refused.

    A view gains none, and neither does a table whose resolved `health:` blocks nothing.
    """
    config = ResolvedProjectConfig(health={"volume": "block"})
    table = dataclasses.replace(make_asset("silver.orders"), path="assets/silver/orders.sql")
    quiet = _with(make_asset("silver.quiet"), health={"volume": "warn"})
    view = _with(make_asset("gold.v"), materialize="view")
    specs = project_health_specs([table, quiet, view], config)
    assert [(s.name, s.path) for s in specs] == [
        ("silver.orders/health_volume", "assets/silver/orders.sql")
    ]
