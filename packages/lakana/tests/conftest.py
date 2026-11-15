"""Shared fixtures for the SDK tests."""

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

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


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """Give a test a temporary directory with a short path, to find in a message.

    The CLI and the SDK shorten and wrap a long path in their messages, so a test that looks for
    one fails when pytest's own `tmp_path` is long (a `--basetemp` of 140 characters or more,
    #597, #604). This directory sits in the system's temporary directory instead and is removed
    afterwards.

    Yields
    ------
    Path
        The directory, resolved.
    """
    path = Path(tempfile.mkdtemp(prefix="lakana-test-")).resolve()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
