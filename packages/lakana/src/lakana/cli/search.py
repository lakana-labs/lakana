"""`lakana search`: find tables, columns, runs and people by a text."""

from __future__ import annotations

import shlex
from enum import StrEnum
from typing import Annotated

import typer

from lakana._models import SearchHit
from lakana.cli import _output
from lakana.cli._state import resolve_environment, state_of


class Kind(StrEnum):
    """The values `--kind` accepts."""

    table = "table"
    column = "column"
    run = "run"
    principal = "principal"


class ModelKind(StrEnum):
    """The values `--model-kind` accepts."""

    fact = "fact"
    dimension = "dimension"
    wide = "wide"
    staging = "staging"


def _name(hit: SearchHit) -> str:
    """Write a hit's name, with a table's declared kind after it.

    Parameters
    ----------
    hit
        The hit.

    Returns
    -------
    str
        The name, followed by ``[kind]`` for a table that declares one.
    """
    return f"{hit.name} [{hit.model_kind}]" if hit.model_kind else hit.name


def _matched(hit: SearchHit) -> str:
    """Write what matched, with the language of a matched text after it.

    Parameters
    ----------
    hit
        The hit.

    Returns
    -------
    str
        The field, followed by ``[language]`` when a text in a language matched.
    """
    return f"{hit.matched} [{hit.language}]" if hit.language else hit.matched


def _where(hit: SearchHit) -> str:
    """Say where a hit is, for a reader who has the name.

    Parameters
    ----------
    hit
        The hit.

    Returns
    -------
    str
        The table's kind for a table, the column's type for a column, the job and repository
        for a run, the principal's type for a principal.
    """
    if hit.kind == "table":
        return (hit.table_kind or "").lower()
    if hit.kind == "column":
        return hit.column_type or ""
    if hit.kind == "run":
        return " / ".join(part for part in (hit.repository, hit.job) if part)
    return (hit.principal_type or "").lower()


def search(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(metavar="TEXT", help="What to find; 1 to 200 characters.")],
    kind: Annotated[Kind | None, typer.Option("--kind", help="Only this kind of result.")] = None,
    model_kind: Annotated[
        ModelKind | None,
        typer.Option(
            "--model-kind",
            help="Only tables whose asset header declares this kind; other results are left out.",
        ),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=100, help="Most results to show (1 to 100).")
    ] = 20,
    offset: Annotated[
        int, typer.Option("--offset", min=0, help="Results to skip, to show the next page.")
    ] = 0,
    lang: Annotated[
        str | None,
        typer.Option(
            "--lang",
            help="Match translated labels and descriptions in this language, such as de or fr-CH.",
        ),
    ] = None,
    env: Annotated[
        str | None, typer.Option("--env", help="Environment (default: the profile's).")
    ] = None,
) -> None:
    """Find tables, columns, labels, descriptions, runs and people that match TEXT.

    Matching ignores case and finds TEXT anywhere in a name, label or description. Exact matches
    come first, then names and labels that start with TEXT, then those that contain it, then
    descriptions. Texts in a table's main language always match; translated ones in your
    language (--lang, else your profile's, else the instance's main language), and the matched
    column shows the language in brackets. A table or column is listed once.
    Runs are found by the first four or more characters of their id, their job or their
    repository. A table is also found by the grain its asset header declares, and shows its kind
    in brackets after its name; a column by the label or description of its declared measure.
    You see only what you may read. Use --output json for every field.

    Parameters
    ----------
    ctx
        The Typer context.
    text
        The text to find.
    kind
        Only this kind of result.
    model_kind
        Only tables that declare this kind.
    limit
        Most results to show.
    offset
        Results to skip.
    lang
        The language to match translated texts in.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    page = state.client().search(
        environment,
        text,
        kind=kind.value if kind is not None else None,
        model_kind=model_kind.value if model_kind is not None else None,
        limit=limit,
        offset=offset,
        lang=lang,
    )
    if state.output == "json":
        _output.emit(state, page)
        return
    if not page.results:
        _output.emit(
            state,
            f"No match for {_output.inline(repr(text))}. Search looks at table names, column "
            "names, labels, descriptions, grains, run ids, jobs, repositories and (for admins) "
            "people; try a shorter text.",
        )
        return
    _output.emit(
        state,
        page.results,
        columns=["kind", "name", "matched", "where", "snippet"],
        derived={"where": _where, "name": _name, "matched": _matched},
        droppable=["snippet", "where"],
    )
    if page.has_more:
        quoted = shlex.quote(_output.inline(text))
        options: list[str] = []
        if kind is not None:
            options += ["--kind", kind.value]
        if model_kind is not None:
            options += ["--model-kind", model_kind.value]
        if limit != 20:
            options += ["--limit", str(limit)]
        if lang is not None:
            options += ["--lang", shlex.quote(_output.inline(lang))]
        options += [
            "--offset",
            str(offset + limit),
            "--env",
            shlex.quote(_output.inline(environment)),
        ]
        # Text that starts with a dash would be read as an option, so it follows a `--`.
        parts = (
            ["lakana", "search", *options, "--", quoted]
            if text.startswith("-")
            else ["lakana", "search", quoted, *options]
        )
        _output.emit(state, "More results: " + " ".join(parts))


def register(app: typer.Typer) -> None:
    """Add `search` to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("search")(search)
