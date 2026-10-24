"""The artifact manifest: its canonical form, its limits and its path rules."""

from __future__ import annotations

import hashlib
import time

import pytest

from lakana_core.artifacts.manifest import (
    ArtifactManifest,
    ManifestFile,
    ManifestLimits,
    canonical_manifest,
    fold_path,
    manifest_digest,
    parse_manifest,
    path_problem,
    paths_problem,
    shown,
)
from lakana_core.config import ArtifactsConfig
from lakana_core.errors import ValidationError

_A = hashlib.sha256(b"a").hexdigest()
_B = hashlib.sha256(b"b").hexdigest()
_MODEL = "68ad4f90fd5eedf03d989cd614b1a1da92ccb7965662d6eaf2524c85dbb8325b"
_LIMITS = ManifestLimits(max_file_bytes=100, max_bytes=150, max_files=3)
_LONG_PART = "must not have a part longer than 255 bytes"
_UNSAFE_TEXT = "must not contain a control, format or surrogate character or a line separator"


def _doc(files: list[dict[str, object]], *, is_directory: bool = True) -> dict[str, object]:
    """Build a manifest document.

    Parameters
    ----------
    files
        The file entries.
    is_directory
        Whether the artifact is a directory.

    Returns
    -------
    dict
        The document.
    """
    return {"format": 1, "is_directory": is_directory, "files": files}


def _file(path: str, size: int = 1, sha256: str = _A) -> dict[str, object]:
    """Build one file entry.

    Parameters
    ----------
    path
        The path.
    size
        The size.
    sha256
        The hash.

    Returns
    -------
    dict
        The entry.
    """
    return {"path": path, "size": size, "sha256": sha256}


def test_the_canonical_form_is_fixed() -> None:
    """These exact bytes, so the digest is the same on every platform."""
    files = [("model.json", 14, _MODEL)]
    assert canonical_manifest(False, files) == (
        b'{"files":[{"path":"model.json","sha256":"' + _MODEL.encode() + b'","size":14}],'
        b'"format":1,"is_directory":false}'
    )
    assert manifest_digest(False, files) == (
        "b97616016d0d6bd401c61c158cbf7405bc346d3d6ee6c22f8ed4099a3fe87c81"
    )


def test_a_valid_manifest_parses_and_reports_its_totals() -> None:
    manifest = parse_manifest(_doc([_file("a.bin", 1, _A), _file("sub/b.bin", 2, _B)]), _LIMITS)
    assert manifest == ArtifactManifest(
        is_directory=True,
        files=(ManifestFile("a.bin", 1, _A), ManifestFile("sub/b.bin", 2, _B)),
    )
    assert (manifest.size_bytes, manifest.file_count) == (3, 2)
    assert manifest.hashes() == {_A: 1, _B: 2}
    assert manifest.to_json() == _doc([_file("a.bin", 1, _A), _file("sub/b.bin", 2, _B)])
    assert manifest.sha256() == hashlib.sha256(manifest.canonical()).hexdigest()
    assert parse_manifest(manifest.to_json()) == manifest


def test_the_same_files_give_the_same_hash() -> None:
    one = parse_manifest(_doc([_file("a", 1, _A)]), _LIMITS)
    two = parse_manifest({"files": [_file("a", 1, _A)], "is_directory": True, "format": 1}, _LIMITS)
    assert one.sha256() == two.sha256()
    assert parse_manifest(_doc([_file("a", 1, _B)]), _LIMITS).sha256() != one.sha256()


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("../etc/passwd", "must not have an empty, '.' or '..' segment"),
        ("a/../b", "must not have an empty, '.' or '..' segment"),
        ("a/./b", "must not have an empty, '.' or '..' segment"),
        ("a//b", "must not have an empty, '.' or '..' segment"),
        ("a/", "must not have an empty, '.' or '..' segment"),
        ("/etc/passwd", "must be relative, without a leading '/'"),
        ("a\\b", "must not contain a backslash; separate segments with '/'"),
        ("a\x00b", _UNSAFE_TEXT),
        ("a\nb", _UNSAFE_TEXT),
        ("a\u202eb", _UNSAFE_TEXT),
        ("a\ud800b", _UNSAFE_TEXT),
        ("a\u2028b", _UNSAFE_TEXT),
        ("x" * 1025, "is longer than 1024 bytes"),
        ("é" * 513, "is longer than 1024 bytes"),
        ("/".join("a" * 33), "has more than 32 segments"),
        ("x" * 256, _LONG_PART),
        ("a/" + "é" * 128, _LONG_PART),
        ("\U0001f600" * 64, _LONG_PART),
    ],
)
def test_a_path_that_leaves_its_directory_is_refused(path: str, problem: str) -> None:
    """Every path a manifest may not contain, with the text a user reads."""
    assert path_problem(path) == problem
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc([_file(path)]), _LIMITS)
    assert str(caught.value) == f"Path {shown(path)} {problem}."


@pytest.mark.parametrize("path", ["", 3, None, ["a"]])
def test_a_path_must_be_a_non_empty_string(path: object) -> None:
    assert path_problem(path) == "a path must be a non-empty string"
    with pytest.raises(ValidationError, match=r"A manifest path must be a non-empty string\."):
        parse_manifest(_doc([{"path": path, "size": 1, "sha256": _A}]), _LIMITS)


def test_the_longest_allowed_path_passes() -> None:
    assert path_problem("/".join(["x" * 255] * 4)[:1024]) is None
    assert path_problem("x" * 255) is None
    assert path_problem("é" * 127 + "x") is None  # 255 bytes in 128 characters
    assert path_problem("\U0001f600" * 63 + "xxx") is None  # 255 bytes in 66 characters
    assert path_problem("/".join("a" * 32)) is None
    assert path_problem("weights/layer 1/Ünïcode.bin") is None


def test_a_shown_path_cannot_forge_a_line() -> None:
    assert shown("a\nb\x1b[31m") == "a\\nb\\x1b[31m"
    assert len(shown("x" * 5000)) == 200
    assert shown(3) == "int"


@pytest.mark.parametrize(
    ("paths", "message"),
    [
        (["Model.bin", "model.bin"], "Paths Model.bin and model.bin differ only by case"),
        (["cafe\u0301", "caf\u00e9"], "differ only by case or Unicode normalisation"),
        (["STRASSE", "stra\u00dfe"], "differ only by case or Unicode normalisation"),
        (["A/x", "a/y"], "Directories A and a differ only by case"),
        (["a", "a/b"], "a is both a file and a directory."),
        (["A", "a/b"], "A is both a file and a directory."),
    ],
)
def test_paths_that_collide_after_folding_are_refused(paths: list[str], message: str) -> None:
    """The consumer's copy must be the same tree on every file system."""
    ordered = sorted(paths, key=lambda p: p.encode("utf-8"))
    assert message in (paths_problem(ordered) or "")
    with pytest.raises(ValidationError, match=message):
        parse_manifest(_doc([_file(p) for p in ordered]), _LIMITS)


def test_folding_is_the_one_discovery_uses() -> None:
    assert fold_path("Model.JSON") == fold_path("model.json")
    assert fold_path("a/B") != fold_path("a/c")
    assert paths_problem(["a/x", "a/y", "b"]) is None


def test_an_exceeded_limit_names_the_setting_and_the_size_found() -> None:
    """Refused before a byte is sent, with the setting and what was found."""
    with pytest.raises(ValidationError) as files:
        parse_manifest(_doc([_file(n) for n in "abcd"]), _LIMITS)
    assert str(files.value) == (
        "The artifact has 4 files, more than LAKANA_ARTIFACTS__MAX_FILES (3). "
        "Store fewer files, or ask the operator to raise the setting."
    )
    with pytest.raises(ValidationError) as one:
        parse_manifest(_doc([_file("big.bin", 101)]), _LIMITS)
    assert str(one.value) == (
        "big.bin is 101 bytes, more than LAKANA_ARTIFACTS__MAX_FILE_BYTES (100). "
        "Store a smaller file, or ask the operator to raise the setting."
    )
    with pytest.raises(ValidationError) as total:
        parse_manifest(_doc([_file("a", 100, _A), _file("b", 51, _B)]), _LIMITS)
    assert str(total.value) == (
        "The artifact is 151 bytes, more than LAKANA_ARTIFACTS__MAX_BYTES (150). "
        "Store less, or ask the operator to raise the setting."
    )
    assert (
        parse_manifest(_doc([_file("a", 100, _A), _file("b", 50, _B)]), _LIMITS).size_bytes == 150
    )


def test_limits_come_from_the_settings() -> None:
    limits = ManifestLimits.from_config(ArtifactsConfig())
    assert limits == ManifestLimits(
        max_file_bytes=2147483648, max_bytes=10737418240, max_files=10000
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "x",
        {"format": 1, "is_directory": True},
        {"format": 1, "is_directory": True, "files": [], "extra": 1},
        {"format": 1, "is_directory": "yes", "files": [_file("a")]},
        {"format": 1, "is_directory": True, "files": "a"},
    ],
)
def test_a_document_of_the_wrong_shape_is_refused(document: object) -> None:
    with pytest.raises(ValidationError, match="exactly the keys files, format and is_directory"):
        parse_manifest(document, _LIMITS)


@pytest.mark.parametrize("version", [2, 0, "1", True, None])
def test_an_unknown_format_says_what_to_upgrade(version: object) -> None:
    with pytest.raises(ValidationError, match="this server reads format 1"):
        parse_manifest({"format": version, "is_directory": True, "files": [_file("a")]}, _LIMITS)


def test_an_empty_artifact_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"An artifact must contain at least one file\."):
        parse_manifest(_doc([]), _LIMITS)


@pytest.mark.parametrize(
    "entry",
    [
        "a",
        {"path": "a", "size": 1},
        {"path": "a", "size": 1, "sha256": _A, "mode": 0o644},
        {"path": "a", "size": -1, "sha256": _A},
        {"path": "a", "size": True, "sha256": _A},
        {"path": "a", "size": 1.0, "sha256": _A},
        {"path": "a", "size": 1, "sha256": _A.upper()},
        {"path": "a", "size": 1, "sha256": _A[:-1]},
        {"path": "a", "size": 1, "sha256": 7},
    ],
)
def test_a_malformed_file_entry_is_refused(entry: object) -> None:
    with pytest.raises(ValidationError):
        parse_manifest(_doc([entry]), _LIMITS)  # ty: ignore[invalid-argument-type]


def test_files_must_be_sorted_and_distinct() -> None:
    for files in ([_file("b"), _file("a")], [_file("a"), _file("a")]):
        with pytest.raises(ValidationError, match="must be sorted by path, without repeats"):
            parse_manifest(_doc(files), _LIMITS)


def test_a_single_file_artifact_is_one_file_name() -> None:
    assert (
        parse_manifest(_doc([_file("model.json")], is_directory=False), _LIMITS).is_directory
        is False
    )
    for files in ([_file("a"), _file("b")], [_file("sub/model.json")]):
        with pytest.raises(ValidationError, match="exactly one file whose path is a file name"):
            parse_manifest(_doc(files, is_directory=False), _LIMITS)


def test_one_hash_has_one_size() -> None:
    with pytest.raises(
        ValidationError, match=r"Files a and b have the same SHA-256 and different sizes\."
    ):
        parse_manifest(_doc([_file("a", 1, _A), _file("b", 2, _A)]), _LIMITS)
    assert parse_manifest(_doc([_file("a", 1, _A), _file("b", 1, _A)]), _LIMITS).hashes() == {_A: 1}


def test_the_canonical_bytes_are_computed_independently() -> None:
    """The digest worked out here and not read back from the code."""
    files = [("a/b.bin", 3, _A), ("model.json", 14, _MODEL)]
    expected = (
        '{"files":[{"path":"a/b.bin","sha256":"' + _A + '","size":3},'
        '{"path":"model.json","sha256":"' + _MODEL + '","size":14}],'
        '"format":1,"is_directory":true}'
    ).encode("ascii")
    assert canonical_manifest(True, files) == expected
    assert manifest_digest(True, files) == hashlib.sha256(expected).hexdigest()


def test_the_canonical_form_is_ascii_whatever_the_path() -> None:
    files = [("é/\u4e2d.bin", 1, _A)]
    assert b'"path":"\\u00e9/\\u4e2d.bin"' in canonical_manifest(False, files)


def test_the_order_of_the_input_does_not_change_the_digest() -> None:
    files = [("a/b.bin", 3, _A), ("model.json", 14, _MODEL), ("z", 1, _B)]
    assert manifest_digest(True, list(reversed(files))) == manifest_digest(True, files)
    assert canonical_manifest(True, files[::-1]) == canonical_manifest(True, files)
    unsorted = _doc([_file("b"), _file("a")])
    with pytest.raises(ValidationError, match="must be sorted by path"):
        parse_manifest(unsorted, _LIMITS)


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("./a", "must not have an empty, '.' or '..' segment"),
        (".", "must not have an empty, '.' or '..' segment"),
        ("..", "must not have an empty, '.' or '..' segment"),
        ("a/..", "must not have an empty, '.' or '..' segment"),
        ("a/b/", "must not have an empty, '.' or '..' segment"),
        ("a/./", "must not have an empty, '.' or '..' segment"),
        ("//a", "must be relative, without a leading '/'"),
        ("/", "must be relative, without a leading '/'"),
        ("C:\\x", "must not contain a backslash; separate segments with '/'"),
        ("..\\x", "must not contain a backslash; separate segments with '/'"),
        ("C:/x", "must not contain ':'"),
        ("z:", "must not contain ':'"),
        ("a\u200db", _UNSAFE_TEXT),
        ("a\u200bb", _UNSAFE_TEXT),
        ("\u202ea", _UNSAFE_TEXT),
        ("a\u2066b", _UNSAFE_TEXT),
        ("\ufeffa", _UNSAFE_TEXT),
        ("a\x7fb", _UNSAFE_TEXT),
        ("a\x85b", _UNSAFE_TEXT),
        ("a\rb", _UNSAFE_TEXT),
        ("a\tb", _UNSAFE_TEXT),
        ("a\x1b[31mb", _UNSAFE_TEXT),
        ("a\u2029b", _UNSAFE_TEXT),
        ("\ud83d\ude00", _UNSAFE_TEXT),
        ("a\udfffb", _UNSAFE_TEXT),
        ("a\x00/../b", _UNSAFE_TEXT),
        ("é" * 127 + "x/" + "é" * 127 + "x", None),
        ("é" * 512, _LONG_PART),
        ("é" * 512 + "a", "is longer than 1024 bytes"),
        ("\U0001f600" * 257, "is longer than 1024 bytes"),
        ("/".join(["a"] * 32), None),
    ],
)
def test_more_hostile_paths(path: str, problem: str | None) -> None:
    assert path_problem(path) == problem


def test_a_refusal_names_the_path_cleaned_for_display() -> None:
    path = "ok/\x1b[2J\u202e\nforged line"
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc([_file(path)]), _LIMITS)
    text = str(caught.value)
    assert text.isascii()
    assert "\n" not in text
    assert "\x1b" not in text
    assert text.startswith("Path ok/\\x1b[2J\\u202e\\nforged line must not contain")
    long = "x" * 1025
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc([_file(long)]), _LIMITS)
    assert len(str(caught.value)) < 300


def test_a_collision_names_both_paths_cleaned_for_display() -> None:
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc([_file("Caf\u00e9"), _file("cafe\u0301")]), _LIMITS)
    assert str(caught.value) == (
        "Paths Caf\\xe9 and cafe\\u0301 differ only by case or Unicode normalisation; rename one."
    )


@pytest.mark.parametrize(
    "paths",
    [
        ["a", "a/b"],
        ["a/b", "a/b/c"],
        ["x/Ünï/a", "x/U\u0308nï/b"],
        ["a/B/c", "a/b/d"],
    ],
)
def test_more_collisions(paths: list[str]) -> None:
    assert paths_problem(sorted(paths, key=lambda p: p.encode())) is not None


def test_nested_directory_prefixes_that_do_not_collide_pass() -> None:
    assert paths_problem(["a/b/c", "a/b/d", "a/e/f", "ab/c", "b"]) is None


def test_the_directory_check_is_the_same_as_folding_each_prefix() -> None:
    """The linear implementation must agree with folding every prefix whole."""
    for path in ("A/ß/E\u0301/x", "İ/ǅ/ΐ/x", "ǆ/\u1e9e/a"):
        segments = path.split("/")
        for depth in range(1, len(segments) + 1):
            prefix = "/".join(segments[:depth])
            assert fold_path(prefix) == "/".join(fold_path(s) for s in segments[:depth])


@pytest.mark.parametrize("size", [-1, 1.5, "1", None, True, False, [1], 2.0**70])
def test_a_size_that_is_not_a_natural_number_is_refused(size: object) -> None:
    with pytest.raises(ValidationError, match="File a must have exactly the keys"):
        parse_manifest(_doc([{"path": "a", "size": size, "sha256": _A}]), _LIMITS)


@pytest.mark.parametrize(
    "digest",
    [
        _A + "\n",
        "\n" + _A[1:],
        _A + "0",
        "g" * 64,
        " " + _A[1:],
        "",
        _A.upper(),
        "é" * 64,
        None,
        b"a",
    ],
)
def test_a_hash_must_be_64_lower_case_hex_characters(digest: object) -> None:
    with pytest.raises(ValidationError, match="File a must have exactly the keys"):
        parse_manifest(_doc([{"path": "a", "size": 1, "sha256": digest}]), _LIMITS)


@pytest.mark.parametrize("size", [10**20, 10**40, 10**5000], ids=["1e20", "1e40", "1e5000"])
def test_a_huge_size_is_a_limit_not_a_crash(size: int) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc([_file("a", size)]), _LIMITS)
    assert str(caught.value) == (
        "a is more than 10^20 bytes, more than LAKANA_ARTIFACTS__MAX_FILE_BYTES (100). "
        "Store a smaller file, or ask the operator to raise the setting."
    )
    with pytest.raises(ValidationError) as shape_only:
        parse_manifest(_doc([_file("a", size)]))
    assert str(shape_only.value) == (
        "File a is 10^20 bytes or more, which no artifact can hold. Store a smaller file."
    )


def test_a_size_just_below_the_cap_is_shown_whole_and_read_back() -> None:
    size = 10**20 - 1
    with pytest.raises(ValidationError, match=f"a is {size} bytes, more than"):
        parse_manifest(_doc([_file("a", size)]), _LIMITS)
    assert parse_manifest(_doc([_file("a", size)])).size_bytes == size


def test_a_total_of_huge_files_is_shown_safely() -> None:
    limits = ManifestLimits(max_file_bytes=10**19, max_bytes=10**20, max_files=100)
    names = [f"f{i:02d}" for i in range(11)]
    files = [_file(n, 10**19, hashlib.sha256(n.encode()).hexdigest()) for n in names]
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc(files), limits)
    assert str(caught.value) == (
        "The artifact is more than 10^20 bytes, more than LAKANA_ARTIFACTS__MAX_BYTES "
        "(100000000000000000000). Store less, or ask the operator to raise the setting."
    )


@pytest.mark.parametrize(
    "version",
    [1.0, 10**5000, [1], {"a": 1}, "x" * 500],
    ids=["float", "huge-int", "list", "dict", "long-string"],
)
def test_a_format_that_only_equals_one_or_is_odd_is_refused_without_a_crash(
    version: object,
) -> None:
    with pytest.raises(ValidationError, match="this server reads format 1") as caught:
        parse_manifest({"format": version, "is_directory": True, "files": [_file("a")]}, _LIMITS)
    assert len(str(caught.value)) < 400


@pytest.mark.parametrize("entry", ["a", None, [1, 2], 3])
def test_an_entry_that_is_not_an_object_has_its_own_message(entry: object) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc([entry]), _LIMITS)  # ty: ignore[invalid-argument-type]
    assert str(caught.value) == (
        "Each file in a manifest must be an object with 'path', 'size' and 'sha256'."
    )


def test_a_string_format_is_shown_quoted() -> None:
    with pytest.raises(ValidationError) as caught:
        parse_manifest({"format": "1", "is_directory": True, "files": [_file("a")]}, _LIMITS)
    assert str(caught.value) == (
        "Manifest format '1' is not supported; this server reads format 1. "
        "Upgrade the lakana package that wrote it, or this server."
    )
    with pytest.raises(ValidationError) as number:
        parse_manifest({"format": 2, "is_directory": True, "files": [_file("a")]}, _LIMITS)
    assert str(number.value).startswith("Manifest format 2 is not supported;")


def test_the_canonical_order_is_the_utf8_byte_order() -> None:
    """Pins the sort key against UTF-16 order, which puts U+10000 before U+FFFF."""
    low, high = "\uffff", "\U00010000"
    assert path_problem(low) is None
    assert path_problem(high) is None
    files = [(high, 1, _A), (low, 2, _B)]
    canonical = canonical_manifest(True, files)
    assert canonical.index(b"\\uffff") < canonical.index(b"\\ud800\\udc00")
    assert canonical == canonical_manifest(True, files[::-1])
    assert manifest_digest(True, files) == hashlib.sha256(canonical).hexdigest()


_WINDOWS_COLON = "must not contain ':'"
_WINDOWS_END = "must not have a part ending in '.' or a space"
_WINDOWS_NAME = "must not use a name Windows reserves, such as 'NUL'"


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("a/C:x", _WINDOWS_COLON),
        ("x/y:stream", _WINDOWS_COLON),
        ("C:/x", _WINDOWS_COLON),
        (":", _WINDOWS_COLON),
        ("a./b", _WINDOWS_END),
        ("a /b", _WINDOWS_END),
        ("x/a.", _WINDOWS_END),
        ("x/a ", _WINDOWS_END),
        ("x/a..", _WINDOWS_END),
        ("dir/NUL", _WINDOWS_NAME),
        ("nul.txt", _WINDOWS_NAME),
        ("Con", _WINDOWS_NAME),
        ("com1.tar.gz", _WINDOWS_NAME),
        ("PRN", _WINDOWS_NAME),
        ("aux.x", _WINDOWS_NAME),
        ("COM0", _WINDOWS_NAME),
        ("lpt9", _WINDOWS_NAME),
        ("COM\u00b9", _WINDOWS_NAME),
        ("lpt\u00b2.txt", _WINDOWS_NAME),
        ("COM\u00b3", _WINDOWS_NAME),
        ("NUL .txt", _WINDOWS_NAME),
        ("a/b/Nul/c", _WINDOWS_NAME),
        ("conin$", _WINDOWS_NAME),
        ("sub/CONOUT$.txt", _WINDOWS_NAME),
        ("CONIN$", _WINDOWS_NAME),
        ("a\x00:", _UNSAFE_TEXT),
        ("a:/..", "must not have an empty, '.' or '..' segment"),
        ("a./..", "must not have an empty, '.' or '..' segment"),
    ],
)
def test_windows_hazards_are_refused_with_their_own_text(path: str, problem: str) -> None:
    assert path_problem(path) == problem
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc([_file(path)]), _LIMITS)
    assert str(caught.value) == f"Path {shown(path)} {problem}."


@pytest.mark.parametrize(
    "path",
    [
        "console",
        "nul_1",
        "con1",
        "com10",
        "com",
        "lpt",
        "COM\u2074",
        "a.nul",
        "NU\u017f",
        "nul\u0131",
        "conin",
        "x/.hidden",
        'a<b>c|d?e*f"g',
        "my file.txt",
        "a b/c",
    ],
)
def test_near_misses_of_the_windows_rules_pass(path: str) -> None:
    assert path_problem(path) is None


def test_the_file_count_limit_is_checked_before_any_file_is_read() -> None:
    entries: list[object] = [None] * 10_001
    with pytest.raises(ValidationError) as caught:
        parse_manifest(_doc(entries), ManifestLimits.from_config(ArtifactsConfig()))  # ty: ignore[invalid-argument-type]
    assert str(caught.value) == (
        "The artifact has 10001 files, more than LAKANA_ARTIFACTS__MAX_FILES (10000). "
        "Store fewer files, or ask the operator to raise the setting."
    )


def _deep_paths(count: int) -> list[str]:
    """Build distinct, sorted, 32-segment paths of about 1,000 bytes.

    Parameters
    ----------
    count
        How many paths.

    Returns
    -------
    list of str
        Paths sharing no directory prefix, so every prefix is new work.
    """
    paths = []
    for i in range(count):
        segments = [f"{i:05d}_{level:02d}".ljust(31, "x") for level in range(32)]
        paths.append("/".join(segments))
    return sorted(paths, key=lambda p: p.encode("utf-8"))


@pytest.mark.slow
def test_validating_a_manifest_at_the_file_count_limit_is_fast() -> None:
    paths = _deep_paths(10_000)
    assert max(len(p.encode()) for p in paths) <= 1024
    document = _doc([_file(p, 1, hashlib.sha256(p.encode()).hexdigest()) for p in paths])
    limits = ManifestLimits.from_config(ArtifactsConfig())
    started = time.perf_counter()
    manifest = parse_manifest(document, limits)
    elapsed = time.perf_counter() - started
    assert manifest.file_count == 10_000
    # About 1 s untraced; under coverage's C tracer 5 s on a desktop and 13 s on a CI runner
    # that shares two cores between two test processes. The limit is there to catch
    # work that grows with the square of the file count, which takes minutes.
    assert elapsed < 60.0, f"validation took {elapsed:.1f}s"


def test_validating_many_flat_and_colliding_paths_is_fast() -> None:
    flat = sorted((f"f{i:05d}" + "y" * 1000 for i in range(10_000)), key=lambda p: p.encode())
    started = time.perf_counter()
    assert paths_problem(flat) is None
    clash = [*flat, "F00000" + "y" * 1000]
    assert paths_problem(clash) is not None
    assert time.perf_counter() - started < 10.0
