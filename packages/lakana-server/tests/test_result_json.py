"""Tests for the bounded JSON page of a query result: budgets, cuts, UTF-8 and every type."""

import ast
import collections
import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pyarrow as pa
import pytest

from lakana_server import result_json
from lakana_server.result_json import CELL_BUDGET, MAX_TYPE_CHARS, PAGE_BUDGET, json_page

ENVELOPE_MAX = 2048
"""The fixed envelope of a page plus a preview table name of 1,024 characters."""

UUID_BYTES = uuid.UUID(int=1).bytes


def page(t: pa.Table, **overrides: object) -> tuple[bytes, dict]:
    """Write a page of a table with the defaults of these tests.

    Parameters
    ----------
    t : pyarrow.Table
        The result.
    **overrides : object
        Any `json_page` argument, replacing the default.

    Returns
    -------
    tuple
        The body bytes and the parsed JSON.
    """
    arguments: dict = {
        "offset": 0,
        "limit": t.num_rows,
        "row_cap": None,
        "duration_ms": 1,
        "masked_columns": [],
        "preview_table": None,
    }
    arguments.update(overrides)
    body = json_page(t, **arguments)
    return body, json.loads(body)


def count_calls(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    name: str,
    by: Callable[..., str] | None = None,
) -> collections.Counter:
    """Wrap a function of a module and count its calls.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The fixture that undoes the wrapping.
    module : types.ModuleType
        The module that holds the function.
    name : str
        The function's name.
    by : callable, optional
        Maps a call's arguments to a label; each label is counted as well. The
        function cannot see which top-level column it works for, so the test
        tells the columns apart by their values.

    Returns
    -------
    collections.Counter
        The number of calls under ``"total"`` and under each label.
    """
    real = getattr(module, name)
    counts: collections.Counter = collections.Counter()

    def wrapper(*args: object, **kwargs: object) -> object:
        counts["total"] += 1
        if by is not None:
            counts[by(*args, **kwargs)] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return counts


def spy_rows(monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str) -> list[int]:
    """Wrap a function that takes a table and record the row count of each call.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The fixture that undoes the wrapping.
    module : types.ModuleType
        The module that holds the function.
    name : str
        The function's name.

    Returns
    -------
    list of int
        The ``num_rows`` of the table passed in each call.
    """
    real = getattr(module, name)
    seen: list[int] = []

    def wrapper(table: pa.Table) -> pa.Table:
        seen.append(table.num_rows)
        return real(table)

    monkeypatch.setattr(module, name, wrapper)
    return seen


def dense_union_of_one_int64(value: int) -> pa.Array:
    """Build a dense union array of one row holding one int64.

    Parameters
    ----------
    value : int
        The value.

    Returns
    -------
    pyarrow.Array
        A one-row union array.
    """
    return pa.UnionArray.from_dense(
        pa.array([0], pa.int8()), pa.array([0], pa.int32()), [pa.array([value], pa.int64())], ["i"]
    )


def nested_struct_table(columns: int, depth: int, field_name_length: int) -> pa.Table:
    """Build an empty table whose columns have deeply nested struct types.

    Parameters
    ----------
    columns : int
        The number of columns.
    depth : int
        How many structs deep each column's type nests.
    field_name_length : int
        The length of every field name in the nesting.

    Returns
    -------
    pyarrow.Table
        A table with no rows and no chunks, so only the header carries the size.
    """
    nested: pa.DataType = pa.int64()
    for level in range(depth):
        nested = pa.struct([(f"{level:0{field_name_length}d}", nested)])
    schema = pa.schema([(f"c{i}", nested) for i in range(columns)])
    return pa.Table.from_batches([], schema=schema)


_OWNERS: dict[int, dict[int, str]] = {}
"""The function name that owns each node, per parsed tree, built on first use."""


def enclosing_function(tree: ast.Module, node: ast.AST) -> str | None:
    """Find the innermost function that contains a node.

    Parameters
    ----------
    tree : ast.Module
        The parsed module.
    node : ast.AST
        A node of the tree.

    Returns
    -------
    str or None
        The function's name, or ``None`` at module level.
    """
    owners = _OWNERS.get(id(tree))
    if owners is None:
        owners = _OWNERS[id(tree)] = {}
        for function in ast.walk(tree):
            if isinstance(function, ast.FunctionDef):
                owners.update({id(child): function.name for child in ast.walk(function)})
    return owners.get(id(node))


def test_a_json_page_never_exceeds_its_byte_budget() -> None:
    t = pa.table({f"c{i}": pa.array([chr(1) * 60000] * 50) for i in range(40)})
    body, p = page(t)
    assert len(body) <= PAGE_BUDGET + ENVELOPE_MAX and p["next_offset"] is not None


def test_one_row_wider_than_the_budget_is_cut_not_sent_whole() -> None:
    column = pa.array([chr(1) * 10922])
    t = pa.table({f"c{i}": column for i in range(12000)})
    body, p = page(t)
    assert len(body) <= PAGE_BUDGET + ENVELOPE_MAX and p["row_cut"] == [0]
    assert p["rows"][0][-1] == {"cut": True} or p["columns_cut"] > 0


def test_the_columns_header_stays_within_the_budget() -> None:
    t = nested_struct_table(columns=3000, depth=30, field_name_length=200)
    body, p = page(t)
    assert len(body) <= PAGE_BUDGET + ENVELOPE_MAX
    assert all(len(c["type"]) <= MAX_TYPE_CHARS for c in p["columns"])
    assert any(c.get("type_cut") for c in p["columns"])
    _, small = page(t, page_budget=10_000)
    assert small["columns_cut"] > 0 and (small["rows"], small["next_offset"]) == ([], None)


def test_masked_columns_count_against_the_budget() -> None:
    masks = [{"table": "gold.orders", "column": "c" * 1000 + str(i)} for i in range(10_000)]
    body, p = page(pa.table({"a": [1]}), masked_columns=masks)
    assert len(body) <= PAGE_BUDGET + ENVELOPE_MAX
    assert 0 < p["masked_columns_cut"] == 10_000 - len(p["masked_columns"])


def test_a_nested_cell_stops_at_64_kib_while_serializing(monkeypatch: pytest.MonkeyPatch) -> None:
    wide = pa.StructArray.from_arrays(
        [pa.array(["x" * 100])] * 5000, [f"f{i}" for i in range(5000)]
    )
    deep = pa.array([[["y" * 50] * 100] * 100])
    calls = count_calls(
        monkeypatch,
        result_json,
        "_leaf",
        by=lambda scalar, _: "wide" if scalar.as_py()[0] == "x" else "deep",
    )
    _, p = page(pa.table({"wide": wide, "deep": deep}))
    assert p["rows"][0] == [{"cut": True}, {"cut": True}]
    assert calls["wide"] < 5000 and calls["deep"] < 10_000


@pytest.mark.parametrize("ch", [chr(1), "\U0001f600", " ", '"'])
def test_a_cut_cell_is_at_most_64_kib_after_escaping(ch: str) -> None:
    cell = page(pa.table({"s": [ch * 20000]}))[1]["rows"][0][0]
    assert cell["cut"] is True and cell["text"].startswith(ch)
    assert len(json.dumps(cell, ensure_ascii=False).encode()) <= CELL_BUDGET


def test_the_page_is_utf8_not_ascii_escaped() -> None:
    assert "\U0001f600".encode() in page(pa.table({"s": ["\U0001f600"]}))[0]


def test_json_conversion_never_materialises_the_whole_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = spy_rows(monkeypatch, result_json, "precut")
    calls = count_calls(monkeypatch, result_json, "_leaf")
    page(pa.table({"a": range(100_000), "b": ["x"] * 100_000}), limit=10)
    assert seen == [10] and calls["total"] <= 20
    tree = ast.parse(Path(result_json.__file__).read_text())
    attrs = [
        (n.attr, enclosing_function(tree, n))
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
    ]
    assert not {a for a, _ in attrs} & {"to_pylist", "to_pydict"}
    assert {f for a, f in attrs if a == "as_py"} <= {"_leaf"}


@pytest.mark.parametrize(
    ("arrow_array", "written"),
    [
        (pa.array([9007199254740993], pa.int64()), ["9007199254740993"]),
        (pa.array([7], pa.int32()), [7]),
        (pa.array([2**64 - 1], pa.uint64()), ["18446744073709551615"]),
        (pa.array([float("nan"), float("inf"), float("-inf")]), ["NaN", "Infinity", "-Infinity"]),
        (pa.array([Decimal("1.5")], pa.decimal128(38, 10)), ["1.5000000000"]),
        (pa.array([b"abc"]), [{"binary": 3}]),
        (pa.array([b"ab"], pa.binary_view()), [{"binary": 2}]),
        (pa.array([[b"abcd"]], pa.list_(pa.binary())), [[{"binary": 4}]]),
        (pa.array(["ab"], pa.string_view()), ["ab"]),
        (pa.array(["x", "y"]).dictionary_encode(), ["x", "y"]),
        (pa.array([UUID_BYTES], pa.uuid()), [str(uuid.UUID(bytes=UUID_BYTES))]),
        (pa.array([[("k", 1)]], pa.map_(pa.string(), pa.int64())), [[{"key": "k", "value": "1"}]]),
        (pa.array([1_000_000_000_123], pa.timestamp("ns")), ["1970-01-01 00:16:40.000000123"]),
        (pa.array([253402300800 * 1000], pa.timestamp("ms")), ["10000-01-01 00:00:00.000"]),
        (pa.array([0], pa.timestamp("us", tz="UTC")), ["1970-01-01 00:00:00.000000Z"]),
        (pa.array([1500], pa.time64("ns")), ["00:00:00.000001500"]),
        (pa.array([1234], pa.duration("ns")), ["1234"]),
        (
            pa.array([pa.MonthDayNano([1, 2, 3])], pa.month_day_nano_interval()),
            [{"months": 1, "days": 2, "nanoseconds": 3}],
        ),
        (dense_union_of_one_int64(1), ["1"]),
        (pa.array([None], pa.null()), [None]),
        (pa.array([[1] * 101]), [["1"] * 100 + [{"cut": True}]]),
        (pa.array([{"a": 1, "b": "x"}]), [{"a": "1", "b": "x"}]),
        # the shapes around them: nulls at every level, other list kinds, other leaves
        (pa.array([True, None]), [True, None]),
        (pa.array([1.5]), [1.5]),
        (pa.array([1], pa.date32()), ["1970-01-02"]),
        (pa.array([[1, 2]], pa.list_(pa.int64(), 2)), [["1", "2"]]),
        (pa.array([[1, 2]], pa.large_list(pa.int64())), [["1", "2"]]),
        (pa.array([None, [1]], pa.list_(pa.int64())), [None, ["1"]]),
        (pa.array([{"a": 1}, None]), [{"a": "1"}, None]),
        (
            pa.array([[("k", None)]], pa.map_(pa.string(), pa.int64())),
            [[{"key": "k", "value": None}]],
        ),
        (pa.array([[{"b": b"xy"}]]), [[{"b": {"binary": 2}}]]),
        (pa.array([b"ab"], pa.binary(2)), [{"binary": 2}]),
        (pa.array([["z" * 20000]]), [[{"cut": True, "text": "z" * 10922}]]),
        (pa.ExtensionArray.from_storage(pa.bool8(), pa.array([1], pa.int8())), ["True"]),
    ],
)
def test_every_result_type_is_written_as_the_plan_says(
    arrow_array: pa.Array, written: list
) -> None:
    rows = page(pa.table({"c": arrow_array}))[1]["rows"]
    assert [row[0] for row in rows] == written


def test_duplicate_column_names_keep_their_own_values() -> None:
    t = pa.Table.from_arrays([pa.array([1]), pa.array(["x"])], names=["a", "a"])
    body, p = page(t)
    assert [c["name"] for c in p["columns"]] == ["a", "a"] and p["rows"] == [["1", "x"]]
    assert body.startswith(b'{"columns":[')


def test_the_envelope_carries_the_caller_values() -> None:
    _, p = page(
        pa.table({"a": [1]}), masked_columns=None, preview_table="gold.orders", duration_ms=None
    )
    assert (p["masked_columns"], p["masked_columns_cut"], p["preview"]) == (
        None,
        0,
        {"table": "gold.orders"},
    )
    assert (p["duration_ms"], p["row_cut"], p["columns_cut"], p["row_count"]) == (None, [], 0, 1)
    _, q = page(pa.table({"a": [1]}), masked_columns=[{"table": "t", "column": "c"}])
    assert (q["masked_columns"], q["preview"]) == ([{"table": "t", "column": "c"}], None)


def test_paging_never_repeats_or_skips_a_row() -> None:
    t = pa.table({"i": range(7)})
    for limit in range(1, 8):
        seen, offset = [], 0
        while offset is not None:
            p = page(t, offset=offset, limit=limit)[1]
            seen += [row[0] for row in p["rows"]]
            offset = p["next_offset"]
        assert seen == [str(i) for i in range(7)]
    empty = page(pa.table({"i": pa.array([], pa.int64())}))[1]
    assert (empty["rows"], empty["row_count"], empty["next_offset"]) == ([], 0, None)
    assert page(t, offset=7, limit=3)[1]["rows"] == []


def test_a_row_cap_bounds_rows_and_row_count() -> None:
    t = pa.table({"i": range(150)})
    p = page(t, limit=100, row_cap=100)[1]
    assert (len(p["rows"]), p["row_count"], p["next_offset"]) == (100, 100, None)
    assert page(t, offset=100, limit=100, row_cap=100)[1]["rows"] == []


def test_a_page_that_runs_out_of_budget_between_rows_continues_at_the_next_row() -> None:
    t = pa.table({"s": ["x" * 100] * 5})
    seen, offset, pages = [], 0, 0
    while offset is not None:
        p = page(t, offset=offset, limit=5, page_budget=250)[1]
        assert p["row_cut"] == [] and p["rows"]
        seen += p["rows"]
        offset, pages = p["next_offset"], pages + 1
    assert seen == [["x" * 100]] * 5 and pages == 3


def test_a_row_cut_inside_ends_the_page_and_the_next_page_starts_after_it() -> None:
    t = pa.table({"a": ["x"] * 3, "b": ["y" * 50] * 3, "c": ["z"] * 3})
    p = page(t, limit=1, page_budget=134)[1]
    assert p["rows"] == [["x", {"cut": True}, {"cut": True}]] and p["row_cut"] == [0]
    assert (p["columns_cut"], p["next_offset"]) == (0, 1)
    q = page(t, limit=1, page_budget=104)[1]
    assert q["rows"] == [["x"]] and (q["row_cut"], q["columns_cut"]) == ([0], 2)


def test_a_header_that_leaves_no_room_for_a_row_sends_no_row() -> None:
    p = page(pa.table({"a": [1, 2]}), page_budget=38)[1]
    assert (p["rows"], p["next_offset"], p["row_count"]) == ([], None, 2)


def test_a_string_over_a_smaller_cell_budget_keeps_the_prefix_that_fits() -> None:
    p = page(pa.table({"s": ["a" * 500]}), cell_budget=100)[1]
    cell = p["rows"][0][0]
    assert cell["cut"] is True and 0 < len(cell["text"]) < 100
    assert len(json.dumps(cell).encode()) <= 100
    assert page(pa.table({"s": ["a" * 500]}), cell_budget=10)[1]["rows"] == [[{"cut": True}]]


def uuid_array() -> pa.Array:
    """Build a one-row array of the UUID extension type.

    Returns
    -------
    pyarrow.Array
        An ``arrow.uuid`` array holding ``UUID_BYTES``.
    """
    return pa.ExtensionArray.from_storage(pa.uuid(), pa.array([UUID_BYTES], pa.binary(16)))


def dense_union_of(
    *members: pa.Array, selected: int = 0, codes: list[int] | None = None
) -> pa.Array:
    """Build a one-row dense union that selects one of its members.

    Parameters
    ----------
    *members : pyarrow.Array
        One single-row array per member.
    selected : int
        The index of the member the row holds.
    codes : list of int, optional
        The type codes of the members; their positions by default.

    Returns
    -------
    pyarrow.Array
        A one-row union array.
    """
    codes = codes or list(range(len(members)))
    return pa.UnionArray.from_dense(
        pa.array([codes[selected]], pa.int8()),
        pa.array([0], pa.int32()),
        list(members),
        [f"m{i}" for i in range(len(members))],
        codes,
    )


@pytest.mark.parametrize(
    ("member", "written"),
    [
        (pa.array([b"ab"]), {"binary": 2}),
        (pa.array([1_000_000_000_123], pa.timestamp("ns")), "1970-01-01 00:16:40.000000123"),
        (uuid_array(), str(uuid.UUID(bytes=UUID_BYTES))),
        (pa.array([253402300800 * 1000], pa.timestamp("ms")), "10000-01-01 00:00:00.000"),
        (pa.array([{"b": b"xy"}]), {"b": {"binary": 2}}),
        (pa.array([[b"abcd"]], pa.list_(pa.binary())), [{"binary": 4}]),
        (pa.array([[("k", 1)]], pa.map_(pa.string(), pa.int64())), [{"key": "k", "value": "1"}]),
        (pa.array([Decimal("1.5")], pa.decimal128(38, 10)), "1.5000000000"),
        (pa.array(["x" * 20000]), {"cut": True, "text": "x" * 10922}),
        (pa.array(["x"]).dictionary_encode(), "x"),
    ],
)
def test_a_union_member_is_written_as_a_value_of_its_own_type(
    member: pa.Array, written: object
) -> None:
    for union in (
        dense_union_of(member),
        dense_union_of(pa.array([0], pa.int8()), member, selected=1, codes=[5, 7]),
    ):
        assert page(pa.table({"u": union}))[1]["rows"] == [[written]]


def test_a_sparse_union_pages_each_row_by_its_own_member() -> None:
    union = pa.UnionArray.from_sparse(
        pa.array([1, 0], pa.int8()),
        [pa.array([b"ab", b"cde"]), pa.array([1_000_000_000_123, 5], pa.timestamp("ns"))],
        ["b", "t"],
    )
    rows = page(pa.table({"u": union}))[1]["rows"]
    assert rows == [["1970-01-01 00:16:40.000000123"], [{"binary": 3}]]
    assert page(pa.table({"u": union}), offset=1, limit=1)[1]["rows"] == [[{"binary": 3}]]


def test_integers_follow_their_type_not_their_value() -> None:
    assert page(pa.table({"i": pa.array([1, 2**60], pa.int64())}))[1]["rows"] == [
        ["1"],
        ["1152921504606846976"],
    ]
    assert page(pa.table({"i": pa.array([1, 2**40], pa.uint64())}))[1]["rows"] == [
        ["1"],
        ["1099511627776"],
    ]
    assert page(pa.table({"i": pa.array([1, 2**31 - 1], pa.int32())}))[1]["rows"] == [
        [1],
        [2**31 - 1],
    ]


def test_a_list_of_uuids_is_written_as_uuid_strings() -> None:
    values = uuid_array()
    text = str(uuid.UUID(bytes=UUID_BYTES))
    plain = pa.ListArray.from_arrays(pa.array([0, 1], pa.int32()), values)
    large = pa.LargeListArray.from_arrays(pa.array([0, 1], pa.int64()), values)
    fixed = pa.FixedSizeListArray.from_arrays(values, 1)
    nested = pa.ListArray.from_arrays(pa.array([0, 1], pa.int32()), plain)
    structs = pa.ListArray.from_arrays(
        pa.array([0, 1], pa.int32()), pa.StructArray.from_arrays([values], ["u"])
    )
    for column, written in (
        (plain, [text]),
        (large, [text]),
        (fixed, [text]),
        (nested, [[text]]),
        (structs, [{"u": text}]),
    ):
        assert page(pa.table({"c": column}))[1]["rows"] == [[written]]


@pytest.mark.parametrize("list_view", [pa.list_view, pa.large_list_view])
def test_a_list_view_is_written_as_a_list(list_view: Callable[[pa.DataType], pa.DataType]) -> None:
    stamps = pa.array([[1_000_000_000_123]], list_view(pa.timestamp("ns")))
    blobs = pa.array([[b"xy"]], list_view(pa.binary()))
    rows = page(pa.table({"t": stamps, "b": blobs}))[1]["rows"]
    assert rows == [[["1970-01-01 00:16:40.000000123"], [{"binary": 2}]]]


def test_the_preview_name_counts_against_the_page_budget() -> None:
    t = pa.table({"a": ["x" * 100] * 50})
    body, p = page(t, preview_table=chr(1) * 1024, page_budget=2000)
    assert len(body) <= 2000 + 400 and p["preview"]["cut"] is True


def test_a_long_preview_name_is_cut_with_a_marker() -> None:
    assert page(pa.table({"a": [1]}), preview_table="a" * 2000)[1]["preview"] == {
        "table": "a" * 1024,
        "cut": True,
    }
    body, p = page(pa.table({"a": [1]}), preview_table=chr(1) * 1024, page_budget=100)
    assert (
        p["preview"]["cut"] is True
        and 0 < len(p["preview"]["table"]) < 1024
        and len(body) <= 100 + 400
    )


def test_work_is_bounded_with_the_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    column = pa.array([["z" * 1000] * 100])
    t = pa.table({f"c{i}": column for i in range(300)})
    calls = count_calls(monkeypatch, result_json, "_leaf")
    body, p = page(t, page_budget=100_000)
    assert len(p["rows"][0]) == 300 and set(map(json.dumps, p["rows"][0])) == {'{"cut": true}'}
    assert p["row_cut"] == [0] and calls["total"] < 1000 and len(body) <= 100_000 + ENVELOPE_MAX


def test_a_page_of_long_enum_lists_decodes_only_the_head_of_each_list() -> None:
    labels = pa.array([f"{i:04d}" + "x" * 596 for i in range(50)])
    codes = pa.DictionaryArray.from_arrays(
        pa.array([i % 50 for i in range(200_000)], pa.int8()), labels
    )
    lists = pa.ListArray.from_arrays(pa.array(range(0, 200_001, 40_000), pa.int32()), codes)
    t = pa.table({"e": lists})
    original = pa.default_memory_pool()
    pool = pa.proxy_memory_pool(original)
    pa.set_memory_pool(pool)
    try:
        rows = page(t, limit=5)[1]["rows"]
    finally:
        pa.set_memory_pool(original)
    assert (
        len(rows) == 5 and rows[0][0][100] == {"cut": True} and rows[0][0][1] == "0001" + "x" * 596
    )
    assert pool.max_memory() < 20 * 1024 * 1024
