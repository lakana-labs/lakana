"""Tests for the generator of the CLI, settings and OpenAPI references."""

import json
import re
from pathlib import Path
from typing import Annotated, Literal

import pytest
import typer
from fastapi import APIRouter
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from scripts.generate_reference import (
    CLI_PAGE,
    OPENAPI_PAGE,
    SETTINGS_PAGE,
    docstring_parameters,
    main,
    real_pages,
    render_cli,
    render_openapi,
    render_settings,
)


def _fixture_app(*, extra: bool = False) -> typer.Typer:
    """Build a tiny Typer app.

    Parameters
    ----------
    extra
        Whether to add a second command.

    Returns
    -------
    typer.Typer
        An app with one command, one group with one sub-command, and a hidden command.
    """
    app = typer.Typer(help="The tiny tool.", add_completion=False)
    group = typer.Typer(help="Manage things.", no_args_is_help=True)

    @app.command()
    def hello(
        name: str = typer.Argument(..., help="Who to greet."),
        times: int = typer.Option(2, "--times", help="How often."),
        loud: bool = typer.Option(False, "--loud", help="Shout."),
        mode: Literal["a", "b"] = typer.Option("a", help="Which mode."),
        low: int = typer.Option(3, min=1, max=5),
        floor: int = typer.Option(3, min=2),
        ceiling: int = typer.Option(3, max=9),
        many: Annotated[list[str] | None, typer.Option(help="Repeat me.")] = None,
        rate: float = typer.Option(1.5, help="A number."),
    ) -> None:
        """Greet someone.

            An indented line.

        A second paragraph | with a pipe.
        """

    @app.command(hidden=True)
    def secret_one() -> None:
        """Never listed."""

    @group.command("bare")
    def bare() -> None:
        pass

    @group.command("make")
    def make() -> None:
        """Make a thing."""

    app.add_typer(group, name="thing")
    if extra:

        @app.command()
        def goodbye() -> None:
            """Say goodbye."""

    return app


class _Inner(BaseModel):
    """Inner limits.

    Parameters
    ----------
    size
        How big, in bytes.
        Over two lines.
    token, other
        Shared text.

        After a blank line.

    Returns
    -------
    nothing
        Not a parameter.
    """

    size: int = Field(default=5, gt=0)
    token: SecretStr | None = None
    other: str = "x"
    key: SecretStr = SecretStr("hidden-default")


class _Settings(BaseSettings):
    """Tiny settings.

    Parameters
    ----------
    inner
        The inner group.
    flag
        Whether to do it.
    """

    model_config = SettingsConfigDict(env_prefix="TINY_", env_nested_delimiter="__")
    inner: _Inner = _Inner()
    flag: bool = True
    where: Literal["x", "y"] = "x"
    names: list[str] = []
    wait: float = 2.0
    options: dict[str, str] = {}
    required: str


def test_the_cli_page_lists_commands_options_and_help_in_order() -> None:
    """Every visible command appears with its arguments and options."""
    page = render_cli(_fixture_app(), prog="tiny")
    assert "tiny hello" in page
    assert "Greet someone." in page
    assert "Who to greet." in page
    assert "`--times`" in page
    assert "`--loud`" in page
    assert "tiny thing make" in page
    assert page.index("tiny hello") < page.index("tiny thing make")
    assert "secret-one" not in page
    assert "A second paragraph | with a pipe." in page
    assert "\n\nAn indented line.\n\n" in page
    assert "whole number, 1 to 5" in page
    assert "whole number, 2 or more" in page
    assert "whole number, 9 or less" in page
    assert "Repeat me." in page
    assert "| `--many` | text, repeatable | none |" in page
    assert "`HELLO" not in page


def test_a_command_added_to_the_app_shows_up() -> None:
    """The page follows the code."""
    assert "goodbye" not in render_cli(_fixture_app(), prog="tiny")
    assert "tiny goodbye" in render_cli(_fixture_app(extra=True), prog="tiny")


def test_the_cli_page_has_no_fenced_or_indented_code() -> None:
    """The block checker cannot judge generated code blocks, so there are none."""
    page = render_cli(_fixture_app(), prog="tiny")
    assert "```" not in page
    assert not any(line.startswith("    ") for line in page.splitlines())


def test_the_settings_page_lists_every_variable_with_type_default_and_text() -> None:
    """Nested fields get the prefix and the double underscore."""
    page = render_settings([("worker", _Settings)])
    assert "`TINY_INNER__SIZE`" in page
    assert "How big, in bytes. Over two lines." in page
    assert "above 0" in page
    assert "`TINY_INNER__TOKEN`" in page
    assert "Shared text." in page
    assert "`TINY_FLAG`" in page
    assert "Whether to do it." in page
    assert "`x`, `y`" in page
    assert "not set" in page
    assert "hidden-default" not in page
    assert "| list | empty |" in page
    assert "| mapping |" in page
    assert "| number | `2` |" in page
    assert "| text | required |" in page


def test_a_setting_added_to_a_model_shows_up() -> None:
    """The page follows the models."""

    class _More(_Settings):
        """More.

        Parameters
        ----------
        added
            A new knob.
        """

        added: int = 7

    assert "TINY_ADDED" not in render_settings([("worker", _Settings)])
    assert "`TINY_ADDED`" in render_settings([("worker", _More)])


def test_a_variable_two_components_read_names_both() -> None:
    """The reader column merges the models."""
    page = render_settings([("server", _Settings), ("worker", _Settings)])
    assert page.count("`TINY_FLAG`") == 1
    assert "server, worker" in page


def test_a_variable_without_a_documented_parameter_has_a_visible_gap() -> None:
    """An undocumented field is not silently blank."""

    class _Bare(BaseSettings):
        """Bare."""

        model_config = SettingsConfigDict(env_prefix="TINY_")
        lonely: int = 1

    assert "(not documented)" in render_settings([("worker", _Bare)])


def test_docstring_parameters_reads_numpydoc() -> None:
    """Names, comma lists, wrapped text, blank lines and the next section."""
    parsed = docstring_parameters(_Inner)
    assert parsed["size"] == "How big, in bytes. Over two lines."
    assert parsed["token"] == parsed["other"] == "Shared text. After a blank line."
    assert "nothing" not in parsed
    assert docstring_parameters(BaseModel) == {}


def test_docstring_parameters_of_a_class_without_a_section_is_empty() -> None:
    """No docstring, or no Parameters section, gives nothing."""

    class _NoDoc(BaseModel):
        x: int = 1

    class _NoSection(BaseModel):
        """Only a summary."""

        x: int = 1

    assert docstring_parameters(_NoDoc) == {}
    assert docstring_parameters(_NoSection) == {}


def test_a_later_source_fills_in_a_missing_description() -> None:
    """The first documented text wins; an undocumented field does not hide a documented one."""

    class _Undocumented(BaseSettings):
        """Undocumented."""

        model_config = SettingsConfigDict(env_prefix="TINY_")
        flag: bool = True

    page = render_settings([("worker", _Undocumented), ("server", _Settings)])
    assert "Whether to do it." in page
    assert "worker, server" in page


def test_check_fails_on_a_stale_page_and_names_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A page that differs from the code is reported with the regenerating command."""
    pages = {"reference/cli.md": "fresh\n"}
    assert main(["--check"], site=tmp_path, generate=lambda: pages) == 1
    out = capsys.readouterr().err
    assert "reference/cli.md" in out
    assert "uv run python scripts/generate_reference.py" in out
    assert main([], site=tmp_path, generate=lambda: pages) == 0
    assert main([], site=tmp_path, generate=lambda: pages) == 0
    assert (tmp_path / "reference" / "cli.md").read_text(encoding="utf-8") == "fresh\n"
    assert main(["--check"], site=tmp_path, generate=lambda: pages) == 0
    pages["reference/cli.md"] = "newer\n"
    assert main(["--check"], site=tmp_path, generate=lambda: pages) == 1


@pytest.mark.slow
def test_the_committed_pages_are_current() -> None:
    """The real pages equal what the code produces (the CI check, in the test run)."""
    assert main(["--check"]) == 0


def test_the_page_names_are_the_documented_ones() -> None:
    """The constants the docs and CI refer to."""
    assert (CLI_PAGE, SETTINGS_PAGE, OPENAPI_PAGE) == (
        "reference/cli.md",
        "reference/settings.md",
        "reference/openapi.json",
    )


def _fixture_router() -> APIRouter:
    """Build a router with one route.

    Returns
    -------
    APIRouter
        A router with ``GET /api/v1/things``.
    """
    router = APIRouter(prefix="/api/v1/things")

    @router.get("")
    def list_things(limit: int = 10) -> list[str]:
        """List things.

        Parameters
        ----------
        limit
            How many.

        Returns
        -------
        list of str
            The things.
        """
        return []

    return router


def test_the_openapi_document_describes_the_routers_it_is_given() -> None:
    """The document is OpenAPI JSON with the routes of the given routers and a fixed version."""
    text = render_openapi([_fixture_router()])
    doc = json.loads(text)
    assert doc["openapi"].startswith("3.")
    assert list(doc["paths"]) == ["/api/v1/things"]
    assert doc["info"]["title"] == "Lakana platform API"
    assert doc["info"]["version"] == "unreleased"
    assert "https://lakana-labs.github.io/lakana/reference/rest-api/" in doc["info"]["description"]
    assert text.endswith("}\n")
    assert render_openapi([_fixture_router()]) == text


@pytest.mark.slow
def test_the_committed_openapi_document_is_the_platform_api_only() -> None:
    """Every route is under /api/; the Iceberg REST catalog has its own specification."""
    doc = json.loads(real_pages()[OPENAPI_PAGE])
    assert doc["paths"]
    assert [path for path in doc["paths"] if not path.startswith("/api/")] == []


@pytest.mark.slow
def test_an_instance_serves_the_committed_platform_routes(tmp_path: Path) -> None:
    """``/openapi.json`` of a running instance has the same platform routes as the snapshot."""
    from lakana_core.config import DatabaseConfig
    from lakana_server.app import create_app
    from lakana_server.settings import Settings

    served = create_app(
        Settings(database=DatabaseConfig(url=f"sqlite:///{tmp_path}/x.db"))
    ).openapi()
    committed = json.loads(real_pages()[OPENAPI_PAGE])
    platform = {path: item for path, item in served["paths"].items() if path.startswith("/api/")}
    assert platform == committed["paths"]


_INTERNAL = re.compile(
    r"lakana_(?:core|server|worker)|\b[a-z_]+\.[a-z_]+\.[a-z_]+|\b[a-z_]+\.[a-z_]+\(|`[A-Za-z]+\.[a-z_]+`"
    r"|__repr|\bmodel_dump\b|\bSecretStr\b|\basyncio\b|`None`"
)


@pytest.mark.slow
def test_no_setting_description_names_internal_code() -> None:
    """The settings page is user documentation: a description names no internal code."""
    page = real_pages()[SETTINGS_PAGE]
    rows = [line for line in page.splitlines() if line.startswith("| `LAKANA_")]
    assert rows
    bad = [row.split("|")[1].strip() for row in rows if _INTERNAL.search(row.split("|", 5)[5])]
    assert bad == [], f"describe these for a user, not a maintainer: {bad}"


_MAINTAINER_TEXT = re.compile(
    r"lakana_(?:core|server|worker)|\bRaises\b|\bPermissionDenied\b|\bNotFoundError\b"
    r"|\bConflictError\b|\bValidationError\b|install_exception_handlers|\bprincipal\b: "
    r"|`(?:principal|catalog|session|service|settings|request|p)`|\bMapped to\b"
    r"|\bthe authenticated caller\b|\bfor this request\b",
)
"""Words of a maintainer's docstring that have no place in the published contract."""


def _operations() -> list[tuple[str, str, dict]]:
    """List the operations of the committed document.

    Returns
    -------
    list of tuple
        The method, the path and the operation object of each route.
    """
    doc = json.loads(real_pages()[OPENAPI_PAGE])
    return [
        (method.upper(), path, operation)
        for path, item in doc["paths"].items()
        for method, operation in item.items()
    ]


@pytest.mark.slow
def test_every_route_has_a_summary_and_a_description_for_a_user() -> None:
    """Each operation has a summary and a description that says more than the summary."""
    lacking = []
    for method, path, operation in _operations():
        summary = operation.get("summary", "")
        description = operation.get("description", "")
        if not summary or not description or description.startswith(summary):
            lacking.append(f"{method} {path}")
    assert lacking == []


@pytest.mark.slow
def test_no_route_description_shows_maintainer_text() -> None:
    """Internal parameter names and ``Raises`` lines stay in the code, out of the contract."""
    leaks = []
    for method, path, operation in _operations():
        for field in ("summary", "description"):
            found = _MAINTAINER_TEXT.search(operation.get(field, ""))
            if found:
                leaks.append(f"{method} {path} {field}: {found.group(0)!r}")
    assert leaks == []


@pytest.mark.slow
def test_every_route_that_needs_a_caller_documents_the_refusal_of_a_missing_one() -> None:
    """Every route answers 401 for a missing or wrong credential, and says so."""
    missing = [
        f"{method} {path}"
        for method, path, operation in _operations()
        if "401" not in operation["responses"]
    ]
    assert missing == []


@pytest.mark.slow
def test_every_parameter_of_every_route_has_a_description() -> None:
    """Path, query and header parameters say what they are for."""
    lacking = [
        f"{method} {path} {parameter['in']} {parameter['name']}"
        for method, path, operation in _operations()
        for parameter in operation.get("parameters", [])
        if not parameter.get("description")
    ]
    assert lacking == []
