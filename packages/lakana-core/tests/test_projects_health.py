"""`health:` in the asset header and lakana.yaml: forms, refusals, resolution (#340)."""

from __future__ import annotations

import dataclasses

import pytest
from test_projects_dag import make_asset

from lakana_core.catalog.health_signals import HealthEffects
from lakana_core.projects.frontmatter import AssetMetadata, parse_front_matter
from lakana_core.projects.health import (
    entry_effects,
    health_issues,
    normalize_health,
    resolve_effects,
)
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import parse_project_config

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
