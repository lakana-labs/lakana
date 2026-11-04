"""`lakana term *`: read the environment's glossary."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from lakana._models import TermLink
from lakana.cli import _output
from lakana.cli._state import resolve_environment, state_of

term_app = typer.Typer(
    help="Read the glossary: business terms and the tables and columns they point to.",
    no_args_is_help=True,
)

_GLOSSARY_URL = "https://lakana-labs.github.io/lakana/reference/glossary/"

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_LANG = Annotated[
    str | None,
    typer.Option("--lang", help="Show the texts in this language, such as de or fr-CH."),
]
_OWNER = Annotated[str | None, typer.Option("--owner", help="Only terms with exactly this owner.")]
_REPOSITORY = Annotated[
    str | None, typer.Option("--repository", help="Only terms this repository defines.")
]


@term_app.command("list")
def list_terms(
    ctx: typer.Context,
    owner: _OWNER = None,
    repository: _REPOSITORY = None,
    lang: _LANG = None,
    env: _ENV = None,
) -> None:
    """List the glossary's terms, sorted by key, with their names in your language.

    Parameters
    ----------
    ctx
        The Typer context.
    owner
        Only terms with this owner.
    repository
        Only terms of this repository.
    lang
        The language of the texts.
    env
        The environment; the profile's by default.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    terms = list(
        state.client().terms.iter(environment, owner=owner, repository=repository, lang=lang)
    )
    if not terms and state.output != "json":
        _output.emit(state, _no_terms(environment, owner, repository))
        return
    _output.emit(
        state, terms, columns=["key", "name", "owner", "repository"], droppable=["repository"]
    )


def _no_terms(environment: str, owner: str | None, repository: str | None) -> str:
    """Say why `term list` found nothing: the filters, or a glossary without terms.

    Parameters
    ----------
    environment
        The environment.
    owner
        The `--owner` filter, if given.
    repository
        The `--repository` filter, if given.

    Returns
    -------
    str
        The message, safe to print.
    """
    where = _output.inline(environment)
    filters = {"owner": owner, "repository": repository}
    given = {name: value for name, value in filters.items() if value is not None}
    if not given:
        return (
            f"No terms in {where}. A repository defines them in glossary/*.yaml: {_GLOSSARY_URL}"
        )
    named = " and ".join(f"{name} {_output.inline(repr(value))}" for name, value in given.items())
    options = " and ".join(f"--{name}" for name in given)
    return f"No terms in {where} with {named}. Drop {options} to see every term."


def _block(text: str) -> str:
    """Keep a text of several lines under its label, each line after the first indented.

    Parameters
    ----------
    text
        The text, such as a definition.

    Returns
    -------
    str
        The text, safe to print, so that no line of it can pass for a label of its own.
    """
    return _output.safe_text(text).replace("\n", "\n" + " " * len("Definition:  "))


def _link_line(link: TermLink) -> str:
    """Write one link: the table or column and its kind, or `missing`.

    Parameters
    ----------
    link
        The link.

    Returns
    -------
    str
        An indented line, safe to print.
    """
    target = link.table if link.column is None else f"{link.table}.{link.column}"
    return _output.inline(f"  {target} ({'missing' if link.missing else link.kind})", 500)


@term_app.command("get")
def get(ctx: typer.Context, key: str, lang: _LANG = None, env: _ENV = None) -> None:
    """Show term KEY: its definition, synonyms, owner, links and related terms.

    A link shows only when you may read its table; one whose table does not exist, or whose
    column the table no longer has, shows as missing.

    Parameters
    ----------
    ctx
        The Typer context.
    key
        The term's id in the glossary.
    lang
        The language of the texts.
    env
        The environment; the profile's by default.
    """
    state = state_of(ctx)
    term = state.client().terms.get(resolve_environment(state, env), key, lang=lang)
    if state.output == "json":
        _output.print_json(term)
        return
    inline = _output.inline
    related = ", ".join(
        f"{inline(r.key)} — {inline(r.name)}" if r.name else f"{inline(r.key)} (missing)"
        for r in term.related
    )
    lines = [
        f"Term:        {inline(term.key)} — {inline(term.name)}",
        f"Definition:  {_block(term.definition)}",
        f"Synonyms:    {', '.join(inline(s) for s in term.synonyms) or '(none)'}",
        f"Owner:       {inline(term.owner)}",
        f"Repository:  {inline(term.repository)}",
        f"Language:    {inline(term.language)}",
        "Links:" if term.links else "Links:       (none you may read)",
        *(_link_line(link) for link in term.links),
        f"Related:     {related or '(none)'}",
        f"Named by:    {', '.join(inline(k) for k in term.related_by) or '(none)'}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")


def register(app: typer.Typer) -> None:
    """Add the `term` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(term_app, name="term")
