"""The `layers:` section of `lakana.yaml`: its schema, its texts and a table's layer (#252)."""

from __future__ import annotations

import pydantic
import pytest

from lakana_core.catalog import loads
from lakana_core.catalog import service as catalog_service
from lakana_core.projects.frontmatter import parse_front_matter
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.layers import (
    LAYER_WRITES,
    LAYERS,
    MEDALLION_LAYERS,
    MEDALLION_LAYERS_FLOW,
    LayerRules,
    LayersConfig,
    layer_of,
)
from lakana_core.projects.project_config import parse_project_config

_BRONZE = "  bronze:\n    reads: []\n    write: [append, merge]\n    kinds: none\n"
_SILVER = (
    "  silver:\n    reads: [bronze, silver]\n    write: [replace, append, merge, view]\n"
    "    kinds: [staging]\n"
)
_GOLD = (
    "  gold:\n    reads: [silver, gold]\n    write: [replace, append, merge, history, view]\n"
    "    kinds: [fact, dimension, wide]\n"
)
MEDALLION_YAML = "layers:\n" + _BRONZE + _SILVER + _GOLD
"""The medallion default as a team writes it: `layers` on line 1, `bronze` on 2, its rules on
3-5, `silver` on 6 (rules 7-9), `gold` on 10 (rules 11-13)."""

_EMPTY = (
    "layers: is empty; declare bronze, silver and gold, each with reads, write and kinds, or "
    "remove `layers:` to deploy without layer rules. The medallion default: `layers: {bronze: "
    "{reads: [], write: [append, merge], kinds: none}, silver: {reads: [bronze, silver], write: "
    "[replace, append, merge, view], kinds: [staging]}, gold: {reads: [silver, gold], write: "
    "[replace, append, merge, history, view], kinds: [fact, dimension, wide]}}`"
)
_NOT_A_LAYER = (
    "is not a layer; list bronze, silver or gold. Tables outside them are sources, which only "
    "bronze reads"
)
_NOT_A_WRITE = (
    "is not a write mode a layer takes; list replace, append, merge, history or view "
    "(`materialize: incremental` counts as append)"
)
_KINDS_SHAPE = "must be `none` or a list of kinds, such as `[fact, dimension, wide]`"


def _refused(text: str) -> list[tuple[int | None, str]]:
    """Parse a `lakana.yaml` that must be refused.

    Parameters
    ----------
    text
        The file's content.

    Returns
    -------
    list of tuple
        ``(line, message)`` of every issue, in order.
    """
    with pytest.raises(ProjectValidationError) as caught:
        parse_project_config(text)
    return [(issue.line, issue.message) for issue in caught.value.issues]


def _with(old: str, new: str) -> str:
    """Return the medallion default with one line replaced.

    Parameters
    ----------
    old
        A line of `MEDALLION_YAML`, with its newline; it must occur once.
    new
        What replaces it.

    Returns
    -------
    str
        The changed document.
    """
    assert MEDALLION_YAML.count(old) == 1
    return MEDALLION_YAML.replace(old, new)


def test_the_medallion_default_parses_to_the_constant() -> None:
    config = parse_project_config(MEDALLION_YAML)
    assert config.layers == MEDALLION_LAYERS
    assert MEDALLION_LAYERS.for_layer("bronze") == LayerRules(
        reads=(), write=("append", "merge"), kinds="none"
    )
    assert MEDALLION_LAYERS.for_layer("silver").kinds == ("staging",)
    assert MEDALLION_LAYERS.for_layer("gold").reads == ("silver", "gold")
    assert parse_project_config("repo: x\n").layers is None


def test_the_three_names_exist_once_in_core() -> None:
    assert LAYERS == ("bronze", "silver", "gold")
    assert LAYER_WRITES == ("replace", "append", "merge", "history", "view")
    assert frozenset(LAYERS) == catalog_service._MEDALLION_TOP_LEVEL_NAMESPACES
    assert loads.LOAD_LAYER == "bronze"


def test_the_default_on_one_line_is_valid_yaml_of_the_same_rules() -> None:
    assert MEDALLION_LAYERS_FLOW == (
        "layers: {bronze: {reads: [], write: [append, merge], kinds: none}, "
        "silver: {reads: [bronze, silver], write: [replace, append, merge, view], "
        "kinds: [staging]}, gold: {reads: [silver, gold], write: "
        "[replace, append, merge, history, view], kinds: [fact, dimension, wide]}}"
    )
    assert parse_project_config(MEDALLION_LAYERS_FLOW + "\n").layers == MEDALLION_LAYERS


@pytest.mark.parametrize(
    ("text", "line"), [("layers:\n", 1), ("layers: {}\n", 1), ("repo: x\nlayers:\n", 2)]
)
def test_an_empty_section_shows_the_whole_default(text: str, line: int) -> None:
    assert _refused(text) == [(line, _EMPTY)]


def test_a_section_that_is_no_mapping_shows_one_layer() -> None:
    assert _refused("layers: [bronze]\n") == [
        (
            1,
            "layers: must map bronze, silver and gold to their rules, such as "
            "`bronze: {reads: [], write: [append, merge], kinds: none}`",
        )
    ]


def test_each_missing_layer_is_named_with_its_default() -> None:
    assert _refused("layers:\n" + _BRONZE + _GOLD) == [
        (
            1,
            "layers: needs silver; once lakana.yaml has `layers:`, it declares bronze, silver "
            "and gold, each with reads, write and kinds. The medallion default for silver: "
            "`silver: {reads: [bronze, silver], write: [replace, append, merge, view], "
            "kinds: [staging]}`",
        )
    ]
    assert [message[:22] for _, message in _refused("layers:\n" + _BRONZE)] == [
        "layers: needs silver; ",
        "layers: needs gold; on",
    ]


def test_each_missing_rule_is_named_with_its_default() -> None:
    no_kinds = _SILVER.replace("    kinds: [staging]\n", "")
    assert _refused("layers:\n" + _BRONZE + no_kinds + _GOLD) == [
        (
            6,
            "layers.silver: needs `kinds:`; every layer declares reads, write and kinds. The "
            "medallion default: `kinds: [staging]`",
        )
    ]
    assert _refused("layers:\n  bronze:\n    kinds: none\n" + _SILVER + _GOLD) == [
        (
            2,
            "layers.bronze: needs `reads:`; every layer declares reads, write and kinds. The "
            "medallion default: `reads: []`",
        ),
        (
            2,
            "layers.bronze: needs `write:`; every layer declares reads, write and kinds. The "
            "medallion default: `write: [append, merge]`",
        ),
    ]


@pytest.mark.parametrize("value", [" staging", ""])
def test_a_layer_that_is_no_mapping_shows_its_default(value: str) -> None:
    assert _refused("layers:\n" + _BRONZE + f"  silver:{value}\n" + _GOLD) == [
        (
            6,
            "layers.silver: must be a mapping of reads, write and kinds, such as "
            "`{reads: [bronze, silver], write: [replace, append, merge, view], kinds: [staging]}`",
        )
    ]


def test_an_unknown_layer_and_an_unknown_rule_are_refused() -> None:
    assert _refused(MEDALLION_YAML + "  platinum:\n    reads: []\n") == [
        (
            14,
            "layers: `platinum` is not a layer; Lakana's layers are bronze, silver and gold, the "
            "top-level namespaces every table lives under. Remove it, or put its rules under one "
            "of those",
        )
    ]
    with_unknown = _with(
        "    kinds: [staging]\n", "    kinds: [staging]\n    reads_from: [bronze]\n"
    )
    assert _refused(with_unknown) == [
        (10, "layers.silver: `reads_from` is not a rule; a layer declares reads, write and kinds")
    ]


@pytest.mark.parametrize(
    ("old", "new", "line", "message"),
    [
        (
            "    reads: [silver, gold]\n",
            "    reads: [raw, silver, gold]\n",
            11,
            f"layers.gold.reads: `raw` {_NOT_A_LAYER}",
        ),
        (
            "    reads: [silver, gold]\n",
            "    reads: [silver, silver, silver, gold]\n",
            11,
            "layers.gold.reads: lists `silver` twice",
        ),
        (
            "    write: [append, merge]\n",
            "    write: [append, incremental]\n",
            4,
            f"layers.bronze.write: `incremental` {_NOT_A_WRITE}",
        ),
        (
            "    write: [append, merge]\n",
            "    write: [append, upsert]\n",
            4,
            f"layers.bronze.write: `upsert` {_NOT_A_WRITE}",
        ),
        (
            "    write: [append, merge]\n",
            "    write: [merge, merge]\n",
            4,
            "layers.bronze.write: lists `merge` twice",
        ),
        (
            "    kinds: [fact, dimension, wide]\n",
            "    kinds: [fact, star]\n",
            13,
            "layers.gold.kinds: `star` is not a kind; list fact, dimension, wide or staging, or "
            "write `none`",
        ),
        (
            "    kinds: [fact, dimension, wide]\n",
            "    kinds: [fact, fact]\n",
            13,
            "layers.gold.kinds: lists `fact` twice",
        ),
        (
            "    kinds: [fact, dimension, wide]\n",
            "    kinds: any\n",
            13,
            "layers.gold.kinds: there is no `any`; list the kinds this layer's assets may declare, "
            "or write `none` if they declare none",
        ),
        (
            "    kinds: [fact, dimension, wide]\n",
            "    kinds: []\n",
            13,
            "layers.gold.kinds: an empty list would allow no asset; list the kinds this layer's "
            "assets may declare, or write `none` if they declare none",
        ),
        (
            "    reads: [silver, gold]\n",
            "    reads: silver\n",
            11,
            "layers.gold.reads: must be a list, such as `[silver, gold]`",
        ),
        (
            "    reads: []\n",
            "    reads:\n",
            3,
            "layers.bronze.reads: must be a list, such as `[]`",
        ),
    ],
)
def test_each_bad_value_gets_its_text(old: str, new: str, line: int, message: str) -> None:
    assert _refused(_with(old, new)) == [(line, message)]


def test_none_and_empty_lists_are_rules_too() -> None:
    text = (
        _with("    kinds: [fact, dimension, wide]\n", "    kinds: none\n")
        .replace("    write: [append, merge]\n", "    write: []\n")
        .replace("    reads: [bronze, silver]\n", "    reads: []\n")
    )
    layers = parse_project_config(text).layers
    assert layers is not None
    assert (layers.gold.kinds, layers.bronze.write, layers.silver.reads) == ("none", (), ())


@pytest.mark.parametrize("value", [" staging", "", " None"])
def test_kinds_that_is_neither_none_nor_a_list_is_refused_in_words(value: str) -> None:
    text = _with("    kinds: [staging]\n", f"    kinds:{value}\n")
    assert _refused(text) == [(9, f"layers.silver.kinds: {_KINDS_SHAPE}")]


def test_junk_values_are_refused_on_one_line_and_case_counts() -> None:
    assert _refused(_with("    reads: [silver, gold]\n", "    reads: [1, Silver, gold]\n")) == [
        (11, f"layers.gold.reads: `1` {_NOT_A_LAYER}"),
        (11, f"layers.gold.reads: `Silver` {_NOT_A_LAYER}"),
    ]
    assert _refused(_with("    write: [append, merge]\n", "    write: [Append]\n")) == [
        (4, f"layers.bronze.write: `Append` {_NOT_A_WRITE}")
    ]
    [(line, message)] = _refused(MEDALLION_YAML + '  "plat\\u202einum": {}\n')
    assert line == 14
    assert message.startswith("layers: `plat\\u202einum` is not a layer; ")
    assert "‮" not in message
    assert "\n" not in message


def test_an_environment_may_not_set_layers() -> None:
    issues = _refused(MEDALLION_YAML + "environments:\n  prod:\n    layers: {}\n")
    assert [message for _, message in issues] == [
        "environments.prod.layers: Extra inputs are not permitted"
    ]


def test_the_resolved_configuration_leaves_layers_out() -> None:
    resolved = parse_project_config(MEDALLION_YAML).resolved_for("dev")
    assert "layers" not in resolved.model_dump()


def test_a_header_cannot_turn_a_rule_off() -> None:
    with pytest.raises(ProjectValidationError) as caught:
        parse_front_matter("/* lakana\nlayers: none\n*/\nSELECT 1 AS a\n", path="assets/gold/a.sql")
    assert [issue.message for issue in caught.value.issues] == [
        "layers: Extra inputs are not permitted"
    ]


@pytest.mark.parametrize(
    ("name", "layer"),
    [
        ("bronze.orders", "bronze"),
        ("silver.orders", "silver"),
        ("gold.daily_sales", "gold"),
        ("gold.sales.daily", "gold"),
        ("Gold.sales", None),
        ("raw.pos_exports", None),
        ("models.churn_model", None),
        ("gold", None),
        ("orders", None),
    ],
)
def test_a_tables_layer_is_the_first_part_of_its_name(name: str, layer: str | None) -> None:
    assert layer_of(name) == layer


def test_the_model_refuses_what_lakana_yaml_refuses() -> None:
    stored = MEDALLION_LAYERS.model_dump(mode="json")
    with pytest.raises(pydantic.ValidationError, match="is not a layer"):
        LayersConfig.model_validate(
            {**stored, "gold": {"reads": ["raw"], "write": [], "kinds": "none"}}
        )
    with pytest.raises(pydantic.ValidationError, match="Extra inputs"):
        LayersConfig.model_validate({**stored, "platinum": stored["gold"]})


def test_the_medallion_default_lets_gold_keep_history() -> None:
    assert MEDALLION_LAYERS.gold.write == ("replace", "append", "merge", "history", "view")
    assert MEDALLION_LAYERS.silver.write == ("replace", "append", "merge", "view")
    assert "history" in LAYER_WRITES
