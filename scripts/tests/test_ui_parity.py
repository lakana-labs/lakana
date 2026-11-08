"""Tests for the inventory of every command and route with its place in the interface."""

import pytest

from scripts.check_ui_parity import PARITY_FILE, live_keys, load_entries, problems

_LIVE = {"table undrop", "GET /api/v1/jobs"}


@pytest.fixture(scope="module")
def live() -> set[str]:
    """Walk the real command tree and routers once for the module.

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
    entries = {"table undrop": {"class": "ui", "part": 3}}
    [message] = problems(entries, _LIVE)
    assert "GET /api/v1/jobs" in message
    assert "add it to ui/parity.yaml with class" in message


def test_a_stale_entry_is_reported() -> None:
    """An entry whose command or route no longer exists is reported."""
    entries = {
        "table undrop": {"class": "ui", "part": 3},
        "GET /api/v1/jobs": {"class": "read", "part": 5},
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
