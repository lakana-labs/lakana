"""Generate the CLI, settings and REST API references of the documentation site from the code.

``docs/site/reference/cli.md`` is written from the Typer app (every command and sub-command,
with its arguments, options and help text, in the app's order). ``docs/site/reference/settings.md``
is written from the settings models of lakana-core and lakana-server (every ``LAKANA_*``
variable with its type, default, reader and description, taken from the ``Parameters`` section
of the model's docstring). ``docs/site/reference/openapi.json`` is the OpenAPI document of the
platform REST API (every route under ``/api/``; the Iceberg REST catalog is versioned by the
Iceberg specification and left out), the snapshot ``scripts/openapi_diff.py`` compares in CI.

No page is edited by hand. ``--check`` writes nothing and fails when a page differs from
what the code produces, naming the page and the command that regenerates it. The Markdown pages
contain no fenced or indented code, because the block checker (``scripts/docs_blocks.py``) can
only judge blocks that a person marked.

Usage: ``uv run python scripts/generate_reference.py [--check]``
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import typer
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings
from typer._click import Command, Context, Parameter
from typer.core import TyperArgument, TyperGroup, TyperOption

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"
CLI_PAGE = "reference/cli.md"
SETTINGS_PAGE = "reference/settings.md"
OPENAPI_PAGE = "reference/openapi.json"
OPENAPI_TITLE = "Lakana platform API"
OPENAPI_VERSION = "unreleased"
"""The snapshot's ``info.version``; the release workflow writes the release's version instead."""
CONTRACT_URL = "https://lakana-labs.github.io/lakana/reference/rest-api/"
REGENERATE = "uv run python scripts/generate_reference.py"
_MAX_HEADING = 6
_NOT_DOCUMENTED = "(not documented)"


def _cell(text: str) -> str:
    """Make text safe inside a Markdown table cell.

    Parameters
    ----------
    text
        Any text.

    Returns
    -------
    str
        The text on one line with ``|`` escaped.
    """
    return " ".join(text.split()).replace("|", "\\|")


def _paragraphs(text: str) -> str:
    r"""Rewrite help text as plain paragraphs that cannot turn into a code block.

    Parameters
    ----------
    text
        A help text, possibly with indented lines and click's ``\\b`` marker.

    Returns
    -------
    str
        Paragraphs separated by one blank line, every line without leading whitespace.
    """
    text = "\n".join(line for line in inspect.cleandoc(text).splitlines() if line.strip() != "\b")
    blocks = (" ".join(line.strip() for line in block.splitlines()) for block in text.split("\n\n"))
    return "\n\n".join(block for block in blocks if block)


def _type_name(param: Parameter) -> str:
    """Name a parameter's type for a reader.

    Parameters
    ----------
    param
        A click argument or option.

    Returns
    -------
    str
        For example ``text``, ``whole number, 1 to 200`` or ``one of a, b``.
    """
    desc = param.type_descriptor
    if isinstance(param, TyperOption) and param.is_flag:
        return "flag"
    if desc.is_choice:
        return "one of " + ", ".join(f"`{_choice_text(choice)}`" for choice in desc.choices or ())
    annotation = desc.annotation
    if desc.is_list:
        (annotation,) = get_args(annotation)
    if desc.is_ranged:
        base = "whole number" if annotation is int else "number"
        low, high = desc.parameter_info.min, desc.parameter_info.max
        if low is not None and high is not None:
            return f"{base}, {low:g} to {high:g}"
        if low is not None:
            return f"{base}, {low:g} or more"
        return f"{base}, {high:g} or less"
    if desc.is_file:
        base = "file"
    elif desc.is_path:
        base = "path"
    else:
        names = {str: "text", int: "whole number", float: "number", bool: "true or false"}
        base = names.get(annotation) or getattr(annotation, "__name__", str(annotation))
    return f"{base}, repeatable" if desc.is_list else base


def _choice_text(choice: object) -> str:
    """Spell one allowed value of a choice parameter as it is typed.

    Parameters
    ----------
    choice
        An enum member or a plain value.

    Returns
    -------
    str
        The member's value for an enum, otherwise the value as text.
    """
    return str(getattr(choice, "value", choice))


def _default(param: Parameter) -> str:
    """Describe a parameter's default for a reader.

    Parameters
    ----------
    param
        A click argument or option.

    Returns
    -------
    str
        ``required``, ``none`` or the default in code formatting.
    """
    if param.required:
        return "required"
    default = param.default
    if default is None or default == ():
        return "none"
    if isinstance(default, bool):
        return "on" if default else "off"
    return f"`{default}`"


def _param_name(param: Parameter, ctx: Context) -> str:
    """Spell a parameter the way it is typed.

    Parameters
    ----------
    param
        A click argument or option.
    ctx
        The context of the command that owns it.

    Returns
    -------
    str
        An option's names (``-v, --verbose``, ``--a / --no-a``) or an argument's metavar.
    """
    if isinstance(param, TyperOption):
        names = ", ".join(f"`{name}`" for name in param.opts)
        if param.secondary_opts:
            names += " / " + ", ".join(f"`{name}`" for name in param.secondary_opts)
        return names
    return f"`{(param.metavar or param.name or '').upper()}`"


def _visible(command: Command) -> list[Parameter]:
    """List the parameters of a command a reader can use.

    Parameters
    ----------
    command
        A click command.

    Returns
    -------
    list of Parameter
        Arguments first, then options, hidden ones left out.
    """
    shown = [p for p in command.params if not getattr(p, "hidden", False)]
    return [p for p in shown if isinstance(p, TyperArgument)] + [
        p for p in shown if not isinstance(p, TyperArgument)
    ]


def _param_table(command: Command, ctx: Context) -> list[str]:
    """Render the arguments and options of a command as a table.

    Parameters
    ----------
    command
        A click command.
    ctx
        Its context.

    Returns
    -------
    list of str
        Markdown lines; empty when the command takes nothing.
    """
    params = _visible(command)
    if not params:
        return []
    lines = ["| Name | Type | Default | Description |", "|---|---|---|---|"]
    for param in params:
        text = getattr(param, "help", None) or ""
        env = getattr(param, "envvar", None)
        if env:
            text += f" Also read from `{env}`."
        lines.append(
            f"| {_param_name(param, ctx)} | {_cell(_type_name(param))} | {_cell(_default(param))}"
            f" | {_cell(text)} |"
        )
    return [*lines, ""]


def _render_children(command: Command, path: str, depth: int, out: list[str]) -> None:
    """Render the visible sub-commands of a group, in the order the app lists them.

    Parameters
    ----------
    command
        A command; one that is not a group has none.
    path
        The words that reach it.
    depth
        The depth of its sub-commands.
    out
        The page's lines, appended to.
    """
    if isinstance(command, TyperGroup):
        for name, child in command.commands.items():
            if not child.hidden:
                _render_command(child, f"{path} {name}", depth, out)


def _render_command(command: Command, path: str, depth: int, out: list[str]) -> None:
    """Render a command and, for a group, everything below it.

    Parameters
    ----------
    command
        A click command or group.
    path
        The words that reach it, for example ``lakana query list``.
    depth
        How far below the page title it is; sets the heading level.
    out
        The page's lines, appended to.
    """
    out.append(f"{'#' * min(depth + 1, _MAX_HEADING)} `{path}`")
    out.append("")
    ctx = Context(command, info_name=path)
    usage = " ".join([path, *command.collect_usage_pieces(ctx)])
    out.append(f"Usage: `{usage}`")
    out.append("")
    if command.help:
        out.extend([_paragraphs(command.help), ""])
    out.extend(_param_table(command, ctx))
    _render_children(command, path, depth + 1, out)


def render_cli(app: typer.Typer, prog: str = "lakana") -> str:
    """Render the CLI reference page.

    Parameters
    ----------
    app
        The Typer app.
    prog
        The program name the commands are typed with.

    Returns
    -------
    str
        The Markdown page.
    """
    root = typer.main.get_command(app)
    out = [
        "# Command line reference",
        "",
        "<!-- Generated by scripts/generate_reference.py from the Typer app. Do not edit by hand:",
        f"run `{REGENERATE}` and commit the result. -->",
        "",
        f"Every `{prog}` command and sub-command, with its arguments and options, in the order",
        f"`{prog} --help` lists them. The options of the first section go before the command",
        f"name, as in `{prog} --output json whoami`.",
        "",
    ]
    ctx = Context(root, info_name=prog)
    out.extend(["## Options for every command", ""])
    out.append(f"Usage: `{' '.join([prog, *root.collect_usage_pieces(ctx)])}`")
    out.append("")
    out.extend(_param_table(root, ctx))
    _render_children(root, prog, 1, out)
    return "\n".join(out).rstrip("\n") + "\n"


def docstring_parameters(model: type) -> dict[str, str]:
    """Read the ``Parameters`` section of a class docstring.

    Parameters
    ----------
    model
        A class with a numpydoc docstring.

    Returns
    -------
    dict of str to str
        The description of each parameter, on one line. ``a, b`` entries give both names the
        same text.
    """
    if not model.__doc__:
        return {}
    lines = inspect.cleandoc(model.__doc__).splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Parameters")
    except StopIteration:
        return {}
    result: dict[str, str] = {}
    names: list[str] = []
    text: list[str] = []

    def flush() -> None:
        """Store the entry read so far."""
        for name in names:
            result[name] = " ".join(text).strip()

    for line in lines[start + 2 :]:
        if line and not line[0].isspace():
            if re.fullmatch(r"[A-Z][A-Za-z ]*", line):
                break
            flush()
            names = [n.strip() for n in line.split(",")]
            text = []
        else:
            text.append(line.strip())
    flush()
    return {k: " ".join(v.split()) for k, v in result.items()}


def _is_model(annotation: object) -> bool:
    """Tell whether an annotation is a nested settings model.

    Parameters
    ----------
    annotation
        A field annotation.

    Returns
    -------
    bool
        True for a ``BaseModel`` subclass.
    """
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _type_text(annotation: object) -> str:
    """Name a field's type for a reader.

    Parameters
    ----------
    annotation
        A field annotation.

    Returns
    -------
    str
        For example ``whole number``, ``true or false`` or ```a`, `b```.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        parts = [a for a in get_args(annotation) if a is not type(None)]
        return " or ".join(dict.fromkeys(_type_text(p) for p in parts))
    if origin is Literal:
        return ", ".join(f"`{a}`" for a in get_args(annotation))
    if origin is list:
        return "list"
    if origin is dict:
        return "mapping"
    simple = {
        int: "whole number",
        float: "number",
        bool: "true or false",
        str: "text",
        Path: "path",
        SecretStr: "secret text",
    }
    return simple.get(annotation, getattr(annotation, "__name__", str(annotation)))


def _bounds(field: FieldInfo) -> str:
    """Describe a field's numeric bounds.

    Parameters
    ----------
    field
        A pydantic field.

    Returns
    -------
    str
        For example ``above 0`` or ``1 or more``; empty when it has none.
    """
    words = []
    for item in field.metadata:
        for attr, template in (
            ("gt", "above {}"),
            ("ge", "{} or more"),
            ("lt", "below {}"),
            ("le", "{} or less"),
        ):
            value = getattr(item, attr, None)
            if value is not None:
                words.append(template.format(f"{value:g}" if isinstance(value, float) else value))
    return ", ".join(words)


def _default_text(field: FieldInfo, value: object) -> str:
    """Describe a field's default for a reader.

    Parameters
    ----------
    field
        A pydantic field.
    value
        The default of the field on the instance that owns it.

    Returns
    -------
    str
        ``required``, ``not set`` or the value in code formatting.
    """
    if field.is_required():
        return "required"
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if isinstance(value, SecretStr):
        return "not set"
    if isinstance(value, list) and not value:
        return "empty"
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return f"`{value}`"


def _variables(
    model: type[BaseModel], prefix: str, owner: BaseModel | None, group: str
) -> list[dict[str, str]]:
    """List the environment variables a model reads.

    Parameters
    ----------
    model
        A settings model or a nested model.
    prefix
        The text every variable name starts with.
    owner
        The default instance of the model, for default values; ``None`` when it has none.
    group
        The top-level field these variables belong to (empty for the model's own fields).

    Returns
    -------
    list of dict
        One entry per variable with its name, group, type, default and description.
    """
    docs = docstring_parameters(model)
    found: list[dict[str, str]] = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        value = getattr(owner, name, field.default) if owner is not None else field.default
        if _is_model(annotation):
            assert isinstance(annotation, type)
            found.extend(
                _variables(
                    annotation,
                    f"{prefix}{name.upper()}__",
                    value if isinstance(value, BaseModel) else None,
                    group or name,
                )
            )
            continue
        kind = _type_text(annotation)
        bounds = _bounds(field)
        found.append(
            {
                "name": f"{prefix}{name.upper()}",
                "group": group,
                "type": f"{kind}, {bounds}" if bounds else kind,
                "default": _default_text(field, value),
                "text": docs.get(name, _NOT_DOCUMENTED),
            }
        )
    return found


def render_settings(sources: Sequence[tuple[str, type[BaseSettings]]]) -> str:
    """Render the settings reference page.

    Parameters
    ----------
    sources
        ``(reader, settings class)`` pairs: the component that reads the model, and the model.
        A variable more than one model has names every reader.

    Returns
    -------
    str
        The Markdown page.
    """
    merged: dict[str, dict[str, Any]] = {}
    for reader, settings in sources:
        prefix = str(settings.model_config.get("env_prefix", ""))
        for entry in _variables(settings, prefix.upper(), None, ""):
            known = merged.setdefault(entry["name"], {**entry, "readers": []})
            if known["text"] == _NOT_DOCUMENTED and entry["text"] != _NOT_DOCUMENTED:
                known["text"] = entry["text"]
            known["readers"].append(reader)
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in merged.values():
        groups.setdefault(entry["group"], []).append(entry)
    out = [
        "# Settings reference",
        "",
        "<!-- Generated by scripts/generate_reference.py from the settings models. Do not edit by",
        f"hand: run `{REGENERATE}` and commit the result. -->",
        "",
        "Every `LAKANA_*` environment variable of the server and the workers. A nested setting",
        "joins its group and its name with two underscores: `LAKANA_RUN__LEASE_SECONDS` is the",
        '`lease_seconds` of the `run` group. "Read by" names the component that reads the',
        "variable; set a variable both read in the `x-lakana-env` block of the Compose file, which",
        "the server and every worker share. How to set them: [Deploy with",
        "Compose](../how-to/deploy-with-compose.md).",
        "",
    ]
    for group, entries in groups.items():
        title = f"`LAKANA_{group.upper()}__*`" if group else "General"
        out.extend([f"## {title}", "", "| Variable | Type | Default | Read by | Description |"])
        out.append("|---|---|---|---|---|")
        for entry in entries:
            out.append(
                f"| `{entry['name']}` | {_cell(entry['type'])} | {_cell(entry['default'])}"
                f" | {', '.join(entry['readers'])} | {_cell(entry['text'])} |"
            )
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def render_openapi(routers: Sequence[APIRouter]) -> str:
    """Render the OpenAPI document of the platform REST API.

    Parameters
    ----------
    routers
        The routers of the platform API, in the order the application includes them.

    Returns
    -------
    str
        The document as indented JSON with a final newline. Its ``info.version`` is
        ``OPENAPI_VERSION``, so the snapshot changes only when the API does.
    """
    from lakana_server.api.routing import describe_openapi

    app = FastAPI(
        title=OPENAPI_TITLE,
        version=OPENAPI_VERSION,
        description=(
            "The platform REST API of Lakana: every route under `/api/`. It is a versioned "
            f"contract: {CONTRACT_URL}. The Iceberg REST catalog under `/v1` is not part of "
            "this document; it follows the Apache Iceberg REST catalog specification."
        ),
    )
    for router in routers:
        app.include_router(router)
    describe_openapi(app, routers)
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"


def real_pages() -> dict[str, str]:
    """Generate the pages from the real code.

    Returns
    -------
    dict of str to str
        The Markdown of each page, by path under ``docs/site``.
    """
    from lakana.cli.app import build_app
    from lakana_core.config import Settings as CoreSettings
    from lakana_server.api.iceberg.router import router as iceberg_router
    from lakana_server.app import ROUTERS
    from lakana_server.settings import Settings as ServerSettings

    return {
        CLI_PAGE: render_cli(build_app(), prog="lakana"),
        SETTINGS_PAGE: render_settings([("server", ServerSettings), ("worker", CoreSettings)]),
        OPENAPI_PAGE: render_openapi(
            [router for router in ROUTERS if router is not iceberg_router]
        ),
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    site: Path = SITE,
    generate: Callable[[], Mapping[str, str]] = real_pages,
) -> int:
    """Write the generated pages, or check that they are current.

    Parameters
    ----------
    argv
        Command-line arguments; ``None`` reads ``sys.argv``.
    site
        The directory the page paths are relative to.
    generate
        Produces the pages; replaced in tests.

    Returns
    -------
    int
        0 when the pages were written or are current, 1 when ``--check`` finds a stale page.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--check", action="store_true", help="write nothing; fail when a page is stale"
    )
    args = parser.parse_args(argv)
    stale = 0
    for relative, text in generate().items():
        path = site / relative
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if args.check:
            if current != text:
                stale += 1
                print(
                    f"docs/site/{relative} is stale: it differs from what the code produces. "
                    f"Regenerate it with `{REGENERATE}` and commit the result.",
                    file=sys.stderr,
                )
        elif current != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote docs/site/{relative}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
