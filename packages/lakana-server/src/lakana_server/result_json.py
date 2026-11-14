r"""The bounded JSON page of a query result.

A person asks for one page of a result as JSON, whatever the data is. This
module keeps that one answer bounded in three layers.

1. `precut` shortens every value in pyarrow, before any Python object exists:
   strings to `MAX_STRING_CHARS` plus one, lists to `MAX_LIST_ITEMS` plus one
   elements at every level, binary to its length, and temporal and decimal
   values to their text form.
2. A cell is written into a local buffer that counts the bytes of the escaped
   text. Past `CELL_BUDGET` the buffer is dropped and the cell becomes
   ``{"cut": true}``.
3. The page counts the `columns` header, the `masked_columns` list and every
   row against `PAGE_BUDGET`, inside a row as well, so the first row can never
   push the answer past the budget either.

The page is UTF-8, never ASCII-escaped: a character costs at most six bytes
(a control character as ``\u0001``), which is why a string is cut to 10,922
characters (at most 65,532 bytes).
"""

import json
import math
import uuid
from collections.abc import Iterable

import pyarrow as pa
import pyarrow.compute as pc

CELL_BUDGET = 65_536
PAGE_BUDGET = 4 * 1024 * 1024
MAX_STRING_CHARS = 10_922
MAX_LIST_ITEMS = 100
MAX_TYPE_CHARS = 256
MAX_PREVIEW_CHARS = 1_024
MAX_PAGE_ROWS = 1_000
DEFAULT_PAGE_ROWS = 100

_CUT = '{"cut":true}'
_MARKER_OVERHEAD = len(json.dumps({"cut": True, "text": ""}).encode())


class _CellOverflow(Exception):
    """Raised when a cell no longer fits its budget."""


class _CellBuffer:
    """The text of one cell, with a running count of its UTF-8 bytes.

    Parameters
    ----------
    budget : int
        The most bytes the cell may take.
    """

    __slots__ = ("budget", "parts", "used")

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.parts: list[str] = []
        self.used = 0

    @property
    def room(self) -> int:
        """The bytes still free in the cell.

        Returns
        -------
        int
            The budget minus what is written.
        """
        return self.budget - self.used

    def add(self, text: str) -> None:
        """Append a piece of the cell.

        Parameters
        ----------
        text : str
            The piece, already escaped.

        Raises
        ------
        _CellOverflow
            When the piece does not fit the budget; it still counts in `used`,
            as work attempted.
        """
        self.used += _size(text)
        if self.used > self.budget:
            raise _CellOverflow
        self.parts.append(text)


def _size(text: str) -> int:
    """Return the length of a text in UTF-8 bytes.

    Parameters
    ----------
    text : str
        The text.

    Returns
    -------
    int
        Its encoded length.
    """
    return len(text) if text.isascii() else len(text.encode())


def _without_uuid(kind: pa.DataType) -> pa.DataType:
    """Replace the UUID extension by its 16-byte storage type, at any depth.

    The list kernels cannot build an array of an extension type, so lists,
    structs and maps are cast to a type without it before they are cut. Unions
    are left alone: their members are cut one by one.

    Parameters
    ----------
    kind : pyarrow.DataType
        Any type.

    Returns
    -------
    pyarrow.DataType
        The type with every UUID below list, struct and map types replaced.
    """
    if isinstance(kind, pa.UuidType):
        return pa.binary(16)
    if pa.types.is_list(kind):
        return pa.list_(_without_uuid(kind.value_type))
    if pa.types.is_large_list(kind):
        return pa.large_list(_without_uuid(kind.value_type))
    if pa.types.is_fixed_size_list(kind):
        return pa.list_(_without_uuid(kind.value_type), kind.list_size)
    if pa.types.is_struct(kind):
        return pa.struct([pa.field(f.name, _without_uuid(f.type), f.nullable) for f in kind])
    if pa.types.is_map(kind):
        return pa.map_(_without_uuid(kind.key_type), _without_uuid(kind.item_type))
    return kind


def _cut_array(arr: pa.Array, original: pa.DataType | None = None) -> pa.Array:
    """Shorten the values of an array, recursing through nested types.

    Only the rows of the array are ever cut: a list is sliced to its head
    before its elements are, so a long list never decodes or copies more than
    `MAX_LIST_ITEMS` plus one elements per row.

    Parameters
    ----------
    arr : pyarrow.Array
        The array, of any type DuckDB can return.
    original : pyarrow.DataType, optional
        The type before any cast, which tells a UUID (kept as its bytes) from
        other 16-byte binary (replaced by its length); the array's own type
        by default.

    Returns
    -------
    pyarrow.Array
        The same rows with every value cut; its type may differ (binary
        becomes its length, temporal and decimal values become text).
    """
    original = arr.type if original is None else original
    plain = _without_uuid(arr.type)
    if plain != arr.type:
        arr = arr.cast(plain)
    if isinstance(original, pa.UuidType):
        return arr
    if pa.types.is_dictionary(arr.type):
        return _cut_array(arr.dictionary_decode(), original.value_type)
    if pa.types.is_string_view(arr.type):
        arr = arr.cast(pa.large_string())
    if pa.types.is_string(arr.type) or pa.types.is_large_string(arr.type):
        return pc.utf8_slice_codeunits(arr, 0, MAX_STRING_CHARS + 1)  # ty: ignore[missing-attribute]
    if pa.types.is_binary_view(arr.type):
        arr = arr.cast(pa.large_binary())
    if (
        pa.types.is_binary(arr.type)
        or pa.types.is_large_binary(arr.type)
        or pa.types.is_fixed_size_binary(arr.type)
    ):
        return pc.binary_length(arr)  # ty: ignore[missing-attribute]
    if pa.types.is_list_view(arr.type) or pa.types.is_large_list_view(arr.type):
        arr = _list_of_view(arr)
    if (
        pa.types.is_map(arr.type)
        or pa.types.is_fixed_size_list(arr.type)
        or pa.types.is_list(arr.type)
        or pa.types.is_large_list(arr.type)
    ):
        return _cut_list(arr, original)
    if pa.types.is_struct(arr.type):
        children = [_cut_array(arr.field(i), original[i].type) for i in range(arr.type.num_fields)]
        return _struct_of(arr, children)
    if pa.types.is_union(arr.type):
        return _cut_union(arr, original)
    is_text_form = pa.types.is_temporal(arr.type) and not pa.types.is_interval(arr.type)
    if is_text_form or pa.types.is_decimal(arr.type):
        return pc.cast(arr, pa.string())
    return arr


def _struct_of(arr: pa.Array, children: list[pa.Array]) -> pa.Array:
    """Rebuild a struct array over new children, keeping names and nulls.

    Parameters
    ----------
    arr : pyarrow.Array
        The struct array.
    children : list of pyarrow.Array
        One replacement array per field, as long as `arr`.

    Returns
    -------
    pyarrow.Array
        The struct array over the children.
    """
    fields = [
        pa.field(arr.type[i].name, child.type, arr.type[i].nullable)
        for i, child in enumerate(children)
    ]
    return pa.StructArray.from_arrays(children, fields=fields, mask=arr.is_null())


def _cut_union(arr: pa.Array, original: pa.DataType) -> pa.Array:
    """Cut every member of a union array, keeping the member of each row.

    Parameters
    ----------
    arr : pyarrow.Array
        A sparse or dense union array.
    original : pyarrow.DataType
        Its type before any cast.

    Returns
    -------
    pyarrow.Array
        The union over the cut members, with the same names and type codes.
    """
    kind = arr.type
    members = [_cut_array(arr.field(i), original[i].type) for i in range(kind.num_fields)]
    names = [kind[i].name for i in range(kind.num_fields)]
    if kind.mode == "sparse":
        return pa.UnionArray.from_sparse(arr.type_codes, members, names, kind.type_codes)
    return pa.UnionArray.from_dense(arr.type_codes, arr.offsets, members, names, kind.type_codes)


def _list_of_view(arr: pa.Array) -> pa.Array:
    """Turn a list view into a list of the same rows.

    pyarrow's own cast from a list view to a list builds an invalid offsets
    buffer, so the lists are rebuilt from their lengths.

    Parameters
    ----------
    arr : pyarrow.Array
        A list view or large list view array.

    Returns
    -------
    pyarrow.Array
        A list or large list array over the flattened elements.
    """
    lengths = pc.fill_null(pc.list_value_length(arr), 0)  # ty: ignore[missing-attribute]
    offsets = pa.concat_arrays([pa.array([0], lengths.type), pc.cumulative_sum(lengths)])  # ty: ignore[missing-attribute]
    make = pa.LargeListArray if pa.types.is_large_list_view(arr.type) else pa.ListArray
    return make.from_arrays(offsets, arr.flatten(), mask=arr.is_null())


def _cut_list(arr: pa.Array, original: pa.DataType) -> pa.Array:
    """Cut a list-like array to the head of each list, and each element in turn.

    Parameters
    ----------
    arr : pyarrow.Array
        A list, large list, fixed-size list or map array, without UUIDs.
    original : pyarrow.DataType
        Its type before any cast.

    Returns
    -------
    pyarrow.Array
        A list array with at most `MAX_LIST_ITEMS` plus one cut elements each;
        a map becomes a list of ``{"key", "value"}`` structs.
    """
    if pa.types.is_map(arr.type):
        arr = arr.cast(
            pa.list_(pa.struct([("key", arr.type.key_type), ("value", arr.type.item_type)]))
        )
    elif pa.types.is_fixed_size_list(arr.type):
        arr = arr.cast(pa.list_(arr.type.value_field))
    arr = pc.list_slice(arr, 0, MAX_LIST_ITEMS + 1)  # ty: ignore[missing-attribute]
    lengths = pc.fill_null(pc.list_value_length(arr), 0)  # ty: ignore[missing-attribute]
    offsets = pa.concat_arrays([pa.array([0], lengths.type), pc.cumulative_sum(lengths)])  # ty: ignore[missing-attribute]
    values = _cut_array(arr.flatten(), _item_original(original))
    return type(arr).from_arrays(offsets, values, mask=arr.is_null())


def precut(table: pa.Table) -> pa.Table:
    """Cut every value of a table in pyarrow, before any Python object exists.

    Parameters
    ----------
    table : pyarrow.Table
        The rows of one page.

    Returns
    -------
    pyarrow.Table
        The same rows, with strings cut to 10,923 characters, lists to 101
        elements at every level, binary replaced by its length and temporal,
        decimal and map values turned into text or entry lists.
    """
    arrays = [_cut_array(column.combine_chunks(), column.type) for column in table.columns]
    return pa.Table.from_arrays(arrays, names=table.column_names)


def _leaf(scalar: pa.Scalar, original: pa.DataType) -> object:
    """Return the JSON value of one leaf: the only place a Python value is made.

    Parameters
    ----------
    scalar : pyarrow.Scalar
        A valid, pre-cut leaf scalar.
    original : pyarrow.DataType
        The type the column had before `precut`, which tells a binary length
        or a UUID from a plain number.

    Returns
    -------
    object
        A JSON-serialisable value: a number, a string, a bool, or a small dict
        for binary and intervals. 64-bit integers (always, by type) and
        non-finite floats are strings, so no client rounds them.
    """
    if isinstance(original, pa.UuidType):
        return str(uuid.UUID(bytes=scalar.as_py()))
    if (
        pa.types.is_binary(original)
        or pa.types.is_large_binary(original)
        or pa.types.is_binary_view(original)
        or pa.types.is_fixed_size_binary(original)
    ):
        return {"binary": scalar.as_py()}
    if pa.types.is_interval(original):
        value = scalar.as_py()
        return {"months": value.months, "days": value.days, "nanoseconds": value.nanoseconds}
    if pa.types.is_floating(original):
        value = scalar.as_py()
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if pa.types.is_integer(original):
        value = scalar.as_py()
        return value if original.bit_width < 64 else str(value)
    if pa.types.is_boolean(original) or pa.types.is_string(scalar.type):
        return scalar.as_py()
    return str(scalar)


def _plain(original: pa.DataType) -> pa.DataType:
    """Return the type a value has once `precut` decoded it.

    Parameters
    ----------
    original : pyarrow.DataType
        A column or field type.

    Returns
    -------
    pyarrow.DataType
        The value type of a dictionary, otherwise the type itself.
    """
    return original.value_type if pa.types.is_dictionary(original) else original


def _item_original(original: pa.DataType) -> pa.DataType:
    """Return the original type of the elements of a list-like type.

    Parameters
    ----------
    original : pyarrow.DataType
        A list, large list, fixed-size list or map type.

    Returns
    -------
    pyarrow.DataType
        The element type; for a map, the entry struct that `precut` made.
    """
    if pa.types.is_map(original):
        return pa.struct([("key", original.key_type), ("value", original.item_type)])
    return original.value_type


def _cut_string(text: str, room: int) -> str:
    """Write a long string as a cut marker holding its longest prefix that fits.

    Parameters
    ----------
    text : str
        The string.
    room : int
        The bytes left in the cell.

    Returns
    -------
    str
        ``{"cut":true,"text":<prefix>}``, at most `room` bytes.

    Raises
    ------
    _CellOverflow
        When not even an empty marker fits.
    """
    head = text[:MAX_STRING_CHARS]
    available = room - _MARKER_OVERHEAD
    if available < 0:
        raise _CellOverflow
    low, high = 0, len(head)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(json.dumps(head[:middle], ensure_ascii=False)) - 2 <= available:
            low = middle
        else:
            high = middle - 1
    return json.dumps({"cut": True, "text": head[:low]}, ensure_ascii=False, separators=(",", ":"))


def _emit(scalar: pa.Scalar, original: pa.DataType, buf: _CellBuffer, *, top: bool) -> None:
    """Write one value into a cell buffer, walking nested values scalar by scalar.

    Parameters
    ----------
    scalar : pyarrow.Scalar
        The pre-cut value.
    original : pyarrow.DataType
        Its type before `precut`.
    buf : _CellBuffer
        The cell being written.
    top : bool
        Whether the value is the whole cell, so a string may be cut to a prefix
        that fits the budget.

    Raises
    ------
    _CellOverflow
        When the cell outgrows its budget.
    """
    original = _plain(original)
    if isinstance(scalar, pa.UnionScalar) and scalar.is_valid:
        original = original[original.type_codes.index(scalar.type_code)].type
        scalar = scalar.value
    if scalar is None or not scalar.is_valid:
        buf.add("null")
    elif pa.types.is_struct(scalar.type):
        buf.add("{")
        for i in range(scalar.type.num_fields):
            if i:
                buf.add(",")
            buf.add(json.dumps(scalar.type.field(i).name, ensure_ascii=False) + ":")
            _emit(scalar[i], original[i].type, buf, top=False)
        buf.add("}")
    elif pa.types.is_list(scalar.type) or pa.types.is_large_list(scalar.type):
        items = scalar.values
        buf.add("[")
        for i in range(min(len(items), MAX_LIST_ITEMS)):
            if i:
                buf.add(",")
            _emit(items[i], _item_original(original), buf, top=False)
        if len(items) > MAX_LIST_ITEMS:
            buf.add("," + _CUT)
        buf.add("]")
    else:
        leaf = _leaf(scalar, original)
        text = json.dumps(leaf, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if isinstance(leaf, str) and (
            len(leaf) > MAX_STRING_CHARS or (top and _size(text) > buf.room)
        ):
            text = _cut_string(leaf, buf.room)
        buf.add(text)


def _cell(scalar: pa.Scalar, original: pa.DataType, cell_budget: int) -> tuple[str, int]:
    """Write one cell of a row, or its cut marker when it outgrows the budget.

    Parameters
    ----------
    scalar : pyarrow.Scalar
        The pre-cut value of the cell.
    original : pyarrow.DataType
        The column's type before `precut`.
    cell_budget : int
        The most bytes the cell may take.

    Returns
    -------
    tuple
        The JSON text of the cell, at most `cell_budget` bytes, and how many
        bytes were written to get it (a cut cell still cost its work).
    """
    buf = _CellBuffer(cell_budget)
    try:
        _emit(scalar, original, buf, top=True)
    except _CellOverflow:
        return _CUT, buf.used
    return "".join(buf.parts), buf.used


def _placeholders(
    cells: list[str], used: int, remaining: int, page_budget: int
) -> tuple[list[str], int, int, bool]:
    """End a row whose next cell no longer fits, with as many markers as fit.

    Parameters
    ----------
    cells : list of str
        The cells written so far.
    used : int
        The page bytes used so far.
    remaining : int
        How many cells the row still lacks.
    page_budget : int
        The page budget.

    Returns
    -------
    tuple
        The cells, the bytes used, how many cells are left out entirely, and
        ``True`` (the row is cut).
    """
    placed = 0
    while placed < remaining:
        cost = len(_CUT) + (1 if cells else 0)
        if used + cost > page_budget:
            break
        cells.append(_CUT)
        used += cost
        placed += 1
    return cells, used, remaining - placed, True


def _write_row(
    columns: list[pa.ChunkedArray],
    originals: list[pa.DataType],
    k: int,
    used: int,
    page_budget: int,
    cell_budget: int,
    work: list[int],
) -> tuple[list[str], int, int, bool] | None:
    """Write row k of a pre-cut page against the page budget.

    Besides the bytes kept, the page counts the bytes it attempted, so cells
    that are written and then dropped cannot keep the server busy: once the
    attempts reach twice the page budget, the rest of the row is placeholders.

    Parameters
    ----------
    columns : list of pyarrow.ChunkedArray
        The pre-cut columns.
    originals : list of pyarrow.DataType
        The column types before `precut`.
    k : int
        The row's index on the page.
    used : int
        The page bytes used so far.
    page_budget : int
        The page budget.
    cell_budget : int
        The cell budget.
    work : list of int
        One element: the bytes attempted on this page so far, updated here.

    Returns
    -------
    tuple or None
        The cells, the bytes used, the cells left out and whether the row is
        cut; ``None`` when the row cannot start (or, past the first row, when
        its first cell does not fit), so the page ends before it.
    """
    used += 2 + (1 if k else 0)
    if used > page_budget:
        return None
    cells: list[str] = []
    for j, (column, original) in enumerate(zip(columns, originals, strict=True)):
        fits = work[0] < 2 * page_budget
        if fits:
            text, attempted = _cell(column[k], original, cell_budget)
            work[0] += attempted
            cost = _size(text) + (1 if cells else 0)
            fits = used + cost <= page_budget
        if not fits:
            if j == 0 and k:
                return None
            return _placeholders(cells, used, len(columns) - j, page_budget)
        cells.append(text)
        used += cost
    return cells, used, 0, False


def _next(position: int, total: int) -> int | None:
    """Return where the next page starts, or ``None`` at the end.

    Parameters
    ----------
    position : int
        The row after the last one written.
    total : int
        The number of rows the result has, after the row cap.

    Returns
    -------
    int or None
        `position` while rows remain.
    """
    return position if position < total else None


def _write_rows(
    result: pa.Table,
    offset: int,
    count: int,
    total: int,
    used: int,
    page_budget: int,
    cell_budget: int,
) -> tuple[list[str], list[int], int, int | None]:
    """Write the rows of a page, stopping at the page budget.

    Parameters
    ----------
    result : pyarrow.Table
        The whole result.
    offset : int
        The first row of the page.
    count : int
        How many rows the page asks for (at least one).
    total : int
        The number of rows the result has, after the row cap.
    used : int
        The page bytes used by the header and the masks.
    page_budget : int
        The page budget.
    cell_budget : int
        The cell budget.

    Returns
    -------
    tuple
        The rows as JSON text, the indexes of the cut rows, how many cells of
        the cut row were left out, and the next offset.
    """
    table = precut(result.slice(offset, count))
    originals = [field.type for field in result.schema]
    rows: list[str] = []
    work = [0]
    for k in range(count):
        written = _write_row(table.columns, originals, k, used, page_budget, cell_budget, work)
        if written is None:
            return rows, [], 0, offset + k if k else None
        cells, used, left_out, cut = written
        rows.append("[" + ",".join(cells) + "]")
        if cut:
            return rows, [k], left_out, _next(offset + k + 1, total)
    return rows, [], 0, _next(offset + count, total)


def _fit(
    entries: Iterable[str], total: int, used: int, page_budget: int
) -> tuple[list[str], int, int]:
    """Keep the leading entries of a list that fit the page budget.

    Parameters
    ----------
    entries : iterable of str
        The entries as JSON text, produced lazily.
    total : int
        How many entries there are.
    used : int
        The page bytes used so far.
    page_budget : int
        The page budget.

    Returns
    -------
    tuple
        The entries that fit, how many were left out, and the bytes used.
    """
    kept: list[str] = []
    for entry in entries:
        cost = _size(entry) + (1 if kept else 0)
        if used + cost > page_budget:
            break
        kept.append(entry)
        used += cost
    return kept, total - len(kept), used


def _column_entry(field: pa.Field) -> str:
    """Describe a column for the header, with a long type name cut.

    Parameters
    ----------
    field : pyarrow.Field
        The column.

    Returns
    -------
    str
        ``{"name", "type"}`` as JSON text, plus ``"type_cut": true`` when the
        type name was cut to `MAX_TYPE_CHARS`.
    """
    type_name = str(field.type)
    entry: dict[str, object] = {"name": field.name, "type": type_name[:MAX_TYPE_CHARS]}
    if len(type_name) > MAX_TYPE_CHARS:
        entry["type_cut"] = True
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def _preview_json(name: str, room: int) -> str:
    """Write the previewed table's name, cut to a prefix that fits `room`.

    Parameters
    ----------
    name : str
        The dotted table name.
    room : int
        The page bytes still free.

    Returns
    -------
    str
        ``{"table": name}``; the name is cut to `MAX_PREVIEW_CHARS` and to what
        fits, and ``"cut": true`` is added when it was cut.
    """

    def render(length: int) -> str:
        entry: dict[str, object] = {"table": name[:length]}
        if length < len(name):
            entry["cut"] = True
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

    low, high = 0, min(len(name), MAX_PREVIEW_CHARS)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(render(middle)) <= room:
            low = middle
        else:
            high = middle - 1
    return render(low)


def json_page(
    result: pa.Table,
    *,
    offset: int,
    limit: int,
    row_cap: int | None,
    duration_ms: int | None,
    masked_columns: list[dict[str, str]] | None,
    preview_table: str | None,
    page_budget: int = PAGE_BUDGET,
    cell_budget: int = CELL_BUDGET,
) -> bytes:
    """Write one page of a query result as bounded UTF-8 JSON.

    The preview name (cut to 1,024 characters), the `columns` header, the
    `masked_columns` list and the rows are counted against `page_budget` in that
    order, so the answer is never larger than the budget plus a small fixed
    envelope, whatever the data.

    Parameters
    ----------
    result : pyarrow.Table
        The whole result. Only the requested slice is converted.
    offset : int
        The first row of the page.
    limit : int
        The most rows the page holds.
    row_cap : int or None
        The most rows the result exposes (100 for a preview), or ``None``.
    duration_ms : int or None
        How long the query ran, when known.
    masked_columns : list of dict or None
        The masks applied, each ``{"table", "column"}``; ``None`` when unknown.
    preview_table : str or None
        The dotted name of the previewed table, for a preview.
    page_budget : int
        The page budget in bytes.
    cell_budget : int
        The cell budget in bytes.

    Returns
    -------
    bytes
        The UTF-8 JSON body: ``columns``, ``columns_cut``, ``masked_columns``,
        ``masked_columns_cut``, ``rows``, ``row_cut``, ``row_count``,
        ``next_offset``, ``duration_ms`` and ``preview``.
    """
    total = result.num_rows if row_cap is None else min(result.num_rows, row_cap)
    used = 2 + (4 if masked_columns is None else 2) + 2
    preview = "null" if preview_table is None else _preview_json(preview_table, page_budget - used)
    used += _size(preview)
    columns, columns_cut, used = _fit(
        (_column_entry(field) for field in result.schema), len(result.schema), used, page_budget
    )
    masks, masks_cut = None, 0
    if masked_columns is not None:
        entries = (json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in masked_columns)
        masks, masks_cut, used = _fit(entries, len(masked_columns), used, page_budget)
    count = max(0, min(limit, total - offset))
    rows: list[str] = []
    row_cut: list[int] = []
    next_offset = None
    if columns_cut == 0 and count > 0:
        rows, row_cut, columns_cut, next_offset = _write_rows(
            result, offset, count, total, used, page_budget, cell_budget
        )
    body = (
        f'{{"columns":[{",".join(columns)}],"columns_cut":{columns_cut},'
        f'"masked_columns":{"null" if masks is None else "[" + ",".join(masks) + "]"},'
        f'"masked_columns_cut":{masks_cut},"rows":[{",".join(rows)}],"row_cut":{json.dumps(row_cut)},'
        f'"row_count":{total},"next_offset":{json.dumps(next_offset)},'
        f'"duration_ms":{json.dumps(duration_ms)},"preview":{preview}}}'
    )
    return body.encode("utf-8")
