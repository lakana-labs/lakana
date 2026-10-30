"""Translation files: discovery, parsing, and validation against the project."""

from __future__ import annotations

import dataclasses
import difflib
import os
import time
from pathlib import Path

import pytest
from test_projects_dag import make_asset

from lakana_core.projects import project_config as project_config_module
from lakana_core.projects import translations as translations_module
from lakana_core.projects import yaml_loader
from lakana_core.projects.asset_model import ParsedAsset
from lakana_core.projects.discovery import _read
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.project_config import (
    FREE_TEXT_MESSAGE,
    NEEDS_MAIN_LANGUAGE,
    ProjectConfig,
    TranslationsConfig,
    parse_project_config,
)
from lakana_core.projects.translations import (
    DiscoveredTranslation,
    TableTranslation,
    discover_translations,
    header_texts,
    parse_translations,
    translated_texts,
    translation_issues,
)

_LIMITS = DeployLimits()
_TAG_TEXT = (
    "is not a language tag Lakana takes: a language, then optionally a script and a region "
    "(BCP 47), such as en, de, fr-CH or zh-Hant"
)


def _sales(columns: tuple[str, ...] | None = ("store_id", "net_amount")) -> ParsedAsset:
    """Build `gold.fact_sales` with a label, a description, a grain, columns and a measure.

    Parameters
    ----------
    columns
        The SELECT's columns, or `None` for `SELECT *`.

    Returns
    -------
    ParsedAsset
        The asset.
    """
    return make_asset(
        "gold.fact_sales",
        columns=columns,
        metadata=AssetMetadata(
            materialize="table",
            label="Daily sales",
            description="One row per store and day.",
            kind="fact",
            grain="One row per store and day.",
            key=("store_id",),
            columns={"store_id": {"label": "Store", "description": "The store."}},
            measures={
                "net_amount": {"aggregation": "sum", "currency": "EUR", "label": "Net revenue"}
            },
        ),
    )


def _files(root: Path, files: dict[str, str]) -> Path:
    """Write files under `root`.

    Parameters
    ----------
    root
        The checkout.
    files
        Relative path → content.

    Returns
    -------
    Path
        `root`.
    """
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _issues_of(files: dict[str, str], config: str = "main_language: en\n") -> list[str]:
    """Discover, parse and validate translation texts against `gold.fact_sales`.

    Parameters
    ----------
    files
        File name under translations/ → content.
    config
        The `lakana.yaml`.

    Returns
    -------
    list of str
        Every issue as ``path:line: message`` (line omitted when unknown).
    """
    found = [
        DiscoveredTranslation(f"translations/{name}", name.removesuffix(".yaml"), text)
        for name, text in files.items()
    ]
    parsed, problems = parse_translations(found, limits=_LIMITS)
    issues = [*problems, *translation_issues(parse_project_config(config), [_sales()], parsed)]
    return [issue.render() for issue in issues]


def test_a_flat_folder_of_canonical_yaml_files_is_found(tmp_path: Path) -> None:
    root = _files(tmp_path, {"translations/fr.yaml": "{}\n", "translations/de-CH.yaml": "{}\n"})
    found, issues = discover_translations(root, limits=_LIMITS)
    assert issues == []
    assert [(f.path, f.language, f.text) for f in found] == [
        ("translations/de-CH.yaml", "de-CH", "{}\n"),
        ("translations/fr.yaml", "fr", "{}\n"),
    ]


def test_no_folder_finds_nothing(tmp_path: Path) -> None:
    assert discover_translations(tmp_path, limits=_LIMITS) == ([], [])


def test_a_file_named_translations_is_refused(tmp_path: Path) -> None:
    _files(tmp_path, {"translations": "gold.t: {label: T}\n"})
    assert discover_translations(tmp_path, limits=_LIMITS) == (
        [],
        [
            ProjectIssue(
                "translations",
                None,
                "translations must be a folder holding one <language>.yaml file per language, "
                "such as translations/de.yaml; move this file there under its language's name",
            )
        ],
    )


def test_a_yaml_suffix_in_another_case_gets_the_rename_hint(tmp_path: Path) -> None:
    root = _files(tmp_path, {"translations/fr.YAML": "{}\n", "translations/de.Yml": "{}\n"})
    _, issues = discover_translations(root, limits=_LIMITS)
    assert [i.render() for i in issues] == [
        "translations/de.Yml: a translation file ends in .yaml; rename it to translations/de.yaml",
        "translations/fr.YAML: a translation file ends in .yaml; rename it to translations/fr.yaml",
    ]


def test_every_entry_counts_toward_the_fifty_and_the_rest_is_not_examined(
    tmp_path: Path,
) -> None:
    files = {f"translations/note{i:03}.md": "x\n" for i in range(60)}
    root = _files(tmp_path, {**files, "translations/zu.yaml": "{}\n"})
    found, issues = discover_translations(root, limits=_LIMITS)
    assert found == []
    assert len(issues) == 51
    assert issues[-1].message == (
        "translations/ holds 61 files; a project has at most 50 languages besides its main language"
    )


def test_wrong_names_and_shapes_are_refused_with_the_fix(tmp_path: Path) -> None:
    root = _files(
        tmp_path,
        {
            "translations/de.yml": "{}\n",
            "translations/notes.md": "x\n",
            "translations/German.yaml": "{}\n",
            "translations/DE.yaml": "{}\n",
            "translations/sub/fr.yaml": "{}\n",
            "translations/README": "x\n",
        },
    )
    (root / "translations" / "it.yaml").symlink_to(root / "translations" / "de.yml")
    _, issues = discover_translations(root, limits=_LIMITS)
    assert [(i.path, i.message) for i in issues] == [
        (
            "translations/DE.yaml",
            "`DE` is written `de`; use that spelling: rename the file to translations/de.yaml",
        ),
        (
            "translations/German.yaml",
            f"`German` {_TAG_TEXT}; name the file after the language of its texts, such as "
            "translations/de.yaml",
        ),
        (
            "translations/README",
            "translations/ holds only one <language>.yaml file per language, such as "
            "translations/de.yaml; move this file elsewhere",
        ),
        (
            "translations/de.yml",
            "a translation file ends in .yaml; rename it to translations/de.yaml",
        ),
        ("translations/it.yaml", "symlinks are not allowed under translations/"),
        (
            "translations/notes.md",
            "translations/ holds only one <language>.yaml file per language, such as "
            "translations/de.yaml; move this file elsewhere",
        ),
        (
            "translations/sub",
            "translations/ holds files, not folders; put each language's texts in one file such "
            "as translations/de.yaml",
        ),
    ]


def test_a_link_out_of_the_checkout_is_refused_and_never_read(tmp_path: Path) -> None:
    """Adversarial: a linked folder or file pointing outside the checkout is refused by name."""
    outside = _files(tmp_path / "outside", {"fr.yaml": "gold.t: {label: secret}\n"})
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "translations").symlink_to(outside)
    assert discover_translations(checkout, limits=_LIMITS) == (
        [],
        [ProjectIssue("translations", None, "symlinks are not allowed under translations/")],
    )
    (checkout / "translations").unlink()
    (checkout / "translations").mkdir()
    (checkout / "translations" / "fr.yaml").symlink_to(outside / "fr.yaml")
    found, issues = discover_translations(checkout, limits=_LIMITS)
    assert found == []
    assert [i.render() for i in issues] == [
        "translations/fr.yaml: symlinks are not allowed under translations/"
    ]


def test_a_link_swapped_in_after_the_listing_is_refused_by_the_open(tmp_path: Path) -> None:
    """Race: the listing's link check is advisory; `_read`'s `O_NOFOLLOW` open holds."""
    root = _files(tmp_path, {"secret.yaml": "x: 1\n"})
    (root / "translations").mkdir()
    (root / "translations" / "fr.yaml").symlink_to(root / "secret.yaml")
    issues: list[ProjectIssue] = []
    data = _read(root / "translations" / "fr.yaml", "translations/fr.yaml", 1024, issues)
    assert data is None
    assert [i.render() for i in issues] == [
        "translations/fr.yaml: symlinks are not allowed under translations/"
    ]


def test_an_unsafe_file_name_is_escaped(tmp_path: Path) -> None:
    root = _files(tmp_path, {"translations/f‮r.yaml": "{}\n"})
    _, issues = discover_translations(root, limits=_LIMITS)
    assert [i.render() for i in issues] == [
        "translations/f\\u202er.yaml: the path has a non-UTF-8 or control character"
    ]


def test_an_oversized_or_non_utf8_file_is_refused(tmp_path: Path) -> None:
    root = _files(tmp_path, {"translations/fr.yaml": "# " + "x" * 2048 + "\n"})
    (root / "translations" / "de.yaml").write_bytes(b"\xff\n")
    found, issues = discover_translations(root, limits=DeployLimits(translation_bytes=1024))
    assert found == []
    assert [i.render() for i in issues] == [
        "translations/de.yaml: translations/de.yaml is not valid UTF-8",
        "translations/fr.yaml: larger than 1 KiB",
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_unreadable_folder_is_an_issue(tmp_path: Path) -> None:
    root = _files(tmp_path, {"translations/fr.yaml": "{}\n"})
    (root / "translations").chmod(0)
    try:
        found, issues = discover_translations(root, limits=_LIMITS)
    finally:
        (root / "translations").chmod(0o755)
    assert found == []
    assert [i.render() for i in issues] == ["translations: cannot be read: Permission denied"]


def test_more_than_fifty_languages_are_refused(tmp_path: Path) -> None:
    root = _files(
        tmp_path,
        {f"translations/a{chr(97 + i // 26)}{chr(97 + i % 26)}.yaml": "{}\n" for i in range(51)},
    )
    found, issues = discover_translations(root, limits=_LIMITS)
    assert len(found) == 50
    assert [i.message for i in issues] == [
        "translations/ holds 51 files; a project has at most 50 languages besides its main language"
    ]


def test_a_complete_file_is_accepted() -> None:
    text = (
        "gold.fact_sales:\n  label: Ventes\n  description: Une ligne.\n  grain: Une ligne.\n"
        "  columns:\n    store_id: {label: Magasin, description: Le magasin.}\n"
        "  measures:\n    net_amount: {label: CA net}\n"
    )
    assert _issues_of({"fr.yaml": text}) == []


def test_parsed_texts_are_stripped_and_kept() -> None:
    found = [
        DiscoveredTranslation(
            "translations/fr.yaml",
            "fr",
            "gold.fact_sales:\n  label: '  Ventes  '\n  columns:\n    store_id: {label: M}\n"
            "gold.empty:\n",
        )
    ]
    [parsed], issues = parse_translations(found, limits=_LIMITS)
    assert issues == []
    assert parsed.language == "fr"
    assert parsed.tables["gold.fact_sales"].label == "Ventes"
    assert parsed.tables["gold.empty"] == TableTranslation()


def test_an_empty_file_has_no_texts() -> None:
    found = [DiscoveredTranslation("translations/fr.yaml", "fr", "")]
    [parsed], issues = parse_translations(found, limits=_LIMITS)
    assert (parsed.tables, issues) == ({}, [])


def test_bad_yaml_is_refused_at_its_line() -> None:
    assert _issues_of({"fr.yaml": "gold.fact_sales: &a\n  label: x\n"}) == [
        "translations/fr.yaml:1: translations/fr.yaml: anchors and aliases are not allowed."
    ]


def test_a_file_that_is_not_a_mapping_of_tables_is_refused() -> None:
    message = (
        "translations/fr.yaml:1: translations/fr.yaml must be a mapping of table names to their "
        "texts, such as `gold.fact_sales: {label: Ventes}`"
    )
    assert _issues_of({"fr.yaml": "- gold.fact_sales\n"}) == [message]
    assert _issues_of({"fr.yaml": "1: {label: x}\n"}) == [message]


def test_too_many_keys_are_refused_before_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translations_module, "MAX_FILE_ENTRIES", 8)
    text = (
        "gold.fact_sales:\n  columns: {a: {label: A}, b: {label: B}}\n  measures: {c: {label: C}}\n"
    )
    refusal = (
        "translations/fr.yaml: has {} keys, and a translation file has at most 8; keep only the "
        "texts of tables, columns and measures the project builds"
    )
    assert _issues_of({"fr.yaml": text}) == [refusal.format(9)]
    nested = "gold.fact_sales:\n  x: {y: {z: [1, 2, {w: 3}]}}\n  v: {u: 1, t: 2}\n"
    assert _issues_of({"fr.yaml": nested}) == [refusal.format(11)]
    under = _issues_of({"fr.yaml": "gold.fact_sales:\n  columns: {a: {label: A}}\n"})
    assert [line.split(": ", 1)[1].startswith("`a` is not a column") for line in under] == [True]


def test_an_unknown_table_is_refused_with_a_suggestion() -> None:
    assert _issues_of({"fr.yaml": "gold.fact_sale:\n  label: Ventes\n"}) == [
        "translations/fr.yaml:1: `gold.fact_sale` is not a table this project builds; did you "
        "mean `gold.fact_sales`? Fix the name, or remove the entry"
    ]
    assert _issues_of({"fr.yaml": "silver.other:\n  label: X\n"}) == [
        "translations/fr.yaml:1: `silver.other` is not a table this project builds; fix the "
        "name, or remove the entry"
    ]


def test_an_unknown_table_with_unsafe_characters_is_escaped() -> None:
    [rendered] = _issues_of({"fr.yaml": '"gold.a\\u202eb":\n  label: X\n'})
    assert "‮" not in rendered
    assert "`gold.a\\u202eb` is not a table this project builds" in rendered


def test_field_errors_name_the_table_and_the_field() -> None:
    assert _issues_of({"fr.yaml": "gold.fact_sales:\n  labl: Ventes\n"}) == [
        "translations/fr.yaml:2: gold.fact_sales.labl: Extra inputs are not permitted"
    ]
    assert _issues_of({"fr.yaml": 'gold.fact_sales:\n  label: "a\\nb"\n'}) == [
        "translations/fr.yaml:2: gold.fact_sales.label: must be one line"
    ]
    assert _issues_of({"fr.yaml": f"gold.fact_sales:\n  description: {'x' * 4001}\n"}) == [
        "translations/fr.yaml:2: gold.fact_sales.description: must be at most 4000 characters"
    ]
    assert _issues_of({"fr.yaml": "gold.fact_sales:\n  columns:\n    store_id: {}\n"}) == [
        "translations/fr.yaml:3: gold.fact_sales.columns.store_id: needs `label:` or `description:`"
    ]
    assert _issues_of({"fr.yaml": f"gold.fact_sales:\n  grain: {'x' * 301}\n"}) == [
        "translations/fr.yaml:2: gold.fact_sales.grain: must be at most 300 characters"
    ]
    assert _issues_of({"fr.yaml": "gold.fact_sales:\n  columns:\n    store_id: Magasin\n"}) == [
        "translations/fr.yaml:2: gold.fact_sales.columns: `store_id` must be a mapping such as "
        "`{label: …, description: …}`"
    ]
    assert _issues_of({"fr.yaml": "gold.fact_sales:\n  measures: [net_amount]\n"}) == [
        "translations/fr.yaml:2: gold.fact_sales.measures: Input should be a valid dictionary"
    ]


def test_unsafe_characters_in_texts_are_refused() -> None:
    text = (
        'gold.fact_sales:\n  label: "Ven\\u202etes"\n'
        '  columns:\n    store_id: {description: "a\\u0007b"}\n'
        '  measures:\n    net_amount: {label: "x\\u2028y"}\n'
    )
    assert _issues_of({"fr.yaml": text}) == [
        f"translations/fr.yaml:2: gold.fact_sales.label: {FREE_TEXT_MESSAGE}",
        "translations/fr.yaml:4: gold.fact_sales.columns.store_id.description: "
        + FREE_TEXT_MESSAGE,
        f"translations/fr.yaml:6: gold.fact_sales.measures.net_amount.label: {FREE_TEXT_MESSAGE}",
    ]


def test_a_column_the_header_does_not_describe_is_refused_even_for_select_star() -> None:
    found = [
        DiscoveredTranslation(
            "translations/fr.yaml",
            "fr",
            "gold.fact_sales:\n  columns:\n    store_idd: {label: M}\n",
        )
    ]
    parsed, _ = parse_translations(found, limits=_LIMITS)
    star = _sales(columns=None)
    issues = translation_issues(parse_project_config("main_language: en\n"), [star], parsed)
    assert [i.render() for i in issues] == [
        "translations/fr.yaml:3: `store_idd` is not a column the header of `gold.fact_sales` "
        "describes (`columns:` in assets/gold/fact_sales.sql); fix the name, or describe the "
        "column there first"
    ]


def test_a_text_without_its_main_language_text_is_refused() -> None:
    bare = make_asset("gold.bare", metadata=AssetMetadata(materialize="table"))
    labelled = make_asset(
        "gold.labelled",
        metadata=AssetMetadata(materialize="table", columns={"c": {"label": "C"}}),
    )
    found = [
        DiscoveredTranslation(
            "translations/fr.yaml",
            "fr",
            "gold.fact_sales:\n  columns:\n    store_id: {label: M}\n"
            "  measures:\n    net_amount: {description: D}\n    gross: {label: G}\n"
            "gold.bare:\n  label: Nu\n"
            "gold.labelled:\n  columns:\n    c: {description: D}\n",
        )
    ]
    parsed, _ = parse_translations(found, limits=_LIMITS)
    config = parse_project_config("main_language: en\n")
    issues = translation_issues(config, [_sales(), bare, labelled], parsed)
    assert [i.render() for i in issues] == [
        "translations/fr.yaml:5: measure `net_amount` of `gold.fact_sales` has no description "
        "to translate; add `description:` to it under `measures:` in "
        "assets/gold/fact_sales.sql, or remove this text",
        "translations/fr.yaml:6: `gross` is not a measure of `gold.fact_sales` (`measures:` in "
        "assets/gold/fact_sales.sql); fix the name, or remove the entry",
        "translations/fr.yaml:8: the header of `gold.bare` has no label to translate; add "
        "`label:` in assets/gold/bare.sql, or remove this text",
        "translations/fr.yaml:11: the header of `gold.labelled` gives column `c` no description "
        "to translate; add `description:` for it under `columns:` in assets/gold/labelled.sql, "
        "or remove this text",
    ]


def test_an_artifact_has_no_texts_to_translate() -> None:
    model = dataclasses.replace(make_asset("ml.model"), output="artifact")
    assert header_texts(model) == []
    found = [DiscoveredTranslation("translations/fr.yaml", "fr", "ml.model:\n  label: M\n")]
    parsed, _ = parse_translations(found, limits=_LIMITS)
    issues = translation_issues(parse_project_config("main_language: en\n"), [model], parsed)
    assert [i.render() for i in issues] == [
        "translations/fr.yaml:1: `ml.model` is an artifact, which has no catalog texts to "
        "translate; remove the entry"
    ]


def test_the_main_language_file_and_a_missing_main_language_are_refused() -> None:
    assert _issues_of({"en.yaml": "{}\n"}) == [
        "translations/en.yaml: `en` is the project's main language (`main_language:` in "
        "lakana.yaml); its texts belong in the asset headers. Move them there, or remove the file"
    ]
    assert _issues_of({"fr.yaml": "{}\n"}, config="repo: x\n") == [
        f"lakana.yaml: {NEEDS_MAIN_LANGUAGE}"
    ]
    assert _issues_of({}, config="repo: x\n") == []


def test_a_missing_main_language_is_refused_once_for_many_files() -> None:
    files = {"fr.yaml": "{}\n", "de.yaml": "gold.fact_sales: {label: Verkäufe}\n"}
    assert _issues_of(files, config="repo: x\n") == [f"lakana.yaml: {NEEDS_MAIN_LANGUAGE}"]


def test_strict_mode_names_the_missing_keys() -> None:
    config = "main_language: en\ntranslations:\n  require: [fr, it]\n"
    assert _issues_of({"fr.yaml": "gold.fact_sales:\n  label: Ventes\n"}, config) == [
        "translations/fr.yaml: translations.require in lakana.yaml needs a `fr` text for every "
        "header text, and 5 are missing: gold.fact_sales.columns.store_id.description, "
        "gold.fact_sales.columns.store_id.label, gold.fact_sales.description, "
        "gold.fact_sales.grain, gold.fact_sales.measures.net_amount.label. Add them to "
        "translations/fr.yaml, or remove `fr` from translations.require.",
        "lakana.yaml: translations.require lists `it`, but there is no translations/it.yaml; add "
        "the file with a text for every header text, or remove `it` from the list",
    ]


def test_strict_mode_with_one_gap_and_without_any() -> None:
    config = "main_language: en\ntranslations:\n  require: [fr]\n"
    complete = (
        "gold.fact_sales:\n  label: V\n  description: U\n  grain: U\n"
        "  columns:\n    store_id: {label: M, description: L}\n"
    )
    assert _issues_of({"fr.yaml": complete}, config) == [
        "translations/fr.yaml: translations.require in lakana.yaml needs a `fr` text for every "
        "header text, and 1 is missing: gold.fact_sales.measures.net_amount.label. Add them to "
        "translations/fr.yaml, or remove `fr` from translations.require."
    ]
    whole = complete + "  measures:\n    net_amount: {label: CA}\n"
    assert _issues_of({"fr.yaml": whole}, config) == []


def test_strict_mode_skips_the_main_language() -> None:
    config = ProjectConfig(
        repo="x", main_language="en", translations=TranslationsConfig(require=("en",))
    )
    assert translation_issues(config, [_sales()], []) == []


def test_strict_mode_names_at_most_twenty_keys() -> None:
    columns = {f"c{i:02}": {"label": f"L{i}"} for i in range(25)}
    wide = make_asset("gold.wide", metadata=AssetMetadata(materialize="table", columns=columns))
    found = [DiscoveredTranslation("translations/fr.yaml", "fr", "{}\n")]
    parsed, _ = parse_translations(found, limits=_LIMITS)
    config = parse_project_config("main_language: en\ntranslations:\n  require: [fr]\n")
    (issue,) = translation_issues(config, [wide], parsed)
    assert "25 are missing: gold.wide.columns.c00.label, " in issue.message
    assert "gold.wide.columns.c19.label … and 5 more. Add them" in issue.message


def test_header_texts_lists_every_text_of_an_asset() -> None:
    assert header_texts(_sales()) == [
        "gold.fact_sales.label",
        "gold.fact_sales.description",
        "gold.fact_sales.grain",
        "gold.fact_sales.columns.store_id.label",
        "gold.fact_sales.columns.store_id.description",
        "gold.fact_sales.measures.net_amount.label",
    ]


def test_translated_texts_lists_every_text_of_an_entry() -> None:
    entry = TableTranslation.model_validate(
        {
            "label": "V",
            "grain": "G",
            "columns": {"c": {"description": "D"}},
            "measures": {"m": {"label": "L", "description": "E"}},
        }
    )
    assert translated_texts("gold.t", entry) == [
        "gold.t.label",
        "gold.t.grain",
        "gold.t.columns.c.description",
        "gold.t.measures.m.label",
        "gold.t.measures.m.description",
    ]


def _counting(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count YAML parses and close-name searches, and make the per-issue re-parse fail.

    Parameters
    ----------
    monkeypatch
        The fixture.

    Returns
    -------
    dict of str to int
        ``parses`` and ``suggestions``, updated as they happen.
    """
    counts = {"parses": 0, "suggestions": 0}
    parse, close = yaml_loader._NoAliasLoader.get_single_node, difflib.get_close_matches

    def counted_parse(self: yaml_loader._NoAliasLoader) -> object:
        """Count one parse.

        Parameters
        ----------
        self
            The loader.

        Returns
        -------
        object
            The node.
        """
        counts["parses"] += 1
        return parse(self)

    def counted_close(word: str, possibilities: list[str], n: int, cutoff: float) -> list[str]:
        """Count one search.

        Parameters
        ----------
        word
            As for difflib.
        possibilities
            As for difflib.
        n
            As for difflib.
        cutoff
            As for difflib.

        Returns
        -------
        list of str
            The matches.
        """
        counts["suggestions"] += 1
        return close(word, possibilities, n=n, cutoff=cutoff)

    def no_reparse(*args: object, **kwargs: object) -> None:
        """Fail: a line must come from the one parse.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError("re-parsed for a key line")

    monkeypatch.setattr(yaml_loader._NoAliasLoader, "get_single_node", counted_parse)
    monkeypatch.setattr(translations_module.difflib, "get_close_matches", counted_close)
    monkeypatch.setattr(yaml_loader, "key_line", no_reparse)
    monkeypatch.setattr(project_config_module, "key_line", no_reparse)
    monkeypatch.setattr(translations_module, "key_line", no_reparse, raising=False)
    return counts


def test_issues_are_capped_per_file_and_each_file_is_parsed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = _counting(monkeypatch)
    unknown = "".join(f"silver.t{i:03}: {{label: X}}\n" for i in range(60))
    extra = "gold.fact_sales:\n" + "".join(f"  k{i:03}: x\n" for i in range(55))
    issues = _issues_of({"fr.yaml": unknown, "de.yaml": extra})
    assert counts == {"parses": 3, "suggestions": 50}  # each file once, and lakana.yaml
    fr = [i for i in issues if i.startswith("translations/fr.yaml")]
    de = [i for i in issues if i.startswith("translations/de.yaml")]
    assert len(fr) == len(de) == 51
    assert fr[0] == (
        "translations/fr.yaml:1: `silver.t000` is not a table this project builds; fix the "
        "name, or remove the entry"
    )
    assert fr[49].startswith("translations/fr.yaml:50: `silver.t049`")
    assert fr[50] == (
        "translations/fr.yaml: … and 10 more problems in translations/fr.yaml; fix the ones "
        "above and deploy again to see the rest"
    )
    assert de[0] == "translations/de.yaml:2: gold.fact_sales.k000: Extra inputs are not permitted"
    assert de[50] == (
        "translations/de.yaml: … and 5 more problems in translations/de.yaml; fix the ones "
        "above and deploy again to see the rest"
    )


def test_a_worst_case_file_is_checked_in_bounded_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversarial: thousands of unknown tables and nested extra keys cost one parse.

    Before, every issue parsed the file again, and a 1 MiB file took hours. The file is kept
    small enough for the unit-test budget; the structural bound (`_counting`) holds at any size.
    """
    counts = _counting(monkeypatch)
    unknown = "".join(f"gold.fact_sales{i:05}: {{label: X}}\n" for i in range(1500))
    bad = "".join(f"silver.t{i:05}: {{bad: {{deep: {{er: 1}}}}}}\n" for i in range(800))
    extra = "gold.fact_sales:\n" + "".join(f"  k{i:05}: {{x: 1}}\n" for i in range(800))
    text = unknown + bad + extra
    assert len(text.encode()) < 100 * 1024
    found = [DiscoveredTranslation("translations/fr.yaml", "fr", text)]
    config = parse_project_config("main_language: en\n")
    counts["parses"] = 0
    started = time.perf_counter()
    parsed, problems = parse_translations(found, limits=_LIMITS)
    issues = translation_issues(config, [_sales()], parsed)
    elapsed = time.perf_counter() - started
    assert counts["parses"] == 1
    assert counts["suggestions"] <= 50
    assert len(problems) == len(issues) == 51
    assert elapsed < 1.0, f"took {elapsed:.2f} s"
