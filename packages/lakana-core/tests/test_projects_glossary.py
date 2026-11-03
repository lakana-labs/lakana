"""Glossary files: `glossary/<area>.yaml`, discovered, parsed and checked for form (#375)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from lakana_core.projects.glossary import (
    MAX_GLOSSARY_FILES,
    DiscoveredGlossaryFile,
    Glossary,
    GlossaryEntry,
    TermTextsEntry,
    discover_glossary,
    glossary_entries,
    manifest_glossary,
    parse_glossary,
)
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.translations import DiscoveredTranslation, parse_translations

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


def test_a_term_over_the_column_cap_is_refused_before_its_columns_are_walked() -> None:
    columns = ", ".join(f"c{n}" for n in range(40_000))
    text = f"a:\n  name: A\n  definition: B\n  owner: c\n  links: {{gold.t: [{columns}]}}\n"
    _, issues = _parse(("glossary/a.yaml", text))
    assert [i.split(": ", 1)[1] for i in issues] == [
        "a.links: links 40000 columns, and a term links at most 200; link the columns people "
        "should use for the term"
    ]


def test_a_blank_column_is_refused_with_the_identifier_rule() -> None:
    text = "a:\n  name: A\n  definition: B\n  owner: c\n  links: {gold.t: ['  ']}\n"
    _, issues = _parse(("glossary/a.yaml", text))
    assert len(issues) == 1 and "a column of `gold.t`" in issues[0]
    assert issues[0].endswith("and must not be empty")


def test_glossary_entries_is_the_manifest_form_and_reads_back() -> None:
    glossary, _ = parse_glossary((DiscoveredGlossaryFile("glossary/sales.yaml", _SALES),))
    assert glossary is not None
    translations, _ = parse_translations(
        (
            DiscoveredTranslation(
                "translations/fr.yaml", "fr", "glossary:\n  net_revenue:\n    name: CA net\n"
            ),
        ),
        limits=DeployLimits(),
    )
    entries = glossary_entries(glossary, translations)
    assert entries["gross_revenue"] == {
        "name": "Gross revenue",
        "definition": "Revenue before returns and discounts.",
        "owner": "finance",
    }
    assert entries["net_revenue"]["links"] == {
        "gold.dim_store": [],
        "gold.fact_sales": ["net_amount"],
    }
    assert entries["net_revenue"]["translations"] == {"fr": {"name": "CA net"}}
    read = manifest_glossary({"version": 13, "glossary": entries})
    assert read["net_revenue"] == GlossaryEntry(
        "net_revenue",
        "Net revenue",
        "Revenue after returns and discounts, before tax.",
        "finance",
        ("revenue", "sales", "turnover"),
        (("gold.dim_store", None), ("gold.fact_sales", "net_amount")),
        ("gross_revenue",),
        {"fr": TermTextsEntry("CA net", None, ())},
    )


def test_manifest_glossary_drops_what_it_cannot_store(caplog: pytest.LogCaptureFixture) -> None:
    good = {"name": "A", "definition": "B", "owner": "c"}
    manifest = {
        "glossary": {
            "ok": good,
            "x" * 200: good,
            "long_table": {**good, "links": {"a." + "t" * 2000: []}},
            "bad_language": {**good, "translations": {"xx_YY!": {"name": "A"}}},
            "bad_synonyms": {**good, "synonyms": "revenue"},
            "bad_columns": {**good, "links": {"gold.a": [1, 2]}},
            "no_name": {"definition": "B", "owner": "c"},
        }
    }
    assert list(manifest_glossary(manifest)) == ["ok"]
    assert manifest_glossary({"version": 12}) == {}
    assert manifest_glossary(["not", "a", "manifest"]) == {}
    assert "dropped" in caplog.text


def test_manifest_glossary_drops_a_term_over_any_limit_of_the_files(
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = {"name": "A", "definition": "B", "owner": "c"}
    over = {
        "long_name": {**good, "name": "n" * 101},
        "nul_name": {**good, "name": "A\x00"},
        "long_definition": {**good, "definition": "d" * 4001},
        "nul_owner": {**good, "owner": "c\x00"},
        "many_synonyms": {**good, "synonyms": [f"s{n}" for n in range(21)]},
        "long_synonym": {**good, "synonyms": ["s" * 101]},
        "many_related": {**good, "related": [f"r{n}" for n in range(21)]},
        "many_tables": {**good, "links": {f"gold.t{n}": [] for n in range(51)}},
        "many_columns": {**good, "links": {"gold.t": [f"c{n}" for n in range(201)]}},
        "nul_column": {**good, "links": {"gold.t": ["c\x00"]}},
        "nul_table": {**good, "links": {"gold.t\x00": []}},
        "long_translated_name": {**good, "translations": {"fr": {"name": "n" * 101}}},
        "long_translated_definition": {
            **good,
            "translations": {"fr": {"definition": "d" * 4001}},
        },
        "many_translated_synonyms": {
            **good,
            "translations": {"fr": {"synonyms": [f"s{n}" for n in range(21)]}},
        },
    }
    manifest = {"glossary": {"ok": {**good, "synonyms": ["s" * 100]}, **over}}
    assert list(manifest_glossary(manifest)) == ["ok"]
    assert caplog.text.count("dropped") == len(over)


_REFERENCE = Path(__file__).resolve().parents[3] / "docs" / "site" / "reference" / "glossary.md"


def test_the_reference_quotes_every_deploy_message() -> None:
    from lakana_core.projects.glossary import (
        artifact_columns_message,
        missing_column_message,
        missing_link_message,
        missing_related_message,
        owned_elsewhere_message,
    )

    text = " ".join(_REFERENCE.read_text(encoding="utf-8").split())
    for message in (
        missing_column_message(
            "net_revenue", "gold.fact_sales", "net_amout", ("date", "store_id", "net_amount")
        ),
        missing_link_message("net_revenue", "gold.fact_sale", "dev", "gold.fact_sales"),
        artifact_columns_message("net_revenue", "gold.churn_model"),
        missing_related_message("net_revenue", "gros_revenue", "dev", "gross_revenue"),
        owned_elsewhere_message("net_revenue", "finance", "dev"),
    ):
        assert message in text, message


def test_a_missing_column_lists_at_most_twenty_columns() -> None:
    from lakana_core.projects.glossary import missing_column_message

    columns = tuple(f"c{i}" for i in range(25))
    assert missing_column_message("t", "a.b", "x", columns) == (
        "t links a.b column x, which a.b does not have (columns: "
        + ", ".join(columns[:20])
        + " … and 5 more); fix the column name or remove the link."
    )


def _links_issues(links: str) -> list[str]:
    """Parse one term whose `links:` is `links` and return its issues without file and line.

    Parameters
    ----------
    links
        The `links:` value, as YAML.

    Returns
    -------
    list of str
        The issues' messages.
    """
    text = f"a:\n  name: A\n  definition: B\n  owner: c\n  links: {links}\n"
    _, issues = _parse(("glossary/a.yaml", text))
    return [i.split(": ", 1)[1] for i in issues]


def test_a_link_table_key_with_a_control_or_bidi_character_cannot_forge_a_line() -> None:
    for key in ('"gold.x\\nlakana.yaml:1: forged"', '"gold.x\\u202e"', '" gold.x"'):
        (issue,) = _links_issues("{" + key + ": []}")
        assert "\n" not in issue and "\u202e" not in issue
        assert issue.startswith("a.links: `"), issue
        assert "is not a table name" in issue, issue


def test_a_link_table_or_column_name_over_1024_characters_is_refused() -> None:
    column = "c" * 1025
    assert _links_issues(f"{{gold.a: [{column}]}}") == [
        "a.links: a column of `gold.a` has 1025 characters, and a column name has at most 1024; "
        "fix the column name"
    ]
    # YAML allows a key this long only in the explicit form `? key`
    (issue,) = _links_issues("\n    ? gold." + "t" * 1020 + "\n    : []")
    assert issue.startswith("a.links: `gold.ttt") and issue.endswith(
        "is not a table name: it has 1025 characters, and a table name has at most 1024; write "
        "the table's dotted name, as the project builds it, such as `gold.fact_sales`"
    ), issue


def test_synonyms_equal_after_unicode_normalisation_are_refused() -> None:
    nfc, nfd = "caf\u00e9", "cafe\u0301"
    base = "a:\n  name: Net revenue\n  definition: B\n  owner: c\n"
    _, twice = _parse(("glossary/a.yaml", base + f"  synonyms: [{nfc}, {nfd}]\n"))
    named = _parse(
        (
            "glossary/a.yaml",
            f"a:\n  name: {nfc}\n  definition: B\n  owner: c\n  synonyms: [{nfd}]\n",
        )
    )[1]
    assert twice == [
        f"glossary/a.yaml:5: a.synonyms: lists `{nfd}` twice (case does not count); remove one"
    ]
    assert named == [
        f"glossary/a.yaml:5: a.synonyms: `{nfd}` is the term's name; a synonym is another word "
        "for it, so remove it here"
    ]


def test_a_root_folder_named_glossary_in_another_case_is_refused(tmp_path: Path) -> None:
    (tmp_path / "Glossary").mkdir()
    (tmp_path / "Glossary" / "sales.yaml").write_text(_SALES)
    assert [i.render() for i in discover_glossary(tmp_path)[1]] == [
        "Glossary: the glossary folder is spelled `glossary`, in lowercase; rename Glossary to "
        "glossary, or give it another name if it holds no glossary"
    ]
    assert discover_glossary(tmp_path)[0] is None

    if (tmp_path / "glossary").exists():
        return  # a case-insensitive filesystem cannot hold `glossary` beside `Glossary`
    (tmp_path / "glossary").mkdir()
    (tmp_path / "glossary" / "sales.yaml").write_text(_SALES)
    found, issues = discover_glossary(tmp_path)
    assert [f.path for f in found or ()] == ["glossary/sales.yaml"]
    assert [i.path for i in issues] == ["Glossary"]


def test_a_rollback_is_told_a_next_step_it_can_follow() -> None:
    from lakana_core.projects.glossary import owned_elsewhere_message

    assert owned_elsewhere_message("net_revenue", "finance", "dev", rollback=True) == (
        "net_revenue is a term of repository finance in dev, and a term id belongs to the "
        "repository that deployed it first; roll back to or deploy a version without "
        "net_revenue, or remove it from finance first."
    )


def test_a_refused_related_id_cannot_forge_a_line() -> None:
    text = 'a:\n  name: A\n  definition: B\n  owner: c\n  related: ["b\\nlakana.yaml:1: forged"]\n'
    _, issues = _parse(("glossary/a.yaml", text))
    assert issues == [
        "glossary/a.yaml:5: a.related: `b\\nlakana.yaml:1: forged` is not a term id; a term id is "
        "lowercase letters, digits and underscores, starts with a letter and has at most 63 "
        "characters, such as `net_revenue`"
    ]


@pytest.mark.parametrize(
    "value",
    ['["s\\nlakana.yaml:1: forged"]', '["s\\u202e"]', '["s", "s\\u2028x"]'],
)
def test_a_refused_synonym_cannot_forge_a_line(value: str) -> None:
    for text in (
        f"a:\n  name: A\n  definition: B\n  owner: c\n  synonyms: {value}\n",
        "a:\n  name: A\n  definition: B\n  owner: c\n  synonyms:\n"
        + "".join(f"    - {json.dumps(synonym)}\n" for synonym in json.loads(value)),
    ):
        _, issues = _parse(("glossary/a.yaml", text))
        assert issues and all(
            "\n" not in i and "\u202e" not in i and "\u2028" not in i for i in issues
        ), issues
    _, problems = parse_translations(
        [
            DiscoveredTranslation(
                "translations/fr.yaml", "fr", f"glossary:\n  a:\n    synonyms: {value}\n"
            )
        ],
        limits=DeployLimits(),
    )
    rendered = [p.render() for p in problems]
    assert rendered and all("\n" not in r and "\u202e" not in r for r in rendered), rendered


def test_an_unreadable_root_a_linked_glossary_and_an_unreadable_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    assert discover_glossary(tmp_path / "gone") == (None, [])
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "glossary").symlink_to(tmp_path / "elsewhere")
    assert [i.render() for i in discover_glossary(tmp_path)[1]] == [
        "glossary: symlinks are not allowed under glossary/"
    ]
    (tmp_path / "glossary").unlink()
    (tmp_path / "glossary").mkdir()
    real = os.scandir

    def scandir(path: Path) -> Iterator[os.DirEntry[str]]:
        """Refuse to list `glossary/`, as a folder without read permission would.

        Parameters
        ----------
        path
            The folder.

        Returns
        -------
        Iterator
            The real listing of any other folder.

        Raises
        ------
        PermissionError
            For `glossary/`.
        """
        if Path(path).name == "glossary":
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert discover_glossary(tmp_path) == (
        None,
        [ProjectIssue("glossary", None, "cannot be read: Permission denied")],
    )


def test_a_file_over_the_key_cap_and_a_term_that_is_not_a_mapping_are_refused() -> None:
    from lakana_core.projects.glossary import MAX_GLOSSARY_ENTRIES

    big = "a:\n  synonyms: [" + ", ".join("s" for _ in range(MAX_GLOSSARY_ENTRIES)) + "]\n"
    _, too_many = _parse(("glossary/big.yaml", big))
    glossary, scalar = _parse(("glossary/a.yaml", "net_revenue: 5\n"))
    assert len(too_many) == 1 and "has more than 50000 keys and list items" in too_many[0]
    assert scalar == [
        "glossary/a.yaml:1: `net_revenue` must be a mapping of its fields, such as "
        "`net_revenue: {name: …, definition: …, owner: …}`"
    ]
    assert "net_revenue" in glossary.invalid


def test_a_glossary_translation_that_is_not_a_mapping_is_refused() -> None:
    _, problems = parse_translations(
        (
            DiscoveredTranslation(
                "translations/fr.yaml", "fr", "glossary:\n  net_revenue: 5\n  1: {name: x}\n"
            ),
        ),
        limits=DeployLimits(),
    )
    assert [p.render() for p in problems] == [
        "translations/fr.yaml:2: `glossary.net_revenue` must be a mapping such as "
        "`{name: …, definition: …, synonyms: […]}`",
        "translations/fr.yaml:3: `glossary.1` must be a mapping such as "
        "`{name: …, definition: …, synonyms: […]}`",
    ]


_STORED = {"name": "N", "definition": "D", "owner": "o"}


@pytest.mark.parametrize(
    "damage",
    [
        {"name": ""},
        {"related": ["B"]},
        {"links": {"gold.a": ["c" * 1025]}},
        {"translations": "fr"},
        {"translations": {"fr": "x"}},
        {"translations": {"fr": {"synonyms": "x"}}},
        {"translations": {"fr": {"name": ""}}},
    ],
)
def test_a_damaged_stored_term_is_dropped(damage: dict[str, object]) -> None:
    assert manifest_glossary({"glossary": {"a": _STORED}}) != {}
    assert manifest_glossary({"glossary": {"a": {**_STORED, **damage}}}) == {}
