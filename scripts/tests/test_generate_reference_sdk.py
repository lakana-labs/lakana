"""Tests for the Python SDK reference pages, one per resource module of the client."""

from __future__ import annotations

import enum
import re
import types
from pathlib import Path, PurePosixPath

import pytest

from scripts.generate_reference import (
    DocEntry,
    NumpyDoc,
    client_resources,
    format_signature,
    parse_numpydoc,
    render_sdk_pages,
)

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs" / "site"


class _Sentinel(enum.Enum):
    """A private marker for an argument left out."""

    UNSET = "unset"


class _Opaque:
    """A private default without a name."""


class _Transport:
    """Stands in for the HTTP transport."""


class Alpha:
    """A returned thing."""


class FakeError(Exception):
    """Something went wrong.

    Parameters
    ----------
    message
        What happened.
    code
        A machine-readable code.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class FakeMissing(FakeError):
    """A thing is missing."""


class _Hidden(FakeError):
    """A private base the public error skips over."""


class FakeGone(_Hidden):
    """A thing is gone."""


class Alphas:
    """Read and write alphas.

    The second paragraph
    of the class.

    Parameters
    ----------
    transport
        The transport.
    """

    LIMIT = 3

    def __init__(self, transport: _Transport) -> None:
        self._transport = transport

    def get(
        self,
        name: str,
        *,
        env: str | None = None,
        limit: int = 50,
        since=_Sentinel.UNSET,  # ruff: ignore[missing-type-function-argument]
    ) -> Alpha:
        """Get one alpha.

        It is read from the server.

        Parameters
        ----------
        name
            Its name.
        env
            The environment, `a | b`.
        limit
            How many, at most.
        since
            From when.

        Returns
        -------
        Alpha
            The alpha.

        Raises
        ------
        lakana.errors.NotFound
            When absent.
        FakeMissing
            When gone.
        TimeoutError
            When slow.
        """
        raise NotImplementedError

    def _private(self) -> None:
        """Not for a reader."""

    @property
    def count(self) -> int:
        """How many there are.

        Returns
        -------
        int
            The number.
        """
        return 0

    @property
    def label(self):  # ruff: ignore[missing-return-type-public-function]
        """A label without a type.

        Returns
        -------
        str
            The label.
        """
        return ""

    @staticmethod
    def parse(text: str, /, *rest: str, **extra: object) -> Alpha:
        """Parse an alpha.

        Parameters
        ----------
        text
            The text.
        *rest
            More text.
        **extra
            Options.

        Returns
        -------
        Alpha
            The alpha.
        """
        raise NotImplementedError

    def walk(self, start: int = 0) -> object:
        """Walk the alphas.

        Parameters
        ----------
        start
            Where to start.

        Yields
        ------
        Alpha
            Each alpha.
        """
        yield Alpha()


class Betas:
    """Betas for tasks."""

    def __init__(self, transport: _Transport) -> None:
        self._transport = transport

    def put(self, value: object, keep=_Opaque()) -> None:  # ruff: ignore[missing-type-function-argument, function-call-in-default-argument]
        """Store a value."""

    def many(
        self,
        mark: str | _Sentinel | None = None,
        rows: dict[str, list[int | None]] | _Rows | None = None,
    ) -> _AlphaList:
        """List alphas.

        Parameters
        ----------
        mark
            A mark.
        rows
            Some rows.

        Returns
        -------
        list of Alpha
            The alphas.
        """
        return []


_AlphaList = list[Alpha]
"""A private alias the page spells out."""
_Rows = list[dict[str, object]]
"""Another one, inside a union."""


def _make(transport: _Transport) -> Alphas:
    """Build an alphas resource the way a factory would.

    Parameters
    ----------
    transport
        The transport.

    Returns
    -------
    Alphas
        The resource.
    """
    return Alphas(transport)


class FakeClient:
    """The fake client.

    It talks to nothing.

    Parameters
    ----------
    url
        Where the server is.
    """

    def __init__(self, url: str | None = None) -> None:
        self._transport = _Transport()
        self.ci_token: str | None = None
        """Not a resource."""
        self.alphas = Alphas(self._transport)
        self.betas = Betas(self._transport)
        """For the helpers inside a task."""
        self.url_text = url
        self.cache = dict()
        self.made = _make(self._transport)
        self.first = self.second = Alphas(self._transport)
        self._alphas = Alphas(self._transport)
        print("built")

    @classmethod
    def build(cls, url: str) -> FakeClient:
        """Build a client for a URL.

        Parameters
        ----------
        url
            Where the server is.

        Returns
        -------
        FakeClient
            The client.
        """
        return cls(url)

    @property
    def where(self) -> str:
        """The server URL.

        Returns
        -------
        str
            The URL.
        """
        return ""

    def ping(self) -> bool:
        """Ask whether the server answers.

        Returns
        -------
        bool
            Whether it does.

        Raises
        ------
        FakeError
            When it does not.
        """
        raise NotImplementedError


def helper(path: str) -> str:
    """Help with a path.

    Parameters
    ----------
    path
        The path.

    Returns
    -------
    str
        The same path.
    """
    return path


Alphas.__module__ = "fake.resources.alpha"
Betas.__module__ = "fake.resources.beta_gamma"
helper.__module__ = "fake.helpers"

FAKE = types.ModuleType("lakana")
FAKE.__all__ = ["Client", "FakeError", "helper", "__version__"]
FAKE.Client = FakeClient
FAKE.FakeError = FakeError
FAKE.helper = helper
FAKE.__version__ = "1.2.3"


def _module(**names: object) -> types.ModuleType:
    """Build a package module that exports the given names.

    Parameters
    ----------
    **names
        Each exported name and its object.

    Returns
    -------
    types.ModuleType
        A module named ``lakana`` with ``__all__`` in the given order.
    """
    module = types.ModuleType("lakana")
    module.__all__ = list(names)
    for name, value in names.items():
        setattr(module, name, value)
    return module


def test_parse_numpydoc_reads_summary_body_and_sections() -> None:
    doc = parse_numpydoc("""Do a thing.

    More words
    here.

    Parameters
    ----------
    a, b
        Shared.
    c : int
        Typed.

    Returns
    -------
    Thing
        The thing.

    Raises
    ------
    lakana.errors.NotFound
        When absent.
    """)
    assert (doc.summary, doc.body) == ("Do a thing.", "More words here.")
    assert [e.name for e in doc.sections["Parameters"]] == ["a", "b", "c"]
    assert doc.sections["Parameters"][2].text == "Typed."
    assert doc.sections["Returns"] == [DocEntry("Thing", "The thing.")]
    assert parse_numpydoc(None) == NumpyDoc("", "", {})


def test_parse_numpydoc_of_a_summary_alone_has_no_body_or_sections() -> None:
    assert parse_numpydoc("Only this.") == NumpyDoc("Only this.", "", {})


def test_client_resources_follow_the_constructor_and_skip_the_rest() -> None:
    found = client_resources(FakeClient, "fake.resources")
    assert [(r.attribute, r.cls.__name__) for r in found] == [
        ("alphas", "Alphas"),
        ("betas", "Betas"),
    ]
    assert found[1].note == "For the helpers inside a task."


def test_format_signature_handles_kinds_defaults_and_sentinels() -> None:
    assert format_signature("client.alphas.get", Alphas.get, bound=True) == (
        "client.alphas.get(name: str, *, env: str | None = None, limit: int = 50, since=UNSET)"
        " -> Alpha"
    )
    assert format_signature(
        "lakana.Client.build", FakeClient.__dict__["build"].__func__, bound=True
    ).startswith("lakana.Client.build(")


def test_format_signature_writes_markers_varargs_and_unnamed_private_defaults() -> None:
    assert format_signature("client.alphas.parse", Alphas.parse, bound=False) == (
        "client.alphas.parse(text: str, /, *rest: str, **extra: object) -> Alpha"
    )
    assert format_signature("client.betas.put", Betas.put, bound=True) == (
        "client.betas.put(value: object, keep=…) -> None"
    )

    def typed(count: int, items: list[int] | None = None):  # ruff: ignore[missing-return-type-private-function]
        """Take real annotations.

        Parameters
        ----------
        count
            A count.
        items
            Some items.
        """

    typed.__annotations__ = {"count": int, "items": list[int]}
    assert format_signature("f", typed, bound=False) == "f(count: int, items: list[int] = None)"


def test_a_resource_page_shows_methods_with_tables_and_public_errors() -> None:
    pages = render_sdk_pages(FAKE, FakeClient, "fake.resources")
    assert list(pages)[:3] == [
        "reference/sdk/index.md",
        "reference/sdk/client.md",
        "reference/sdk/errors.md",
    ]
    alpha = pages["reference/sdk/alpha.md"]
    assert "\n## `client.alphas`\n" in alpha and "\n### `client.alphas.get`\n" in alpha
    assert "| `name` | `str` | required |" in alpha
    assert "| `lakana.NotFound` | When absent. |" in alpha  # lakana.errors.X → the exported name
    assert "_private" not in alpha and "transport" not in alpha
    assert "reference/sdk/beta-gamma.md" in pages
    assert "(not documented)" in pages["reference/sdk/beta-gamma.md"]  # a missing parameter shows


def test_a_resource_page_shows_every_part_in_order() -> None:
    alpha = render_sdk_pages(FAKE, FakeClient, "fake.resources")["reference/sdk/alpha.md"]
    assert alpha == (
        "# Python SDK: Alpha\n"
        "\n"
        "<!-- Generated by scripts/generate_reference.py from the docstrings of the lakana"
        " package. Do not edit by hand:\n"
        "run `uv run python scripts/generate_reference.py` and commit the result. -->\n"
        "\n"
        "Part of the [Python SDK reference](index.md).\n"
        "\n"
        "## `client.alphas`\n"
        "\n"
        "Read and write alphas.\n"
        "\n"
        "The second paragraph of the class.\n"
        "\n"
        "### `client.alphas.get`\n"
        "\n"
        "Signature: `client.alphas.get(name: str, *, env: str | None = None, limit: int = 50,"
        " since=UNSET) -> Alpha`\n"
        "\n"
        "Get one alpha.\n"
        "\n"
        "It is read from the server.\n"
        "\n"
        "| Name | Type | Default | Description |\n"
        "|---|---|---|---|\n"
        "| `name` | `str` | required | Its name. |\n"
        "| `env` | `str | None` | `None` | The environment, `a | b`. |\n"
        "| `limit` | `int` | `50` | How many, at most. |\n"
        "| `since` |  | `UNSET` | From when. |\n"
        "\n"
        "Returns: `Alpha`. The alpha.\n"
        "\n"
        "| Error | When |\n"
        "|---|---|\n"
        "| `lakana.NotFound` | When absent. |\n"
        "| `FakeMissing` | When gone. |\n"
        "| `TimeoutError` | When slow. |\n"
        "\n"
        "### `client.alphas.count`\n"
        "\n"
        "Read-only attribute: `int`\n"
        "\n"
        "How many there are.\n"
        "\n"
        "### `client.alphas.label`\n"
        "\n"
        "Read-only attribute.\n"
        "\n"
        "A label without a type.\n"
        "\n"
        "### `client.alphas.parse`\n"
        "\n"
        "Signature: `client.alphas.parse(text: str, /, *rest: str, **extra: object) -> Alpha`\n"
        "\n"
        "Parse an alpha.\n"
        "\n"
        "| Name | Type | Default | Description |\n"
        "|---|---|---|---|\n"
        "| `text` | `str` | required | The text. |\n"
        "| `*rest` | `str` | any number | More text. |\n"
        "| `**extra` | `object` | any number | Options. |\n"
        "\n"
        "Returns: `Alpha`. The alpha.\n"
        "\n"
        "### `client.alphas.walk`\n"
        "\n"
        "Signature: `client.alphas.walk(start: int = 0) -> object`\n"
        "\n"
        "Walk the alphas.\n"
        "\n"
        "| Name | Type | Default | Description |\n"
        "|---|---|---|---|\n"
        "| `start` | `int` | `0` | Where to start. |\n"
        "\n"
        "Yields: `Alpha`. Each alpha.\n"
    )


def test_a_resource_with_a_note_and_no_parameters_or_members() -> None:
    beta = render_sdk_pages(FAKE, FakeClient, "fake.resources")["reference/sdk/beta-gamma.md"]
    assert beta.startswith("# Python SDK: Beta gamma\n")
    assert "## `client.betas`\n\nFor the helpers inside a task.\n\nBetas for tasks.\n" in beta
    assert (
        "### `client.betas.put`\n\nSignature: `client.betas.put(value: object, keep=…) -> None`"
        in beta
    )
    assert "| `keep` |  | `…` | (not documented) |" in beta
    assert "| Error |" not in beta


def test_a_signature_spells_out_private_aliases_and_leaves_out_private_classes() -> None:
    assert format_signature("client.betas.many", Betas.many, bound=True) == (
        "client.betas.many(mark: str | None = None,"
        " rows: dict[str, list[int | None]] | list[dict[str, object]] | None = None)"
        " -> list[Alpha]"
    )
    beta = render_sdk_pages(FAKE, FakeClient, "fake.resources")["reference/sdk/beta-gamma.md"]
    assert "| `mark` | `str | None` | `None` | A mark. |" in beta


def test_the_client_page_shows_the_constructor_and_each_member() -> None:
    client = render_sdk_pages(FAKE, FakeClient, "fake.resources")["reference/sdk/client.md"]
    assert client.startswith("# Python SDK: The client\n")
    assert "\nThe fake client.\n\nIt talks to nothing.\n" in client
    assert "\nSignature: `lakana.Client(url: str | None = None)`\n" in client
    assert "| `url` | `str | None` | `None` | Where the server is. |" in client
    assert (
        "\n## `lakana.Client.build`\n\nSignature: `lakana.Client.build(url: str) -> FakeClient`\n"
        in client
    )
    assert "\n## `client.where`\n\nRead-only attribute: `str`\n" in client
    assert "\n## `client.ping`\n" in client
    assert "| `lakana.FakeError` | When it does not. |" in client  # a bare exported name
    assert "ci_token" not in client and "transport" not in client
    assert client.index("## `lakana.Client.build`") < client.index("## `client.where`")


def test_the_errors_page_names_the_public_base_and_the_constructor() -> None:
    package = _module(
        Client=FakeClient, FakeError=FakeError, FakeMissing=FakeMissing, FakeGone=FakeGone
    )
    errors = render_sdk_pages(package, FakeClient, "fake.resources")["reference/sdk/errors.md"]
    assert errors.startswith("# Python SDK: Errors\n")
    assert (
        "\n## `lakana.FakeError`\n\nSubclass of `Exception`.\n\nSomething went wrong.\n" in errors
    )
    assert "Signature: `lakana.FakeError(message: str, *, code: str | None = None)`" in errors
    assert "| `code` | `str | None` | `None` | A machine-readable code. |" in errors
    assert (
        "\n## `lakana.FakeMissing`\n\nSubclass of `lakana.FakeError`.\n\nA thing is missing.\n"
        in errors
    )
    assert (
        "\n## `lakana.FakeGone`\n\nSubclass of `lakana.FakeError`.\n" in errors
    )  # _Hidden skipped
    assert errors.count("Signature:") == 1  # only the error that defines its own constructor


def test_every_export_is_on_the_index() -> None:
    index = render_sdk_pages(FAKE, FakeClient, "fake.resources")["reference/sdk/index.md"]
    for name in FAKE.__all__:
        assert f"`lakana.{name}`" in index or f"`{name}`" in index


def test_the_index_names_each_kind_and_links_each_page() -> None:
    package = _module(
        Client=FakeClient,
        Alpha=Alpha,
        FakeError=FakeError,
        helper=helper,
        LIMIT=3,
        __version__="1.2.3",
    )
    index = render_sdk_pages(package, FakeClient, "fake.resources")["reference/sdk/index.md"]
    assert index.startswith("# Python SDK reference\n")
    assert "| `lakana.Client` | class | [The client](client.md) |" in index
    assert "| `lakana.Alpha` | class |  |" in index
    assert "| `lakana.FakeError` | error | [Errors](errors.md) |" in index
    assert "| `lakana.helper` | function | [Helpers](helpers.md) |" in index
    assert "| `lakana.LIMIT` | value |  |" in index
    assert "| `lakana.__version__` | version text |  |" in index
    assert "| `client.alphas` | [Alpha](alpha.md) | Read and write alphas. |" in index
    assert "| `client.betas` | [Beta gamma](beta-gamma.md) | Betas for tasks. |" in index


def test_an_exported_function_has_a_page_of_its_module() -> None:
    helpers = render_sdk_pages(FAKE, FakeClient, "fake.resources")["reference/sdk/helpers.md"]
    assert helpers.startswith("# Python SDK: Helpers\n")
    assert "\n## `lakana.helper`\n\nSignature: `lakana.helper(path: str) -> str`\n" in helpers
    assert "Returns: `str`. The same path." in helpers


def test_a_page_produced_twice_is_refused() -> None:
    def clash(value: int) -> int:
        """Clash with a resource page.

        Parameters
        ----------
        value
            A value.

        Returns
        -------
        int
            The value.
        """
        return value

    clash.__module__ = "fake.alpha"
    with pytest.raises(ValueError, match=r"reference/sdk/alpha\.md"):
        render_sdk_pages(_module(Client=FakeClient, clash=clash), FakeClient, "fake.resources")


def test_the_real_sdk_pages_name_only_public_errors() -> None:
    import lakana
    from lakana.client import Client

    pages = render_sdk_pages(lakana, Client, "lakana.resources")
    text = "\n".join(pages.values())
    assert not re.search(r"lakana\._[A-Za-z]", text) and "(not documented)" not in text
    public = {f"lakana.{n}" for n in lakana.__all__}
    for error in re.findall(r"^\| `([\w.]+)` \|", text, flags=re.MULTILINE):
        if "." in error and not error.startswith("client."):
            assert error in public, error
    for resource in client_resources(Client, "lakana.resources"):
        assert f"## `client.{resource.attribute}`" in text
    assert all(
        "```" not in p and not any(line.startswith("    ") for line in p.splitlines())
        for p in pages.values()
    )


def _url(path: str) -> str:
    """Give the URL a page's path is published at, as the site's directory URLs do.

    Parameters
    ----------
    path
        The page's path under ``docs/site``.

    Returns
    -------
    str
        ``a/b.md`` gives ``a/b/``; ``a/index.md`` gives ``a/``.
    """
    stem = PurePosixPath(path).with_suffix("")
    return f"{stem.parent if stem.name == 'index' else stem}/"


def test_no_sdk_page_takes_the_url_of_another_page_or_shows_maintainer_text() -> None:
    import lakana
    from lakana.client import Client

    pages = render_sdk_pages(lakana, Client, "lakana.resources")
    others = {
        _url(page.relative_to(SITE).as_posix())
        for page in SITE.rglob("*.md")
        if not page.relative_to(SITE).as_posix().startswith("reference/sdk/")
    }
    assert [path for path in pages if _url(path) in others] == []
    assert len({_url(path) for path in pages}) == len(pages)
    text = "\n".join(pages.values())
    leak = re.compile(
        r"lakana_(?:core|server|worker)|install_exception_handlers|NotFoundError|ConflictError"
    )
    assert leak.findall(text) == []
    assert re.findall(r"(?<![\w.])_[A-Za-z]\w*", text) == []  # no private name, not even bare
