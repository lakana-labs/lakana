"""The CLI's host key types stay those core keeps, in the same order.

The CLI may not import core, so `lakana.cli.repos.HOST_KEY_TYPES` repeats
`lakana_core.projects.probes.KEY_TYPES`; this package may import both.
"""

from __future__ import annotations

from lakana.cli.repos import HOST_KEY_TYPES
from lakana_core.projects.probes import KEY_TYPES


def test_the_cli_ranks_the_key_types_core_keeps() -> None:
    """`--save` picks the strongest of exactly the types a probe can report."""
    assert HOST_KEY_TYPES == KEY_TYPES
