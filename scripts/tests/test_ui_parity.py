"""Tests for the inventory of every command and route with its place in the interface."""

from pathlib import Path

import pytest
import yaml

from scripts.check_ui_parity import PARITY_FILE, live_keys, load_entries, problems

_LIVE = {"table undrop", "GET /api/v1/jobs"}


@pytest.fixture(scope="module")
def live() -> set[str]:
    """Walk the real command tree and the committed routes once for the module.

    Returns
    -------
    set of str
        Every CLI command path and ``/api/v1`` operation.
    """
    return live_keys()


def test_the_real_inventory_matches_the_live_commands_and_routes(live: set[str]) -> None:
    """Every live command and route has an entry, and every entry a live key.

    Parameters
    ----------
    live
        The live keys.
    """
    assert problems(load_entries(PARITY_FILE), live) == []


def test_a_missing_entry_names_the_key_and_the_next_step() -> None:
    """A live key without an entry is reported with what to add."""
    entries = {"table undrop": {"class": "ui", "part": 3, "done": True}}
    [message] = problems(entries, _LIVE)
    assert "GET /api/v1/jobs" in message
    assert "add it to ui/parity.yaml with class" in message


def test_a_stale_entry_is_reported() -> None:
    """An entry whose command or route no longer exists is reported."""
    entries = {
        "table undrop": {"class": "ui", "part": 3, "done": True},
        "GET /api/v1/jobs": {"class": "read", "part": 5, "done": True},
        "gone": {"class": "git", "reason": "x"},
    }
    [message] = problems(entries, _LIVE)
    assert "gone" in message
    assert "remove it from ui/parity.yaml" in message


def test_the_real_inventory_fails_when_an_entry_is_removed_or_unknown(live: set[str]) -> None:
    """Dropping a real entry or adding an unknown key is caught.

    Parameters
    ----------
    live
        The live keys.
    """
    entries = load_entries(PARITY_FILE)
    removed = next(iter(entries))
    del entries[removed]
    entries["no such command"] = {"class": "git", "reason": "x"}
    messages = problems(entries, live)
    assert any(removed in m for m in messages)
    assert any("no such command" in m for m in messages)


def test_invalid_entries_are_reported() -> None:
    """A bad class, a missing or out-of-range part, a missing reason, a new exception."""
    entries = {
        "table undrop": {"class": "maybe"},
        "GET /api/v1/jobs": {"class": "ui"},
        "a": {"class": "ui", "part": 9},
        "b": {"class": "git"},
        "c": {"class": "exception", "reason": "because"},
        "d": {"class": "git", "reason": "x", "part": 3},
    }
    text = "\n".join(problems(entries, _LIVE | set(entries)))
    for key in ("table undrop", "GET /api/v1/jobs", "'a'", "'b'", "'c'", "'d'"):
        assert key in text


def test_a_ui_entry_with_a_reason_and_a_git_entry_with_a_part_are_refused() -> None:
    """The field that belongs to the other classes is refused."""
    entries = {
        "table undrop": {"class": "ui", "part": 3, "reason": "x"},
        "GET /api/v1/jobs": {"class": "git", "reason": "x", "part": 3},
    }
    text = "\n".join(problems(entries, _LIVE))
    assert "drop `reason:`" in text
    assert "drop `part:`" in text


def test_an_empty_entry_and_a_boolean_part_are_refused() -> None:
    """An entry without a value, or with ``part: true``, gets a message naming the key."""
    entries = {"table undrop": None, "GET /api/v1/jobs": {"class": "ui", "part": True}}
    text = "\n".join(problems(entries, _LIVE))
    assert "'table undrop' has no value" in text
    assert "'GET /api/v1/jobs'" in text
    assert "part" in text


def test_a_duplicate_key_in_the_file_is_refused(tmp_path: Path) -> None:
    """YAML would keep the last of two equal keys; the loader names the key instead.

    Parameters
    ----------
    tmp_path
        A scratch directory.
    """
    path = tmp_path / "parity.yaml"
    path.write_text('"a": {class: git, reason: x}\n"a": {class: git, reason: y}\n')
    with pytest.raises(ValueError, match="'a' appears twice"):
        load_entries(path)


def test_done_is_refused_on_git_and_exception() -> None:
    """``done`` belongs to ui and read; git and exception entries are refused with the next step."""
    entries = {
        "a": {"class": "git", "reason": "x", "done": True},
        "task-token mint": {"class": "exception", "reason": "x", "done": True},
    }
    messages = problems(entries, {"a", "task-token mint"})
    assert len(messages) == 2
    assert "'a'" in messages[0]
    assert "drop `done:`" in messages[0]
    assert "'task-token mint'" in messages[1]


def test_done_must_be_true() -> None:
    """Only the boolean true is accepted; unquoted ``yes`` is True and passes."""
    entries = {
        "a": {"class": "ui", "part": 3, "done": False},
        "b": {"class": "read", "part": 3, "done": "yes"},
        "c": {"class": "ui", "part": 3, "done": 1},
    }
    messages = problems(entries, set(entries))
    assert len(messages) == 3
    assert "'a'" in messages[0]
    assert "remove the line" in messages[0]
    assert "'b'" in messages[1]
    assert "'c'" in messages[2]
    parsed = yaml.safe_load("d: { class: ui, part: 3, done: yes }")
    assert problems(parsed, {"d"}) == []


def test_done_is_allowed_on_ui_and_read() -> None:
    """Regression guard: passes before the flag exists, since unknown keys are ignored."""
    entries = {
        "a": {"class": "ui", "part": 3, "done": True},
        "b": {"class": "read", "part": 5, "done": True},
    }
    assert problems(entries, set(entries)) == []


def test_a_ui_or_read_entry_of_a_finished_part_without_done_is_refused() -> None:
    """Every part of M7 is finished: an entry it places in the interface must be built."""
    entries = {
        "table undrop": {"class": "ui", "part": 3},
        "GET /api/v1/jobs": {"class": "read", "part": 5},
    }
    messages = problems(entries, _LIVE)
    assert len(messages) == 2
    for key, message in zip(("table undrop", "GET /api/v1/jobs"), messages, strict=True):
        assert repr(key) in message
        assert "which is finished, but has no `done: true`" in message
        assert "class git with a reason" in message


def test_an_entry_of_a_part_not_yet_finished_may_lack_done() -> None:
    """Only the finished parts require the flag; a later part's entry is a plan, not a gap."""
    entries = {
        "table undrop": {"class": "ui", "part": 3},
        "GET /api/v1/jobs": {"class": "read", "part": 5, "done": True},
    }
    assert problems(entries, _LIVE, finished_parts=frozenset({5})) == []
    [message] = problems(entries, _LIVE, finished_parts=frozenset({3}))
    assert "'table undrop'" in message


def test_the_real_inventory_fails_when_a_done_flag_is_removed(live: set[str]) -> None:
    """Dropping one `done: true` from the real file is caught, naming the entry.

    Parameters
    ----------
    live
        The live keys.
    """
    entries = load_entries(PARITY_FILE)
    key = next(k for k, entry in entries.items() if entry.get("done") is True)
    del entries[key]["done"]
    [message] = problems(entries, live)
    assert repr(key) in message
    assert "has no `done: true`" in message
