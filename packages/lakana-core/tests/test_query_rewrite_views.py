"""Expanding views into subqueries before resolution."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from lakana_core.errors import ValidationError
from lakana_core.query_rewrite import (
    MAX_EXPANDED_VIEWS,
    MAX_VIEW_DEPTH,
    ViewDefinition,
    expand_views,
    extract_table_refs,
)


def _lookup(views: dict[str, str]) -> Callable[[str], ViewDefinition | None]:
    """Build a lookup over `{dotted: sql}`.

    Parameters
    ----------
    views
        The views.

    Returns
    -------
    callable
        `dotted -> ViewDefinition | None`.
    """

    def lookup(dotted: str) -> ViewDefinition | None:
        sql = views.get(dotted)
        return None if sql is None else ViewDefinition(name=dotted, sql=sql)

    return lookup


def test_a_query_without_views_is_returned_unchanged() -> None:
    """No view, no rewrite: the original text comes back."""
    expanded = expand_views("SELECT id FROM bronze.orders", _lookup({}))

    assert expanded.sql == "SELECT id FROM bronze.orders"
    assert expanded.views == ()


def test_a_view_becomes_a_subquery_and_only_base_tables_remain() -> None:
    """After expansion every reference is a base table, so policies apply to them."""
    expanded = expand_views(
        "SELECT v.id, x.n FROM gold.v JOIN gold.x AS x ON v.id = x.id",
        _lookup({"gold.v": "WITH t AS (SELECT id FROM bronze.orders) SELECT id FROM t"}),
    )

    assert expanded.views == ("gold.v",)
    assert "AS v" in expanded.sql
    assert extract_table_refs(expanded.sql) == ["gold.x", "bronze.orders"]


def test_an_alias_on_the_view_reference_is_kept() -> None:
    """`FROM gold.v AS w` still resolves `w.id`."""
    expanded = expand_views(
        "SELECT w.id FROM gold.v AS w", _lookup({"gold.v": "SELECT id FROM bronze.orders"})
    )

    assert "AS w" in expanded.sql


def test_a_column_qualified_with_the_views_full_name_loses_the_schema_part() -> None:
    """`gold.v.id` has to find the subquery that is only called `v` now."""
    expanded = expand_views(
        "SELECT gold.v.id, v.id, other.v.id FROM gold.v",
        _lookup({"gold.v": "SELECT id FROM bronze.orders"}),
    )

    assert "SELECT v.id, v.id, other.v.id FROM" in expanded.sql


def test_a_view_name_used_in_another_scope_keeps_its_qualifiers() -> None:
    """`gold.v` outside and `silver.v` inside are both `v`: no qualifier is cut or rebound."""
    expanded = expand_views(
        "SELECT id FROM gold.v WHERE EXISTS (SELECT 1 FROM silver.v WHERE silver.v.id = gold.v.id)",
        _lookup({"gold.v": "SELECT id FROM bronze.a", "silver.v": "SELECT id FROM bronze.b"}),
    )

    assert "silver.v.id = gold.v.id" in expanded.sql


def test_the_schema_is_cut_off_a_view_column_only_in_the_views_own_scope() -> None:
    """In a subquery the full name stays as written, as for a table."""
    expanded = expand_views(
        "SELECT gold.v.id, (SELECT gold.v.id) FROM gold.v",
        _lookup({"gold.v": "SELECT id FROM bronze.orders"}),
    )

    assert "SELECT v.id, (SELECT gold.v.id) FROM" in expanded.sql


def test_nested_views_expand_recursively_innermost_first() -> None:
    """A view may reference views; each is expanded once per reference."""
    expanded = expand_views(
        "SELECT id FROM gold.v_out",
        _lookup({"gold.v_out": "SELECT id FROM gold.v_in", "gold.v_in": "SELECT id FROM bronze.o"}),
    )

    assert expanded.views == ("gold.v_in", "gold.v_out")
    assert extract_table_refs(expanded.sql) == ["bronze.o"]


def test_a_cte_named_like_a_view_is_not_expanded() -> None:
    """A name the query binds itself in `WITH` is not a catalog reference."""
    expanded = expand_views(
        "WITH v AS (SELECT 1 AS id) SELECT id FROM v", _lookup({"v": "SELECT 2 AS id"})
    )

    assert expanded.views == ()


def test_a_cycle_is_refused_naming_the_path() -> None:
    """No cycle."""
    with pytest.raises(ValidationError, match=r"gold\.a -> gold\.b -> gold\.a"):
        expand_views(
            "SELECT id FROM gold.a",
            _lookup({"gold.a": "SELECT id FROM gold.b", "gold.b": "SELECT id FROM gold.a"}),
        )


def test_a_new_view_can_not_reference_itself_through_another() -> None:
    """`root` puts the view being replaced on the stack, so its new SQL can't close a loop."""
    views = {"gold.a": "SELECT 1 AS id", "gold.b": "SELECT id FROM gold.a"}

    with pytest.raises(ValidationError, match=r"gold\.a -> gold\.b -> gold\.a"):
        expand_views("SELECT id FROM gold.b", _lookup(views), root="gold.a")


def test_views_nest_at_most_ten_deep() -> None:
    """A depth of 10 plans; an eleventh level is refused."""
    chain = {f"gold.v{i}": f"SELECT id FROM gold.v{i + 1}" for i in range(1, MAX_VIEW_DEPTH + 1)}
    chain[f"gold.v{MAX_VIEW_DEPTH + 1}"] = "SELECT id FROM bronze.o"

    expand_views("SELECT id FROM gold.v2", _lookup(chain))
    with pytest.raises(ValidationError, match="more than 10 deep"):
        expand_views("SELECT id FROM gold.v1", _lookup(chain))


def test_a_stored_view_is_rechecked_against_the_ast_guard() -> None:
    """Defence in depth: a view row altered outside Lakana can't smuggle a file read in."""
    with pytest.raises(ValidationError, match="read_parquet"):
        expand_views(
            "SELECT * FROM gold.v", _lookup({"gold.v": "SELECT * FROM read_parquet('x.parquet')"})
        )


def test_a_join_inside_parentheses_on_a_view_reference_expands_correctly() -> None:
    """A parenthesized join's `joins` lives on the Table node itself.

    Before the fix, replacing that node with only its alias silently dropped
    the join to `gold.x`, and the query returned wrong results with no
    error. `gold.x` must survive expansion as a base-table reference
    alongside `bronze.orders`.
    """
    expanded = expand_views(
        "SELECT v.id, x.id FROM (gold.v JOIN gold.x ON v.id = x.id)",
        _lookup({"gold.v": "SELECT id FROM bronze.orders"}),
    )

    assert expanded.views == ("gold.v",)
    assert extract_table_refs(expanded.sql) == ["gold.x", "bronze.orders"]


def test_a_pivot_on_a_view_reference_expands_correctly() -> None:
    """A `PIVOT` attached to a view reference is carried onto the replacing subquery."""
    expanded = expand_views(
        "SELECT * FROM gold.v PIVOT (SUM(amount) FOR region IN ('eu', 'us'))",
        _lookup({"gold.v": "SELECT amount, region FROM bronze.orders"}),
    )

    assert expanded.views == ("gold.v",)
    assert extract_table_refs(expanded.sql) == ["bronze.orders"]
    assert "PIVOT" in expanded.sql


def test_a_tablesample_on_a_view_reference_expands_correctly() -> None:
    """A `TABLESAMPLE` attached to a view reference is carried onto the replacing subquery."""
    expanded = expand_views(
        "SELECT * FROM gold.v TABLESAMPLE 10%", _lookup({"gold.v": "SELECT id FROM bronze.orders"})
    )

    assert expanded.views == ("gold.v",)
    assert extract_table_refs(expanded.sql) == ["bronze.orders"]
    assert "TABLESAMPLE" in expanded.sql


def test_a_plain_alias_and_a_normal_join_still_work_after_the_arg_fix() -> None:
    """The common cases the arg-carrying fix must not regress: alias and an outer join."""
    aliased = expand_views(
        "SELECT w.id FROM gold.v AS w", _lookup({"gold.v": "SELECT id FROM bronze.orders"})
    )
    assert "AS w" in aliased.sql

    joined = expand_views(
        "SELECT v.id, x.n FROM gold.v JOIN gold.x AS x ON v.id = x.id",
        _lookup({"gold.v": "SELECT id FROM bronze.orders"}),
    )
    assert extract_table_refs(joined.sql) == ["gold.x", "bronze.orders"]


def test_an_unsupported_construct_on_a_view_reference_is_refused() -> None:
    """A construct with no established-safe transplant fails closed, not silently."""
    with pytest.raises(ValidationError, match=r"'gold\.v' can't be used with hints yet"):
        expand_views(
            "SELECT * FROM gold.v WITH (NOLOCK)",
            _lookup({"gold.v": "SELECT id FROM bronze.orders"}),
        )


def test_a_lateral_view_on_a_view_reference_is_refused() -> None:
    """`LATERAL VIEW` has no valid transplant onto a subquery; it fails closed with a clear error.

    Carried over, it rendered SQL DuckDB refused with a raw parser error.
    """
    with pytest.raises(ValidationError, match=r"'gold\.v' can't be used with laterals yet"):
        expand_views(
            "SELECT * FROM (gold.v LATERAL VIEW EXPLODE(tags) t AS tag)",
            _lookup({"gold.v": "SELECT id, tags FROM bronze.orders"}),
        )


def test_the_same_view_referenced_twice_is_looked_up_once() -> None:
    """A repeated reference costs one lookup, not one per occurrence."""
    calls: list[str] = []

    def counting_lookup(dotted: str) -> ViewDefinition | None:
        calls.append(dotted)
        if dotted == "gold.v":
            return ViewDefinition(name=dotted, sql="SELECT id FROM bronze.orders")
        return None

    expand_views("SELECT * FROM gold.v a JOIN gold.v b ON a.id = b.id", counting_lookup)

    assert calls.count("gold.v") == 1


def test_expansion_is_capped_against_reference_blowup() -> None:
    """5 references per level over 6 levels must refuse quickly.

    Without a cap this is 5**6 = 15625 substitutions; with one it fails
    well before that. The bound is on the work done (definitions looked up), which
    does not depend on the machine; the time bound only catches a hang, and is wide
    because the suite also runs under coverage on a busy machine (1.009 s was seen).
    """
    fanout, levels = 5, 6
    views = {
        f"gold.v{level}": " UNION ALL ".join(
            f"SELECT id FROM gold.v{level + 1}" for _ in range(fanout)
        )
        for level in range(1, levels)
    }
    views[f"gold.v{levels}"] = "SELECT id FROM bronze.o"

    lookup = _lookup(views)
    looked_up: list[str] = []

    def counting_lookup(dotted: str) -> ViewDefinition | None:
        looked_up.append(dotted)
        return lookup(dotted)

    started = time.monotonic()
    with pytest.raises(ValidationError, match=f"more than {MAX_EXPANDED_VIEWS}"):
        expand_views("SELECT id FROM gold.v1", counting_lookup)
    elapsed = time.monotonic() - started

    assert len(looked_up) <= MAX_EXPANDED_VIEWS + fanout, len(looked_up)
    assert elapsed < 10.0, f"expansion took {elapsed:.3f}s; the cap must fail fast, not hang"


@pytest.mark.parametrize(
    "stored", ["SELECT id, $1 AS u FROM bronze.orders", "SELECT id FROM bronze.orders WHERE id > ?"]
)
def test_a_stored_view_with_a_placeholder_is_refused_at_expansion(stored: str) -> None:
    """The placeholder rule applies to stored view SQL: a placeholder could read a URL list too.

    Creation refuses one, but a row predating the fix, or written some other
    way, must still never reach the rewriter. The message is the fixed
    text, echoing neither the view's SQL nor anything bound.
    """
    with pytest.raises(ValidationError, match="placeholders are not allowed") as caught:
        expand_views("SELECT id FROM gold.v", _lookup({"gold.v": stored}))

    assert "bronze.orders" not in str(caught.value)


@pytest.mark.parametrize(
    "stored",
    [
        "SELECT current_query() AS q FROM bronze.orders",
        "SELECT * FROM (SELECT id, current_query() AS q FROM bronze.orders) AS s",
        "WITH c AS (SELECT current_query() AS q) SELECT q FROM c",
    ],
)
def test_a_stored_view_calling_current_query_is_refused_at_expansion(stored: str) -> None:
    """The introspection ban applies to stored view SQL: it would return the rewritten statement.

    The message names the function only, never the view's SQL or a table.
    """
    with pytest.raises(ValidationError, match="current_query may not be called") as caught:
        expand_views("SELECT q FROM gold.v", _lookup({"gold.v": stored}))

    assert "bronze.orders" not in str(caught.value)


@pytest.mark.parametrize("function_name", ["setval", "__internal_barrier"])
def test_a_stored_view_calling_a_refused_duckdb_2_function_is_refused_at_expansion(
    function_name: str,
) -> None:
    """A view's stored SQL may not call a state-changing or DuckDB-internal function either.

    Parameters
    ----------
    function_name
        The refused function the stored SQL calls.
    """
    stored = f"SELECT id, {function_name}(id, 1) AS v FROM bronze.orders"

    with pytest.raises(ValidationError, match=function_name) as caught:
        expand_views("SELECT v FROM gold.v", _lookup({"gold.v": stored}))

    assert "bronze.orders" not in str(caught.value)


def test_an_expanded_view_renders_its_lambdas_in_the_keyword_form() -> None:
    """A view stored with `x -> …` (valid on DuckDB 1.5) expands to the form DuckDB 2 runs."""
    expanded = expand_views(
        "SELECT doubled FROM gold.v",
        _lookup({"gold.v": "SELECT list_transform(tags, x -> x * 2) AS doubled FROM bronze.t"}),
    )

    assert "LIST_TRANSFORM(tags, LAMBDA x : x * 2)" in expanded.sql
    assert "->" not in expanded.sql


def test_a_stored_view_with_a_sort_by_is_named_as_unreadable() -> None:
    """A view saved with `SORT BY` before sqlglot 30.25 is unreadable, not the reader's mistake.

    sqlglot 30.24 parsed `SORT BY` (and dropped it), so such a view can be stored. The reader
    never wrote the clause: they get the view's name and its owner as the next step, exactly as
    for any other stored SQL that no longer parses, and not the `ORDER BY` hint.
    """
    lookup = _lookup({"gold.v": "SELECT a FROM bronze.secret_tbl SORT BY a"})

    with pytest.raises(ValidationError) as caught:
        expand_views("SELECT * FROM gold.v", lookup)

    assert str(caught.value) == "View 'gold.v' could not be read. Ask its owner to recreate it."
