"""Generate the CLI, settings, REST API and SDK references of the documentation site from the code.

The CLI reference is written from the Typer app (every command and sub-command, with its
arguments, options and help text, in the app's order): ``docs/site/reference/cli.md`` is the
index with the options for every command, ``reference/cli/commands.md`` has the commands outside
a group, and ``reference/cli/lakana-<group>.md`` is one page per command group.
``docs/site/reference/settings.md`` is written from the settings models of lakana-core and
lakana-server (every ``LAKANA_*`` variable with its type, default, reader and description,
taken from the ``Parameters`` section of the model's docstring).
``docs/site/reference/openapi.json`` is the OpenAPI document of the platform REST API (every
route under ``/api/``; the Iceberg REST catalog is versioned by the Iceberg specification and
left out), the snapshot ``scripts/openapi_diff.py`` compares in CI. From that document,
``reference/rest/<tag>.md`` is one page per OpenAPI tag, and the ``rest-pages`` region of
``reference/rest-api.md`` lists them. The Python SDK reference, ``reference/sdk/``, is written
from the docstrings of the ``lakana`` package: an index of its exports and the client's
resources, the client, the errors, one page per module of the exported functions and one per
module of the client's resources.

The script also owns the directories in ``OWNED_DIRS`` (a page there that the code no longer
produces is deleted) and the ``api-nav`` region of ``mkdocs.yml``, the lines between the
``generated:begin api-nav`` and ``generated:end api-nav`` markers, which list the API reference.

No page is edited by hand. ``--check`` writes nothing and fails when a page differs from
what the code produces, or is left over in an owned directory, naming the file and the command
that regenerates it. The Markdown pages
contain no fenced or indented code, because the block checker (``scripts/docs_blocks.py``) can
only judge blocks that a person marked.

Usage: ``uv run python scripts/generate_reference.py [--check]``
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import sys
import textwrap
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
SITE_PREFIX = "docs/site/"
"""The repository path of the site; the page constants below are relative to it."""
CLI_PAGE = "reference/cli.md"
SETTINGS_PAGE = "reference/settings.md"
OPENAPI_PAGE = "reference/openapi.json"
OPENAPI_TITLE = "Lakana platform API"
OPENAPI_VERSION = "unreleased"
"""The snapshot's ``info.version``; the release workflow writes the release's version instead."""
CONTRACT_URL = "https://lakana-labs.github.io/lakana/reference/rest-api/"
REGENERATE = "uv run python scripts/generate_reference.py"
OWNED_DIRS: tuple[str, ...] = (
    "docs/site/reference/cli",
    "docs/site/reference/rest",
    "docs/site/reference/sdk",
)
"""Directories only this script writes: a page in them that the code no longer produces goes."""
NAV_REGION = "api-nav"
"""The marked region of ``mkdocs.yml`` that lists the API reference pages."""
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
        The text on one line with ``|`` escaped outside backtick code spans. Inside a code span
        a ``|`` does not end the cell, and an escape would show as a backslash.
    """
    parts = re.split(r"(`[^`]*`)", " ".join(text.split()))
    return "".join(part if i % 2 else part.replace("|", "\\|") for i, part in enumerate(parts))


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


def _generated_note(source: str = "the Typer app") -> list[str]:
    """Return the comment every generated Markdown page carries below its title.

    Parameters
    ----------
    source
        What the page is generated from.

    Returns
    -------
    list of str
        The comment's lines and a blank line.
    """
    return [
        f"<!-- Generated by scripts/generate_reference.py from {source}. Do not edit by hand:",
        f"run `{REGENERATE}` and commit the result. -->",
        "",
    ]


def _first_sentence(text: str) -> str:
    """Take the first sentence of a help text.

    Parameters
    ----------
    text
        A help text.

    Returns
    -------
    str
        Its first paragraph up to and including the first full stop that ends a sentence, or the
        whole first paragraph when it has none.
    """
    first = _paragraphs(text).split("\n\n")[0]
    match = re.match(r"(.+?[.!?])(?:\s|$)", first)
    return match.group(1) if match else first


def _page(lines: list[str], source: str = "the Typer app") -> str:
    """Join a page's lines with the generated comment after the title.

    Parameters
    ----------
    lines
        The page's lines, starting with the title and a blank line.
    source
        What the page is generated from, named in the comment.

    Returns
    -------
    str
        The Markdown page, ending in one newline.
    """
    return "\n".join([*lines[:2], *_generated_note(source), *lines[2:]]).rstrip("\n") + "\n"


def render_cli_pages(app: typer.Typer, prog: str = "lakana") -> dict[str, str]:
    """Render the CLI reference: an index, the top-level commands, and one page per group.

    Parameters
    ----------
    app
        The Typer app.
    prog
        The program name the commands are typed with.

    Returns
    -------
    dict of str to str
        Each page by site-relative path: ``CLI_PAGE``, ``reference/cli/commands.md``, then
        ``reference/cli/<prog>-<group>.md`` in the app's order. A group page is named after the
        command a reader types, so no group can take ``index.md`` (the URL of ``CLI_PAGE``) or
        ``commands.md``.
    """
    root = typer.main.get_command(app)
    children: dict[str, Command] = getattr(root, "commands", {})
    visible = {name: child for name, child in children.items() if not child.hidden}
    commands = {n: c for n, c in visible.items() if not isinstance(c, TyperGroup)}
    groups = {n: c for n, c in visible.items() if isinstance(c, TyperGroup)}

    ctx = Context(root, info_name=prog)
    index = [
        "# Command line reference",
        "",
        f"Every `{prog}` command and sub-command, with its arguments and options, in the order",
        f"`{prog} --help` lists them. The options of the first section go before the command",
        f"name, as in `{prog} --output json whoami`.",
        "",
        "## Options for every command",
        "",
        f"Usage: `{' '.join([prog, *root.collect_usage_pieces(ctx)])}`",
        "",
        *_param_table(root, ctx),
        "## Commands",
        "",
    ]
    names = ", ".join(f"`{name}`" for name in commands)
    index.append("- [Top-level commands](cli/commands.md)" + (f": {names}" if names else ""))
    for name, group in groups.items():
        line = f"- [`{prog} {name}`](cli/{prog}-{name}.md)"
        index.append(line + (f": {_first_sentence(group.help)}" if group.help else ""))

    top = ["# Top-level commands", ""]
    for name, command in commands.items():
        _render_command(command, f"{prog} {name}", 1, top)

    pages = {CLI_PAGE: _page(index), "reference/cli/commands.md": _page(top)}
    for name, group in groups.items():
        out: list[str] = []
        _render_command(group, f"{prog} {name}", 0, out)
        pages[f"reference/cli/{prog}-{name}.md"] = _page(out)
    return pages


ACRONYMS = {"ci": "CI", "api": "API", "sql": "SQL", "sdk": "SDK", "oidc": "OIDC"}
"""Words a title spells in capitals."""


def title_from_slug(slug: str) -> str:
    """Turn a page slug into a title.

    Parameters
    ----------
    slug
        Words joined by hyphens or underscores, for example ``ci-trust-rules``.

    Returns
    -------
    str
        The words with spaces, the first capitalised and acronyms in capitals, for example
        ``CI trust rules``.
    """
    words = [ACRONYMS.get(word, word) for word in re.split(r"[-_]", slug)]
    return " ".join([words[0][:1].upper() + words[0][1:], *words[1:]])


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
    entries = parse_numpydoc(model.__doc__).sections.get("Parameters", [])
    return {entry.name: entry.text for entry in entries}


@dataclass(frozen=True)
class DocEntry:
    """One entry of a numpydoc section.

    Parameters
    ----------
    name
        The parameter, type or error the entry names.
    text
        Its description, on one line.
    """

    name: str
    text: str


@dataclass(frozen=True)
class NumpyDoc:
    """A numpydoc docstring, read into its parts.

    Parameters
    ----------
    summary
        The first paragraph.
    body
        The paragraphs between the summary and the first section, separated by one blank line.
    sections
        The entries of each section, by the section's heading (``Parameters``, ``Returns`` …).
    """

    summary: str
    body: str
    sections: dict[str, list[DocEntry]]


def parse_numpydoc(doc: str | None) -> NumpyDoc:
    """Read a numpydoc docstring into its summary, body and sections.

    A section is a line at the left margin underlined by three or more hyphens.

    Parameters
    ----------
    doc
        A docstring, or ``None``.

    Returns
    -------
    NumpyDoc
        The parts; an entry ``a, b`` gives both names the same text, and ``c : int`` is named
        ``c``.
    """
    lines = inspect.cleandoc(doc or "").splitlines()
    heads = [
        i
        for i in range(len(lines) - 1)
        if lines[i].strip()
        and not lines[i][0].isspace()
        and re.fullmatch(r"-{3,}", lines[i + 1].strip())
    ]
    intro = _paragraphs("\n".join(lines[: heads[0] if heads else len(lines)]))
    summary, _, body = intro.partition("\n\n")
    sections: dict[str, list[DocEntry]] = {}
    for n, start in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        entries: list[DocEntry] = []
        names: list[str] = []
        text: list[str] = []
        for line in [*lines[start + 2 : end], "\0"]:  # "\0" flushes the last entry
            if line and not line[0].isspace():
                entries += [DocEntry(name, " ".join(" ".join(text).split())) for name in names]
                names, text = [p.strip() for p in line.split(" : ")[0].split(",")], []
            else:
                text.append(line.strip())
        sections[lines[start].strip()] = entries
    return NumpyDoc(summary, body, sections)


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


REST_REGION = "rest-pages"
"""The marked region of ``reference/rest-api.md`` that lists the REST pages."""
REST_INDEX_PAGE = "reference/rest-api.md"
_JSON_TYPES = {
    "string": "text",
    "integer": "whole number",
    "number": "number",
    "boolean": "true or false",
    "null": "null",
}
_ROUTES = {True: "route", False: "routes"}
_TAG_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def escape_text(text: str) -> str:
    """Make text from the code show as text in Markdown.

    Parameters
    ----------
    text
        A summary, description or table cell.

    Returns
    -------
    str
        Outside backtick code spans, ``<`` and ``>`` as entities and a line starting with ``#``
        escaped, so neither becomes an HTML tag or a heading; code spans are unchanged.
    """
    parts = re.split(r"(`[^`]*`)", text)
    for i in range(0, len(parts), 2):
        part = parts[i].replace("<", "&lt;").replace(">", "&gt;")
        start = r"(^|\n)" if i == 0 else r"(\n)"
        parts[i] = re.sub(start + r"([ \t]*)#", r"\1\2\\#", part)
    return "".join(parts)


def schema_type(schema: Mapping[str, Any]) -> str:
    """Name the type of an OpenAPI schema in words.

    Parameters
    ----------
    schema
        A JSON schema from the OpenAPI document.

    Returns
    -------
    str
        For example ```ThingOut```, ``text or null``, ``list of `A``` or ``text (date-time)``;
        ``any`` when the schema says nothing the reader can use.
    """
    if "$ref" in schema:
        return f"`{schema['$ref'].rsplit('/', 1)[-1]}`"
    parts = schema.get("anyOf") or schema.get("oneOf")
    if parts:
        names = list(dict.fromkeys(schema_type(part) for part in parts))
        return " or ".join([n for n in names if n != "null"] + [n for n in names if n == "null"])
    if "enum" in schema:
        return "one of " + ", ".join(f"`{_json_value(v)}`" for v in schema["enum"])
    if "const" in schema:
        return f"`{_json_value(schema['const'])}`"
    kind = schema.get("type")
    if kind == "array":
        return "list of " + schema_type(schema.get("items", {}))
    if kind == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, Mapping):
            return "mapping of text to " + schema_type(extra)
        return "mapping of text to any" if extra is True else "object"
    if isinstance(kind, str) and kind in _JSON_TYPES:
        name = _JSON_TYPES[kind]
        return f"{name} ({schema['format']})" if "format" in schema else name
    return "any"


def _json_value(value: object) -> str:
    """Spell a JSON value as a reader types it.

    Parameters
    ----------
    value
        An allowed value from a schema.

    Returns
    -------
    str
        A string as is, anything else as JSON.
    """
    return value if isinstance(value, str) else json.dumps(value)


def tag_slug(tag: str) -> str:
    """Name the page of an OpenAPI tag.

    Parameters
    ----------
    tag
        The tag.

    Returns
    -------
    str
        The tag itself when it is lower-case words joined by hyphens; otherwise the tag in lower
        case with every run of other characters turned into one hyphen.

    Raises
    ------
    ValueError
        When the name would be empty, or ``index``, which would make the page the index of the
        ``reference/rest/`` section instead of a page of its own.
    """
    slug = tag if _TAG_SLUG.fullmatch(tag) else re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")
    if slug in {"", "index"}:
        raise ValueError(
            f"the OpenAPI tag {tag!r} gives the page name {slug!r}, which cannot be a page of "
            "its own; rename the tag"
        )
    return slug


def _tagged(doc: Mapping[str, Any]) -> dict[str, list[tuple[str, str, Mapping[str, Any]]]]:
    """Group the operations of a document by the page of their tag.

    Parameters
    ----------
    doc
        The OpenAPI document.

    Returns
    -------
    dict
        Each page slug, in the order its tag first appears, with the method, path and operation
        of each of its routes in document order.

    Raises
    ------
    ValueError
        When a route has no tag, or two tags give the same page.
    """
    groups: dict[str, list[tuple[str, str, Mapping[str, Any]]]] = {}
    owners: dict[str, str] = {}
    for path, item in doc["paths"].items():
        for method, operation in item.items():
            tags = operation.get("tags")
            if not tags:
                raise ValueError(f"{method.upper()} {path} has no tag; give its router a tag")
            for tag in tags:
                slug = tag_slug(tag)
                owner = owners.setdefault(slug, tag)
                if owner != tag:
                    raise ValueError(
                        f"the tags {owner!r} and {tag!r} give the same page "
                        f"reference/rest/{slug}.md; rename one of them"
                    )
                groups.setdefault(slug, []).append((method, path, operation))
    return groups


def _object_schema(schema: Mapping[str, Any], doc: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Find the object whose fields a body has.

    Parameters
    ----------
    schema
        The body's schema.
    doc
        The OpenAPI document, for its component schemas.

    Returns
    -------
    Mapping or None
        The object schema of a ``$ref``, of an object written in place, or of the one part of a
        union besides ``null``; ``None`` when the body is not such an object.
    """
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = doc.get("components", {}).get("schemas", {}).get(name, {})
    parts = [p for p in schema.get("anyOf", ()) if p.get("type") != "null"]
    if len(parts) == 1:
        return _object_schema(parts[0], doc)
    return schema if "properties" in schema else None


def _field_table(schema: Mapping[str, Any]) -> list[str]:
    """Render the fields of an object schema, one level deep.

    Parameters
    ----------
    schema
        An object schema with ``properties``.

    Returns
    -------
    list of str
        Markdown table lines and a blank line.
    """
    required = set(schema.get("required", ()))
    lines = ["| Field | Type | Required | Description |", "|---|---|---|---|"]
    for name, prop in schema["properties"].items():
        lines.append(
            f"| `{name}` | {_cell(escape_text(schema_type(prop)))}"
            f" | {'yes' if name in required else 'no'}"
            f" | {_cell(escape_text(prop.get('description', '')))} |"
        )
    return [*lines, ""]


def _body_text(content: Mapping[str, Any]) -> str:
    """Name what an answer's body holds.

    Parameters
    ----------
    content
        The answer's ``content`` by media type.

    Returns
    -------
    str
        The JSON body's type, other media types in code formatting, or ``none``.
    """
    kinds = [
        schema_type(body.get("schema", {})) if media == "application/json" else f"`{media}`"
        for media, body in content.items()
    ]
    return " or ".join(kinds) or "none"


def _render_operation(
    method: str, path: str, operation: Mapping[str, Any], doc: Mapping[str, Any]
) -> list[str]:
    """Render one route of a tag page.

    Parameters
    ----------
    method
        The HTTP method, in lower case.
    path
        The route's path.
    operation
        Its operation object.
    doc
        The OpenAPI document, for its component schemas.

    Returns
    -------
    list of str
        Markdown lines.
    """
    out = [f"## `{method.upper()} {path}`", ""]
    if operation.get("deprecated"):
        out += [
            "Deprecated: it will be removed in a major release (see "
            "[Deprecations](../rest-api.md#deprecations)).",
            "",
        ]
    for text in (operation.get("summary", ""), operation.get("description", "")):
        if text:
            out += [escape_text(_paragraphs(text)), ""]
    params = operation.get("parameters", [])
    if params:
        out += ["Parameters:", "", "| Name | In | Required | Type | Description |"]
        out.append("|---|---|---|---|---|")
        for param in params:
            out.append(
                f"| `{param['name']}` | {param['in']} | {'yes' if param.get('required') else 'no'}"
                f" | {_cell(escape_text(schema_type(param.get('schema', {}))))}"
                f" | {_cell(escape_text(param.get('description', '')))} |"
            )
        out.append("")
    body = operation.get("requestBody")
    if body:
        media, content = next(iter(body["content"].items()))
        schema = content.get("schema", {})
        need = "required" if body.get("required") else "optional"
        out += [f"Request body ({media}, {need}): {escape_text(schema_type(schema))}", ""]
        fields = _object_schema(schema, doc)
        if fields is not None:
            out += _field_table(fields)
    answers = operation.get("responses", {})
    out += ["Answers:", "", "| Status | Meaning | Body |", "|---|---|---|"]
    for status, answer in answers.items():
        out.append(
            f"| `{status}` | {_cell(escape_text(answer.get('description', '')))}"
            f" | {_cell(escape_text(_body_text(answer.get('content', {}))))} |"
        )
    return [*out, "", *_answer_fields(answers, doc)]


def _answer_fields(answers: Mapping[str, Any], doc: Mapping[str, Any]) -> list[str]:
    """Render the fields of a route's successful answer.

    Parameters
    ----------
    answers
        The route's answers by status.
    doc
        The OpenAPI document, for its component schemas.

    Returns
    -------
    list of str
        The field table of the first ``2xx`` answer whose JSON body is a ``$ref`` to an object,
        under ``Answer fields:``; empty when there is none.
    """
    for status, answer in answers.items():
        schema = answer.get("content", {}).get("application/json", {}).get("schema", {})
        fields = _object_schema(schema, doc) if "$ref" in schema else None
        if status.startswith("2") and fields is not None:
            return [f"Answer fields: {schema_type(schema)}", "", *_field_table(fields)]
    return []


def render_rest_pages(doc: Mapping[str, Any]) -> dict[str, str]:
    """Render one REST API reference page per OpenAPI tag.

    Parameters
    ----------
    doc
        The OpenAPI document of the platform API.

    Returns
    -------
    dict of str to str
        Each page by site-relative path, ``reference/rest/<tag_slug(tag)>.md``, in the order the
        tags first appear over the document's paths.
    """
    pages: dict[str, str] = {}
    for slug, operations in _tagged(doc).items():
        lines = [
            f"# REST API: {title_from_slug(slug)}",
            "",
            "These routes of the platform API come from its [OpenAPI document](../openapi.json);"
            " [The REST API as a contract](../rest-api.md) says what Lakana promises about them.",
            "",
        ]
        for method, path, operation in operations:
            lines += _render_operation(method, path, operation, doc)
        pages[f"reference/rest/{slug}.md"] = _page(lines, "openapi.json")
    return pages


def rest_index(doc: Mapping[str, Any]) -> str:
    """Render the list of REST pages for the index page's ``rest-pages`` region.

    Parameters
    ----------
    doc
        The OpenAPI document of the platform API.

    Returns
    -------
    str
        One Markdown list line per tag page with its number of routes.
    """
    return "".join(
        f"- [{title_from_slug(slug)}](rest/{slug}.md): {len(ops)} {_ROUTES[len(ops) == 1]}\n"
        for slug, ops in _tagged(doc).items()
    )


SDK_DIR = "reference/sdk"
"""The site directory of the Python SDK reference."""
_ANY_NUMBER = "any number"
_STATIC = "static"
_CLASS = "class"
_PROPERTY = "property"


@dataclass(frozen=True)
class SdkResource:
    """A resource object the client exposes.

    Parameters
    ----------
    attribute
        The client's attribute, for example ``tables``.
    cls
        The resource's class.
    note
        The attribute docstring written below the assignment, or empty.
    """

    attribute: str
    cls: type
    note: str


def client_resources(client_cls: type, package: str) -> list[SdkResource]:
    """Find the resources a client's constructor sets.

    A resource is a public attribute the constructor sets to an instance of a class from the
    resources package.

    Parameters
    ----------
    client_cls
        The client class.
    package
        The dotted name of the resources package, for example ``lakana.resources``.

    Returns
    -------
    list of SdkResource
        The resources in the order the constructor sets them.
    """
    module = sys.modules[client_cls.__module__]
    init = ast.parse(textwrap.dedent(inspect.getsource(client_cls.__init__))).body[0]
    assert isinstance(init, ast.FunctionDef)
    found: list[SdkResource] = []
    for index, node in enumerate(init.body):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue  # AnnAssign (ci_token) and the like
        target, call = node.targets[0], node.value
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and not target.attr.startswith("_")
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
        ):
            continue
        cls = getattr(module, call.func.id, None)
        if not (isinstance(cls, type) and cls.__module__.startswith(package + ".")):
            continue
        after = init.body[index + 1] if index + 1 < len(init.body) else None
        note = (
            after.value.value
            if isinstance(after, ast.Expr)
            and isinstance(after.value, ast.Constant)
            and isinstance(after.value.value, str)
            else ""
        )
        found.append(SdkResource(target.attr, cls, inspect.cleandoc(note)))
    return found


class _Shown:
    """Text that ``inspect`` writes as it is, in a signature.

    Parameters
    ----------
    text
        The text.
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


_PRIVATE_NAME = re.compile(r"(?<![\w.])_[A-Za-z]\w*")
"""A private name in an annotation: ``_Unset``, not ``lakana._x`` or ``__version__``."""


def _annotation_text(annotation: object, namespace: Mapping[str, Any]) -> str:
    """Write an annotation the way its source spells it, without private names.

    Parameters
    ----------
    annotation
        A parameter or return annotation.
    namespace
        The globals of the function, to look a private name up in.

    Returns
    -------
    str
        A string annotation (``from __future__ import annotations``) as it is, any other
        through ``inspect.formatannotation``; empty for none. In a string annotation a private
        alias is spelt out (``_TableList`` gives ``list[Table]``), and a private class, such as
        the type of a sentinel, is left out of its union.
    """
    if annotation is inspect.Parameter.empty:
        return ""
    if not isinstance(annotation, str):
        return inspect.formatannotation(annotation)

    def spell(match: re.Match[str]) -> str:
        """Spell out a private alias; keep any other private name.

        Parameters
        ----------
        match
            A private name.

        Returns
        -------
        str
            The alias's value, or the name.
        """
        value = namespace.get(match.group(0))
        return match.group(0) if value is None or isinstance(value, type) else _alias_text(value)

    parts = _union_parts(_PRIVATE_NAME.sub(spell, annotation))
    return " | ".join(part for part in parts if not _PRIVATE_NAME.fullmatch(part))


def _alias_text(value: object) -> str:
    """Spell a type alias with the plain names of its parts.

    Parameters
    ----------
    value
        A type or generic alias, for example ``list[Table]``.

    Returns
    -------
    str
        For example ``list[Table]`` or ``list[dict[str, Any]]``, without module names.
    """
    origin, args = get_origin(value), get_args(value)
    if origin is not None and args:
        return f"{origin.__name__}[{', '.join(_alias_text(arg) for arg in args)}]"
    return getattr(value, "__name__", repr(value))


def _union_parts(text: str) -> list[str]:
    """Split an annotation at the ``|`` that are outside brackets.

    Parameters
    ----------
    text
        An annotation, for example ``dict[str, int | None] | None``.

    Returns
    -------
    list of str
        The parts of the union, stripped; one part for an annotation that is not a union.
    """
    parts: list[str] = []
    depth = start = 0
    for i, char in enumerate(text):
        depth += {"[": 1, "]": -1}.get(char, 0)
        if char == "|" and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    return [*parts, text[start:].strip()]


def _sdk_default(default: object) -> str:
    """Write a parameter's default.

    Parameters
    ----------
    default
        The default value.

    Returns
    -------
    str
        Its ``repr``; a value of a private type, such as a sentinel, by its ``name``, or ``…``
        when it has none.
    """
    if type(default).__name__.startswith("_"):
        name = getattr(default, "name", None)
        return name if isinstance(name, str) else "…"
    return repr(default)


def _parameters(func: Callable[..., Any], *, bound: bool) -> list[inspect.Parameter]:
    """List the parameters a caller passes.

    Parameters
    ----------
    func
        A function.
    bound
        Whether the first parameter (``self`` or ``cls``) is bound, and so left out.

    Returns
    -------
    list of inspect.Parameter
        The parameters in order.
    """
    params = list(inspect.signature(func).parameters.values())
    return params[1:] if bound else params


def format_signature(call: str, func: Callable[..., Any], *, bound: bool) -> str:
    """Write a function's signature the way a caller reads it.

    Parameters
    ----------
    call
        What the caller writes before the parentheses, for example ``client.tables.get``.
    func
        The function.
    bound
        Whether the first parameter (``self`` or ``cls``) is bound, and so left out.

    Returns
    -------
    str
        For example ``client.tables.get(name: str, *, limit: int = 50) -> Table``: annotations
        as the source spells them without private names, ``/`` and ``*`` where the kinds change,
        and a private sentinel default by its name.
    """
    signature = inspect.signature(func)
    namespace = getattr(func, "__globals__", {})
    params = [
        param.replace(
            annotation=(
                param.empty
                if param.annotation is param.empty
                else _Shown(_annotation_text(param.annotation, namespace))
            ),
            default=param.empty
            if param.default is param.empty
            else _Shown(_sdk_default(param.default)),
        )
        for param in _parameters(func, bound=bound)
    ]
    returns = signature.return_annotation
    shown = signature.replace(
        parameters=params,
        return_annotation=(
            signature.empty
            if returns is signature.empty
            else _Shown(_annotation_text(returns, namespace))
        ),
    )
    return call + str(shown)


def _members(cls: type) -> list[tuple[str, str, Callable[..., Any]]]:
    """List the public methods and properties a class defines, in definition order.

    Parameters
    ----------
    cls
        A class.

    Returns
    -------
    list of tuple
        Each member's name, kind (``method``, ``static``, ``class`` or ``property``) and
        function (a property's getter). The last definition of an overloaded name is the one
        the class holds.
    """
    found: list[tuple[str, str, Callable[..., Any]]] = []
    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        if isinstance(value, staticmethod):
            found.append((name, _STATIC, value.__func__))
        elif isinstance(value, classmethod):
            found.append((name, _CLASS, value.__func__))
        elif isinstance(value, property) and value.fget is not None:
            found.append((name, _PROPERTY, value.fget))
        elif inspect.isfunction(value):
            found.append((name, "method", value))
    return found


def _doc_text(doc: NumpyDoc) -> list[str]:
    """Render a docstring's summary and body as paragraphs.

    Parameters
    ----------
    doc
        The parsed docstring.

    Returns
    -------
    list of str
        Markdown lines, each paragraph followed by a blank line.
    """
    lines: list[str] = []
    for part in (doc.summary, doc.body):
        if part:
            lines += [escape_text(_paragraphs(part)), ""]
    return lines


def _sdk_param_table(func: Callable[..., Any], doc: NumpyDoc, *, bound: bool) -> list[str]:
    """Render the parameters of a function as a table.

    Parameters
    ----------
    func
        The function.
    doc
        The docstring whose ``Parameters`` section describes them.
    bound
        Whether the first parameter is bound, and so left out.

    Returns
    -------
    list of str
        Markdown lines; empty when the function takes nothing.
    """
    params = _parameters(func, bound=bound)
    if not params:
        return []
    described = {entry.name: entry.text for entry in doc.sections.get("Parameters", [])}
    lines = ["| Name | Type | Default | Description |", "|---|---|---|---|"]
    for param in params:
        stars = {param.VAR_POSITIONAL: "*", param.VAR_KEYWORD: "**"}.get(param.kind, "")
        name = stars + param.name
        if stars:
            default = _ANY_NUMBER
        elif param.default is param.empty:
            default = "required"
        else:
            default = f"`{_sdk_default(param.default)}`"
        kind = _annotation_text(param.annotation, getattr(func, "__globals__", {}))
        text = described.get(name) or described.get(param.name) or _NOT_DOCUMENTED
        lines.append(
            f"| `{name}` | {_cell(f'`{kind}`' if kind else '')} | {_cell(default)}"
            f" | {_cell(escape_text(text))} |"
        )
    return [*lines, ""]


def _error_name(name: str, package: types.ModuleType) -> str:
    """Name an error the way a user imports it.

    Parameters
    ----------
    name
        The name in a ``Raises`` section.
    package
        The SDK package.

    Returns
    -------
    str
        ``lakana.errors.X`` and an exported bare ``X`` as ``lakana.X``; anything else, such as
        a builtin error, as it is.
    """
    prefix = f"{package.__name__}.errors."
    if name.startswith(prefix):
        return f"{package.__name__}.{name.removeprefix(prefix)}"
    return f"{package.__name__}.{name}" if name in package.__all__ else name


def _render_member(
    call: str, kind: str, func: Callable[..., Any], package: types.ModuleType, level: int
) -> list[str]:
    """Render one method or property.

    Parameters
    ----------
    call
        What a caller writes, for example ``client.tables.get``.
    kind
        ``method``, ``static``, ``class`` or ``property``.
    func
        The function, or a property's getter.
    package
        The SDK package, to name the errors.
    level
        The heading level.

    Returns
    -------
    list of str
        Markdown lines: the heading, the signature, the text, the parameters, what it returns
        or yields, and the errors.
    """
    doc = parse_numpydoc(func.__doc__)
    lines = [f"{'#' * level} `{call}`", ""]
    if kind == _PROPERTY:
        returns = _annotation_text(inspect.signature(func).return_annotation, func.__globals__)
        lines += [f"Read-only attribute: `{returns}`" if returns else "Read-only attribute.", ""]
        return lines + _doc_text(doc)
    bound = kind != _STATIC
    lines += [f"Signature: `{format_signature(call, func, bound=bound)}`", "", *_doc_text(doc)]
    lines += _sdk_param_table(func, doc, bound=bound)
    for section in ("Returns", "Yields"):
        for entry in doc.sections.get(section, []):
            lines += [escape_text(f"{section}: `{entry.name}`. {entry.text}"), ""]
    raises = doc.sections.get("Raises", [])
    if raises:
        lines += ["| Error | When |", "|---|---|"]
        lines += [
            f"| `{_error_name(e.name, package)}` | {_cell(escape_text(e.text))} |" for e in raises
        ]
        lines.append("")
    return lines


def _constructor(call: str, cls: type, doc: NumpyDoc) -> list[str]:
    """Render how a class is built: its signature and the table of its parameters.

    Parameters
    ----------
    call
        What a caller writes, for example ``lakana.Client``.
    cls
        The class.
    doc
        The class docstring, whose ``Parameters`` section describes the constructor.

    Returns
    -------
    list of str
        Markdown lines.
    """
    signature = format_signature(call, cls.__init__, bound=True).removesuffix(" -> None")
    return [f"Signature: `{signature}`", "", *_sdk_param_table(cls.__init__, doc, bound=True)]


def _sdk_stem(module: str) -> str:
    """Name the page of a module.

    Parameters
    ----------
    module
        A dotted module name, for example ``lakana.resources.ci_trust_rules``.

    Returns
    -------
    str
        Its last part with ``-`` for ``_``, for example ``ci-trust-rules``.
    """
    return module.rpartition(".")[2].replace("_", "-")


def _add_page(pages: dict[str, str], path: str, text: str) -> None:
    """Add a page, refusing a path that is already taken.

    Parameters
    ----------
    pages
        The pages so far, added to.
    path
        The page's site-relative path.
    text
        The page.

    Raises
    ------
    ValueError
        When two parts of the SDK give the same page.
    """
    if path in pages:
        raise ValueError(
            f"two parts of the SDK give the same page {path}; rename the module of one of them"
        )
    pages[path] = text


def _index_row(
    name: str, obj: object, package: types.ModuleType, client_cls: type
) -> tuple[str, str]:
    """Say what kind of name an export is and which page documents it.

    Parameters
    ----------
    name
        The exported name.
    obj
        What it names.
    package
        The SDK package.
    client_cls
        The client class.

    Returns
    -------
    tuple of str
        The kind (``class``, ``error``, ``function``, ``version text`` or ``value``) and a
        link to its page, or empty.
    """
    if obj is client_cls:
        return _CLASS, "[The client](client.md)"
    if isinstance(obj, type):
        return ("error", "[Errors](errors.md)") if issubclass(obj, BaseException) else (_CLASS, "")
    if inspect.isfunction(obj):
        stem = _sdk_stem(obj.__module__)
        return "function", f"[{title_from_slug(stem)}]({stem}.md)"
    return ("version text" if name == "__version__" else "value"), ""


def render_sdk_pages(
    package: types.ModuleType, client_cls: type, resources_package: str
) -> dict[str, str]:
    """Render the Python SDK reference from the docstrings of the package.

    Parameters
    ----------
    package
        The SDK package; its ``__all__`` is what it exports.
    client_cls
        The client class, one of the exports.
    resources_package
        The dotted name of the package of the resource classes.

    Returns
    -------
    dict of str to str
        Each page by site-relative path: ``reference/sdk/index.md``, ``client.md``,
        ``errors.md``, one page per module of the exported functions, then one page per module
        of the client's resources, in the client's order.

    Raises
    ------
    ValueError
        When two modules give the same page.
    """
    pkg = package.__name__
    source = f"the docstrings of the {pkg} package"
    exported = [(name, getattr(package, name)) for name in package.__all__]
    client_name = next(name for name, obj in exported if obj is client_cls)
    public = {obj: name for name, obj in exported if isinstance(obj, type)}
    errors = [(n, o) for n, o in exported if isinstance(o, type) and issubclass(o, BaseException)]
    functions: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
    for name, obj in exported:
        if inspect.isfunction(obj):
            functions.setdefault(_sdk_stem(obj.__module__), []).append((name, obj))
    modules: dict[str, list[SdkResource]] = {}
    for resource in client_resources(client_cls, resources_package):
        modules.setdefault(_sdk_stem(resource.cls.__module__), []).append(resource)

    index = [
        "# Python SDK reference",
        "",
        f"Every name the `{pkg}` package exports and every resource of its client, with the"
        " methods a program calls, written from the docstrings of the code.",
        "",
        "## Exported names",
        "",
        "| Name | Kind | Page |",
        "|---|---|---|",
    ]
    for name, obj in exported:
        kind, link = _index_row(name, obj, package, client_cls)
        shown = name if name.startswith("_") else f"{pkg}.{name}"  # `__version__`, not `lakana._`
        index.append(f"| `{shown}` | {kind} | {link} |")
    index += [
        "",
        "## Resources of the client",
        "",
        "| Attribute | Page | Description |",
        "|---|---|---|",
    ]
    for stem, resources in modules.items():
        for resource in resources:
            summary = parse_numpydoc(resource.cls.__doc__).summary
            index.append(
                f"| `client.{resource.attribute}` | [{title_from_slug(stem)}]({stem}.md)"
                f" | {_cell(escape_text(summary))} |"
            )

    client_doc = parse_numpydoc(client_cls.__doc__)
    call = f"{pkg}.{client_name}"
    client = [
        "# The client",
        "",
        *_doc_text(client_doc),
        *_constructor(call, client_cls, client_doc),
        "Each resource of the client has its page in the [Python SDK reference](index.md).",
        "",
    ]
    for name, kind, func in _members(client_cls):
        prefix = call if kind in {_CLASS, _STATIC} else "client"
        client += _render_member(f"{prefix}.{name}", kind, func, package, 2)

    error_page = [
        "# Errors",
        "",
        f"The errors the `{pkg}` package exports and what each means; each method of the client"
        " names the ones it raises.",
        "",
    ]
    for name, cls in errors:
        base = next(b for b in cls.__mro__[1:] if b in public or b.__module__ == "builtins")
        base_name = f"{pkg}.{public[base]}" if base in public else base.__name__
        doc = parse_numpydoc(cls.__doc__)
        error_page += [f"## `{pkg}.{name}`", "", f"Subclass of `{base_name}`.", "", *_doc_text(doc)]
        if "__init__" in vars(cls):
            error_page += _constructor(f"{pkg}.{name}", cls, doc)

    pages: dict[str, str] = {}
    _add_page(pages, f"{SDK_DIR}/index.md", _page(index, source))
    _add_page(pages, f"{SDK_DIR}/client.md", _page(client, source))
    _add_page(pages, f"{SDK_DIR}/errors.md", _page(error_page, source))
    for stem, funcs in functions.items():
        lines = [
            f"# {title_from_slug(stem)}",
            "",
            "Part of the [Python SDK reference](index.md).",
            "",
        ]
        for name, func in funcs:
            lines += _render_member(f"{pkg}.{name}", _STATIC, func, package, 2)
        _add_page(pages, f"{SDK_DIR}/{stem}.md", _page(lines, source))
    for stem, resources in modules.items():
        lines = [
            f"# {title_from_slug(stem)}",
            "",
            "Part of the [Python SDK reference](index.md).",
            "",
        ]
        for resource in resources:
            lines += [f"## `client.{resource.attribute}`", ""]
            if resource.note:
                lines += [escape_text(_paragraphs(resource.note)), ""]
            lines += _doc_text(parse_numpydoc(resource.cls.__doc__))
            for name, kind, func in _members(resource.cls):
                lines += _render_member(
                    f"client.{resource.attribute}.{name}", kind, func, package, 3
                )
        _add_page(pages, f"{SDK_DIR}/{stem}.md", _page(lines, source))
    return pages


def splice_region(text: str, name: str, body: str, *, path: str) -> str:
    """Replace the lines between a region's ``generated:begin`` and ``generated:end`` markers.

    Parameters
    ----------
    text
        The file's text.
    name
        The region's name, as the markers spell it.
    body
        The new lines of the region, unindented; each non-empty line is indented like the
        begin marker.
    path
        The file's repository path, named in the error.

    Returns
    -------
    str
        The text with the region's body replaced.

    Raises
    ------
    ValueError
        When the begin and end markers are not each present exactly once, begin first.
    """
    lines = text.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if f"generated:begin {name}" in line]
    ends = [i for i, line in enumerate(lines) if f"generated:end {name}" in line]
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        raise ValueError(
            f"{path}: the markers of the generated region {name} are missing or appear more "
            f"than once; restore one `generated:begin {name}` and one `generated:end {name}` line"
        )
    begin, end = begins[0], ends[0]
    marker = lines[begin]
    indent = marker[: len(marker) - len(marker.lstrip())]
    spliced = [indent + line if line.strip() else line for line in body.splitlines(keepends=True)]
    return "".join(lines[: begin + 1] + spliced + lines[end:])


def render_api_nav(parts: Sequence[tuple[str, Sequence[str]]]) -> str:
    """Render the navigation of the API reference as unindented YAML.

    Parameters
    ----------
    parts
        Each part's title and its pages, as paths under ``docs/site``.

    Returns
    -------
    str
        One navigation group per part, listing its pages in order.
    """
    out: list[str] = []
    for title, pages in parts:
        out.append(f"- {title}:\n")
        out.extend(f"    - {page}\n" for page in pages)
    return "".join(out)


def real_pages(root: Path = ROOT) -> dict[str, str]:
    """Generate the pages from the real code.

    Parameters
    ----------
    root
        The repository root; ``mkdocs.yml`` is read from it.

    Returns
    -------
    dict of str to str
        The text of each generated file, by repository path, including ``mkdocs.yml`` with its
        ``api-nav`` region written.
    """
    import lakana
    from lakana.cli.app import build_app
    from lakana.client import Client
    from lakana_core.config import Settings as CoreSettings
    from lakana_server.api.iceberg.router import router as iceberg_router
    from lakana_server.app import ROUTERS
    from lakana_server.settings import Settings as ServerSettings

    cli = render_cli_pages(build_app(), prog="lakana")
    openapi = render_openapi([router for router in ROUTERS if router is not iceberg_router])
    doc = json.loads(openapi)
    rest = render_rest_pages(doc)
    sdk = render_sdk_pages(lakana, Client, "lakana.resources")
    resource_pages = {
        f"{SDK_DIR}/{_sdk_stem(resource.cls.__module__)}.md"
        for resource in client_resources(Client, "lakana.resources")
    }
    nav = render_api_nav(
        [
            ("Command line", list(cli)),
            ("REST API", [REST_INDEX_PAGE, *rest]),
            ("Python SDK", [p for p in sdk if p not in resource_pages] + sorted(resource_pages)),
        ]
    )
    rest_index_page = SITE_PREFIX + REST_INDEX_PAGE
    return {
        **{SITE_PREFIX + path: text for path, text in cli.items()},
        SITE_PREFIX + SETTINGS_PAGE: render_settings(
            [("server", ServerSettings), ("worker", CoreSettings)]
        ),
        SITE_PREFIX + OPENAPI_PAGE: openapi,
        rest_index_page: splice_region(
            (root / rest_index_page).read_text(encoding="utf-8"),
            REST_REGION,
            rest_index(doc),
            path=rest_index_page,
        ),
        **{SITE_PREFIX + path: text for path, text in rest.items()},
        **{SITE_PREFIX + path: text for path, text in sdk.items()},
        "mkdocs.yml": splice_region(
            (root / "mkdocs.yml").read_text(encoding="utf-8"), NAV_REGION, nav, path="mkdocs.yml"
        ),
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    generate: Callable[[], Mapping[str, str]] = real_pages,
) -> int:
    """Write the generated files, or check that they are current.

    Besides writing what ``generate`` produces, a Markdown page in one of ``OWNED_DIRS`` that it
    no longer produces is deleted, or, under ``--check``, reported.

    Parameters
    ----------
    argv
        Command-line arguments; ``None`` reads ``sys.argv``.
    root
        The directory the file paths are relative to (the repository root).
    generate
        Produces the files, by repository path; replaced in tests.

    Returns
    -------
    int
        0 when the files were written or are current, 1 when ``--check`` finds a stale or
        left-over file.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--check", action="store_true", help="write nothing; fail when a page is stale"
    )
    args = parser.parse_args(argv)
    stale = 0
    pages = generate()
    for relative, text in pages.items():
        path = root / relative
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if args.check:
            if current != text:
                stale += 1
                print(
                    f"{relative} is stale: it differs from what the code produces. "
                    f"Regenerate it with `{REGENERATE}` and commit the result.",
                    file=sys.stderr,
                )
        elif current != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {relative}")
    for owned in OWNED_DIRS:
        directory = root / owned
        if not directory.is_dir():
            continue
        for page in sorted(directory.rglob("*.md")):
            relative = page.relative_to(root).as_posix()
            if relative in pages:
                continue
            if args.check:
                stale += 1
                print(
                    f"{relative} is left over: the code no longer produces it. "
                    f"Regenerate with `{REGENERATE}` and commit the deletion.",
                    file=sys.stderr,
                )
            else:
                page.unlink()
                print(f"deleted {relative}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
