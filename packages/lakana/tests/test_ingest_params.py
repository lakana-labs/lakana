"""`_params.translate`: `:name` outside literals and comments, nothing inside them."""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lakana.ingest._errors import UsageError
from lakana.ingest._params import translate


def test_numeric_style_reuses_the_number_of_a_repeated_name() -> None:
    """PostgreSQL: `$1` twice for one name."""
    sql, values = translate(
        "SELECT * FROM t WHERE a > :since AND b > :since AND c = :x",
        {"since": 1, "x": 2},
        style="numeric",
    )
    assert sql == "SELECT * FROM t WHERE a > $1 AND b > $1 AND c = $2"
    assert values == [1, 2]


@pytest.mark.parametrize(
    "text",
    [
        "SELECT ':no' FROM t",
        'SELECT ":no" FROM t',
        "SELECT $$ :no $$ FROM t",
        "SELECT $tag$ :no $tag$ FROM t",
        "SELECT 1 -- :no\nFROM t",
        "SELECT /* :no /* nested */ :no */ 1",
        "SELECT a::text FROM t",
        "SELECT E'\\' :no' FROM t",
    ],
)
def test_literals_comments_and_casts_are_untouched(text: str) -> None:
    """No placeholder is found, so no value may be given."""
    assert translate(text, None, style="numeric") == (text, [])


def test_pyformat_doubles_a_literal_percent() -> None:
    """PyMySQL formats the text with `%`: a `%` of the query must survive."""
    sql, values = translate(
        "SELECT '5%' AS p, a FROM t WHERE a LIKE :p",
        {"p": "x%"},
        style="pyformat",
        backslash_escapes=True,
    )
    assert sql == "SELECT '5%%' AS p, a FROM t WHERE a LIKE %(p)s"
    assert values == {"p": "x%"}


def test_a_placeholder_without_a_value_and_a_value_without_a_placeholder_are_named() -> None:
    """Both directions fail naming the name."""
    with pytest.raises(UsageError, match="placeholder :since has no value in params"):
        translate("SELECT :since", {}, style="qmark")
    with pytest.raises(UsageError, match="params has since, which the query does not use"):
        translate("SELECT 1", {"since": 1}, style="qmark")


def test_a_none_parameter_is_refused_naming_the_default() -> None:
    """Review Focus 1: comparing with NULL would silently return no rows on the first run."""
    with pytest.raises(
        UsageError, match=r"parameter since is None.*ctx\.watermark\(default=\.\.\.\)"
    ):
        translate("SELECT * FROM t WHERE u > :since", {"since": None}, style="numeric")


_INSIDE = st.text(alphabet=st.sampled_from(list("abc :_-$/*'\"\n")), max_size=30)


@given(_INSIDE)
def test_no_placeholder_is_found_inside_a_dollar_quote(inside: str) -> None:
    """Whatever a dollar-quoted body holds (without its own tag), it is left alone."""
    body = inside.replace("$q$", "")
    text = f"SELECT $q${body}$q$"
    assert translate(text, None, style="numeric") == (text, [])


@given(st.text(alphabet=st.sampled_from(list('abc :_-$/*"\n')), max_size=30))
def test_no_placeholder_is_found_inside_a_single_quoted_literal(inside: str) -> None:
    """A literal's body (its quotes doubled) never yields a placeholder."""
    text = "SELECT '" + inside.replace("'", "''") + "'"
    assert translate(text, None, style="numeric") == (text, [])


@given(st.text(alphabet=st.sampled_from(list("abc :_-$/*'\"")), max_size=30))
def test_no_placeholder_is_found_inside_a_line_comment(inside: str) -> None:
    """A `--` comment's body (no newline) never yields a placeholder."""
    text = f"SELECT 1 -- {inside}\n"
    assert translate(text, None, style="numeric") == (text, [])


@given(st.sampled_from(["SELECT 'a'", "SELECT $$x$$", "SELECT 1 -- c\n", "SELECT /* c */ 1"]))
def test_a_placeholder_outside_any_literal_is_found_once(prefix: str) -> None:
    """A `:name` after the last literal is found, exactly once."""
    sql, values = translate(f"{prefix} + :p", {"p": 7}, style="numeric")
    assert sql == f"{prefix} + $1"
    assert values == [7]


@pytest.mark.parametrize("style", ["numeric", "qmark", "pyformat"])
def test_a_backtick_identifier_is_untouched(style: Literal["numeric", "qmark", "pyformat"]) -> None:
    """MariaDB and SQLite quote identifiers with backticks; PyMySQL would not escape one."""
    text = "SELECT `a:no` FROM t"
    assert translate(text, None, style=style)[0] == text


def test_a_hash_comment_hides_a_placeholder_from_pyformat_only() -> None:
    """MariaDB reads `#` to the line's end as a comment; PostgreSQL reads `#` as an operator."""
    assert translate("SELECT 1 # :no\n", None, style="pyformat") == ("SELECT 1 # :no\n", {})
    assert translate("SELECT 1 # :p", {"p": 1}, style="numeric") == ("SELECT 1 # $1", [1])


def test_a_backslash_escapes_a_double_quote_where_the_database_uses_backslashes() -> None:
    r"""MariaDB's default reads `"a\\" :no"` as one string; the placeholder stays inside it."""
    text = 'SELECT "a\\" :no" FROM t'
    assert translate(text, None, style="pyformat", backslash_escapes=True) == (text, {})


def test_numeric_and_qmark_values_follow_the_text() -> None:
    """`qmark` repeats a value per use; `numeric` numbers each name once, in order of first use."""
    assert translate("SELECT :b, :a, :b", {"a": 1, "b": 2}, style="qmark") == (
        "SELECT ?, ?, ?",
        [2, 1, 2],
    )
    assert translate("SELECT :b, :a, :b", {"a": 1, "b": 2}, style="numeric") == (
        "SELECT $1, $2, $1",
        [2, 1],
    )


@pytest.mark.parametrize(
    "text",
    [
        "SELECT 'it''s :no' FROM t",
        "SELECT ':no",
        "SELECT 1 /* :no",
        "SELECT /* outer /* :no */ still :no",
        "SELECT $q$ :no",
        "SELECT 1 -- :no",
    ],
)
def test_a_doubled_quote_and_an_unterminated_literal_or_comment_hide_the_rest(text: str) -> None:
    """A doubled quote stays inside its literal; an unterminated one runs to the text's end."""
    assert translate(text, None, style="numeric") == (text, [])


def test_pyformat_doubles_a_percent_outside_a_literal_too() -> None:
    """MariaDB's modulo operator survives PyMySQL's formatting."""
    assert translate("SELECT a % 2 FROM t WHERE b = :p", {"p": 1}, style="pyformat") == (
        "SELECT a %% 2 FROM t WHERE b = %(p)s",
        {"p": 1},
    )


# --- pyformat reads quotes and comments as MariaDB does -----------------------------------------
# PyMySQL writes each value, escaped for a string literal, into the text. Wherever this tokenizer
# and MariaDB disagree about where a literal or comment starts or ends, a later placeholder could
# land inside what MariaDB reads as a literal, and the value's own quotes would end it there.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # `--x` is two minus signs in MariaDB, not a comment: the quote after it opens a literal.
        ("SELECT 1 --x, :p", "SELECT 1 --x, %(p)s"),
        # PostgreSQL's dollar quote is no quote in MariaDB.
        ("SELECT $$, :p, $$", "SELECT $$, %(p)s, $$"),
        # MariaDB does not nest block comments: the first `*/` ends it.
        ("SELECT /* /* */ :p", "SELECT /* /* */ %(p)s"),
        # `e'...'` is PostgreSQL's escape string; MariaDB reads e as a name and backslashes by
        # the server's mode alone.
        ("SELECT e'\\', :p", "SELECT e'\\', %(p)s"),
    ],
)
def test_pyformat_reads_comments_and_quotes_as_mariadb_does(text: str, expected: str) -> None:
    """Each placeholder MariaDB reads as code is bound, and nothing else is."""
    assert translate(text, {"p": 1}, style="pyformat") == (expected, {"p": 1})


def test_pyformat_still_reads_a_dash_dash_space_comment() -> None:
    """``-- `` with a space, a tab or a newline after it is MariaDB's line comment."""
    for text in ("SELECT 1 -- :no\n", "SELECT 1 --\t:no\n", "SELECT 1 --"):
        assert translate(text, None, style="pyformat") == (text, {})


@pytest.mark.parametrize("opener", ["/*!", "/*!50110 ", "/*M!", "/*M!100100 "])
def test_pyformat_refuses_an_executable_comment(opener: str) -> None:
    """MariaDB runs the text of `/*! ... */` (or skips it, by version): no telling which."""
    with pytest.raises(UsageError, match=r"executable comment"):
        translate(f"SELECT 1 {opener} ':p' */ , :p", {"p": 1}, style="pyformat")
