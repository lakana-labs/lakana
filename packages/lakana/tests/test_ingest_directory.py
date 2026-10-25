"""`lakana.ingest._directory`: the walker reads below an ingestion root and never leaves it."""

from __future__ import annotations

import datetime as dt
import os
import socket
from pathlib import Path

import pytest

from lakana.ingest import CapExceeded
from lakana.ingest._directory import (
    MAX_DEPTH,
    DirectoryError,
    RootUnavailable,
    matches,
    pattern_segments,
    read,
    walk,
)

_OUTSIDE = b"outside-the-root"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Build a root with ``exports/`` below it and a secret file outside the root.

    Parameters
    ----------
    tmp_path
        pytest's directory.

    Returns
    -------
    Path
        The root directory; ``tmp_path / "outside"`` holds ``secret.csv``.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_bytes(_OUTSIDE)
    root = tmp_path / "root"
    (root / "exports" / "2026" / "10").mkdir(parents=True)
    (root / "exports" / "a.csv").write_bytes(b"id\n1\n")
    (root / "exports" / "b.csv").write_bytes(b"id\n2\n3\n")
    (root / "exports" / "notes.txt").write_bytes(b"x")
    (root / "exports" / "2026" / "10" / "c.csv").write_bytes(b"id\n4\n")
    stamp = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC).timestamp()
    os.utime(root / "exports" / "a.csv", (stamp, stamp))
    return root


def test_a_pattern_lists_regular_files_below_the_base(tree: Path) -> None:
    """`*.csv` and `**/*.csv` find the files; sizes and UTC mtimes are right."""
    flat = walk(str(tree), "exports", "*.csv", max_files=10)
    deep = walk(str(tree), "exports", "**/*.csv", max_files=10)
    below = walk(str(tree), "", "exports/*/*/*.csv", max_files=10)

    assert [f.path for f in flat.files] == ["a.csv", "b.csv"]
    assert [f.path for f in deep.files] == ["2026/10/c.csv", "a.csv", "b.csv"]
    assert [f.path for f in below.files] == ["exports/2026/10/c.csv"]
    first = flat.files[0]
    assert first.size == 5
    assert first.modified == dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)
    assert flat.skipped == deep.skipped == []


def test_a_link_anywhere_is_listed_as_skipped_and_never_followed(tree: Path) -> None:
    """A file link and a directory link out of the root are skipped and never read."""
    outside = tree.parent / "outside"
    (tree / "exports" / "link.csv").symlink_to(outside / "secret.csv")
    (tree / "exports" / "away").symlink_to(outside)

    listing = walk(str(tree), "exports", "**/*.csv", max_files=10)

    assert [f.path for f in listing.files] == ["2026/10/c.csv", "a.csv", "b.csv"]
    assert listing.skipped == ["away", "link.csv"]
    with pytest.raises(DirectoryError, match="symbolic link"):
        read(str(tree), "exports", "link.csv", max_bytes=100)
    with pytest.raises(DirectoryError, match="symbolic link"):
        read(str(tree), "exports", "away/secret.csv", max_bytes=100)


def test_a_link_as_the_base_or_the_root_is_refused(tree: Path) -> None:
    """A base that is a link, or a root that is one, is refused; nothing below is read."""
    (tree / "via").symlink_to(tree.parent / "outside")
    (tree.parent / "rootlink").symlink_to(tree)

    with pytest.raises(DirectoryError, match="'via' is a symbolic link"):
        walk(str(tree), "via", "*.csv", max_files=10)
    with pytest.raises(RootUnavailable):
        walk(str(tree.parent / "rootlink"), "exports", "*.csv", max_files=10)


@pytest.mark.parametrize(
    ("base", "pattern"),
    [
        ("exports", "../outside/*.csv"),
        ("exports", "a/../../x"),
        ("exports/..", "*.csv"),
        ("../outside", "*.csv"),
        ("/etc", "*"),
        ("exports", "/etc/*"),
        ("exports", "a\x00b"),
    ],
)
def test_dotdot_an_absolute_path_or_a_nul_is_refused(tree: Path, base: str, pattern: str) -> None:
    """`..`, an absolute path or a NUL in a base or a pattern: refused before any open."""
    with pytest.raises(DirectoryError, match="must be relative"):
        walk(str(tree), base, pattern, max_files=10)


def test_dotdot_in_a_read_is_refused(tree: Path) -> None:
    """`read` refuses a relative path with `..` the same way."""
    with pytest.raises(DirectoryError, match="must be relative"):
        read(str(tree), "exports", "../../outside/secret.csv", max_bytes=100)


def test_an_empty_pattern_is_refused(tree: Path) -> None:
    """An empty pattern would match nothing; the message gives an example."""
    with pytest.raises(DirectoryError, match=r"\*\.csv"):
        walk(str(tree), "exports", "", max_files=10)


def test_fifos_and_sockets_are_skipped_and_never_opened(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIFO and a Unix socket that match are skipped; reading the FIFO does not block."""
    os.mkfifo(tree / "exports" / "pipe.csv")
    monkeypatch.chdir(tree / "exports")
    server = socket.socket(socket.AF_UNIX)
    server.bind("sock.csv")
    try:
        listing = walk(str(tree), "exports", "*.csv", max_files=10)

        assert [f.path for f in listing.files] == ["a.csv", "b.csv"]
        assert listing.skipped == ["pipe.csv", "sock.csv"]
        with pytest.raises(DirectoryError, match="not a regular file"):
            read(str(tree), "exports", "pipe.csv", max_bytes=100)
    finally:
        server.close()


def test_a_file_swapped_for_a_link_after_listing_is_refused(tree: Path) -> None:
    """`walk`, then replace the file with a link to a file outside, then `read`: refused."""
    listing = walk(str(tree), "exports", "*.csv", max_files=10)
    (tree / "exports" / "a.csv").unlink()
    (tree / "exports" / "a.csv").symlink_to(tree.parent / "outside" / "secret.csv")

    with pytest.raises(DirectoryError, match="symbolic link") as caught:
        read(str(tree), "exports", listing.files[0].path, max_bytes=100)
    assert _OUTSIDE.decode() not in str(caught.value)


def test_a_directory_swapped_for_a_link_after_listing_is_refused(tree: Path) -> None:
    """A directory on the file's path replaced by a link: the read is refused."""
    (tree / "exports" / "2026" / "10").rename(tree / "moved")
    (tree / "exports" / "2026" / "10").symlink_to(tree.parent / "outside")

    with pytest.raises(DirectoryError, match="symbolic link"):
        read(str(tree), "exports", "2026/10/secret.csv", max_bytes=100)


def test_the_cap_stops_reading(tree: Path) -> None:
    """A file larger than `max_bytes` raises CapExceeded naming the file and the cap."""
    assert read(str(tree), "exports", "b.csv", max_bytes=7) == b"id\n2\n3\n"
    with pytest.raises(CapExceeded, match=r"'b.csv' is 7 bytes, more than max_file_bytes \(6\)"):
        read(str(tree), "exports", "b.csv", max_bytes=6)


def test_max_files_names_the_cap_and_the_pattern(tree: Path) -> None:
    """More matches than `max_files` stop with a message naming the cap and the pattern."""
    with pytest.raises(CapExceeded, match=r"'\*\*/\*.csv' matches more than max_files \(2\)"):
        walk(str(tree), "exports", "**/*.csv", max_files=2)


def test_a_directory_deeper_than_the_limit_is_listed_as_skipped(tmp_path: Path) -> None:
    """A file `MAX_DEPTH` levels down is found; the directory one level deeper is skipped."""
    deep = tmp_path.joinpath(*["d"] * (MAX_DEPTH - 1))
    (deep / "d").mkdir(parents=True)
    (deep / "found.csv").write_bytes(b"x")
    (deep / "d" / "lost.csv").write_bytes(b"x")

    listing = walk(str(tmp_path), "", "**/*.csv", max_files=10)

    assert [f.path for f in listing.files] == ["/".join(["d"] * (MAX_DEPTH - 1) + ["found.csv"])]
    assert listing.skipped == ["/".join(["d"] * MAX_DEPTH) + "/"]


def test_the_walk_descends_only_where_the_pattern_can_match(tree: Path) -> None:
    """A directory the pattern cannot reach is never listed, even when it cannot be read."""
    locked = tree / "exports" / "private"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert [f.path for f in walk(str(tree), "exports", "*.csv", max_files=10).files] == [
            "a.csv",
            "b.csv",
        ]
        if os.geteuid() != 0:
            with pytest.raises(DirectoryError, match=r"may not read 'private'.*narrow the pattern"):
                walk(str(tree), "exports", "**/*.csv", max_files=10)
    finally:
        locked.chmod(0o755)


def test_a_missing_root_is_unavailable(tmp_path: Path) -> None:
    """A root that does not exist raises `RootUnavailable` (the caller adds the fixed text)."""
    with pytest.raises(RootUnavailable, match="is not available"):
        walk(str(tmp_path / "nowhere"), "exports", "*.csv", max_files=10)


def test_a_missing_base_names_it(tree: Path) -> None:
    """A base that does not exist under the root names the missing component."""
    with pytest.raises(DirectoryError, match="'imports' does not exist"):
        walk(str(tree), "imports", "*.csv", max_files=10)


def test_the_errors_are_not_retried(tree: Path) -> None:
    """Every attempt would refuse the same path again: the harness fails the task at once."""
    assert DirectoryError.retryable is False
    assert RootUnavailable.retryable is False


def test_a_pattern_deeper_than_the_limit_is_refused(tree: Path) -> None:
    """A pattern with more segments than `MAX_DEPTH` could never match: refused."""
    with pytest.raises(DirectoryError, match=f"more than {MAX_DEPTH} segments"):
        walk(str(tree), "exports", "/".join(["*"] * (MAX_DEPTH + 1)), max_files=10)


def test_a_base_that_is_a_file_names_it(tree: Path) -> None:
    """A base whose component is a regular file is not a directory."""
    with pytest.raises(DirectoryError, match=r"'a\.csv' is not a directory"):
        walk(str(tree), "exports/a.csv", "*", max_files=10)


def test_a_directory_whose_name_matches_is_not_a_file(tree: Path) -> None:
    """A directory named like a file the pattern wants is neither read nor skipped."""
    (tree / "exports" / "folder.csv").mkdir()

    listing = walk(str(tree), "exports", "*.csv", max_files=10)

    assert [f.path for f in listing.files] == ["a.csv", "b.csv"]
    assert listing.skipped == []


def test_the_walk_is_bounded_in_entries(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A walk that looks at more than `MAX_ENTRIES` entries stops, saying to narrow."""
    monkeypatch.setattr("lakana.ingest._directory.MAX_ENTRIES", 3)

    with pytest.raises(DirectoryError, match="more than 3 entries; narrow the pattern"):
        walk(str(tree), "exports", "**/*.csv", max_files=10)


def test_a_missing_file_names_it(tree: Path) -> None:
    """Reading a file that is gone since the walk names it."""
    with pytest.raises(
        DirectoryError, match=r"'gone\.csv' cannot be read: 'gone\.csv' does not exist"
    ):
        read(str(tree), "exports", "gone.csv", max_bytes=100)


def test_a_read_without_a_file_is_refused(tree: Path) -> None:
    """`read` of the base itself is refused."""
    with pytest.raises(DirectoryError, match="a file path is needed"):
        read(str(tree), "exports", ".", max_bytes=100)


def test_a_directory_swapped_for_a_link_during_the_walk_is_skipped(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory replaced by a link between its `lstat` and its open is skipped, not followed."""
    real_open = os.open

    def swapping_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if path == "2026" and "dir_fd" in kwargs:
            (tree / "exports" / "2026").rename(tree / "moved")
            (tree / "exports" / "2026").symlink_to(tree.parent / "outside")
        return real_open(path, flags, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(os, "open", swapping_open)

    listing = walk(str(tree), "exports", "**/*.csv", max_files=10)

    assert [f.path for f in listing.files] == ["a.csv", "b.csv"]
    assert listing.skipped == ["2026"]


def test_a_directory_that_cannot_be_listed_names_it(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that opens but cannot be listed names itself and the next step."""

    def refuse(fd: int) -> list[str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "listdir", refuse)

    with pytest.raises(DirectoryError, match=r"'\.' cannot be listed .*read access"):
        walk(str(tree), "exports", "*.csv", max_files=10)


def test_a_file_that_grows_while_it_is_read_stops_at_the_cap(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap holds even when the size `fstat` saw was smaller than what is read."""
    real_fstat = os.fstat

    def shrunk(fd: int) -> os.stat_result:
        found = real_fstat(fd)
        return os.stat_result((found.st_mode, *[0] * 5, 1, *[0] * 3))

    monkeypatch.setattr(os, "fstat", shrunk)

    with pytest.raises(CapExceeded, match=r"grew past max_file_bytes \(6\)"):
        read(str(tree), "exports", "b.csv", max_bytes=6)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.csv", "a.csv", True),
        ("*.csv", "x/a.csv", False),
        ("**/*.csv", "a.csv", True),
        ("**/*.csv", "x/y/a.csv", True),
        ("x/**", "x/y/z", True),
        ("x/**/z/*.csv", "x/z/a.csv", True),
        ("x/**/z/*.csv", "x/q/z/r/a.csv", False),
        ("2026-??/*.parquet", "2026-10/p.parquet", True),
        ("[ab].csv", "c.csv", False),
    ],
)
def test_matches_is_the_walks_glob(pattern: str, path: str, expected: bool) -> None:
    """The object stores match keys with the same rules as the walk."""
    assert matches(pattern_segments(pattern), path.split("/")) is expected


def test_a_modification_time_is_kept_to_the_microsecond(tree: Path) -> None:
    """Nanoseconds are cut, so a stored watermark compares equal to the file's time."""
    target = tree / "exports" / "a.csv"
    os.utime(target, ns=(1_790_000_000_123_456_789, 1_790_000_000_123_456_789))

    (first,) = walk(str(tree), "exports", "a.csv", max_files=1).files

    assert first.modified == dt.datetime(2026, 9, 21, 14, 13, 20, 123_456, tzinfo=dt.UTC)
