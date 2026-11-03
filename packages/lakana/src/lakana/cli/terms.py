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
        _output.emit(
            state,
            f"No terms in {_output.inline(environment)}. A repository defines them in "
            f"glossary/*.yaml: {_GLOSSARY_URL}",
        )
        return
    _output.emit(
        state, terms, columns=["key", "name", "owner", "repository"], droppable=["repository"]
    )


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

    A link shows only when you may read its table; one whose table does not exist shows as
    missing.

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
    related = ", ".join(
        f"{r.key} — {r.name}" if r.name else f"{r.key} (missing)" for r in term.related
    )
    lines = [
        f"Term:        {term.key} — {term.name}",
        f"Definition:  {term.definition}",
        f"Synonyms:    {', '.join(term.synonyms) or '(none)'}",
        f"Owner:       {term.owner}",
        f"Repository:  {term.repository}",
        f"Language:    {term.language}",
        "Links:" if term.links else "Links:       (none you may read)",
        *(_link_line(link) for link in term.links),
        f"Related:     {related or '(none)'}",
        f"Named by:    {', '.join(term.related_by) or '(none)'}",
    ]
    sys.stdout.write(_output.safe_text("\n".join(lines)) + "\n")


def register(app: typer.Typer) -> None:
    """Add the `term` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(term_app, name="term")
