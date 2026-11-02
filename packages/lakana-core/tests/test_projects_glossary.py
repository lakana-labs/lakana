"""Glossary files: `glossary/<area>.yaml`, discovered, parsed and checked for form (#375)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lakana_core.projects.glossary import (
    MAX_GLOSSARY_FILES,
    DiscoveredGlossaryFile,
    Glossary,
    discover_glossary,
    parse_glossary,
)

_SALES = """\
net_revenue:
  name: Net revenue
  definition: >
    Revenue after returns and discounts, before tax.
  synonyms: [revenue, sales, turnover]
  owner: finance
  links:
    gold.fact_sales: [net_amount]
    gold.dim_store: []
  related: [gross_revenue]
gross_revenue:
  name: Gross revenue
  definition: Revenue before returns and discounts.
  owner: finance
"""


def _parse(*files: tuple[str, str]) -> tuple[Glossary, list[str]]:
    """Parse files given as (path, text) and render the issues.

    Parameters
    ----------
    *files
        Repository-relative path and content of each file.

    Returns
    -------
    tuple
        The `Glossary` and the rendered issues.
    """
    glossary, issues = parse_glossary(tuple(DiscoveredGlossaryFile(p, t) for p, t in files))
    assert glossary is not None
    return glossary, [issue.render() for issue in issues]


def test_a_glossary_file_parses_into_terms_with_their_fields() -> None:
    glossary, issues = _parse(("glossary/sales.yaml", _SALES))
    assert issues == []
    term = glossary.terms["net_revenue"]
    assert (term.name, term.definition, term.owner, term.path) == (
        "Net revenue",
        "Revenue after returns and discounts, before tax.",
        "finance",
        "glossary/sales.yaml",
    )
    assert term.synonyms == ("revenue", "sales", "turnover")
    assert term.links == {"gold.fact_sales": ("net_amount",), "gold.dim_store": ()}
    assert term.related == ("gross_revenue",)
    assert list(glossary.terms) == ["gross_revenue", "net_revenue"]
    assert glossary.line("net_revenue", "links", "gold.dim_store") == 9


def test_no_glossary_folder_is_none_and_other_files_are_ignored(tmp_path: Path) -> None:
    assert discover_glossary(tmp_path) == (None, [])
    (tmp_path / "glossary").mkdir()
    (tmp_path / "glossary" / "README.md").write_text("Notes\n")
    (tmp_path / "glossary" / "sales.yaml").write_text(_SALES)
    found, issues = discover_glossary(tmp_path)
    assert issues == []
    assert [f.path for f in found or ()] == ["glossary/sales.yaml"]


def test_folders_links_other_suffixes_and_a_file_named_glossary_are_refused(
    tmp_path: Path,
) -> None:
    (tmp_path / "glossary").write_text("x")
    assert [i.render() for i in discover_glossary(tmp_path)[1]] == [
        "glossary: glossary must be a folder of YAML files of terms, such as "
        "glossary/sales.yaml; rename this file, or move it out of the repository's root"
    ]
    (tmp_path / "glossary").unlink()
    (tmp_path / "glossary" / "sub").mkdir(parents=True)
    (tmp_path / "glossary" / "sales.yml").write_text(_SALES)
    (tmp_path / "glossary" / "link.yaml").symlink_to(tmp_path / "glossary" / "sales.yml")
    assert sorted(i.render() for i in discover_glossary(tmp_path)[1]) == [
        "glossary/link.yaml: symlinks are not allowed under glossary/",
        "glossary/sales.yml: a glossary file ends in .yaml; rename it to glossary/sales.yaml",
        "glossary/sub: glossary/ holds YAML files, not folders; put the terms in files directly "
        "under glossary/, such as glossary/sales.yaml",
    ]


def test_more_than_fifty_files_and_an_oversized_file_are_refused(tmp_path: Path) -> None:
    folder = tmp_path / "glossary"
    folder.mkdir()
    for n in range(MAX_GLOSSARY_FILES + 1):
        (folder / f"a{n:02}.yaml").write_text("")
    (folder / "a00.yaml").write_text("x" * (1024 * 1024 + 1))
    rendered = [i.render() for i in discover_glossary(tmp_path)[1]]
    assert "glossary/a00.yaml: larger than 1024 KiB" in rendered
    assert rendered[-1] == (
        "glossary: glossary/ holds 51 YAML files, and a repository has at most 50; merge some "
        "of them, such as one file per area"
    )


def test_a_key_that_is_not_a_term_id_is_refused_as_written() -> None:
    text = (
        "yes:\n  name: A\n  definition: B\n  owner: c\nNet-Revenue:\n  name: A\n"
        "  definition: B\n  owner: c\n"
    )
    _, issues = _parse(("glossary/a.yaml", text))
    rule = (
        "is not a term id; a term id is lowercase letters, digits and underscores, starts with "
        "a letter and has at most 63 characters, such as `net_revenue`. Rename the key; the "
        "shown name goes under `name:`"
    )
    assert issues == [
        f"glossary/a.yaml:1: `yes` {rule}",
        f"glossary/a.yaml:5: `Net-Revenue` {rule}",
    ]


def test_values_yaml_reads_as_other_types_are_refused_with_the_quote_hint() -> None:
    text = "a:\n  name: yes\n  definition: B\n  owner: 123\n  synonyms: [2024]\n"
    _, issues = _parse(("glossary/a.yaml", text))
    hint = "must be a text; in YAML, quote a value that reads as a number, a date, `yes` or `null`"
    assert issues == [
        f"glossary/a.yaml:2: a.name: {hint}",
        f"glossary/a.yaml:4: a.owner: {hint}",
        f"glossary/a.yaml:5: a.synonyms.0: {hint}",
    ]


def test_unknown_and_missing_fields_name_the_allowed_ones() -> None:
    _, issues = _parse(("glossary/a.yaml", "a:\n  name: A\n  descripton: B\n"))
    assert issues == [
        "glossary/a.yaml:3: a.descripton: is not a term field; did you mean `definition`? A "
        "term has `name`, `definition`, `owner`, `synonyms`, `links` and `related`",
        "glossary/a.yaml:1: a: needs `definition:`, what the term means, in a sentence or two",
        "glossary/a.yaml:1: a: needs `owner:`, the team or person who answers for the term, "
        "such as `owner: finance`",
    ]


def test_synonyms_equal_after_case_folding_are_refused() -> None:
    base = "a:\n  name: Net revenue\n  definition: B\n  owner: c\n"
    _, twice = _parse(("glossary/a.yaml", base + "  synonyms: [Revenue, revenue]\n"))
    _, named = _parse(("glossary/a.yaml", base + "  synonyms: [net Revenue]\n"))
    assert twice == [
        "glossary/a.yaml:5: a.synonyms: lists `revenue` twice (case does not count); remove one"
    ]
    assert named == [
        "glossary/a.yaml:5: a.synonyms: `net Revenue` is the term's name; a synonym is another "
        "word for it, so remove it here"
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", '"A\\tB"', "a.name: must not contain a tab; write it on one line with spaces"),
        ("name", "'" + "x" * 101 + "'", "a.name: must be at most 100 characters"),
        ("definition", "'" + "x" * 4001 + "'", "a.definition: must be at most 4000 characters"),
        (
            "owner",
            "'  '",
            "a.owner: must not be empty; name the team or person who answers for the term",
        ),
        (
            "synonyms",
            "[" + ", ".join(f"s{n}" for n in range(21)) + "]",
            "a.synonyms: has 21 synonyms, and a term has at most 20 per language; keep the words "
            "people search for",
        ),
        (
            "links",
            "[gold.a]",
            "a.links: must be a mapping of tables to their columns, such as "
            "`{gold.fact_sales: [net_amount], gold.dim_store: []}`",
        ),
        (
            "links",
            "{fact_sales: []}",
            "a.links: `fact_sales` is not a table name; write the "
            "table's dotted name, as the project builds it, such as `gold.fact_sales`",
        ),
        (
            "links",
            "{gold.a: x}",
            "a.links: `gold.a` must be a list of its columns, such as "
            "`[net_amount]`; write `[]` to link the table itself",
        ),
        ("links", "{gold.a: [x, x]}", "a.links: lists column `x` of `gold.a` twice; remove one"),
        (
            "links",
            "{" + ", ".join(f"gold.t{n}: []" for n in range(51)) + "}",
            "a.links: links 51 tables, and a term links at most 50; link the tables people should "
            "use for the term",
        ),
        ("related", "[b, b]", "a.related: names `b` twice; remove one"),
        ("related", "[a]", "a.related: names the term itself; remove `a`"),
        (
            "related",
            "[B]",
            "a.related: `B` is not a term id; a term id is lowercase letters, "
            "digits and underscores, starts with a letter and has at most 63 characters, such as "
            "`net_revenue`",
        ),
    ],
)
def test_each_field_rule_is_refused_with_its_fix(field: str, value: str, message: str) -> None:
    fields = {"name": "A", "definition": "B", "owner": "c", field: value}
    text = "a:\n" + "".join(f"  {k}: {v}\n" for k, v in fields.items())
    glossary, issues = _parse(("glossary/a.yaml", text))
    assert [i.split(": ", 1)[1] for i in issues] == [message]
    assert "a" in glossary.invalid and "a" not in glossary.terms


def test_a_term_id_in_two_files_and_too_many_terms_are_refused() -> None:
    term = "  name: A\n  definition: B\n  owner: c\n"
    _, issues = _parse(("glossary/a.yaml", "x:\n" + term), ("glossary/b.yaml", "x:\n" + term))
    assert issues == [
        "glossary/b.yaml:1: `x` is defined in glossary/a.yaml too; a term id is unique across "
        "the glossary files. Rename one of them, or merge the two terms"
    ]
    many = "".join(f"t{n}:\n{term}" for n in range(2001))
    _, issues = _parse(("glossary/a.yaml", many))
    assert issues == [
        "glossary: glossary/ defines 2001 terms, and a repository has at most 2,000; move some "
        "to another repository's glossary, or remove the ones nobody uses"
    ]


def test_a_file_that_is_not_a_mapping_and_yaml_aliases_are_refused() -> None:
    _, listed = _parse(("glossary/a.yaml", "- a\n"))
    _, aliased = _parse(("glossary/a.yaml", "a: &x\n  name: A\nb: *x\n"))
    assert listed == [
        "glossary/a.yaml:1: glossary/a.yaml must be a mapping of term ids to terms, such as "
        "`net_revenue: {name: Net revenue, definition: …, owner: finance}`"
    ]
    assert len(aliased) == 1 and "alias" in aliased[0]
