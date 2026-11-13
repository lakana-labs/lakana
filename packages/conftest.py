"""Loads the memory limit, the temp cleanup and the CI test selection for the tests below."""

from collections.abc import Iterator

import pytest
from scripts.pytest_memory_limit import pytest_configure
from scripts.pytest_selection import pytest_collection_modifyitems
from scripts.pytest_writable_tmp import _writable_temp_trees
from scripts.test_support.ports import release_closed_ports

__all__ = ["_writable_temp_trees", "pytest_collection_modifyitems", "pytest_configure"]


@pytest.fixture(autouse=True)
def _release_closed_ports() -> Iterator[None]:
    """Free the ports `closed_port` reserved for the test, in all four packages.

    Yields
    ------
    None
        Control to the test.
    """
    yield
    release_closed_ports()
