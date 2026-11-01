"""`ast` discovery of one Python asset file.

The file is parsed, never imported or executed: deploy runs in the worker,
and repository code only ever runs inside a task's subprocess. Everything a
file can contain ends as a `ProjectIssue`.
"""

from __future__ import annotations

import ast
import hashlib
import threading
import warnings

import pydantic

from lakana_core.artifacts.manifest import shown
from lakana_core.connections.kinds import NAME
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.projects.asset_model import (
    DECIMALS_MODES,
    DEFAULT_DECIMALS,
    EVOLVE_NEEDS_REPLACE,
    KEY_NONE,
    MAX_CONNECTIONS,
    MAX_KEEP_VERSIONS,
    PYTHON_HISTORY_LATER,
    WATERMARK_MESSAGE,
    WRITE_CHOICES,
    ParsedAsset,
    key_issues,
    model_issues,
    write_choice_message,
    write_rule_issues,
)
from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.frontmatter import (
    IDENTIFIER_MESSAGE,
    AssetMetadata,
    merge_metadata,
    unsafe_identifier,
)
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.project_config import AssetDefaults, pydantic_issues

PYTHON_SOURCE_PREFIX = "python:sha256:"
"""Prefix of a Python asset's `normalized_sql`: the digest of its source, for the diff."""

_KNOWN = (
    "inputs",
    "description",
    "label",
    "owner",
    "tags",
    "materialize",
    "columns",
    "retries",
    "retry_delay",
    "timeout",
    "output",
    "keep_versions",
    "decimals",
    "connections",
    "write",
    "key",
    "watermark",
    "evolve_schema",
    "kind",
    "grain",
    "references",
    "measures",
    "checks",
)
_SHAPES: dict[str, tuple[type | tuple[type, ...], str]] = {
    "inputs": (dict, "a dict of strings"),
    "columns": (dict, "a dict of strings or dicts"),
    "label": (str, "a string"),
    "tags": (list, "a list of strings"),
    "description": (str, "a string"),
    "owner": (str, "a string"),
    "materialize": (str, "a string"),
    "output": (str, "a string"),
    "decimals": (str, "a string"),
    "connections": (list, "a list of strings"),
    "write": (str, "a string"),
    "key": ((list, str), "a list of strings or one string"),
    "kind": (str, "a string"),
    "grain": (str, "a string"),
    "references": (dict, "a dict of strings"),
    "measures": (dict, "a dict of dicts"),
    "checks": (dict, "a dict of strings"),
    "watermark": (str, "a string"),
    "evolve_schema": (bool, "True or False"),
}
_TWO_LEVEL = frozenset({"measures", "columns"})
"""Arguments whose values may be dicts of literal dicts, read with `_measures_literal`."""
_DEFAULTS: dict[str, tuple[object, ...]] = {
    "write": ("replace",),
    "key": ([],),
    "evolve_schema": (False,),
}
"""Values of the write model that an artifact asset may still give: they change nothing."""
_NOT_FOR_ARTIFACTS = (
    "label",
    "materialize",
    "columns",
    "write",
    "key",
    "watermark",
    "evolve_schema",
    "kind",
    "grain",
    "references",
    "measures",
    "checks",
)
"""Arguments that describe a table, refused on an artifact asset unless `_DEFAULTS` has them."""
CONTEXT_PARAMETER = "ctx"
"""The parameter that receives the asset's context: never an input."""
_CONTEXT_IS_NOT_AN_INPUT = "`ctx` is the asset's context; give the input another parameter name"
_ALIAS_HINT = (
    "the decorator must be lakana.asset: use `from lakana import asset` with `@asset(...)`, "
    "or `import lakana` with `@lakana.asset(...)`; aliases and re-exports are not resolved"
)
_ONLY_DECORATOR = "the asset function must have @asset(...) as its only decorator"
_PARENTHESES = "write @asset(...) with parentheses"
_NOT_LITERAL = object()
_DUPLICATE_KEYS = object()

_Function = ast.FunctionDef | ast.AsyncFunctionDef


def _quoted(value: object) -> str:
    """Show text from the file in a message: ASCII-escaped, cut, in single quotes.

    Parameters
    ----------
    value
        A name or a string the file wrote; untrusted and of any length (`shown` names the
        type of anything else).

    Returns
    -------
    str
        The value in single quotes, at most 200 characters inside them.
    """
    return f"'{shown(value)}'"


def _bound_names(node: ast.stmt) -> set[str]:
    """Find the names a top-level statement binds, outside nested functions and classes.

    Parameters
    ----------
    node
        One statement of the module's top level.

    Returns
    -------
    set of str
        The names it assigns, defines, imports or loops over, however deep inside the
        statement's own blocks (an ``if`` or ``try`` body counts; a function body does not).
    """
    names: set[str] = set()
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(current.name)
            continue
        if isinstance(current, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # A comprehension has a scope of its own: its loop variables are not the module's.
            # What leaks out of it: a `:=` target anywhere inside (PEP 572), and the first
            # iterable, which is evaluated in the enclosing scope.
            names.update(
                inner.target.id for inner in ast.walk(current) if isinstance(inner, ast.NamedExpr)
            )
            pending.append(current.generators[0].iter)
            continue
        if isinstance(current, ast.Name) and isinstance(current.ctx, (ast.Store, ast.Del)):
            names.add(current.id)
        elif isinstance(current, (ast.MatchAs, ast.MatchStar)) and current.name is not None:
            names.add(current.name)
        elif isinstance(current, ast.MatchMapping) and current.rest is not None:
            names.add(current.rest)
        elif isinstance(current, (ast.Import, ast.ImportFrom)):
            for alias in current.names:
                top = alias.name.split(".")[0]
                # `import lakana.sub` binds `lakana` to the same module: not a rebinding.
                same_module = isinstance(current, ast.Import) and alias.asname is None
                if not (same_module and top == "lakana"):
                    names.add(alias.asname or top)
        elif isinstance(current, ast.ExceptHandler) and current.name is not None:
            names.add(current.name)
        pending.extend(ast.iter_child_nodes(current))
    return names


def _bindings(node: ast.stmt, name_bound: bool, module_bound: bool) -> tuple[bool, bool]:
    """Update how the module imports the decorator after one top-level statement.

    Parameters
    ----------
    node
        The statement just executed.
    name_bound
        Whether `asset` was bound to `lakana.asset` before it.
    module_bound
        Whether `lakana` was bound to the module before it.

    Returns
    -------
    tuple of bool
        Whether ``from lakana import asset`` binds `asset`, and whether
        ``import lakana`` binds `lakana`, now. An ``as`` alias binds neither, and a later
        statement that binds the same name to anything else (an assignment, a ``def``, another
        import) takes the binding away.
    """
    rebound = _bound_names(node)
    if "asset" in rebound:
        name_bound = False
    if "lakana" in rebound:
        module_bound = False
    if isinstance(node, ast.ImportFrom) and node.module == "lakana" and node.level == 0:
        name_bound = name_bound or any(
            alias.name == "asset" and alias.asname in (None, "asset") for alias in node.names
        )
    elif isinstance(node, ast.Import):
        module_bound = module_bound or any(
            alias.name == "lakana" and alias.asname in (None, "lakana") for alias in node.names
        )
    return name_bound, module_bound


def _classify(decorator: ast.expr, name_bound: bool, module_bound: bool) -> str:
    """Decide what a decorator is.

    Parameters
    ----------
    decorator
        One decorator expression.
    name_bound
        `_bindings`' first value.
    module_bound
        `_bindings`' second value.

    Returns
    -------
    str
        ``asset`` for `lakana.asset`, ``lookalike`` for something named
        `asset` that does not resolve to it, ``other`` for anything else.
    """
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name) and target.id == "asset":
        return "asset" if name_bound else "lookalike"
    if isinstance(target, ast.Attribute) and target.attr == "asset":
        base = target.value
        if isinstance(base, ast.Name) and base.id == "lakana" and module_bound:
            return "asset"
        return "lookalike"
    return "other"


def _strings(nodes: list[ast.expr]) -> list[str] | None:
    """Read a list of string literals.

    Parameters
    ----------
    nodes
        The element expressions.

    Returns
    -------
    list of str or None
        The strings, or `None` if any element is not a string literal.
    """
    values: list[str] = []
    for node in nodes:
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            return None
        values.append(node.value)
    return values


def _literal(node: ast.expr) -> object:
    """Evaluate a decorator argument without evaluating code.

    Parameters
    ----------
    node
        The argument's expression.

    Returns
    -------
    object
        `None`, a string, a number, a list of strings or a dict of strings;
        `_DUPLICATE_KEYS` for a dict with a repeated key, `_NOT_LITERAL` for
        anything else (a name, a call, an f-string, bytes).
    """
    if isinstance(node, ast.Constant):
        value = node.value
        return value if value is None or isinstance(value, (str, int, float)) else _NOT_LITERAL
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        return -node.operand.value
    if isinstance(node, (ast.List, ast.Tuple)):
        items = _strings(node.elts)
        return items if items is not None else _NOT_LITERAL
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            return _NOT_LITERAL
        keys = _strings([key for key in node.keys if key is not None])
        if keys is not None and len(set(keys)) != len(keys):
            return _DUPLICATE_KEYS
        values = _strings(node.values)
        if keys is None or values is None:
            return _NOT_LITERAL
        return dict(zip(keys, values, strict=True))
    return _NOT_LITERAL


def _display_keys(node: ast.Dict) -> list[str] | object:
    """Read the keys of a dict display that holds nested values.

    Parameters
    ----------
    node
        The dict display.

    Returns
    -------
    list of str or object
        The keys; `_NOT_LITERAL` for a `**` unpacking or a key that is not a string literal;
        `_DUPLICATE_KEYS` for a repeated key.
    """
    if any(key is None for key in node.keys):
        return _NOT_LITERAL
    keys = _strings([key for key in node.keys if key is not None])
    if keys is None:
        return _NOT_LITERAL
    if len(set(keys)) != len(keys):
        return _DUPLICATE_KEYS
    return keys


def _measure_literal(node: ast.expr) -> object:
    """Evaluate one measure of `measures=`: a dict of strings to strings or lists of strings.

    Parameters
    ----------
    node
        The measure's expression.

    Returns
    -------
    object
        The dict; `_literal`'s result for anything that is not a dict display (the metadata
        model refuses it with its own text); `_DUPLICATE_KEYS` for a repeated key;
        `_NOT_LITERAL` for a key that is not a string or a value that is neither `_literal`'s
        nor a non-dict.
    """
    if not isinstance(node, ast.Dict):
        return _literal(node)
    keys = _display_keys(node)
    if not isinstance(keys, list):
        return keys
    values = [_literal(value) for value in node.values]
    if any(v is _NOT_LITERAL or v is _DUPLICATE_KEYS or isinstance(v, dict) for v in values):
        return _NOT_LITERAL
    return dict(zip(keys, values, strict=True))


def _measures_literal(node: ast.expr) -> object:
    """Evaluate `measures=` or `columns=`: a dict of names to literals or `_measure_literal` dicts.

    Parameters
    ----------
    node
        The argument's expression.

    Returns
    -------
    object
        The dict; `_literal`'s result for anything that is not a dict display (the shape check
        refuses it); `_DUPLICATE_KEYS` or `_NOT_LITERAL` as for one measure.
    """
    if not isinstance(node, ast.Dict):
        return _literal(node)
    keys = _display_keys(node)
    if not isinstance(keys, list):
        return keys
    values = [_measure_literal(value) for value in node.values]
    for value in values:
        if value is _NOT_LITERAL or value is _DUPLICATE_KEYS:
            return value
    return dict(zip(keys, values, strict=True))


def _arguments(
    call: ast.Call, path: str, issues: list[ProjectIssue]
) -> tuple[dict[str, object], set[str]]:
    """Read the decorator's keyword arguments.

    Parameters
    ----------
    call
        The decorator call.
    path
        Repository-relative path, for issues.
    issues
        Collects issues.

    Returns
    -------
    tuple
        Known arguments with a literal value of the right shape; `None`
        values are left out (they mean "not set"), except for `output` and
        `decimals`, which the decorator refuses as `None` and so does deploy. And the names of
        the known arguments that were left out after an issue, so a caller
        need not read the issues to find them.
    """
    line = call.lineno
    if call.args:
        issues.append(ProjectIssue(path, line, "@asset takes keyword arguments only"))
    values: dict[str, object] = {}
    seen: set[str] = set()
    dropped: set[str] = set()
    for keyword in call.keywords:
        name = keyword.arg
        if name is None:
            issues.append(
                ProjectIssue(path, line, "keyword arguments must be written out, not unpacked")
            )
            continue
        if name not in _KNOWN:
            issues.append(ProjectIssue(path, line, f"unknown argument {_quoted(name)}"))
            continue
        if name in seen:
            issues.append(ProjectIssue(path, keyword.lineno, f"repeats argument {name!r}"))
            continue
        seen.add(name)
        value = _measures_literal(keyword.value) if name in _TWO_LEVEL else _literal(keyword.value)
        if value is _DUPLICATE_KEYS:
            issues.append(ProjectIssue(path, line, f"argument {name!r} repeats a key"))
            dropped.add(name)
            continue
        if value is _NOT_LITERAL:
            issues.append(ProjectIssue(path, line, f"argument {name!r} must be a literal"))
            dropped.add(name)
            continue
        if value is None and name not in ("output", "decimals"):
            continue
        shape = _SHAPES.get(name)
        if shape is not None and not isinstance(value, shape[0]):
            issues.append(ProjectIssue(path, line, f"argument {name!r} must be {shape[1]}"))
            dropped.add(name)
            continue
        if (
            name == "inputs"
            and isinstance(value, dict)
            and any(unsafe_identifier(item) for item in (*value, *value.values()))
        ):
            issues.append(ProjectIssue(path, line, f"an input {IDENTIFIER_MESSAGE}"))
            dropped.add(name)
            continue
        values[name] = value
    return values, dropped


def _output(
    values: dict[str, object],
    dropped: set[str],
    path: str,
    line: int,
    issues: list[ProjectIssue],
) -> tuple[str, int | None]:
    """Read what the asset produces and how many versions it keeps.

    A number is never turned into text here: Python raises on a number of more
    than 4300 digits, and a deploy must report an issue, not fail.

    Parameters
    ----------
    values
        `_arguments`' first result.
    dropped
        `_arguments`' second result.
    path
        Repository-relative path, for issues.
    line
        The decorator's line.
    issues
        Collects issues.

    Returns
    -------
    tuple
        ``table`` or ``artifact``, and `keep_versions` (`None` keeps all).
    """
    output = values.get("output", "table")
    # `_arguments` leaves out an `output` of the wrong shape and has already said so.
    output_is_wrong = output not in ("table", "artifact") or "output" in dropped
    if output not in ("table", "artifact"):
        issues.append(
            ProjectIssue(
                path,
                line,
                f"argument 'output' must be 'table' or 'artifact', got {_quoted(output)}",
            )
        )
        output = "table"
    keep = values.get("keep_versions")
    kept: int | None = None
    if keep is not None:
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
            issues.append(
                ProjectIssue(
                    path, line, "argument 'keep_versions' must be an integer of at least 1"
                )
            )
        elif keep > MAX_KEEP_VERSIONS:
            issues.append(
                ProjectIssue(
                    path,
                    line,
                    f"argument 'keep_versions' must be at most {MAX_KEEP_VERSIONS}; "
                    "remove it to keep every version",
                )
            )
        elif output != "artifact" and not output_is_wrong:
            issues.append(
                ProjectIssue(
                    path,
                    line,
                    "argument 'keep_versions' applies only to an asset with "
                    'output="artifact"; remove it or set output="artifact"',
                )
            )
        else:
            kept = keep
    if output == "artifact":
        # `write="replace"` and `key=[]` say what an artifact asset does anyway.
        given = {
            name
            for name in _NOT_FOR_ARTIFACTS
            if name in values and values[name] not in _DEFAULTS.get(name, ())
        }
        for name in _NOT_FOR_ARTIFACTS:
            if name in given:
                issues.append(
                    ProjectIssue(
                        path,
                        line,
                        f"argument {name!r} does not apply to an artifact asset; remove it",
                    )
                )
    return str(output), kept


def _decimals(values: dict[str, object], path: str, line: int, issues: list[ProjectIssue]) -> str:
    """Read how SQL decimals reach the function.

    Parameters
    ----------
    values
        `_arguments`' first result.
    path
        Repository-relative path, for issues.
    line
        The decorator's line.
    issues
        Collects issues.

    Returns
    -------
    str
        ``float`` (also after an issue, and when the argument is absent) or ``exact``.
    """
    decimals = values.get("decimals", DEFAULT_DECIMALS)
    if decimals in DECIMALS_MODES:
        return str(decimals)
    # A value of the wrong shape (not a string) never reaches `values`: `_arguments` reported it.
    issues.append(
        ProjectIssue(
            path,
            line,
            f"argument 'decimals' must be 'float' or 'exact', got {_quoted(decimals)}",
        )
    )
    return DEFAULT_DECIMALS


def _connections(
    values: dict[str, object], path: str, line: int, issues: list[ProjectIssue]
) -> tuple[str, ...]:
    """Read the connections the asset declares.

    Parameters
    ----------
    values
        `_arguments`' first result; a `connections` of the wrong shape never reaches it.
    path
        Repository-relative path, for issues.
    line
        The decorator's line.
    issues
        Collects issues.

    Returns
    -------
    tuple of str
        The names in the order given; empty when the argument is absent.
    """
    declared = values.get("connections")
    names: list[str] = declared if isinstance(declared, list) else []
    if len(names) > MAX_CONNECTIONS:
        issues.append(
            ProjectIssue(path, line, f"an asset declares at most {MAX_CONNECTIONS} connections")
        )
    seen: set[str] = set()
    for name in names:
        if not NAME.match(name):
            issues.append(
                ProjectIssue(
                    path,
                    line,
                    "connection names are lower-case letters, digits and '-', starting with "
                    f"a letter, at most 63; got {_quoted(name)}",
                )
            )
        elif name in seen:
            issues.append(ProjectIssue(path, line, f"connection `{name}` is listed twice"))
        seen.add(name)
    return tuple(names)


def _write_model(
    values: dict[str, object], path: str, line: int, issues: list[ProjectIssue]
) -> tuple[str | None, tuple[str, ...] | str, str | None]:
    """Read how a run writes the asset's table, with the rules a SQL block follows.

    One rule is the decorator's own: ``evolve_schema=True`` needs ``write="replace"``, because
    only the upload that replaces the table can change its schema (a SQL block's
    ``evolve_schema`` is for ``write: append``).

    Parameters
    ----------
    values
        `_arguments`' first result; an argument of the wrong shape never reaches it.
    path
        Repository-relative path, for issues.
    line
        The decorator's line.
    issues
        Collects issues.

    Returns
    -------
    tuple
        `write` (`None` when not given), `key` (empty when not given, `KEY_NONE` for
        ``key="none"``, a one-column tuple for a string) and `watermark`; all
        three unset after an issue, so nothing unsafe reaches the metadata.
    """
    given = values.get("write")
    write = given if isinstance(given, str) else None
    listed = values.get("key")
    key: tuple[str, ...] | str = ()
    if listed == KEY_NONE:
        key = KEY_NONE
    elif isinstance(listed, str):
        key = (listed,)
    elif isinstance(listed, list):
        key = tuple(listed)
    marked = values.get("watermark")
    watermark = marked if isinstance(marked, str) else None
    messages = key_issues(key)
    if watermark is not None and unsafe_identifier(watermark):
        messages.append(WATERMARK_MESSAGE)
    if write is not None and write not in WRITE_CHOICES:
        messages.insert(0, write_choice_message(shown(write)))
    else:
        messages.extend(
            write_rule_issues(write, key, watermark, materialize="table", evolve_schema=None)
        )
    if values.get("evolve_schema") is True and (write or "replace") != "replace":
        messages.append(EVOLVE_NEEDS_REPLACE)
    if write == "history":
        # Not a `messages` entry: the key and the rest are valid, so the metadata keeps them and
        # no second, misleading "needs key" follows.
        issues.append(ProjectIssue(path, line, PYTHON_HISTORY_LATER))
    issues.extend(ProjectIssue(path, line, message) for message in messages)
    if messages:
        return None, (), None  # the issues stand; nothing unsafe reaches the metadata
    return write, key, watermark


def _parameters(function: _Function, path: str, issues: list[ProjectIssue]) -> list[str]:
    """List the function's parameters, refusing every shape the harness cannot call.

    Parameters
    ----------
    function
        The asset function.
    path
        Repository-relative path, for issues.
    issues
        Collects issues.

    Returns
    -------
    list of str
        The input parameters' names in declaration order: every parameter but `ctx`, which
        receives the asset's context.
    """
    spec = function.args
    line = function.lineno
    if spec.posonlyargs:
        issues.append(ProjectIssue(path, line, "positional-only parameters are not supported"))
    if spec.vararg is not None:
        issues.append(
            ProjectIssue(path, line, "*args is not supported; declare every input as a parameter")
        )
    if spec.kwarg is not None:
        issues.append(
            ProjectIssue(
                path, line, "**kwargs is not supported; declare every input as a parameter"
            )
        )
    if spec.defaults or any(default is not None for default in spec.kw_defaults):
        issues.append(ProjectIssue(path, line, "a parameter must not have a default"))
    names = [arg.arg for arg in (*spec.posonlyargs, *spec.args, *spec.kwonlyargs)]
    for name in sorted({name for name in names if names.count(name) > 1}):
        issues.append(ProjectIssue(path, line, f"parameter {_quoted(name)} is declared twice"))
    return [name for name in names if name != CONTEXT_PARAMETER]


def _metadata(
    values: dict[str, object],
    docstring: str | None,
    path: str,
    line: int,
    issues: list[ProjectIssue],
    *,
    artifact: bool,
    write_model: tuple[str | None, tuple[str, ...] | str, str | None] = (None, (), None),
) -> AssetMetadata | None:
    """Validate the decorator's metadata with the front-matter model.

    Parameters
    ----------
    values
        `_arguments`' result.
    docstring
        The function's docstring, if any.
    path
        Repository-relative path, for issues.
    line
        The decorator's line.
    issues
        Collects issues.
    artifact
        Whether the asset is an artifact asset: `_output` already refused its
        `materialize` and `columns`, so they are not checked again.
    write_model
        `_write_model`'s result: `write`, `key` and `watermark`.

    Returns
    -------
    AssetMetadata or None
        The metadata with ``materialize: table``, or `None` after an issue.
    """
    left_out = (
        "inputs",
        "materialize",
        "output",
        "keep_versions",
        "decimals",
        "connections",
        "write",
        "key",
        "watermark",
    )
    raw = {name: value for name, value in values.items() if name not in left_out}
    write, key, watermark = write_model
    if write is not None:
        raw["write"] = write
    if key:  # a non-empty tuple or `KEY_NONE`
        raw["key"] = key
    if watermark is not None:
        raw["watermark"] = watermark
    if artifact:
        raw.pop("columns", None)
        raw.pop("evolve_schema", None)
    materialize = values.get("materialize", "table")
    if artifact:
        pass
    elif materialize == "incremental":
        issues.append(
            ProjectIssue(
                path,
                line,
                'a Python asset appends with write="append" and watermark="<column>", '
                "not materialize='incremental'",
            )
        )
    elif materialize != "table":
        issues.append(
            ProjectIssue(
                path,
                line,
                f"a Python asset supports only materialize 'table', got {_quoted(materialize)}",
            )
        )
    raw["materialize"] = "table"
    tags = raw.get("tags")
    if isinstance(tags, list):
        raw["tags"] = tuple(tags)
    if "description" not in raw and docstring:
        raw["description"] = " ".join(
            part.strip() for part in docstring.strip().split("\n\n")[0].splitlines()
        )
    try:
        return AssetMetadata.model_validate(raw)
    except pydantic.ValidationError as exc:
        issues.extend(pydantic_issues(exc, path=path, line=line))
        return None


def _table_problem(table: str) -> str | None:
    """Check one input table name.

    Parameters
    ----------
    table
        The dotted name from the decorator.

    Returns
    -------
    str or None
        What is wrong with it, or `None` if it is a valid dotted table name.
        Unsafe characters never get here: `_arguments` refuses them first.
    """
    if any(part != part.strip() for part in table.split(".")):
        return "a table name part must not have surrounding whitespace"
    try:
        TableRef.from_dotted("-", table)
    except ValidationError as exc:
        return str(exc)
    return None


def _inputs(
    values: dict[str, object],
    parameters: list[str],
    function: _Function,
    path: str,
    line: int,
    issues: list[ProjectIssue],
) -> dict[str, str]:
    """Check `inputs` against the parameters and validate each table name.

    Parameters
    ----------
    values
        `_arguments`' result.
    parameters
        The function's input parameter names (`_parameters`).
    function
        The asset function.
    path
        Repository-relative path, for issues.
    line
        The decorator's line.
    issues
        Collects issues.

    Returns
    -------
    dict
        Parameter → table.
    """
    declared = values.get("inputs", {})
    inputs: dict[str, str] = declared if isinstance(declared, dict) else {}
    for name in parameters:
        if name not in inputs:
            issues.append(
                ProjectIssue(
                    path, function.lineno, f"parameter {_quoted(name)} has no entry in inputs"
                )
            )
    for name, table in inputs.items():
        if name == CONTEXT_PARAMETER:
            issues.append(ProjectIssue(path, line, _CONTEXT_IS_NOT_AN_INPUT))
            continue
        if name not in parameters:
            issues.append(
                ProjectIssue(path, line, f"inputs names {name!r}, which is not a parameter")
            )
        problem = _table_problem(table)
        if problem is not None:
            issues.append(ProjectIssue(path, line, f"input {name!r}: {problem}"))
    return inputs


def _find(tree: ast.Module, path: str) -> tuple[_Function, ast.expr] | list[ProjectIssue]:
    """Find the one top-level function decorated with `lakana.asset`.

    Parameters
    ----------
    tree
        The parsed module.
    path
        Repository-relative path, for issues.

    Returns
    -------
    tuple or list of ProjectIssue
        The function and its decorator, or the issues.
    """
    name_bound = module_bound = False
    issues: list[ProjectIssue] = []
    found: list[tuple[_Function, ast.expr]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name_bound, module_bound = _bindings(node, name_bound, module_bound)
            continue
        ours = []
        others = []
        for decorator in node.decorator_list:
            kind = _classify(decorator, name_bound, module_bound)
            if kind == "lookalike":
                issues.append(ProjectIssue(path, decorator.lineno, _ALIAS_HINT))
            elif kind == "asset":
                ours.append(decorator)
            else:
                others.append(decorator)
        if ours:
            issues.extend(ProjectIssue(path, d.lineno, _ONLY_DECORATOR) for d in others)
        if len(ours) > 1:
            issues.append(ProjectIssue(path, node.lineno, "more than one @asset decorator"))
        if ours:
            found.append((node, ours[0]))
        name_bound, module_bound = _bindings(node, name_bound, module_bound)
    if len(found) > 1:
        issues.append(
            ProjectIssue(
                path, found[1][0].lineno, "more than one @asset function; one file is one asset"
            )
        )
    if issues:
        return issues
    if not found:
        return [ProjectIssue(path, None, f"no function decorated with @asset; {_ALIAS_HINT}")]
    return found[0]


_PARSE_LOCK = threading.Lock()
"""Serializes the parse of a Python asset's source, which silences `SyntaxWarning` around it.

`warnings.catch_warnings` swaps the process-wide filter list on Python 3.11 to 3.13, so two
threads that enter and leave it interleaved can restore each other's changed list and leave
"ignore" installed for good. This lock keeps this module's own parses from doing that. It
cannot stop another thread's unrelated warning from being swallowed during the few
milliseconds of a parse; that costs a missing warning in a log, never a wrong result.
"""


def parse_python_asset(
    asset: DiscoveredAsset, defaults: AssetDefaults
) -> ParsedAsset | list[ProjectIssue]:
    """Parse one Python asset file, returning either the result or its issues.

    Parameters
    ----------
    asset
        The discovered file (`kind == "python"`).
    defaults
        Resolved `lakana.yaml` defaults.

    Returns
    -------
    ParsedAsset or list of ProjectIssue
        The parsed asset, or every issue found in the file.
    """
    path = asset.path
    try:
        with _PARSE_LOCK, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(asset.text)
    except SyntaxError as exc:
        return [ProjectIssue(path, exc.lineno, f"the file does not parse: {exc.msg}")]
    except ValueError as exc:
        reason = (
            "it contains a null byte"
            if "null" in str(exc)
            else "Python could not read it as source text"
        )
        return [ProjectIssue(path, None, f"the file does not parse: {reason}")]
    except (RecursionError, MemoryError):
        return [ProjectIssue(path, None, "the file is nested too deeply")]
    located = _find(tree, path)
    if isinstance(located, list):
        return located
    function, decorator = located
    if not isinstance(decorator, ast.Call):
        return [ProjectIssue(path, decorator.lineno, _PARENTHESES)]
    line = decorator.lineno
    issues: list[ProjectIssue] = []
    if isinstance(function, ast.AsyncFunctionDef):
        issues.append(ProjectIssue(path, function.lineno, "an asset function must not be async"))
    values, dropped = _arguments(decorator, path, issues)
    parameters = _parameters(function, path, issues)
    inputs = _inputs(values, parameters, function, path, line, issues)
    output, keep = _output(values, dropped, path, line, issues)
    decimals = _decimals(values, path, line, issues)
    connections = _connections(values, path, line, issues)
    artifact = output == "artifact"
    write_model = (None, (), None) if artifact else _write_model(values, path, line, issues)
    own = _metadata(
        values,
        ast.get_docstring(function),
        path,
        line,
        issues,
        artifact=artifact,
        write_model=write_model,
    )
    if output == "artifact" and asset.table in inputs.values():
        issues.append(
            ProjectIssue(
                path,
                line,
                f"an artifact asset cannot read its own artifact ({asset.table}); "
                "remove it from inputs",
            )
        )
    if own is not None:
        issues.extend(
            ProjectIssue(path, line, message) for message in model_issues(own, table=asset.table)
        )
    if issues or own is None:
        return issues
    digest = hashlib.sha256(asset.text.encode("utf-8")).hexdigest()
    return ParsedAsset(
        table=asset.table,
        path=path,
        block_line=line,
        body_line=function.lineno,
        metadata=merge_metadata(defaults, own),
        sql="",
        normalized_sql=PYTHON_SOURCE_PREFIX + digest,
        references=tuple(sorted(set(inputs.values()))),
        columns=None,
        lineage={},
        kind="python",
        inputs=tuple((name, inputs[name]) for name in parameters),
        output=output,
        keep_versions=keep,
        decimals=decimals,
        connections=connections,
    )
