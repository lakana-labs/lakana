"""Spread the documentation's example projects over CI's shards by their recorded seconds.

``scripts/run_docs_examples.sh`` runs every example when it is given no name; with
``LAKANA_DOCS_SHARD=I/N`` it asks this module which examples are shard I of N and runs only those.
CI's ``docs-examples (I/N)`` jobs do that side by side, and the required check ``docs-examples``
passes only when all of them did (docs/agent/testing.md, "Running everything locally").

- **Balance.** The examples are taken the longest first, each to the shard with the least time
  so far. The seconds come from ``scripts/docs-example-durations.json``; an example without an
  entry counts `UNKNOWN_EXAMPLE_SECONDS`. Every shard computes the same answer from the same
  directory listing, so each example runs in exactly one shard.
- **Refresh.** ``durations --output FILE LOG...`` reads the ``| example | N s |`` rows the
  runner prints at its end (in a job log from ``gh run view --log``, or its step summary) and
  writes the mean seconds per example.

Usage::

    python -m scripts.docs_example_shards select 2/3            # the names, one per line
    python -m scripts.docs_example_shards durations --output FILE LOG...   # FILE: the json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "examples"
DURATIONS = Path(__file__).with_name("docs-example-durations.json")
SHARD_ENV = "LAKANA_DOCS_SHARD"
UNKNOWN_EXAMPLE_SECONDS = 30.0
"""The seconds of an example that has no recorded time yet: about the median example."""

_ROW = re.compile(r"\| (?P<name>[A-Za-z0-9_-]+) \| (?P<seconds>\d+) s \|")


class ShardError(ValueError):
    """A shard, a durations file or a log that cannot be used; the message says what to do."""


def parse_shard(text: str) -> tuple[int, int]:
    """Read ``I/N`` as shard I of N.

    Parameters
    ----------
    text
        The value of ``LAKANA_DOCS_SHARD``.

    Returns
    -------
    tuple of int
        The shard's number, from one, and the number of shards.

    Raises
    ------
    ShardError
        If the value is not two whole numbers with ``1 <= I <= N``.
    """
    parts = text.strip().split("/")
    try:
        index, count = (int(part) for part in parts)
    except ValueError:
        index, count = 0, 0
    if not 1 <= index <= count:
        raise ShardError(
            f"{SHARD_ENV}={text!r} is not a shard: write it as I/N with 1 <= I <= N, "
            f"for example {SHARD_ENV}=1/3, or unset it to run every example."
        )
    return index, count


def read_durations(path: Path) -> dict[str, float]:
    """Read the recorded seconds per example.

    Parameters
    ----------
    path
        The durations file; a missing file reads as empty.

    Returns
    -------
    dict
        Example name to seconds.

    Raises
    ------
    ShardError
        If the file is not a JSON object of non-negative numbers.
    """
    if not path.exists():
        return {}
    fix = (
        f"{path} is not a JSON object of seconds per example. Restore it from git, or write it "
        "again from a docs-examples job log: uv run python -m scripts.docs_example_shards "
        f"durations --output {path} LOG ..."
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ShardError(fix) from None
    if not isinstance(data, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
        for v in data.values()
    ):
        raise ShardError(fix)
    return {str(k): float(v) for k, v in data.items()}


def assign(names: Iterable[str], seconds: dict[str, float], count: int) -> dict[str, int]:
    """Spread examples over shards by their seconds, the longest first.

    Parameters
    ----------
    names
        The examples; the order does not matter.
    seconds
        Recorded seconds per example; one without counts `UNKNOWN_EXAMPLE_SECONDS`.
    count
        The number of shards.

    Returns
    -------
    dict
        Example to its shard, from one.
    """
    totals = [0.0] * count
    shards: dict[str, int] = {}
    for name in sorted(set(names), key=lambda n: (-seconds.get(n, UNKNOWN_EXAMPLE_SECONDS), n)):
        lightest = min(range(count), key=lambda i: (totals[i], i))
        totals[lightest] += seconds.get(name, UNKNOWN_EXAMPLE_SECONDS)
        shards[name] = lightest + 1
    return shards


def examples(directory: Path | None = None) -> list[str]:
    """List the examples: the directories with a ``run.sh``.

    Parameters
    ----------
    directory
        Where the examples are; `EXAMPLES` (``docs/examples``) by default.

    Returns
    -------
    list of str
        Their names, sorted.
    """
    folder = EXAMPLES if directory is None else directory
    return sorted(p.name for p in folder.iterdir() if (p / "run.sh").is_file())


def select(shard: str, directory: Path | None = None, durations: Path | None = None) -> list[str]:
    """Name the examples of one shard.

    Parameters
    ----------
    shard
        ``I/N``.
    directory
        Where the examples are; `EXAMPLES` by default.
    durations
        The recorded seconds per example; `DURATIONS` by default.

    Returns
    -------
    list of str
        The examples of shard I, sorted; empty when there are more shards than examples.
    """
    index, count = parse_shard(shard)
    recorded = read_durations(DURATIONS if durations is None else durations)
    spread = assign(examples(directory), recorded, count)
    return sorted(name for name, number in spread.items() if number == index)


def measured(texts: Iterable[str]) -> dict[str, float]:
    """Read the mean seconds per example from the runner's summary rows.

    Parameters
    ----------
    texts
        Job logs or step summaries, each holding rows ``| example | N s |``.

    Returns
    -------
    dict
        Example name to its mean seconds, rounded to a tenth, sorted by name.

    Raises
    ------
    ShardError
        If no text holds a row.
    """
    seen: dict[str, list[int]] = {}
    for text in texts:
        for row in _ROW.finditer(text):
            seen.setdefault(row["name"], []).append(int(row["seconds"]))
    if not seen:
        raise ShardError(
            "no row '| example | N s |' found; pass the log of a docs-examples job "
            "(gh run view RUN_ID --log > LOG), whose runner prints one row per example at its end"
        )
    return {name: round(statistics.mean(seen[name]), 1) for name in sorted(seen)}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line.

    Parameters
    ----------
    argv
        The arguments; `sys.argv` without the program by default.

    Returns
    -------
    int
        0 on success, 2 for a shard, file or log that cannot be used (the message says why).
    """
    parser = argparse.ArgumentParser(prog="python -m scripts.docs_example_shards")
    commands = parser.add_subparsers(dest="command", required=True)
    pick = commands.add_parser("select", help="print the examples of shard I of N")
    pick.add_argument("shard", help="I/N")
    write = commands.add_parser("durations", help="write the seconds per example from job logs")
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("logs", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "select":
            for name in select(args.shard):
                print(name)
        else:
            texts = (log.read_text(encoding="utf-8", errors="replace") for log in args.logs)
            seconds = measured(texts)
            args.output.write_text(json.dumps(seconds, indent=1) + "\n", encoding="utf-8")
            print(f"wrote the seconds of {len(seconds)} examples to {args.output}")
    except ShardError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
