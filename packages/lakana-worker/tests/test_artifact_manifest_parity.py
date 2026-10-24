"""The SDK and core judge a manifest path alike.

The harness builds a manifest with `lakana._artifact_manifest`; the server
validates it with `lakana_core.artifacts.manifest`. They are separate code in
separate packages and must not drift: a tree the harness accepts must be one
the server accepts, with the same digest.
"""

from __future__ import annotations

import hashlib
import itertools
import random
import sys

import pytest

from lakana import _artifact_manifest as sdk
from lakana_core.artifacts import manifest as core
from lakana_core.errors import ValidationError

_PATHS = [
    "model.json",
    "a/b/c.bin",
    "../x",
    "a/../b",
    "a/./b",
    "./a",
    "..",
    ".",
    "/abs",
    "//abs",
    "/",
    "a\\b",
    "C:\\x",
    "C:/x",
    "c:",
    "a/c:/x",
    "a:b",
    "x/y:stream",
    "a./b",
    "a /b",
    "a/b.",
    "dir/NUL",
    "nul.txt",
    "Con",
    "com1.tar.gz",
    "COM\u00b9",
    "lpt\u00b3.x",
    "NUL .txt",
    "console",
    "nul_1",
    "COM\u2074",
    "com10",
    "lpt\u00b2",
    "aux",
    "PRN.x",
    "COM0",
    "conin$",
    "CONOUT$.txt",
    "NU\u017f",
    "nul\u0131",
    "NUL/a:",
    "a:/NUL",
    'a<b>c|d?e*f"g',
    "\uffff",
    "\U00010000",
    "a//b",
    "a/",
    "a/b/",
    "a/.",
    "",
    "x" * 1024,
    "x" * 1025,
    "x" * 255,
    "x" * 256,
    "a/" + "x" * 255,
    "a/" + "x" * 256,
    "é" * 127 + "x",
    "é" * 128,
    "\U0001f600" * 63 + "xxx",
    "\U0001f600" * 64,
    "é" * 512,
    "é" * 513,
    "\U0001f600" * 256,
    "\U0001f600" * 257,
    "/".join("a" * 32),
    "/".join("a" * 33),
    "a\x00",
    "a\x00/../b",
    "a\nb",
    "a\rb",
    "a\tb",
    "a\x7fb",
    "a\x85b",
    "a\x1b[31mb",
    "a\u200fb",
    "a\u200db",
    "a\u200bb",
    "a\u202eb",
    "a\u2066b",
    "\ufeffa",
    "a\u2028b",
    "a\u2029b",
    "a\ud800",
    "a\udfffb",
    "\ud83d\ude00",
    "Ünï/cödé",
    "cafe\u0301",
    "weights/layer 1/Ünïcode.bin",
    3,
    None,
    ["a"],
    b"a",
]
_SETS = [
    ["a", "b"],
    ["A", "a"],
    ["A/x", "a/y"],
    ["a", "a/b"],
    ["A", "a/b"],
    ["a/b", "a/b/c"],
    ["cafe\u0301", "caf\u00e9"],
    ["x/Ünï/a", "x/U\u0308nï/b"],
    ["STRASSE", "stra\u00dfe"],
    ["a/x", "a/y", "b/z"],
    ["a/b/c", "a/b/d", "a/e/f", "ab/c", "b"],
    ["İ/x", "i\u0307/y"],
    ["ǅ", "ǆ"],
    [],
]


def _random_sets() -> list[list[str]]:
    """Build a fixed pseudo-random batch of path sets over a collision-prone alphabet.

    Returns
    -------
    list of list of str
        Deterministic, so a failure reproduces.
    """
    rng = random.Random(20261005)
    alphabet = ["a", "A", "b", "/", "/", "é", "e\u0301", "ß", "SS", "ss", "ǅ", "İ", ".", "\\"]
    sets = []
    for _ in range(1500):
        paths = []
        for _ in range(rng.randint(1, 4)):
            path = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6)))
            if core.path_problem(path) is None:
                paths.append(path)
        sets.append(sorted(set(paths), key=lambda p: p.encode("utf-8")))
    return sets


@pytest.mark.parametrize("path", _PATHS, ids=repr)
def test_one_path_is_judged_alike(path: object) -> None:
    assert sdk.path_problem(path) == core.path_problem(path)
    assert sdk.shown(path) == core.shown(path)
    if isinstance(path, str):
        assert sdk.fold_path(path) == core.fold_path(path)


@pytest.mark.parametrize("paths", _SETS, ids=repr)
def test_a_set_of_paths_is_judged_alike(paths: list[str]) -> None:
    assert sdk.paths_problem(paths) == core.paths_problem(paths)
    assert [sdk.fold_path(p) for p in paths] == [core.fold_path(p) for p in paths]


@pytest.mark.slow
def test_every_code_point_is_judged_alike() -> None:
    for code in range(sys.maxunicode + 1):
        path = f"a{chr(code)}b"
        assert sdk.path_problem(path) == core.path_problem(path), hex(code)
    for code in itertools.chain(range(0x3000), range(0x1F000, 0x1F100)):
        text = chr(code)
        assert sdk.fold_path(text) == core.fold_path(text), hex(code)
        assert sdk.shown(text) == core.shown(text), hex(code)


def test_random_sets_are_judged_alike() -> None:
    for paths in _random_sets():
        assert sdk.paths_problem(paths) == core.paths_problem(paths), paths
        for path in paths:
            assert sdk.path_problem(path) == core.path_problem(path)


def test_the_digest_is_the_same() -> None:
    files = [("a/b.bin", 3, "0" * 64), ("model.json", 14, "f" * 64)]
    for is_directory in (True, False):
        assert sdk.canonical_manifest(is_directory, files) == core.canonical_manifest(
            is_directory, files
        )
        assert sdk.manifest_digest(is_directory, files) == core.manifest_digest(is_directory, files)
        assert sdk.manifest_digest(is_directory, files[::-1]) == core.manifest_digest(
            is_directory, files
        )


def test_the_sort_key_is_the_utf8_byte_order_in_both() -> None:
    """U+FFFF sorts before U+10000 in UTF-8 and after it in UTF-16; both copies use UTF-8."""
    low, high = "\uffff", "\U00010000"
    files = [(high, 1, "0" * 64), (low, 2, "f" * 64)]
    for is_directory in (True, False):
        sdk_bytes = sdk.canonical_manifest(is_directory, files)
        assert sdk_bytes == core.canonical_manifest(is_directory, files[::-1])
        assert sdk_bytes.index(b"\\uffff") < sdk_bytes.index(b"\\ud800\\udc00")
        assert sdk.manifest_digest(is_directory, files) == core.manifest_digest(
            is_directory, files[::-1]
        )
        assert sdk.manifest_digest(is_directory, files) == hashlib.sha256(sdk_bytes).hexdigest()


def test_a_tree_the_sdk_builds_is_one_the_server_accepts_with_the_same_digest() -> None:
    paths = sorted(
        ["weights/layer 1/Ünï.bin", "weights/layer 2/b.bin", "config.json", "x/y/z"],
        key=lambda p: p.encode("utf-8"),
    )
    files = [(p, len(p), hashlib.sha256(p.encode()).hexdigest()) for p in paths]
    assert sdk.paths_problem(paths) is None
    document = {
        "format": 1,
        "is_directory": True,
        "files": [{"path": p, "size": s, "sha256": h} for p, s, h in files],
    }
    assert core.parse_manifest(document).sha256() == sdk.manifest_digest(True, files)


def test_a_path_the_sdk_refuses_the_server_refuses_with_the_same_words() -> None:
    for path in _PATHS:
        problem = sdk.path_problem(path)
        if problem is None or not isinstance(path, str) or not path:
            continue
        document = {
            "format": 1,
            "is_directory": True,
            "files": [{"path": path, "size": 1, "sha256": "0" * 64}],
        }
        with pytest.raises(ValidationError) as caught:
            core.parse_manifest(document)
        assert str(caught.value) == f"Path {sdk.shown(path)} {problem}."


def test_the_constants_agree() -> None:
    assert (
        sdk.MANIFEST_FORMAT,
        sdk.MAX_PATH_BYTES,
        sdk.MAX_PATH_SEGMENTS,
        sdk.MAX_SEGMENT_BYTES,
    ) == (
        core.MANIFEST_FORMAT,
        core.MAX_PATH_BYTES,
        core.MAX_PATH_SEGMENTS,
        core.MAX_SEGMENT_BYTES,
    )


def test_a_part_is_at_most_255_bytes_in_both_and_counted_in_bytes() -> None:
    for path, problem in (
        ("é" * 127 + "x", None),  # 255 bytes, 128 characters
        ("é" * 128, "must not have a part longer than 255 bytes"),  # 256 bytes, 128 characters
        ("\U0001f600" * 63 + "xxx", None),
        ("\U0001f600" * 64, "must not have a part longer than 255 bytes"),
        ("x/" + "y" * 256, "must not have a part longer than 255 bytes"),
    ):
        assert sdk.path_problem(path) == problem
        assert core.path_problem(path) == problem


def test_the_sdk_and_core_word_a_deleted_version_alike() -> None:
    """One cause, one text: a download refused by the server and one refused by the SDK."""
    from lakana.resources import artifacts as sdk_resource
    from lakana_core.artifacts import service as core_service

    assert sdk_resource._VERSION_DELETED == core_service.VERSION_DELETED_MESSAGE
