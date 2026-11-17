"""Write each harness's seeds as a libFuzzer corpus: one file per input, named by its hash.

``fuzz/seeds/<name>.json`` holds the seeds of ``fuzz/<name>_fuzzer.py`` as a list of strings, so
a seed reads as text in a review. ClusterFuzzLite wants ``<fuzzer>_seed_corpus.zip`` next to
each fuzzer (``.clusterfuzzlite/build.sh`` runs this with ``--zip``); a local run takes a
directory (without it).

The query guard's seeds are the statements earlier guard-fuzzing runs collected from the
guard's own tests, one per distinct sequence of token types: statements that tokenize alike
take the parser down the same paths, so more of them only slow the fuzzer's start.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

SEEDS = Path(__file__).resolve().parent / "seeds"


def seeds(name: str) -> dict[str, bytes]:
    """Read one harness's seeds.

    Parameters
    ----------
    name
        The harness's name, as in `fuzz.targets.TARGETS`.

    Returns
    -------
    dict of str to bytes
        Each seed's UTF-8 bytes under the SHA-1 of those bytes, libFuzzer's own naming.
    """
    texts = json.loads((SEEDS / f"{name}.json").read_text(encoding="utf-8"))
    encoded = (text.encode("utf-8", "surrogatepass") for text in texts)
    return {hashlib.sha1(data, usedforsecurity=False).hexdigest(): data for data in encoded}


def write(name: str, out: Path, *, as_zip: bool) -> Path:
    """Write one harness's seed corpus under `out`.

    Parameters
    ----------
    name
        The harness's name.
    out
        The directory to write into; it must exist.
    as_zip
        Write ``<name>_fuzzer_seed_corpus.zip`` instead of the directory ``<name>``.

    Returns
    -------
    pathlib.Path
        The archive or directory written.
    """
    corpus = seeds(name)
    if as_zip:
        path = out / f"{name}_fuzzer_seed_corpus.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_name, data in sorted(corpus.items()):
                archive.writestr(file_name, data)
        return path
    path = out / name
    path.mkdir(exist_ok=True)
    for file_name, data in corpus.items():
        (path / file_name).write_bytes(data)
    return path


def main(argv: list[str] | None = None) -> int:
    """Write the seed corpus of every harness.

    Parameters
    ----------
    argv
        The arguments; the process's own when `None`.

    Returns
    -------
    int
        The exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path, help="the directory to write into")
    parser.add_argument("--zip", action="store_true", help="write ClusterFuzzLite's archives")
    args = parser.parse_args(argv)
    for path in sorted(SEEDS.glob("*.json")):
        print(write(path.stem, args.out, as_zip=args.zip))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
