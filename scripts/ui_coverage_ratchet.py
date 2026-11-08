"""Enforce that the web interface's test coverage never decreases.

Reads the total from Vitest's ``coverage-summary.json`` (lines and branches counted
together), compares it against ``ui/.coverage-baseline`` and an absolute floor, and
raises the baseline with ``--update`` when coverage improves. The report is refused
when it is older than a source file, like ``scripts/coverage_ratchet.py``.

Standard library only (plus the sibling ratchet), so CI runs it with the runner's
``python3`` and no ``uv sync``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.coverage_ratchet import DEFAULT_FLOOR, check, read_baseline, write_baseline

SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".css", ".json"})
RERUN = "Re-run: npm --prefix ui run test:coverage"


def ui_percentage(summary: Mapping[str, object]) -> float:
    """Compute the combined line and branch coverage of a Vitest summary.

    Parameters
    ----------
    summary
        The parsed ``coverage-summary.json``.

    Returns
    -------
    float
        Covered lines plus branches over total lines plus branches, in percent;
        ``100.0`` when there is nothing to cover.

    Raises
    ------
    ValueError
        If the summary lacks the ``total`` lines and branches counts.
    """
    total = summary.get("total")
    if not isinstance(total, Mapping):
        raise ValueError("no 'total' entry")
    covered = 0
    count = 0
    for kind in ("lines", "branches"):
        part = total.get(kind)
        if not isinstance(part, Mapping):
            raise ValueError(f"no '{kind}' entry")
        part_total, part_covered = part.get("total"), part.get("covered")
        if not isinstance(part_total, int) or not isinstance(part_covered, int):
            raise ValueError(f"'{kind}' has no counts")
        count += part_total
        covered += part_covered
    return 100.0 if count == 0 else 100.0 * covered / count


def is_stale(*, report: Path, source: Path) -> bool:
    """Report whether the coverage summary predates the code it describes.

    Parameters
    ----------
    report
        Path to ``coverage-summary.json``.
    source
        Directory holding the measured source (``ui/src``).

    Returns
    -------
    bool
        True when any ``.ts``, ``.tsx``, ``.css`` or ``.json`` file under `source`
        is newer than `report`, or when anything cannot be inspected.
    """
    try:
        report_mtime = report.stat().st_mtime
        return any(
            path.stat().st_mtime > report_mtime
            for path in source.rglob("*")
            if path.suffix in SOURCE_SUFFIXES and path.is_file()
        )
    except OSError:
        return True


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ratchet against the UI coverage summary.

    Parameters
    ----------
    argv
        Command-line arguments; None reads ``sys.argv``.

    Returns
    -------
    int
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=Path("ui/coverage/coverage-summary.json"))
    parser.add_argument("--baseline", type=Path, default=Path("ui/.coverage-baseline"))
    parser.add_argument("--source", type=Path, default=Path("ui/src"))
    parser.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    parser.add_argument(
        "--update", action="store_true", help="Write the new baseline when coverage improves."
    )
    args = parser.parse_args(argv)

    if not args.summary.exists():
        print(f"{args.summary} not found. {RERUN}", file=sys.stderr)
        return 1
    if is_stale(report=args.summary, source=args.source):
        print(f"{args.summary} is older than the source it covers. {RERUN}", file=sys.stderr)
        return 1
    try:
        current = ui_percentage(json.loads(args.summary.read_text()))
    except (OSError, ValueError, AttributeError) as error:
        print(
            f"{args.summary} is not a Vitest coverage summary ({error}). {RERUN}", file=sys.stderr
        )
        return 1

    try:
        baseline = read_baseline(args.baseline)
    except OSError as error:
        print(
            f"cannot read {args.baseline} ({error}). Check the path and permissions.",
            file=sys.stderr,
        )
        return 1
    except ValueError:
        print(
            f"{args.baseline} does not hold a percentage between 0 and 100. "
            "Restore it from git, or delete it to start the ratchet from zero.",
            file=sys.stderr,
        )
        return 1

    message = check(current=current, baseline=baseline, floor=args.floor)
    if message is not None:
        print(f"{message} {RERUN}", file=sys.stderr)
        return 1

    print(f"UI coverage {current:.2f}% (baseline {baseline:.2f}%).")
    if args.update and current > baseline:
        write_baseline(args.baseline, current)
        print(f"Baseline raised to {current:.2f}%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
