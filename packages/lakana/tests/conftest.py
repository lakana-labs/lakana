"""Shared fixtures for the SDK tests."""

from collections.abc import Iterator

import pytest

from lakana import _usage


@pytest.fixture(autouse=True)
def _fresh_usage() -> Iterator[None]:
    """Start and end every test with nothing counted, as a new task process would.

    Yields
    ------
    None
        Control to the test.
    """
    _usage.reset()
    yield
    _usage.reset()
