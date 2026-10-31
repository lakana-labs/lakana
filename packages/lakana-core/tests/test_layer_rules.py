"""Layer rules at deploy: one issue per asset, rule and offending read (#252)."""

from __future__ import annotations

from lakana_core.projects.asset_model import ParsedAsset
from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.issues import error_for_reader, withheld_lines
from lakana_core.projects.layer_rules import asset_write, layer_issues
from lakana_core.projects.layers import LAYER_WRITES, MEDALLION_LAYERS, LayerRules, LayersConfig
from lakana_core.projects.parse import file_references, parse_assets
from lakana_core.projects.project_config import AssetDefaults
from lakana_core.projects.python_assets import parse_python_asset

_HEAD = "import lakana\nfrom lakana import asset\n\n\n"
"""Four lines before the decorator, so a Python asset's `block_line` is 5."""

_ARTIFACT = '''\
from pathlib import Path

import lakana
from lakana import asset


@asset(inputs={"train": "SOURCE"}, output="artifact")
def churn_model(train: DataFrame) -> Path:
    """The churn model."""
    return lakana.output_dir() / "model.json"
'''
"""An artifact asset reading SOURCE; its decorator is on line 7."""


def _sql(table: str, text: str) -> ParsedAsset:
    """Parse one SQL asset at the path its table decides.

    Parameters
    ----------
    table
        The dotted table name.
    text
        The file's content.

    Returns
    -------
    ParsedAsset
        The parsed asset.
    """
    path = f"assets/{table.replace('.', '/')}.sql"
    [asset] = parse_assets([DiscoveredAsset(path, table, text)], defaults=AssetDefaults())
    return asset


def _python(table: str, decorator: str, parameters: str = "ctx") -> ParsedAsset:
    """Parse one Python asset whose decorator is on line 5.

    Parameters
    ----------
    table
        The dotted table name; its last part names the function.
    decorator
        The arguments of `@asset(...)`.
    parameters
        The function's parameters.

    Returns
    -------
    ParsedAsset
        The parsed asset.
    """
    name = table.rpartition(".")[2]
    text = (
        f"{_HEAD}@asset({decorator})\ndef {name}({parameters}) -> DataFrame:\n"
        '    """An asset."""\n    return ctx\n'
    )
    return _parsed_python(table, text)


def _artifact(table: str, source: str) -> ParsedAsset:
    """Parse an artifact asset that reads one table.

    Parameters
    ----------
    table
        The artifact's dotted name.
    source
        The table it reads.

    Returns
    -------
    ParsedAsset
        The parsed asset.
    """
    return _parsed_python(table, _ARTIFACT.replace("SOURCE", source))


def _parsed_python(table: str, text: str) -> ParsedAsset:
    """Parse a Python asset and expect success.

    Parameters
    ----------
    table
        The dotted table name.
    text
        The file's content.

    Returns
    -------
    ParsedAsset
        The parsed asset.
    """
    discovered = DiscoveredAsset(f"assets/{table.replace('.', '/')}.py", table, text, "python")
    result = parse_python_asset(discovered, AssetDefaults())
    assert isinstance(result, ParsedAsset), result
    return result


def _layers(**rules: LayerRules) -> LayersConfig:
    """Return the medallion default with some layers' rules replaced.

    Parameters
    ----------
    **rules
        Layer → its rules.

    Returns
    -------
    LayersConfig
        The rules.
    """
    return MEDALLION_LAYERS.model_copy(update=rules)


def _issues(*assets: ParsedAsset, layers: LayersConfig = MEDALLION_LAYERS) -> list[tuple]:
    """Check assets and return ``(path, line, message)`` per issue.

    Parameters
    ----------
    *assets
        The parsed assets.
    layers
        The rules; the medallion default by default.

    Returns
    -------
    list of tuple
        The issues, in order.
    """
    return [(i.path, i.line, i.message) for i in layer_issues(layers, list(assets))]


def test_a_project_without_layers_gets_no_issue() -> None:
    assert layer_issues(None, [_sql("gold.x", "SELECT 1 AS a FROM raw.y\n")]) == []


def test_a_read_from_a_layer_not_listed() -> None:
    asset = _sql(
        "silver.orders_clean",
        "/* lakana\nkind: staging\n*/\n"
        "SELECT o.id FROM bronze.orders AS o JOIN gold.daily_sales AS d ON o.id = d.id\n",
    )
    assert _issues(asset) == [
        (
            "assets/silver/orders_clean.sql",
            4,
            "`silver.orders_clean` reads `gold.daily_sales`, which is in gold, but "
            "layers.silver.reads allows only bronze and silver. Read the data from bronze or "
            "silver instead, or add gold to layers.silver.reads in lakana.yaml",
        )
    ]


def test_a_reference_to_a_layer_not_listed() -> None:
    asset = _sql(
        "gold.fact_sales",
        "/* lakana\nkind: fact\ngrain: One row per sale.\nkey: [sale_id]\nreferences:\n"
        "  store_id: bronze.stores\n  sku: gold.dim_product.sku\n*/\n"
        "SELECT sale_id, store_id, sku FROM silver.sales\n",
    )
    assert _issues(asset) == [
        (
            "assets/gold/fact_sales.sql",
            1,
            "`gold.fact_sales` references `bronze.stores` (`references:`), which is in bronze, "
            "but layers.gold.reads allows only silver and gold. Point the reference at a silver "
            "or gold table, or add bronze to layers.gold.reads in lakana.yaml",
        )
    ]


def test_a_read_with_reads_empty() -> None:
    asset = _sql(
        "bronze.orders", "/* lakana\nwrite: merge\nkey: [id]\n*/\nSELECT id FROM silver.customers\n"
    )
    assert _issues(asset) == [
        (
            "assets/bronze/orders.sql",
            5,
            "`bronze.orders` reads `silver.customers`, which is in silver, but layers.bronze.reads "
            "lists no layer, so bronze reads only sources. Build this table in silver, or add "
            "silver to layers.bronze.reads in lakana.yaml",
        )
    ]
    closed = _layers(gold=LayerRules(reads=(), write=LAYER_WRITES, kinds="none"))
    gold = _sql("gold.totals", "SELECT a FROM silver.b\n")
    assert _issues(gold, layers=closed) == [
        (
            "assets/gold/totals.sql",
            1,
            "`gold.totals` reads `silver.b`, which is in silver, but layers.gold.reads lists no "
            "layer, so gold reads no table. Build this table in silver, or add silver to "
            "layers.gold.reads in lakana.yaml",
        )
    ]


def test_a_source_read_outside_bronze() -> None:
    asset = _sql(
        "gold.daily_sales",
        "/* lakana\nkind: fact\ngrain: One row per day.\nkey: [day]\n*/\n"
        "SELECT day FROM raw.pos_exports\n",
    )
    assert _issues(asset) == [
        (
            "assets/gold/daily_sales.sql",
            6,
            "`gold.daily_sales` reads `raw.pos_exports`, which is outside bronze, silver and gold, "
            "so it is a source, and only bronze reads sources (layers.gold.reads). Load it into "
            "bronze first with a bronze asset, then read the bronze table",
        )
    ]
    bronze = _sql(
        "bronze.pos", "/* lakana\nwrite: merge\nkey: [day]\n*/\nSELECT day FROM raw.pos_exports\n"
    )
    assert _issues(bronze) == []
    referencing = _sql(
        "silver.days",
        "/* lakana\nkind: staging\nreferences:\n  day: raw.calendar\n*/\n"
        "SELECT day FROM bronze.pos\n",
    )
    assert _issues(referencing) == [
        (
            "assets/silver/days.sql",
            1,
            "`silver.days` references `raw.calendar` (`references:`), which is outside bronze, "
            "silver and gold, so it is a source, and only bronze reads sources "
            "(layers.silver.reads). Load it into bronze first with a bronze asset, then point the "
            "reference at the bronze table",
        )
    ]


def test_a_connection_outside_bronze() -> None:
    silver = _python(
        "silver.orders", 'connections=["crm"], write="merge", key=["id"], kind="staging"'
    )
    assert _issues(silver) == [
        (
            "assets/silver/orders.py",
            5,
            "`silver.orders` declares connection `crm`, a source, and only bronze reads sources "
            "(layers.silver.reads). Move the asset to assets/bronze/, then build silver from the "
            "bronze table",
        )
    ]
    assert layer_issues(MEDALLION_LAYERS, [silver])[0].targets == ()
    assert _issues(_python("bronze.orders", 'connections=["crm"], write="merge", key=["id"]')) == []


def test_a_write_mode_not_listed() -> None:
    asset = _sql("bronze.orders", "SELECT 1 AS id\n")
    assert _issues(asset) == [
        (
            "assets/bronze/orders.sql",
            1,
            "`bronze.orders` is written with `replace`, but layers.bronze.write allows only append "
            "and merge. Write it with one of those (`write:` in its header), or add replace to "
            "layers.bronze.write in lakana.yaml",
        )
    ]
    [issue] = layer_issues(MEDALLION_LAYERS, [asset])
    assert (issue.targets, issue.withheld) == ((), None)
    only_merge = _layers(bronze=LayerRules(reads=(), write=("merge",), kinds="none"))
    assert _issues(_python("bronze.scores", ""), layers=only_merge) == [
        (
            "assets/bronze/scores.py",
            5,
            "`bronze.scores` is written with `replace`, but layers.bronze.write allows only merge. "
            "Write it with merge (`write=` in its decorator), or add replace to "
            "layers.bronze.write in lakana.yaml",
        )
    ]


def test_a_view_and_an_incremental_asset() -> None:
    view = _sql("bronze.orders_v", "/* lakana\nmaterialize: view\n*/\nSELECT 1 AS id\n")
    assert asset_write(view) == "view"
    assert _issues(view) == [
        (
            "assets/bronze/orders_v.sql",
            1,
            "`bronze.orders_v` is a view (`materialize: view`), but layers.bronze.write allows "
            "only append and merge. Build it as a table, or add view to layers.bronze.write in "
            "lakana.yaml",
        )
    ]
    incremental = _sql(
        "bronze.events",
        "/* lakana\nmaterialize: incremental\nincremental_key: id\n*/\nSELECT 1 AS id\n",
    )
    assert asset_write(incremental) == "append"
    assert _issues(incremental) == []


def test_an_empty_write_list_closes_the_layer() -> None:
    closed = _layers(bronze=LayerRules(reads=(), write=(), kinds="none"))
    asset = _sql("bronze.orders", "/* lakana\nwrite: merge\nkey: [id]\n*/\nSELECT 1 AS id\n")
    assert _issues(asset, layers=closed) == [
        (
            "assets/bronze/orders.sql",
            1,
            "`bronze.orders` is in bronze, but layers.bronze.write is empty, so this project "
            "builds no bronze asset. Move the asset to another layer, or list its write mode in "
            "layers.bronze.write in lakana.yaml",
        )
    ]


def test_a_kind_missing() -> None:
    assert _issues(_sql("gold.daily_sales", "SELECT 1 AS day FROM silver.days\n")) == [
        (
            "assets/gold/daily_sales.sql",
            1,
            "`gold.daily_sales` declares no kind, but layers.gold.kinds needs every gold asset to "
            "declare one of fact, dimension, wide. Add `kind:` to its header",
        )
    ]
    assert _issues(_python("silver.scores", "")) == [
        (
            "assets/silver/scores.py",
            5,
            "`silver.scores` declares no kind, but layers.silver.kinds needs every silver asset "
            "to declare staging. Add `kind=` to its decorator",
        )
    ]


def test_a_kind_not_listed() -> None:
    staging = _sql(
        "gold.stg_orders", "/* lakana\nkind: staging\n*/\nSELECT id FROM silver.orders\n"
    )
    assert _issues(staging) == [
        (
            "assets/gold/stg_orders.sql",
            1,
            "`gold.stg_orders` declares `kind: staging`, but layers.gold.kinds allows only fact, "
            "dimension, wide. Move it to the layer for staging tables, or declare one of those "
            "kinds",
        )
    ]
    fact = _sql(
        "silver.f", "/* lakana\nkind: fact\ngrain: g\nkey: none\n*/\nSELECT a FROM bronze.b\n"
    )
    assert _issues(fact) == [
        (
            "assets/silver/f.sql",
            1,
            "`silver.f` declares `kind: fact`, but layers.silver.kinds allows only staging. Move "
            "it to the layer for fact tables, or declare `kind: staging`",
        )
    ]


def test_a_kind_where_the_layer_says_none() -> None:
    sql = _sql(
        "bronze.orders",
        "/* lakana\nwrite: merge\nkey: [id]\nkind: staging\n*/\nSELECT 1 AS id\n",
    )
    assert _issues(sql) == [
        (
            "assets/bronze/orders.sql",
            1,
            "`bronze.orders` declares `kind: staging`, but layers.bronze.kinds is none, so a "
            "bronze asset declares no kind. Remove `kind:` from its header",
        )
    ]
    python = _python("bronze.raw", 'write="merge", key=["id"], kind="staging"')
    assert _issues(python)[0][2] == (
        '`bronze.raw` declares `kind="staging"`, but layers.bronze.kinds is none, so a bronze '
        "asset declares no kind. Remove `kind=` from its decorator"
    )


def test_an_asset_outside_the_layers_is_refused() -> None:
    assert _issues(_artifact("models.churn_model", "gold.training_set")) == [
        (
            "assets/models/churn_model.py",
            7,
            "`models.churn_model` is outside bronze, silver and gold, but a project with `layers:` "
            "builds every asset in one of them. Move it to the layer whose assets read it, such as "
            "assets/gold/churn_model.py for gold.churn_model",
        )
    ]


def test_an_artifact_in_a_layer_obeys_reads_only() -> None:
    assert _issues(_artifact("gold.churn_model", "gold.training_set")) == []
    [(_, line, message)] = _issues(_artifact("gold.churn_model", "bronze.raw"))
    assert line == 7
    assert message.startswith("`gold.churn_model` reads `bronze.raw` (input `train`), which is in")
    reader = _python(
        "silver.scores",
        'inputs={"model": "models.churn_model"}, kind="staging"',
        "ctx, model: DataFrame",
    )
    assert _issues(reader)[0][2].startswith(
        "`silver.scores` reads `models.churn_model` (input `model`), which is outside bronze"
    )


def test_a_python_input_from_a_layer_not_listed() -> None:
    asset = _python(
        "silver.features",
        'inputs={"train": "gold.sales"}, kind="staging"',
        "ctx, train: DataFrame",
    )
    assert _issues(asset) == [
        (
            "assets/silver/features.py",
            5,
            "`silver.features` reads `gold.sales` (input `train`), which is in gold, but "
            "layers.silver.reads allows only bronze and silver. Read the data from bronze or "
            "silver instead, or add gold to layers.silver.reads in lakana.yaml",
        )
    ]


def test_reading_its_own_table_crosses_no_layer() -> None:
    own_only = _layers(gold=LayerRules(reads=("silver",), write=LAYER_WRITES, kinds=("fact",)))
    asset = _sql(
        "gold.own",
        "/* lakana\nkind: fact\ngrain: g\nkey: none\nwrite: append\nwatermark: id\n*/\n"
        "SELECT id FROM gold.own UNION ALL SELECT id FROM silver.x\n",
    )
    assert asset.references == ("gold.own", "silver.x")
    assert _issues(asset, layers=own_only) == []


def test_files_unqualified_names_and_ctes_are_not_layer_reads() -> None:
    header = "/* lakana\nkind: wide\ngrain: g\nkey: none\n*/\n"
    for sql in (
        "SELECT * FROM 'exports/x.csv'",
        'SELECT a FROM "a.b"',
        "SELECT a FROM orders",
        "WITH gold AS (SELECT 1 AS a) SELECT a FROM gold",
    ):
        assert _issues(_sql("gold.f", f"{header}{sql}\n")) == [], sql
    # The deploy still refuses the file (`table 'exports/x.csv' not found`).
    file_read = _sql("gold.f", f"{header}SELECT * FROM 'exports/x.csv'\n")
    assert file_references(file_read) == {"exports/x.csv"}


def test_a_read_that_is_also_referenced_gives_one_issue_at_the_read() -> None:
    asset = _sql(
        "gold.fact_visits",
        "/* lakana\nkind: fact\ngrain: g\nkey: [id]\nreferences:\n  id: bronze.stores\n"
        "  sid: bronze.stores.store_id\n*/\nSELECT id, id AS sid FROM bronze.stores\n",
    )
    [(_, line, message)] = _issues(asset)
    assert line == 9
    assert message.startswith("`gold.fact_visits` reads `bronze.stores`, which is in bronze")


def test_a_long_form_reference_is_classified_by_its_first_part_and_withheld_by_both_readings() -> (
    None
):
    asset = _sql(
        "gold.fact",
        "/* lakana\nkind: fact\ngrain: g\nkey: [id]\nreferences:\n  sku: bronze.products.sku\n*/\n"
        "SELECT id, sku FROM silver.s\n",
    )
    [issue] = layer_issues(MEDALLION_LAYERS, [asset])
    assert issue.message.startswith("`gold.fact` references `bronze.products.sku` (`references:`)")
    assert issue.targets == ("bronze.products.sku", "bronze.products")
    stored = withheld_lines([issue])
    readable = {"bronze.products"}
    assert error_for_reader(issue.render(), stored, readable.__contains__) == (
        "assets/gold/fact.sql:1: `gold.fact` breaks layers.gold.reads with a read that is not "
        "shown to you"
    )


def test_a_python_asset_gives_one_issue_per_table_then_its_connections() -> None:
    asset = _python(
        "silver.orders",
        'connections=["crm"], inputs={"train": "gold.sales", "again": "gold.sales", '
        '"raw": "raw.exports"}, write="merge", key=["id"], kind="staging"',
        "ctx, train: DataFrame, again: DataFrame, raw: DataFrame",
    )
    assert _issues(asset) == [
        (
            "assets/silver/orders.py",
            5,
            "`silver.orders` reads `gold.sales` (input `train`), which is in gold, but "
            "layers.silver.reads allows only bronze and silver. Read the data from bronze or "
            "silver instead, or add gold to layers.silver.reads in lakana.yaml",
        ),
        (
            "assets/silver/orders.py",
            5,
            "`silver.orders` reads `raw.exports` (input `raw`), which is outside bronze, silver "
            "and gold, so it is a source, and only bronze reads sources (layers.silver.reads). "
            "Load it into bronze first with a bronze asset, then read the bronze table",
        ),
        (
            "assets/silver/orders.py",
            5,
            "`silver.orders` declares connection `crm`, a source, and only bronze reads sources "
            "(layers.silver.reads). Move the asset to assets/bronze/, then build silver from the "
            "bronze table",
        ),
    ]


def test_issues_come_by_path_then_write_kinds_and_reads_by_name() -> None:
    late = _sql(
        "bronze.mixed",
        "/* lakana\nkind: fact\ngrain: g\nkey: none\n*/\n"
        "SELECT a FROM silver.b UNION ALL SELECT a FROM gold.a\n",
    )
    early = _sql("bronze.a", "SELECT 1 AS a\n")
    issues = _issues(late, early)
    assert [(path, message[: message.index(",")]) for path, _, message in issues] == [
        ("assets/bronze/a.sql", "`bronze.a` is written with `replace`"),
        ("assets/bronze/mixed.sql", "`bronze.mixed` is written with `replace`"),
        ("assets/bronze/mixed.sql", "`bronze.mixed` declares `kind: fact`"),
        ("assets/bronze/mixed.sql", "`bronze.mixed` reads `gold.a`"),
        ("assets/bronze/mixed.sql", "`bronze.mixed` reads `silver.b`"),
    ]


def test_a_read_issue_is_withheld_from_a_reader_who_may_not_read_the_target() -> None:
    asset = _sql(
        "silver.orders_clean",
        "/* lakana\nkind: staging\n*/\nSELECT id FROM gold.daily_sales\n",
    )
    [issue] = layer_issues(MEDALLION_LAYERS, [asset])
    assert issue.targets == ("gold.daily_sales",)
    assert issue.withheld == (
        "`silver.orders_clean` breaks layers.silver.reads with a read that is not shown to you"
    )
    error = issue.render()
    stored = withheld_lines([issue])
    assert error_for_reader(error, stored, lambda name: False) == (
        "assets/silver/orders_clean.sql:4: `silver.orders_clean` breaks layers.silver.reads with "
        "a read that is not shown to you"
    )
    assert error_for_reader(error, stored, lambda name: True) == error


def test_a_table_in_a_layer_that_allows_views_is_told_to_build_a_view() -> None:
    views_only = _layers(silver=LayerRules(reads=("bronze",), write=("view",), kinds="none"))
    table = _sql("silver.orders", "SELECT id FROM bronze.orders\n")
    assert _issues(table, layers=views_only) == [
        (
            "assets/silver/orders.sql",
            1,
            "`silver.orders` is written with `replace`, but layers.silver.write allows only view. "
            "Build it as a view (`materialize: view` in its header), or add replace to "
            "layers.silver.write in lakana.yaml",
        )
    ]
    python = _python("silver.scores", "")
    assert _issues(python, layers=views_only)[0][2] == (
        "`silver.scores` is written with `replace`, but layers.silver.write allows only view. "
        "A Python asset is never a view, so move it to another layer, or add replace to "
        "layers.silver.write in lakana.yaml"
    )
    mixed = _layers(silver=LayerRules(reads=("bronze",), write=("append", "view"), kinds="none"))
    assert _issues(table, layers=mixed)[0][2] == (
        "`silver.orders` is written with `replace`, but layers.silver.write allows only append "
        "and view. Write it with append (`write:` in its header) or build it as a view "
        "(`materialize: view` in its header), or add replace to layers.silver.write in "
        "lakana.yaml"
    )
    several = _layers(
        silver=LayerRules(reads=("bronze",), write=("append", "merge", "view"), kinds="none")
    )
    assert _issues(python, layers=several)[0][2] == (
        "`silver.scores` is written with `replace`, but layers.silver.write allows only append, "
        "merge and view. Write it with append or merge (`write=` in its decorator), or add "
        "replace to layers.silver.write in lakana.yaml"
    )


def test_a_read_from_its_own_layer_with_reads_empty() -> None:
    closed = _layers(gold=LayerRules(reads=(), write=LAYER_WRITES, kinds="none"))
    gold = _sql("gold.t", "SELECT a FROM gold.x\n")
    assert _issues(gold, layers=closed) == [
        (
            "assets/gold/t.sql",
            1,
            "`gold.t` reads `gold.x`, which is in gold, but layers.gold.reads lists no layer, so "
            "gold reads no table. Move this asset to a layer whose reads list gold, or add gold "
            "to layers.gold.reads in lakana.yaml",
        )
    ]
    bronze = _sql(
        "bronze.orders", "/* lakana\nwrite: merge\nkey: [id]\n*/\nSELECT id FROM bronze.raw\n"
    )
    assert _issues(bronze)[0][2] == (
        "`bronze.orders` reads `bronze.raw`, which is in bronze, but layers.bronze.reads lists no "
        "layer, so bronze reads only sources. Read the sources `bronze.raw` is built from "
        "instead, or add bronze to layers.bronze.reads in lakana.yaml"
    )


def test_a_source_read_and_a_python_input_are_withheld_by_their_name() -> None:
    source = _sql("silver.days", "/* lakana\nkind: staging\n*/\nSELECT day FROM raw.pos_exports\n")
    python = _python(
        "silver.features",
        'inputs={"train": "gold.sales"}, kind="staging"',
        "ctx, train: DataFrame",
    )
    for asset, target in ((source, "raw.pos_exports"), (python, "gold.sales")):
        [issue] = layer_issues(MEDALLION_LAYERS, [asset])
        assert issue.targets == (target,)
        assert issue.withheld == (
            f"`{asset.table}` breaks layers.silver.reads with a read that is not shown to you"
        )
        stored = withheld_lines([issue])
        assert target not in error_for_reader(issue.render(), stored, lambda name: False)
