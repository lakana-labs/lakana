"""Tests for comparing a page's output blocks with what the examples printed."""

from pathlib import Path

import pytest

from scripts.docs_blocks import (
    MASKS,
    BlockError,
    check,
    find_blocks,
    fix,
    is_progress,
    main,
    mask,
)

RUN_SH = "#!/usr/bin/env bash\n# docs:start show\necho hi\n# docs:end show\n"
BLOCK = "<!-- output: demo#show -->\n```text\n{}```\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Run 71ddefdc-6a57-4c5b-bac3-cbacc2fb4759 ok", "Run <uuid> ok"),
        ("at 2026-10-21T08:15:30Z", "at <timestamp>"),
        ("at 2026-10-21 08:15:30.123456+02:00", "at <timestamp>"),
        ("(last applied 2026-11-06T02:49Z)", "(last applied <timestamp>)"),
        ("commit ca36c93fe2337b324df3ab8d03cac9b9d1ed1420 done", "commit <hash> done"),
        ("sha256:" + "ab" * 32, "sha256:<hash>"),
        ("lakana 0.8.1.dev6+g6af3c5d2a", "lakana <version>"),
        ("lakana==1.2.3rc1 and 2.0.0", "lakana==<version> and <version>"),
        ("http://127.0.0.1:8765/x", "http://127.0.0.1:<port>/x"),
        ("on localhost:51234.", "on localhost:<port>."),
        ("in /home/you/analytics now", "in <path> now"),
        ("in '/tmp/tmp.Ab12/home/work/x'", "in '<path>'"),
        ("in /var/folders/zz/T/x", "in <path>"),
        ("│ file:///tmp/tmp.Ab12/home/data/lake/gold/fac… │", "│ file://<path> │"),
        (
            "took 0.0s, then 1.5 s, 12ms, 3 minutes",
            "took <duration>, then <duration>, <duration>, <duration>",
        ),
        ("1.5 MiB and 20KiB", "<size> and <size>"),
        ("Resolved 44 packages in 327ms", "Resolved <count> packages in <duration>"),
        ("lakana_pat_Ab12Cd34_s3cr3t-value_9", "lakana_pat_<key id>_<secret>"),
        ("lakana_ci_Ab12Cd34_s3cr3t-value", "lakana_ci_<key id>_<secret>"),
        ("(snapshot 5033228317085625887, 05 Nov 00:33 UTC)", "(snapshot <snapshot>, <alert time>)"),
        ("│ 503322831708562588 │", "│ <snapshot> │"),
        ("[master 9445d5e] Break the data", "[master <short hash>] Break the data"),
        ("[main (root-commit) 1a2b3c4] First", "[main (root-commit) <short hash>] First"),
        ("Run 941ef675 failed, token 634fb88f45cabc42", "Run <short id> failed, token <short id>"),
        ("manifest 420bf73218ec", "manifest <short id>"),
        ("Issued token 634fb88f45cabc42 (x)", "Issued token <short id> (x)"),
        ("Revoked token 634fb88f45cabc42", "Revoked token <short id>"),
        ("│ 941ef675 │ silver.x │", "│ <short id> │ silver.x │"),
        ("│ 420bf73218ec    │", "│ <short id>    │"),
    ],
)
def test_each_mask_hides_the_part_that_differs_per_run(text: str, expected: str) -> None:
    """One rule per row of the masking table."""
    assert mask(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "127.0.0.1 is local",
        "3 rows",
        "station A1 averaged 9.0",
        "/etc/hosts",
        "key_id name",
        "v1.2",
        "1234567 rows on 05 Nov",
        "44 packages",
        "[master] no hash",
        "rows 12345678 in 2 files",
        "revision c9f3d6a8b2e5 applied",
        "a 10-digit word 0123456789 and abcdef0123",
        "a 14-hex word 0123456789abcd here",
        "the file /opt/ab12cd34/x and ab12cd34/y",
        "│ 1234567 │ 12345678901 │",
        "Run id 941ef675abc",
        "a 9-digit 123456789 and 1234567",
        "silver.station_temperatures/no_missing_station",
    ],
)
def test_text_that_is_the_same_on_every_run_is_left_alone(text: str) -> None:
    """A mask that ate stable text would hide a real change."""
    assert mask(text) == text


def _tree(
    tmp_path: Path, block: str, shown: str, captured: str | None, columns: int | None = None
) -> tuple[Path, Path]:
    """Build a root with one page, one example and, optionally, one captured region.

    Parameters
    ----------
    tmp_path
        The test's temporary directory.
    block
        The page's block, with ``{}`` for the shown body.
    shown
        What the page shows.
    captured
        What the example printed, or ``None`` for no capture of the region.
    columns
        The ``COLUMNS`` the example exports, or ``None`` for none.

    Returns
    -------
    tuple of (Path, Path)
        The root and the capture directory.
    """
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "p.md").write_text(block.format(shown), encoding="utf-8")
    (tmp_path / "docs" / "examples" / "demo").mkdir(parents=True)
    (tmp_path / "docs" / "examples" / "demo" / "run.sh").write_text(
        RUN_SH if columns is None else RUN_SH.replace("echo", f"export COLUMNS={columns}\necho"),
        encoding="utf-8",
    )
    captures = tmp_path / "captures"
    (captures / "demo").mkdir(parents=True)
    if captured is not None:
        (captures / "demo" / "show.txt").write_text(captured, encoding="utf-8")
    return tmp_path, captures


def test_an_output_marker_may_name_the_region_it_shows(tmp_path: Path) -> None:
    """`<!-- output: example#region -->` is read into the marker."""
    root, _ = _tree(tmp_path, BLOCK, "hi\n", None)
    (block,) = find_blocks(root / "docs/site/p.md")
    assert block.marker is not None
    assert (block.marker.kind, block.marker.value) == ("output", "demo#show")


@pytest.mark.parametrize("value", ["demo", "demo#", "#show", "a b#c", "a/b#c", "demo#a#b"])
def test_a_malformed_region_name_is_refused_with_the_form(tmp_path: Path, value: str) -> None:
    """The message shows the form and says later blocks were not checked."""
    page = tmp_path / "p.md"
    page.write_text(f"<!-- output: {value} -->\n```text\nx\n```\n", encoding="utf-8")
    with pytest.raises(BlockError, match=r"output: <example>#<region>"):
        find_blocks(page)


def test_a_block_that_equals_the_capture_after_masking_passes(tmp_path: Path) -> None:
    """Identifiers, versions and durations differ per run; the rest must not."""
    root, captures = _tree(
        tmp_path,
        BLOCK,
        "Run 71ddefdc-6a57-4c5b-bac3-cbacc2fb4759 took 0.0s\nlakana 0.8.1\n",
        "Run 11111111-6a57-4c5b-bac3-cbacc2fb4759 took 1.2s\nlakana 0.9.0.dev3+gabc\n",
    )
    counts: dict[str, int] = {}
    assert check(root, root / "docs/site", counts, captures) == ([], 0)
    assert counts == {"output": 1, "unchecked": 0, "compared": 1, "skipped": 0}


def test_trailing_space_and_a_missing_final_newline_do_not_matter(tmp_path: Path) -> None:
    """`curl -s` prints no final newline, and a terminal shows no trailing spaces."""
    root, captures = _tree(tmp_path, BLOCK, '{"status":"ok"}\n', '{"status":"ok"}  ')
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_a_changed_line_is_a_problem_with_a_diff_that_names_page_and_block(
    tmp_path: Path,
) -> None:
    """The diff is of the masked texts, so only a real change shows."""
    root, captures = _tree(tmp_path, BLOCK, "a\nold 1.0s\nc\n", "a\nnew 2.0s\nc\n")
    (problem,), _ = check(root, root / "docs/site", None, captures)
    assert problem.startswith("docs/site/p.md:2: ")
    assert "demo#show" in problem
    assert "-old <duration>" in problem
    assert "+new <duration>" in problem
    assert "--fix" in problem
    assert "--captured" in problem


def test_a_region_without_a_capture_is_a_problem_that_says_what_to_check(tmp_path: Path) -> None:
    """The example did not run the region, or the name is wrong."""
    root, captures = _tree(tmp_path, BLOCK, "hi\n", None)
    (problem,), _ = check(root, root / "docs/site", None, captures)
    assert "docs/site/p.md:2" in problem
    assert "demo#show" in problem
    assert str(captures) in problem


def test_without_a_capture_directory_the_comparison_is_skipped(tmp_path: Path) -> None:
    """A tied block counts as skipped, not as a problem."""
    root, _ = _tree(tmp_path, BLOCK, "hi\n", "other\n")
    counts: dict[str, int] = {}
    assert check(root, root / "docs/site", counts) == ([], 0)
    assert counts["skipped"] == 1
    assert counts["compared"] == 0


def test_a_region_the_example_does_not_have_is_a_problem_even_without_a_capture(
    tmp_path: Path,
) -> None:
    """The name is checked against run.sh whether or not a run exists."""
    root, _ = _tree(tmp_path, BLOCK.replace("show", "gone"), "hi\n", None)
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/p.md:2" in problem
    assert "no region `gone`" in problem


def test_an_untied_output_block_is_counted_as_unchecked(tmp_path: Path) -> None:
    """The ratchet counts these."""
    root, captures = _tree(tmp_path, "<!-- output -->\n```text\n{}```\n", "x\n", None)
    counts: dict[str, int] = {}
    assert check(root, root / "docs/site", counts, captures) == ([], 0)
    assert counts == {"output": 1, "unchecked": 1, "compared": 0, "skipped": 0}


def test_fix_replaces_only_the_lines_that_changed(tmp_path: Path) -> None:
    """A line equal after masking keeps the page's own text; the rest comes from the run."""
    root, captures = _tree(
        tmp_path,
        BLOCK + "\ntext\n\n<!-- output -->\n```text\nuntouched\n```\n",
        "Run 71ddefdc-6a57-4c5b-bac3-cbacc2fb4759 took 0.0s\nold\nlast\n",
        "Run 22222222-6a57-4c5b-bac3-cbacc2fb4759 took 9.0s\nnew\nextra\nlast\n",
    )
    assert fix(root, root / "docs/site", None, captures) == [root / "docs/site/p.md"]
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert (
        "```text\nRun 71ddefdc-6a57-4c5b-bac3-cbacc2fb4759 took 0.0s\nnew\nextra\nlast\n```" in text
    )
    assert "untouched" in text
    assert check(root, root / "docs/site", None, captures) == ([], 0)
    assert fix(root, root / "docs/site", None, captures) == []


def test_fix_never_writes_the_secret_of_a_token(tmp_path: Path) -> None:
    """A token in a run reaches the page as `<secret>`, whether or not the page had the line."""
    secret = "x" * 24
    printed = f"Token: lakana_pat_a445f5d891704fbb_{secret}\nlakana_ci_Ab12Cd34_{secret}\n"
    root, captures = _tree(tmp_path, BLOCK, "old\nold2\n", printed)
    fix(root, root / "docs/site", None, captures)
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert secret not in text
    assert "lakana_pat_a445f5d891704fbb_<secret>" in text
    assert "lakana_ci_Ab12Cd34_<secret>" in text
    assert check(root, root / "docs/site", None, captures) == ([], 0)
    assert fix(root, root / "docs/site", None, captures) == []


RUN_PAGE = (
    "a873a794-e7ea-4caa-9627-5b7ec1ec1715\n"
    "bronze.readings  pending\n"
    "silver.clean  pending\n"
    "bronze.readings  succeeded  attempt 1/3  1.0s\n"
    "silver.clean  succeeded  attempt 1/3  0.0s\n"
    "Run a873a794-e7ea-4caa-9627-5b7ec1ec1715 succeeded\n"
    "silver.clean   passed   2 of 2 checks passed\n"
)


def test_progress_lines_are_not_compared(tmp_path: Path) -> None:
    """A run prints a line when a poll sees a change: which lines, and their order, vary."""
    printed = (
        "11111111-e7ea-4caa-9627-5b7ec1ec1715\n"
        "silver.clean  pending\n"
        "bronze.readings  pending\n"
        "bronze.readings  running  attempt 1/3\n"
        "bronze.readings  ready  Ready, and the only worker is busy.\n"
        "silver.clean  succeeded  attempt 1/3  0.0s\n"
        "bronze.readings  succeeded  attempt 1/3  1.0s\n"
        "silver.clean  setting the table back  attempt 1/3\n"
        "Run 11111111-e7ea-4caa-9627-5b7ec1ec1715 succeeded\n"
        "silver.clean   passed   2 of 2 checks passed\n"
    )
    root, captures = _tree(tmp_path, BLOCK, RUN_PAGE, printed)
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_a_difference_outside_the_progress_lines_is_still_found(tmp_path: Path) -> None:
    """Only the progress lines are left out; the summary after them counts."""
    printed = RUN_PAGE.replace("2 of 2", "1 of 2")
    root, captures = _tree(tmp_path, BLOCK, RUN_PAGE, printed)
    (problem,), _ = check(root, root / "docs/site", None, captures)
    assert "1 of 2" in problem


@pytest.mark.parametrize(
    "line",
    [
        "silver.clean   passed   2 of 2 checks passed",
        "silver.clean failed: check_blocked",
        "silver.clean  warned note",
        "│ silver.clean  │ succeeded │",
        "Run 1 succeeded",
    ],
)
def test_only_a_task_line_is_progress(line: str) -> None:
    """The status must follow the asset after exactly two spaces."""
    assert not is_progress(line)


@pytest.mark.parametrize(
    "line",
    [
        "silver.clean  pending",
        "silver.clean  ready  Ready, and the only worker is busy.",
        "silver.clean  running  attempt 1/3",
        "silver.clean  succeeded  attempt 1/3  1.0s",
        "silver.clean  failed  attempt 1/3  0.0s  failed: check_blocked. Next: x",
        "silver.clean  upstream_failed",
        "silver.clean  cancelled",
        "silver.clean  setting the table back  attempt 1/3",
    ],
)
def test_a_line_of_a_task_that_changed_state_is_progress(line: str) -> None:
    """The lines `_Progress` of the CLI prints."""
    assert is_progress(line)


def test_fix_keeps_the_progress_lines_a_page_shows(tmp_path: Path) -> None:
    """The page shows what a reader sees; fix changes the rest and leaves its progress lines."""
    printed = "silver.clean  running  attempt 1/3\nRun 22222222 failed\n"
    page = "silver.clean  pending\nRun 22222222 succeeded\n"
    root, captures = _tree(tmp_path, BLOCK, page, printed)
    fix(root, root / "docs/site", None, captures)
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert "silver.clean  pending\nRun 22222222 failed\n" in text
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_fix_drops_the_lines_a_run_no_longer_prints_and_keeps_the_progress(tmp_path: Path) -> None:
    """A page with more lines than the run: the extra ones go, the progress lines stay."""
    page = "silver.clean  pending\nRun 22222222 failed\nold line\nanother old line\n"
    printed = "Run 22222222 failed\n"
    root, captures = _tree(tmp_path, BLOCK, page, printed)
    fix(root, root / "docs/site", None, captures)
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert "```text\nsilver.clean  pending\nRun 22222222 failed\n```" in text
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_fix_keeps_an_indented_block_indented(tmp_path: Path) -> None:
    """A block inside a list item keeps its fence's indent."""
    page = "- item\n\n  <!-- output: demo#show -->\n  ```text\n  old\n  ```\n"
    root, captures = _tree(tmp_path, "{}", page, "new\n")
    fix(root, root / "docs/site", None, captures)
    assert "  ```text\n  new\n  ```" in (root / "docs/site/p.md").read_text(encoding="utf-8")


def test_fix_without_a_capture_changes_nothing(tmp_path: Path) -> None:
    """No capture directory, or no capture of the region: the block stays."""
    root, captures = _tree(tmp_path, BLOCK, "old\n", None)
    assert fix(root, root / "docs/site") == []
    assert fix(root, root / "docs/site", None, captures) == []


def test_main_compares_with_the_directory_of_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--captured DIR` turns the comparison on and the summary says how many."""
    root, captures = _tree(tmp_path, BLOCK, "old\n", "new\n")
    assert main(["--root", str(root), "--captured", str(captures)]) == 1
    out = capsys.readouterr().out
    assert "-old" in out
    assert "1 output block compared with a run, 0 skipped, 0 not tied to a region" in out


def test_main_fix_refreshes_from_the_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--fix --captured DIR` rewrites the page and then passes."""
    root, captures = _tree(tmp_path, BLOCK, "old\n", "new\n")
    assert main(["--root", str(root), "--captured", str(captures), "--fix"]) == 0
    assert "updated docs/site/p.md" in capsys.readouterr().out


def test_main_reads_the_directory_from_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_DOCS_CAPTURE` is the flag's default, as in the runner."""
    root, captures = _tree(tmp_path, BLOCK, "same\n", "same\n")
    monkeypatch.setenv("LAKANA_DOCS_CAPTURE", str(captures))
    assert main(["--root", str(root)]) == 0
    assert "1 output block compared with a run" in capsys.readouterr().out


def test_main_says_why_nothing_was_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no capture the skip is announced with the way to turn it on."""
    monkeypatch.delenv("LAKANA_DOCS_CAPTURE", raising=False)
    root, _ = _tree(tmp_path, BLOCK, "x\n", None)
    assert main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "no captured run" in out
    assert "LAKANA_DOCS_CAPTURE" in out
    assert "scripts/run_docs_examples.sh" in out


def test_main_refuses_a_capture_directory_that_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo must not read as "nothing to compare"."""
    root, _ = _tree(tmp_path, BLOCK, "x\n", None)
    assert main(["--root", str(root), "--captured", str(tmp_path / "nope")]) == 1
    assert "nope" in capsys.readouterr().out


def test_every_mask_is_listed_in_the_testing_guide() -> None:
    """The table in docs/agent/testing.md names each rule of `MASKS`."""
    guide = (Path(__file__).resolve().parents[2] / "docs/agent/testing.md").read_text("utf-8")
    missing = [name for name, _, _ in MASKS if f"| `{name}` |" not in guide]
    assert missing == [], f"add these rules to the masks table in docs/agent/testing.md: {missing}"


TABLE = (
    "┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┓\n"
    "┃ when                ┃ id     ┃\n"
    "┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━┩\n"
    "│ 2026-11-15T10:00:00 │ {id} │\n"
    "└─────────────────────┴────────┘\n"
)
LONG_ID = "1234567890123456789"
SHORT_ID = "123456789012345678"


def _table(snapshot: str, width: int = 19) -> str:
    """Build a Rich-style table whose id column is padded to `width`.

    Parameters
    ----------
    snapshot
        The id cell.
    width
        The column's width.

    Returns
    -------
    str
        The table.
    """
    text = TABLE.replace("┳━━━━━━━━┓", "┳" + "━" * (width + 2) + "┓")
    text = text.replace("╇━━━━━━━━┩", "╇" + "━" * (width + 2) + "┩")
    text = text.replace("┴────────┘", "┴" + "─" * (width + 2) + "┘")
    text = text.replace("┃ id     ┃", "┃ id" + " " * (width - 2) + " ┃")
    return text.replace("{id} │", snapshot.ljust(width) + " │")


def test_table_padding_left_by_a_shorter_masked_value_is_ignored(tmp_path: Path) -> None:
    """A snapshot id one digit shorter changes the padding, not the comparison."""
    root, captures = _tree(tmp_path, BLOCK, _table(LONG_ID), _table(SHORT_ID, 18))
    assert check(root, root / "docs/site", None, captures) == ([], 0)
    root2, captures2 = _tree(tmp_path / "b", BLOCK, _table(LONG_ID), _table(SHORT_ID + "6"))
    assert check(root2, root2 / "docs/site", None, captures2) == ([], 0)


def test_a_changed_cell_in_a_table_still_fails(tmp_path: Path) -> None:
    """Collapsing padding does not hide a changed value."""
    shown = _table(LONG_ID)
    root, captures = _tree(tmp_path, BLOCK, shown, shown.replace("10:00:00", "10:00:00 x"))
    problems, _ = check(root, root / "docs/site", None, captures)
    assert problems


def test_a_changed_cell_text_in_a_table_still_fails(tmp_path: Path) -> None:
    """A different word in a cell is reported."""
    shown = _table("ok")
    root, captures = _tree(tmp_path, BLOCK, shown, _table("failed", 19))
    problems, _ = check(root, root / "docs/site", None, captures)
    assert problems


def test_a_changed_header_in_a_table_still_fails(tmp_path: Path) -> None:
    """A renamed or missing column is reported."""
    shown = _table(LONG_ID)
    root, captures = _tree(tmp_path, BLOCK, shown, shown.replace("┃ id ", "┃ ref"))
    problems, _ = check(root, root / "docs/site", None, captures)
    assert problems


def test_fix_takes_the_whole_changed_table_from_the_run(tmp_path: Path) -> None:
    """A changed header brings the run's borders and rows too, so the widths stay equal."""
    shown = _table(LONG_ID)
    printed = _table(SHORT_ID, 18).replace("┃ id ", "┃ ref")
    root, captures = _tree(tmp_path, BLOCK, shown, printed)
    assert fix(root, root / "docs/site", None, captures) == [root / "docs/site/p.md"]
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert "```text\n" + printed + "```" in text


def test_fix_keeps_an_unchanged_table_as_the_page_shows_it(tmp_path: Path) -> None:
    """A table that matches after masking stays as written while another line is fixed."""
    shown = "old\n" + _table(LONG_ID)
    root, captures = _tree(tmp_path, BLOCK, shown, "new\n" + _table(SHORT_ID, 18))
    fix(root, root / "docs/site", None, captures)
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert "```text\nnew\n" + _table(LONG_ID) + "```" in text


WRAPPED_PAGE = (
    "Fired: t: schema changed (snapshot 5033228317085625887, 05 Nov 00:33 UTC);\ncheck it\n"
)
WRAPPED_RUN = (
    "Fired: t: schema changed (snapshot 503322831708562588, 05 Nov 00:33 UTC); check\nit\n"
)


def test_a_sentence_wrapped_at_another_word_is_not_a_difference(tmp_path: Path) -> None:
    """The wrap point moves with the length of masked values; the words are what count."""
    root, captures = _tree(tmp_path, BLOCK, WRAPPED_PAGE, WRAPPED_RUN)
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_a_changed_word_in_wrapped_text_still_fails(tmp_path: Path) -> None:
    """Unwrapping does not hide a different word."""
    root, captures = _tree(tmp_path, BLOCK, WRAPPED_PAGE, WRAPPED_RUN.replace("check", "look"))
    problems, _ = check(root, root / "docs/site", None, captures)
    assert problems


def test_text_does_not_join_a_table_line(tmp_path: Path) -> None:
    """A line moved across a table is a difference."""
    shown = "intro\n" + _table("ok")
    root, captures = _tree(tmp_path, BLOCK, shown, _table("ok") + "intro\n")
    problems, _ = check(root, root / "docs/site", None, captures)
    assert problems


def test_fix_keeps_a_page_that_differs_only_in_wrapping(tmp_path: Path) -> None:
    """A block that matches apart from its line breaks is left as written."""
    root, captures = _tree(tmp_path, BLOCK, WRAPPED_PAGE, WRAPPED_RUN)
    fix(root, root / "docs/site", None, captures)
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert "```text\n" + WRAPPED_PAGE + "```" in text


LONG = "word " * 19 + "end"
WIDTH = 100


@pytest.mark.parametrize(
    ("shown", "printed"),
    [
        (LONG + "\n\nnext\n", LONG + "\nnext\n"),
        (LONG + "\n  next\n", LONG + "\nnext\n"),
        (LONG + "\n- one\n- two\n", LONG + "\n- one - two\n"),
        (LONG + "\nnext  more\n", LONG + "\nnext more\n"),
        ("| a | b |\n| c | d |\n", "| a | b | | c | d |\n"),
        ("+---+---+\n| a | b |\n", "+---+---+ | a | b |\n"),
    ],
    ids=[
        "blank line removed",
        "indent changed",
        "list items joined",
        "spaces inside a line",
        "markdown table rows joined",
        "ascii table rows joined",
    ],
)
def test_only_a_wrap_point_is_ignored(tmp_path: Path, shown: str, printed: str) -> None:
    """Blank lines, indents, spacing, items and table rows all keep counting."""
    root, captures = _tree(tmp_path, BLOCK, shown, printed, WIDTH)
    problems, _ = check(root, root / "docs/site", None, captures)
    assert problems


def test_wraps_on_both_sides_of_a_table_pass(tmp_path: Path) -> None:
    """Text before and after a table may wrap elsewhere; the table is compared as before."""
    shown = LONG + "\nrest\n" + _table("ok") + LONG + "\nrest\n"
    printed = LONG + " rest\n" + _table("ok") + LONG + " rest\n"
    root, captures = _tree(tmp_path, BLOCK, shown, printed, WIDTH)
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_one_stray_box_character_in_a_sentence_is_still_text(tmp_path: Path) -> None:
    """A line with a single box character is unwrapped like any sentence."""
    first = "a │ " + LONG
    root, captures = _tree(tmp_path, BLOCK, first + "\nrest\n", first + " rest\n", WIDTH + 4)
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_fix_keeps_the_pages_wrap_where_only_the_wrap_differs(tmp_path: Path) -> None:
    """A wrap difference and a real change: only the changed stretch comes from the run."""
    shown = LONG + "\nrest\n\nold\n"
    printed = LONG + " rest\n\nnew\n"
    root, captures = _tree(tmp_path, BLOCK, shown, printed, WIDTH)
    fix(root, root / "docs/site", None, captures)
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert "```text\n" + LONG + "\nrest\n\nnew\n```" in text
    assert fix(root, root / "docs/site", None, captures) == []


def test_a_wrap_inside_a_masked_alert_time_passes(tmp_path: Path) -> None:
    """A mask with a space in it matches across the line break."""
    shown = "Fired: schema changed (snapshot 5033228317085625887, 05 Nov 00:33 UTC);\ncheck it\n"
    printed = "Fired: schema changed (snapshot 5033228317085625887, 05 Nov\n00:33 UTC); check it\n"
    root, captures = _tree(tmp_path, BLOCK, shown, printed, 62)
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_a_continuation_that_starts_with_a_word_and_colon_passes(tmp_path: Path) -> None:
    """A wrap before `changed:` is a wrap, not a new `key: value` line."""
    text = "Fired: silver.orders: schema" + " x" * 30
    root, captures = _tree(
        tmp_path, BLOCK, text + "\nchanged: added\n", text + " changed:\nadded\n", len(text) + 4
    )
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_a_wrap_before_a_long_path_passes(tmp_path: Path) -> None:
    """The line may end far short of the width when the next word is long."""
    path = "/srv/lakana/data/warehouse/silver/orders/part-0001.parquet"
    shown = "Wrote the file to\n" + path + "\n"
    root, captures = _tree(tmp_path, BLOCK, shown, "Wrote the file to " + path + "\n", 60)
    assert check(root, root / "docs/site", None, captures) == ([], 0)


def test_a_line_that_fits_is_not_joined_to_the_next(tmp_path: Path) -> None:
    """With the example's width known, a long line that was not wrapped stays a line."""
    shown = LONG + "\nnext\n"
    root, captures = _tree(tmp_path, BLOCK, shown, LONG + " next\n", 120)
    problems, _ = check(root, root / "docs/site", None, captures)
    assert problems
