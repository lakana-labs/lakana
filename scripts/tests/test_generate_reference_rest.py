"""Tests for the REST API reference pages, one per OpenAPI tag."""

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from scripts.generate_reference import (
    REST_REGION,
    escape_text,
    render_rest_pages,
    rest_index,
    schema_type,
    splice_region,
    tag_slug,
)
from scripts.tests.test_generate_reference import _MAINTAINER_TEXT

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs" / "site"


def _ref(name: str) -> dict[str, Any]:
    """Give the ``content`` of a JSON body that is a component schema.

    Parameters
    ----------
    name
        The schema's name.

    Returns
    -------
    dict
        ``{"content": ...}`` with the ``$ref``.
    """
    return {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}


_ERROR = _ref("ErrorOut")

FIXTURE: dict[str, Any] = {
    "paths": {
        "/api/v1/ci-rules": {
            "get": {
                "tags": ["ci-rules"],
                "summary": "List CI rules",
                "responses": {
                    "200": {
                        "description": "The rules.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/CiRule"},
                                }
                            }
                        },
                    }
                },
            }
        },
        "/api/v1/things": {
            "post": {
                "tags": ["things"],
                "summary": "Create a thing",
                "description": "Needs `MANAGE <x>` on <the instance>.\n\n# Not a heading",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "description": "How many | at most.",
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/ThingCreate"}}
                    },
                },
                "responses": {
                    "201": {
                        "description": "Created.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ThingOut"}
                            }
                        },
                    },
                    "404": {"description": "No such thing.", **_ERROR},
                },
            }
        },
        "/api/v1/things/{id}": {
            "get": {
                "tags": ["things"],
                "summary": "Show a thing's picture",
                "description": "The picture.",
                "deprecated": True,
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    }
                ],
                "responses": {
                    "200": {"description": "The picture.", "content": {"image/webp": {}}},
                    "204": {"description": "No picture."},
                },
            }
        },
    },
    "components": {
        "schemas": {
            "CiRule": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "ThingCreate": {
                "type": "object",
                "description": "Parameters\n----------\nname\n    Maintainer text.",
                "properties": {
                    "name": {"type": "string"},
                    "note": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "A note | for <people>.",
                    },
                },
                "required": ["name"],
            },
            "ThingOut": {
                "type": "object",
                "properties": {"id": {"type": "string", "format": "uuid"}},
                "required": ["id"],
            },
            "ErrorOut": {"type": "object", "properties": {"detail": {"type": "string"}}},
        }
    },
}


def _doc(operation: dict[str, Any], schemas: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wrap one operation, tagged ``things``, in a document.

    Parameters
    ----------
    operation
        The operation object, without its tags.
    schemas
        The component schemas.

    Returns
    -------
    dict
        An OpenAPI document with the operation at ``POST /x``.
    """
    return {
        "paths": {"/x": {"post": {"tags": ["things"], **operation}}},
        "components": {"schemas": schemas or {}},
    }


def test_schema_type_reads_unions_arrays_enums_and_maps() -> None:
    """OpenAPI types read as words, not raw JSON."""
    assert schema_type({"$ref": "#/components/schemas/ThingOut"}) == "`ThingOut`"
    assert schema_type({"anyOf": [{"type": "string"}, {"type": "null"}]}) == "text or null"
    assert schema_type({"type": "array", "items": {"$ref": "#/components/schemas/A"}}) == (
        "list of `A`"
    )
    assert schema_type({"type": "string", "enum": ["a", "b"]}) == "one of `a`, `b`"
    assert (
        schema_type({"type": "object", "additionalProperties": {"type": "integer"}})
        == "mapping of text to whole number"
    )
    assert schema_type({"type": "string", "format": "date-time"}) == "text (date-time)"
    assert schema_type({}) == "any"


def test_schema_type_reads_the_other_shapes() -> None:
    """Null first, oneOf, const, plain objects, open maps, bare arrays and odd types."""
    assert schema_type({"oneOf": [{"type": "null"}, {"type": "boolean"}]}) == (
        "true or false or null"
    )
    assert schema_type({"anyOf": [{"type": "string"}, {"type": "string"}]}) == "text"
    assert schema_type({"type": "string", "const": "x"}) == "`x`"
    assert schema_type({"type": "integer", "enum": [1, 2]}) == "one of `1`, `2`"
    assert schema_type({"type": "object", "properties": {}}) == "object"
    assert schema_type({"type": "object", "additionalProperties": False}) == "object"
    assert schema_type({"type": "object", "additionalProperties": True}) == "mapping of text to any"
    assert schema_type({"type": "array"}) == "list of any"
    assert schema_type({"type": "number"}) == "number"
    assert schema_type({"type": "null"}) == "null"
    assert schema_type({"type": ["string", "null"]}) == "any"


def test_escape_text_keeps_code_spans_and_escapes_markup() -> None:
    """``<name>`` and a leading ``#`` show as text; code spans are left alone."""
    assert escape_text("Use `Bearer <token>` or <name>") == "Use `Bearer <token>` or &lt;name&gt;"
    assert escape_text("# not a heading") == "\\# not a heading"


def test_escape_text_escapes_a_heading_on_any_line_but_not_in_a_code_span() -> None:
    """Only a ``#`` that starts a line outside a code span is escaped."""
    assert escape_text("a\n  # b") == "a\n  \\# b"
    assert escape_text("`x`# y\n# z") == "`x`# y\n\\# z"
    assert escape_text("`a\n# b`") == "`a\n# b`"


def test_tag_slug_keeps_good_tags_and_slugifies_others() -> None:
    """A tag that is already a slug is its page name."""
    assert tag_slug("ci-trust-rules") == "ci-trust-rules"
    assert tag_slug("Sign In") == "sign-in"
    assert tag_slug("  Row/Filters!  ") == "row-filters"


@pytest.mark.parametrize("tag", ["index", "Index", "!!!"])
def test_a_tag_that_would_take_the_section_index_or_no_name_is_refused(tag: str) -> None:
    """``rest/index.md`` would be the section's index page, and an empty slug no page at all."""
    with pytest.raises(ValueError, match="rename the tag"):
        tag_slug(tag)


def test_a_tag_page_lists_its_routes_with_parameters_body_and_answers() -> None:
    """Each route of the tag, with its parameters, body, answers and answer fields."""
    page = render_rest_pages(FIXTURE)["reference/rest/things.md"]
    assert page.startswith("# REST API: Things\n")
    assert "\n## `POST /api/v1/things`\n" in page
    assert "| `limit` | query | no | whole number or null |" in page
    assert "`ThingCreate`" in page and "| `name` | text | yes |" in page
    assert "| `201` |" in page and "| `404` |" in page
    assert "```" not in page and not any(line.startswith("    ") for line in page.splitlines())


def test_a_tag_page_shows_every_part_in_order() -> None:
    """The exact layout of a route, escaped where the text holds Markdown."""
    page = render_rest_pages(FIXTURE)["reference/rest/things.md"]
    assert page == (
        "# REST API: Things\n"
        "\n"
        "<!-- Generated by scripts/generate_reference.py from openapi.json. Do not edit by hand:\n"
        "run `uv run python scripts/generate_reference.py` and commit the result. -->\n"
        "\n"
        "These routes of the platform API come from its [OpenAPI document](../openapi.json);"
        " [The REST API as a contract](../rest-api.md) says what Lakana promises about them.\n"
        "\n"
        "## `POST /api/v1/things`\n"
        "\n"
        "Create a thing\n"
        "\n"
        "Needs `MANAGE <x>` on &lt;the instance&gt;.\n"
        "\n"
        "\\# Not a heading\n"
        "\n"
        "Parameters:\n"
        "\n"
        "| Name | In | Required | Type | Description |\n"
        "|---|---|---|---|---|\n"
        "| `limit` | query | no | whole number or null | How many \\| at most. |\n"
        "\n"
        "Request body (application/json, required): `ThingCreate`\n"
        "\n"
        "| Field | Type | Required | Description |\n"
        "|---|---|---|---|\n"
        "| `name` | text | yes |  |\n"
        "| `note` | text or null | no | A note \\| for &lt;people&gt;. |\n"
        "\n"
        "Answers:\n"
        "\n"
        "| Status | Meaning | Body |\n"
        "|---|---|---|\n"
        "| `201` | Created. | `ThingOut` |\n"
        "| `404` | No such thing. | `ErrorOut` |\n"
        "\n"
        "Answer fields: `ThingOut`\n"
        "\n"
        "| Field | Type | Required | Description |\n"
        "|---|---|---|---|\n"
        "| `id` | text (uuid) | yes |  |\n"
        "\n"
        "## `GET /api/v1/things/{id}`\n"
        "\n"
        "Deprecated: it will be removed in a major release (see [Deprecations]"
        "(../rest-api.md#deprecations)).\n"
        "\n"
        "Show a thing's picture\n"
        "\n"
        "The picture.\n"
        "\n"
        "Parameters:\n"
        "\n"
        "| Name | In | Required | Type | Description |\n"
        "|---|---|---|---|---|\n"
        "| `id` | path | yes | text (uuid) |  |\n"
        "\n"
        "Answers:\n"
        "\n"
        "| Status | Meaning | Body |\n"
        "|---|---|---|\n"
        "| `200` | The picture. | `image/webp` |\n"
        "| `204` | No picture. | none |\n"
    )


def test_a_route_without_summary_description_or_parameters_has_only_its_answers() -> None:
    """The CI rules page: no summary text beyond the summary, no parameters, a list answer."""
    page = render_rest_pages(FIXTURE)["reference/rest/ci-rules.md"]
    assert page.startswith("# REST API: CI rules\n")
    body = page.split("## `GET /api/v1/ci-rules`\n", 1)[1]
    assert body == (
        "\n"
        "List CI rules\n"
        "\n"
        "Answers:\n"
        "\n"
        "| Status | Meaning | Body |\n"
        "|---|---|---|\n"
        "| `200` | The rules. | list of `CiRule` |\n"
    )


def test_an_optional_body_of_a_nullable_model_lists_the_model_fields() -> None:
    """``anyOf [$ref, null]`` reads as the model, and an optional body says so."""
    doc = _doc(
        {
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "anyOf": [{"$ref": "#/components/schemas/Key"}, {"type": "null"}]
                        }
                    }
                }
            },
            "responses": {
                "200": {
                    "description": "OK.",
                    "content": {
                        "application/json": {"schema": {"type": "string"}},
                        "text/csv": {},
                    },
                }
            },
        },
        {"Key": {"type": "object", "properties": {"k": {"type": "string"}}}},
    )
    page = render_rest_pages(doc)["reference/rest/things.md"]
    assert "Request body (application/json, optional): `Key` or null\n" in page
    assert "| `k` | text | no |  |" in page
    assert "| `200` | OK. | text or `text/csv` |" in page
    assert "Answer fields" not in page


def test_an_inline_body_lists_its_own_fields() -> None:
    """A body schema written in place (not a ``$ref``) still gets its field table."""
    doc = _doc(
        {
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "title": "TokenIn",
                            "properties": {"id_token": {"type": "string"}},
                            "required": ["id_token"],
                        }
                    }
                },
            },
            "responses": {"200": {"description": "OK."}},
        }
    )
    page = render_rest_pages(doc)["reference/rest/things.md"]
    assert "Request body (application/json, required): object\n" in page
    assert "| `id_token` | text | yes |  |" in page


def test_a_body_without_fields_has_no_field_table() -> None:
    """A list body or a reference to a schema without properties names only its type."""
    doc = _doc(
        {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/E"}}},
            },
            "responses": {
                "201": {
                    "description": "OK.",
                    "content": {
                        "application/json": {
                            "schema": {"anyOf": [{"type": "string"}, {"type": "integer"}]}
                        }
                    },
                },
                "202": {"description": "Queued.", **_ref("E")},
                "400": {"description": "Refused.", **_ref("ErrorOut")},
            },
        },
        {
            "E": {"type": "string", "enum": ["a"]},
            "ErrorOut": FIXTURE["components"]["schemas"]["ErrorOut"],
        },
    )
    page = render_rest_pages(doc)["reference/rest/things.md"]
    assert "Request body (application/json, required): `E`\n" in page
    assert "| Field |" not in page


def test_the_rest_index_names_every_tag_page_with_its_route_count() -> None:
    """One line per tag page, in the order the tags first appear."""
    # FIXTURE lists the ci-rules route first; tags keep the order in which they first appear
    assert rest_index(FIXTURE) == (
        "- [CI rules](rest/ci-rules.md): 1 route\n- [Things](rest/things.md): 2 routes\n"
    )


def test_the_rest_index_splices_into_its_region() -> None:
    """The region name the index page's markers use."""
    text = "a\n<!-- generated:begin rest-pages -->\nold\n<!-- generated:end rest-pages -->\n"
    assert REST_REGION == "rest-pages"
    assert "old" not in splice_region(text, REST_REGION, rest_index(FIXTURE), path="x.md")


def test_two_tags_with_one_slug_are_refused() -> None:
    """Two tags may not share a page."""
    with pytest.raises(ValueError, match="same page"):
        render_rest_pages(
            {"paths": {"/a": {"get": {"tags": ["A b"]}}, "/b": {"get": {"tags": ["a-b"]}}}}
        )


def test_a_path_item_key_that_is_not_a_method_is_not_a_route() -> None:
    """Shared `parameters` and a `summary` of a path item are not operations."""
    doc = {
        "paths": {
            "/a": {
                "summary": "Shared.",
                "parameters": [{"name": "x", "in": "query"}],
                "get": {"tags": ["Things"], "responses": {}},
            }
        }
    }
    pages = render_rest_pages(doc)
    assert list(pages) == ["reference/rest/things.md"]


def test_a_route_without_a_tag_is_refused() -> None:
    """Every route needs a tag, or it would be on no page."""
    with pytest.raises(ValueError, match="GET /a has no tag"):
        render_rest_pages({"paths": {"/a": {"get": {"responses": {}}}}})


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


def _real_doc() -> dict[str, Any]:
    """Read the committed OpenAPI document.

    Returns
    -------
    dict
        The document.
    """
    return json.loads((SITE / "reference" / "openapi.json").read_text(encoding="utf-8"))


def test_the_real_rest_pages_cover_every_route_and_show_no_maintainer_text() -> None:
    """Every route of the committed document is on its tag's page, in user words only.

    A field or parameter name (the first cell of a table row) is part of the contract, so the
    maintainer-text rule, which looks for internal names in prose, does not apply to it.
    """
    doc = _real_doc()
    pages = render_rest_pages(doc)
    assert len(pages) == len(
        {t for i in doc["paths"].values() for o in i.values() for t in o["tags"]}
    )
    text = "\n".join(pages.values())
    for path, item in doc["paths"].items():
        for method in item:
            assert f"## `{method.upper()} {path}`" in text
    prose = "\n".join(re.sub(r"^\| `[^`]+` \|", "|", line) for line in text.splitlines())
    assert _MAINTAINER_TEXT.search(prose) is None
    assert "```" not in text and not any(line.startswith("    ") for line in text.splitlines())


def test_no_rest_page_takes_the_url_of_another_page() -> None:
    """No tag page is a section index, and none is published at a hand-written page's URL."""
    pages = render_rest_pages(_real_doc())
    assert not [path for path in pages if PurePosixPath(path).name == "index.md"]
    others = {
        _url(page.relative_to(SITE).as_posix())
        for page in SITE.rglob("*.md")
        if not page.relative_to(SITE).as_posix().startswith("reference/rest/")
    }
    assert [path for path in pages if _url(path) in others] == []
    assert len({_url(path) for path in pages}) == len(pages)
    assert _url("reference/rest-api.md") != _url("reference/rest/index.md")
