"""The metadata graph's node ids, and the parser of what a caller sends (#374)."""

from __future__ import annotations

import uuid

import pytest

from lakana_core.catalog.graph_model import (
    NodeKind,
    NodeRef,
    check_node,
    column_node,
    node_kind,
    parse_node,
    table_node,
    table_part,
)
from lakana_core.errors import ValidationError

_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gold.dim_store", NodeRef(NodeKind.TABLE, "gold.dim_store")),
        (f"table:{_ID}", NodeRef(NodeKind.TABLE, str(_ID))),
        ("column:gold.dim_store/store_id", NodeRef(NodeKind.COLUMN, "gold.dim_store/store_id")),
        ("asset:sales/gold.fact_sales", NodeRef(NodeKind.ASSET, "sales/gold.fact_sales")),
        ("term:net_revenue", NodeRef(NodeKind.TERM, "net_revenue")),
        (f"row_filter:{_ID}", NodeRef(NodeKind.ROW_FILTER, str(_ID))),
        ("job:sales/nightly", NodeRef(NodeKind.JOB, "sales/nightly")),
    ],
)
def test_ids_and_named_forms_parse(text: str, expected: NodeRef) -> None:
    assert parse_node(text) == expected


def test_ids_built_here_read_back() -> None:
    assert node_kind(column_node(_ID, "a/b")) is NodeKind.COLUMN
    assert table_part(check_node(_ID, "ref_x")) == _ID
    assert table_part(column_node(_ID, "a/b")) == _ID
    assert table_part(table_node(_ID)) == _ID


@pytest.mark.parametrize(
    "text", ["", "nope:x", "table:", "gold.dim\x00store", "x" * 2049, "gold.\u2028dim", "a\u200eb"]
)
def test_a_text_that_is_no_node_id_is_refused_with_the_forms(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_node(text)
    message = str(caught.value)
    assert message.endswith(
        "it starts with table:, column:, asset:, reference:, measure:, term:, check:, "
        "row_filter:, column_mask:, job: or connection:."
    )
    assert "Give a table's dotted name, such as gold.dim_store" in message
    assert "\x00" not in message and "\u2028" not in message and len(message) < 600
