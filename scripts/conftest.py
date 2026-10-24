"""Loads the memory limit, the temp cleanup and the CI test selection for the tests below."""

from scripts.pytest_memory_limit import pytest_configure
from scripts.pytest_selection import pytest_collection_modifyitems
from scripts.pytest_writable_tmp import _writable_temp_trees

__all__ = ["_writable_temp_trees", "pytest_collection_modifyitems", "pytest_configure"]
