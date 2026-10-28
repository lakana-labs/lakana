"""Tests for `scripts.docs_example_shards`: which documentation examples each CI shard runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import docs_example_shards as shards
from scripts.docs_example_shards import (
    DURATIONS,
    UNKNOWN_EXAMPLE_SECONDS,
    ShardError,
    assign,
    examples,
    main,
    measured,
    parse_shard,
    read_durations,
    select,
)


def _tree(tmp_path: Path, names: list[str]) -> Path:
    """Make a ``docs/examples`` with one ``run.sh`` per name, and a directory without one.

    Parameters
    ----------
    tmp_path
        Where to make it.
    names
        The examples.

    Returns
    -------
    Path
        The examples directory.
    """
    directory = tmp_path / "examples"
    for name in names:
        (directory / name).mkdir(parents=True)
        (directory / name / "run.sh").write_text("#!/usr/bin/env bash\n")
    (directory / "not-an-example").mkdir(parents=True)
    return directory


@pytest.mark.parametrize("text", ["1/3", "3/3", " 2/2 "])
def test_a_shard_is_read_as_index_and_count(text: str) -> None:
    index, count = parse_shard(text)
    assert 1 <= index <= count


@pytest.mark.parametrize("text", ["0/3", "4/3", "3", "a/b", "1/2/3", ""])
def test_anything_else_is_refused_with_the_variable_and_an_example(text: str) -> None:
    with pytest.raises(ShardError, match=r"LAKANA_DOCS_SHARD=.*I/N.*1/3"):
        parse_shard(text)


def test_the_longest_examples_go_to_different_shards_and_the_rest_fill_up() -> None:
    seconds = {"a": 120, "b": 110, "c": 40, "d": 35, "e": 30, "f": 10}

    spread = assign(seconds, seconds, 3)

    totals = [sum(seconds[n] for n, s in spread.items() if s == i) for i in (1, 2, 3)]
    assert spread["a"] != spread["b"]
    assert max(totals) - min(totals) <= 40
    assert sorted(spread) == sorted(seconds)


def test_an_example_without_a_recorded_time_counts_the_default() -> None:
    spread = assign(["new", "old"], {"old": UNKNOWN_EXAMPLE_SECONDS + 1}, 2)

    assert spread == {"old": 1, "new": 2}


def test_every_example_is_in_exactly_one_shard(tmp_path: Path) -> None:
    names = [f"example-{n}" for n in range(7)]
    directory = _tree(tmp_path, names)
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps({"example-0": 100, "example-3": 50}))

    picked = [select(f"{i}/3", directory, durations) for i in (1, 2, 3)]

    assert sorted(n for shard in picked for n in shard) == names
    assert all(picked)


def test_more_shards_than_examples_leave_a_shard_empty(tmp_path: Path) -> None:
    directory = _tree(tmp_path, ["only"])

    assert select("2/2", directory, tmp_path / "missing.json") == []


def test_only_directories_with_a_run_script_are_examples(tmp_path: Path) -> None:
    assert examples(_tree(tmp_path, ["b", "a"])) == ["a", "b"]


def test_the_recorded_durations_name_only_examples_that_exist() -> None:
    """A renamed or removed example leaves no stale entry behind, and each entry is a number."""
    recorded = read_durations(DURATIONS)

    assert recorded, f"{DURATIONS} is empty"
    assert set(recorded) <= set(examples()), set(recorded) - set(examples())


def test_every_current_example_has_a_recorded_duration() -> None:
    """A new example is balanced by a guess until its time is recorded; record it."""
    missing = set(examples()) - set(read_durations(DURATIONS))
    assert not missing, (
        f"no recorded seconds for {sorted(missing)} in {DURATIONS.name}: add the time the "
        "runner printed for it (or refresh the file with `python -m scripts.docs_example_shards "
        "durations`)"
    )


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"a": -1}', '{"a": true}'])
def test_a_broken_durations_file_is_refused_with_how_to_repair_it(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "durations.json"
    path.write_text(content)

    with pytest.raises(ShardError, match="Restore it from git"):
        read_durations(path)


def test_the_rows_of_job_logs_give_the_mean_seconds_per_example() -> None:
    first = "2026-10-27T10:00:00Z | b-example | 10 s |\nnoise\n| a | 3 s |\n"
    second = "| b-example | 13 s |\n"

    assert measured([first, second]) == {"a": 3.0, "b-example": 11.5}


def test_a_log_without_rows_is_refused_with_what_to_pass() -> None:
    with pytest.raises(ShardError, match="gh run view"):
        measured(["nothing here"])


def test_the_command_line_selects_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _tree(tmp_path, ["a", "b"])
    durations = tmp_path / "durations.json"
    monkeypatch.setattr(shards, "EXAMPLES", directory)
    monkeypatch.setattr(shards, "DURATIONS", durations)
    log = tmp_path / "job.log"
    log.write_text("| a | 50 s |\n| b | 20 s |\n")

    assert main(["durations", "--output", str(durations), str(log)]) == 0
    assert json.loads(durations.read_text()) == {"a": 50.0, "b": 20.0}
    assert main(["select", "2/2"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "b"


def test_the_command_line_says_what_is_wrong_and_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["select", "9/3"]) == 2
    assert "LAKANA_DOCS_SHARD='9/3' is not a shard" in capsys.readouterr().err
