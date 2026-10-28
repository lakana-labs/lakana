"""The assets `lakana init --template` writes, run against fake helpers."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import ModuleType
from typing import Any

import pyarrow as pa
import pytest

from lakana.cli.project_init import INGEST_TEMPLATES, Template
from lakana.ingest import files, rest, sql


class _Context:
    """A stand-in for the asset's `ctx`.

    Parameters
    ----------
    watermark
        What `watermark()` returns before its default applies.
    """

    def __init__(self, watermark: object) -> None:
        self._watermark = watermark

    def watermark(self, default: object = None) -> object:
        """Return the watermark, or `default` on a first run.

        Parameters
        ----------
        default
            The first run's value.

        Returns
        -------
        object
            The watermark.
        """
        return default if self._watermark is None else self._watermark


def _asset(template: Template) -> Any:  # ruff: ignore[any-type]
    """Execute a template's asset file and return its function.

    Parameters
    ----------
    template
        The template.

    Returns
    -------
    Any
        The decorated asset function.
    """
    (path,) = (p for p in INGEST_TEMPLATES[template] if p.endswith(".py"))
    module = ModuleType("template_asset")
    exec(INGEST_TEMPLATES[template][path], module.__dict__)  # nosec B102
    return module.__dict__[path.rsplit("/", 1)[1].removesuffix(".py")]


def test_the_files_template_passes_the_watermark_as_since(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` on a first run: every file is new."""
    seen: list[object] = []
    monkeypatch.setattr(
        files, "read", lambda c, p, *, since: seen.append((c, p, since)) or pa.table({})
    )

    _asset(Template.ingest_files)(_Context(None))

    assert seen == [("exports", "*.csv", None)]


@pytest.mark.parametrize(("stored", "sent"), [(None, 0), (41, 41)])
def test_the_sql_template_binds_the_last_id(
    monkeypatch: pytest.MonkeyPatch, stored: int | None, sent: int
) -> None:
    """The first run starts after 0; later runs after the largest id loaded."""
    seen: dict[str, object] = {}

    def fake(connection: str, query: str, *, params: dict[str, object]) -> pa.Table:
        seen.update(connection=connection, query=query, params=params)
        return pa.table({"id": pa.array([], pa.int64())})

    monkeypatch.setattr(sql, "read", fake)

    _asset(Template.ingest_sql)(_Context(stored))

    assert seen["connection"] == "source-db"
    assert seen["params"] == {"since": sent}


def test_the_rest_template_makes_updated_at_a_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API's ISO text becomes a UTC timestamp, a type a watermark may have."""
    seen: dict[str, Any] = {}

    def fake(connection: str, path: str, **options: Any) -> pa.Table:  # ruff: ignore[any-type]
        seen.update(connection=connection, path=path, **options)
        return pa.table({"id": [1], "updated_at": ["2026-10-27T10:00:00Z"]})

    monkeypatch.setattr(rest, "read", fake)

    result = _asset(Template.ingest_rest)(_Context(None))

    assert seen["path"] == "items"
    assert seen["records"] == "value"
    assert isinstance(seen["paginate"], rest.NextLink)
    assert seen["params"] == {"updated_since": dt.datetime(2000, 1, 1, tzinfo=dt.UTC)}
    assert result.schema.field("updated_at").type == pa.timestamp("us", "UTC")
    assert result.column("updated_at").to_pylist() == [dt.datetime(2026, 10, 27, 10, tzinfo=dt.UTC)]


def test_the_rest_template_returns_an_empty_page_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """No changed items: the zero-column table goes back, so the run writes nothing."""
    monkeypatch.setattr(rest, "read", lambda *a, **k: pa.table({}))

    result = _asset(Template.ingest_rest)(_Context(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)))

    assert result.num_columns == 0


@pytest.mark.parametrize("template", list(Template))
def test_each_recipe_shows_the_asset_its_template_writes(template: Template) -> None:
    """The how-to's recipes under docs/examples run the assets `lakana init --template` writes."""
    recipe = Path(__file__).resolve().parents[3] / "docs" / "examples" / template.value / "project"
    (path,) = (p for p in INGEST_TEMPLATES[template] if p.endswith(".py"))

    assert (recipe / path).read_text() == INGEST_TEMPLATES[template][path]
