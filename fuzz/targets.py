r"""What each fuzz harness checks: a refusal or a result, never an unexpected exception.

Each target takes the fuzzer's bytes, decodes them as the platform would receive the text and
runs the parser. It returns when the parser accepted the text or refused it with one of the
exceptions named as that parser's refusals below; any other exception propagates, and Atheris
reports it as a crash with the input that caused it.

The text is decoded with ``surrogatepass``: a JSON body can carry a lone surrogate as an escape
(``"\ud800"``), which Python's JSON parser keeps, so text that reaches a parser through the REST
API can hold one (`parse_access_file` refuses it on purpose). Bytes that are not UTF-8 at all are
skipped, since no caller hands such text on.

No Atheris here, so the test suite (``scripts/tests/test_fuzz.py``) runs every seed through these
targets on every pull request.
"""

from __future__ import annotations

import contextlib
import inspect
import sys
from collections.abc import Callable

import sqlglot.errors

from lakana_core.access.file import parse_access_file
from lakana_core.errors import LakanaError, ValidationError
from lakana_core.lineage import extract_lineage
from lakana_core.projects.frontmatter import parse_front_matter
from lakana_core.projects.project_config import parse_project_config
from lakana_core.query_rewrite import (
    TableSource,
    _parse_sql,
    check_blacklist,
    extract_table_refs,
    rewrite_query,
)
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS

FAIL_CLOSED_PASSES: tuple[type[BaseException], ...] = (LakanaError, MemoryError, *SQL_SYNTAX_ERRORS)
"""What `lakana_core.query_rewrite._fail_closed` lets through unchanged.

Every other exception it turns into a refusal: in production a bug in the guard never lets a
query through, but the wrapper would also hide that bug from the fuzzer, so the target calls the
functions it wraps. ``scripts/tests/test_fuzz.py`` keeps this tuple equal to the wrapper's.
"""

GUARD_REFUSALS: tuple[type[BaseException], ...] = (
    LakanaError,
    sqlglot.errors.ParseError,
    sqlglot.errors.TokenError,
)
"""What the guard, the lineage and the rewrite may raise for a query they do not accept.

Narrower than `FAIL_CLOSED_PASSES`, since the fuzzer looks for what production tolerates:

- a `RecursionError` is a refusal only when parsing the text alone (`_parse_sql`, the guard's
  own parse) raises it too: the parser's limit on nesting. One raised by a walker of the guard,
  the lineage or the rewrite over a tree that parsed is a crash: the reader would get a refusal
  that reads as a syntax error for SQL that has none.
- a `MemoryError` is always a crash: no input of 4 KiB (the fuzzers' ``max_len``) should exhaust
  memory.
"""

PARSER_REFUSALS: tuple[type[BaseException], ...] = (ValidationError,)
"""What the project-file and access-file parsers may raise for a file they do not accept.

`ProjectValidationError` (asset front-matter and ``lakana.yaml``) and `AccessFileError` (the
access file) are both `ValidationError`, and so is the YAML loader's `YamlError`. Anything else,
a pydantic error that escaped or a `RecursionError` from the YAML loader, is a bug.
"""

_check_blacklist: Callable[..., None] = inspect.unwrap(check_blacklist)
_extract_table_refs: Callable[..., list[str]] = inspect.unwrap(extract_table_refs)
_rewrite_query: Callable[..., object] = inspect.unwrap(rewrite_query)
_extract_lineage: Callable[..., object] = extract_lineage

_PLAIN = TableSource(
    urls=("s3://lakana/warehouse/t/data/0.parquet",),
    file_format="parquet",
    columns={"id": "BIGINT", "region": "VARCHAR", "email": "VARCHAR"},
)
"""A table read as is: the reader call without a column list."""

_WITH_POLICIES = TableSource(
    urls=("s3://lakana/warehouse/t/data/0.parquet", "s3://lakana/warehouse/t/data/1.parquet"),
    file_format="parquet",
    columns={"id": "BIGINT", "region": "VARCHAR", "email": "VARCHAR"},
    row_filters=("region = 'EU'",),
    column_masks={"email": "'***'"},
)
"""A table with a row filter and a column mask, which the rewrite builds a column list for."""

_EMPTY = TableSource(urls=(), file_format="parquet", columns={"id": "BIGINT"})
"""A table with no files, which the rewrite replaces with a typed relation of no rows."""

_SOURCES = (_PLAIN, _WITH_POLICIES, _EMPTY)


def decode(data: bytes) -> str | None:
    """Decode the fuzzer's bytes as the platform would receive text.

    Parameters
    ----------
    data
        The fuzzer's input.

    Returns
    -------
    str or None
        The text, lone surrogates kept; `None` for bytes that are not UTF-8.
    """
    try:
        return data.decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return None


PARSE_HEADROOM = 16
"""Frames by which `parse_alone_recurses` lowers the recursion limit for its parse.

The guard, the lineage and the rewrite call the parser a few frames deeper than
`parse_alone_recurses` does (``query_guard``, ``check_blacklist``, ``_parse_with_hints``,
``_parse_sql``: one more than ``query_guard``, ``parse_alone_recurses``, ``_parse_sql``; two more
through ``_parse_checked``). A text whose parse ends within those frames of the limit recursed
there and not here, and the target reported the guard's syntax refusal as a crash (#733). The
headroom covers those frames with room for a wrapper or two. It is less than one level of
nesting for most of the parser's constructs (about 23 frames a level for parentheses, 19 for
``CASE``, 18 for ``{``) and under two for a subquery (about 10), so a walker's `RecursionError`
over a tree that parses well within the limit is still a crash.
"""


def parse_alone_recurses(sql: str) -> bool:
    """Say whether parsing the text alone hits the parser's limit on nesting.

    The parse runs with the recursion limit lowered by `PARSE_HEADROOM`, as though it were called
    from at least as deep as the target's own parses: where the stack stands when the fuzzer finds
    a text must not decide whether the text is a refusal or a crash.

    Parameters
    ----------
    sql
        The query text.

    Returns
    -------
    bool
        `True` when the guard's own parse (`_parse_sql`) raises `RecursionError`.
    """
    limit = sys.getrecursionlimit()
    try:
        # Python 3.11 raises RecursionError here when the stack is already deeper than the
        # lowered limit; the parse would then recurse too, and `finally` restores the limit.
        sys.setrecursionlimit(limit - PARSE_HEADROOM)
        _parse_sql(sql, "duckdb")
    except RecursionError:
        return True
    except GUARD_REFUSALS:
        pass
    finally:
        sys.setrecursionlimit(limit)
    return False


def query_guard(data: bytes) -> None:
    """Run a query through the guard and, once it passes, through the lineage and the rewrite.

    The order is the query path's (`lakana_worker.pipeline`): `check_blacklist`, then
    `extract_table_refs` and `extract_lineage`, then `rewrite_query` with a source for every
    table the query names. `extract_lineage` has no `_fail_closed` wrapper, so an unexpected
    exception there would reach the reader as a server error.
    Each table gets one of three sources, by its position, so that the plain read, the read
    with a row filter and a column mask, and the empty relation are all exercised.

    Parameters
    ----------
    data
        The fuzzer's input, read as SQL text.

    Raises
    ------
    Exception
        Any exception outside `GUARD_REFUSALS`, unchanged: that is the finding.
    RecursionError
        Unless parsing the text alone raises it too (`parse_alone_recurses`).
    """
    sql = decode(data)
    if sql is None:
        return
    try:
        _check_blacklist(sql)
        refs = _extract_table_refs(sql)
        _extract_lineage(sql)
        sources = {ref: _SOURCES[index % len(_SOURCES)] for index, ref in enumerate(refs)}
        _rewrite_query(sql, sources)
    except GUARD_REFUSALS:
        return
    except RecursionError:
        if not parse_alone_recurses(sql):
            raise


def project_files(data: bytes) -> None:
    """Parse the text as an asset file's front-matter and as a ``lakana.yaml``.

    Parameters
    ----------
    data
        The fuzzer's input, read as a file's content.

    Raises
    ------
    Exception
        Any exception outside `PARSER_REFUSALS`, unchanged: that is the finding.
    """
    text = decode(data)
    if text is None:
        return
    with contextlib.suppress(PARSER_REFUSALS):
        parse_front_matter(text, path="assets/silver/orders.sql")
    try:
        parse_project_config(text)
    except PARSER_REFUSALS:
        return


def access_file(data: bytes) -> None:
    """Parse the text as an access file.

    Parameters
    ----------
    data
        The fuzzer's input, read as the file's content.

    Raises
    ------
    Exception
        Any exception outside `PARSER_REFUSALS`, unchanged: that is the finding.
    """
    text = decode(data)
    if text is None:
        return
    try:
        parse_access_file(text, file_name="access.yaml")
    except PARSER_REFUSALS:
        return


TARGETS: dict[str, Callable[[bytes], None]] = {
    "query_guard": query_guard,
    "project_files": project_files,
    "access_file": access_file,
}
"""Each harness's name (``fuzz/<name>_fuzzer.py``, its seeds in ``fuzz/seeds/<name>.json``)."""
