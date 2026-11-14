"""Turning a local file or directory into a manifest (what the harness refuses)."""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import sys
from pathlib import Path

import pytest
from scripts.pytest_memory_limit import open_descriptors

from lakana import _artifact_manifest as artifact_manifest
from lakana._artifact_manifest import (
    LocalArtifact,
    ManifestError,
    ScanLimits,
    _hashed,
    manifest_digest,
    scan,
)


def test_a_file_is_a_single_file_artifact(tmp_path: Path) -> None:
    target = tmp_path / "model.json"
    target.write_bytes(b'{"factor": 2}\n')
    found = scan(target)
    assert found.is_directory is False
    assert [(f.path, f.source, f.size) for f in found.files] == [("model.json", str(target), 14)]
    assert found.digest() == "b97616016d0d6bd401c61c158cbf7405bc346d3d6ee6c22f8ed4099a3fe87c81"
    assert found.document() == {
        "format": 1,
        "is_directory": False,
        "files": [{"path": "model.json", "size": 14, "sha256": found.files[0].sha256}],
    }


def test_a_directory_is_walked_in_canonical_order(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "w.bin").write_bytes(b"w")
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "Z.txt").write_bytes(b"z")
    (tmp_path / "empty").mkdir()
    found = scan(tmp_path)
    assert found.is_directory is True
    assert [f.path for f in found.files] == ["Z.txt", "a.txt", "b/w.bin"]
    assert found.files[2].sha256 == hashlib.sha256(b"w").hexdigest()
    assert found.digest() == manifest_digest(
        True, [(f.path, f.size, f.sha256) for f in found.files]
    )


def test_scan_refuses_a_symbolic_link_and_names_it(tmp_path: Path) -> None:
    """A link is never followed, wherever it sits."""
    secret = tmp_path / "secret"
    secret.write_bytes(b"not yours")
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    (out / "model.json").write_bytes(b"m")
    (out / "sub" / "leak").symlink_to(secret)
    with pytest.raises(ManifestError) as file_link:
        scan(out)
    assert str(file_link.value) == (
        "sub/leak is a symbolic link; an artifact holds only regular files and directories. "
        "Copy what it points to instead."
    )
    (out / "sub" / "leak").unlink()
    (out / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ManifestError, match="linked is a symbolic link"):
        scan(out)
    top = tmp_path / "top"
    top.symlink_to(out, target_is_directory=True)
    with pytest.raises(ManifestError) as linked_dir:
        scan(top)
    assert "Copy what it points to instead." in str(linked_dir.value)
    assert "Copy the file" not in str(linked_dir.value)
    alias = tmp_path / "alias.json"
    alias.symlink_to(secret)
    with pytest.raises(ManifestError, match="is a symbolic link"):
        scan(alias)


def test_scan_refuses_what_is_not_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "model.json").write_bytes(b"m")
    os.mkfifo(out / "pipe")
    with pytest.raises(ManifestError) as pipe:
        scan(out)
    assert str(pipe.value) == (
        "pipe is not a regular file (a device, a socket or a pipe); an artifact holds only "
        "regular files."
    )
    (out / "pipe").unlink()
    monkeypatch.chdir(out)  # a relative name: macOS caps a socket's path at 104 bytes
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind("sock")
        with pytest.raises(ManifestError, match="sock is not a regular file"):
            scan(out)


def test_scan_refuses_a_hard_link_from_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"shared inode")
    out = tmp_path / "out"
    out.mkdir()
    os.link(outside, out / "linked.bin")
    with pytest.raises(ManifestError) as caught:
        scan(out)
    assert str(caught.value) == (
        "linked.bin is hard-linked from outside the directory; copy the file instead."
    )
    (out / "linked.bin").unlink()
    (out / "a.bin").write_bytes(b"same")
    os.link(out / "a.bin", out / "b.bin")
    assert [f.path for f in scan(out).files] == ["a.bin", "b.bin"]
    os.link(outside, tmp_path / "single.bin")
    with pytest.raises(ManifestError) as single:
        scan(tmp_path / "single.bin")
    assert str(single.value) == (
        "single.bin has another hard link, so its content is reachable under a second name; "
        "copy the file and give the copy."
    )


def test_scan_reports_names_the_server_would_refuse(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "a\\b").write_bytes(b"x")
    with pytest.raises(ManifestError) as caught:
        scan(out)
    assert str(caught.value) == (
        "a\\\\b must not contain a backslash; separate segments with '/'; rename it."
    )
    (out / "a\\b").unlink()
    (out / "Model.bin").write_bytes(b"1")
    (out / "model.bin").write_bytes(b"2")
    if len(os.listdir(out)) == 2:  # a case-sensitive file system
        with pytest.raises(ManifestError, match="differ only by case or Unicode normalisation"):
            scan(out)


def test_scan_refuses_nothing_and_missing(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as absent:
        scan(tmp_path / "nope")
    assert str(absent.value).endswith(
        "nope does not exist. Check the path, then run the upload again."
    )
    (tmp_path / "only-dirs" / "x").mkdir(parents=True)
    with pytest.raises(ManifestError) as empty:
        scan(tmp_path / "only-dirs")
    assert str(empty.value) == (
        "The directory has no files; an artifact must contain at least one file."
    )
    assert isinstance(LocalArtifact(False, ()), LocalArtifact)
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ManifestError, match="is not a regular file"):
        scan(tmp_path / "pipe")


def test_a_file_that_changes_under_the_scan_is_refused(tmp_path: Path) -> None:
    """Between the walk and the hash a file may vanish or be replaced; neither is followed."""
    with pytest.raises(ManifestError, match=r"gone\.bin could not be read"):
        _hashed(str(tmp_path / "gone.bin"), "gone.bin")
    (tmp_path / "now-a-directory").mkdir()
    with pytest.raises(ManifestError, match="now-a-directory is not a regular file"):
        _hashed(str(tmp_path / "now-a-directory"), "now-a-directory")
    os.mkfifo(tmp_path / "now-a-pipe")
    with pytest.raises(ManifestError, match="now-a-pipe is not a regular file"):
        _hashed(str(tmp_path / "now-a-pipe"), "now-a-pipe")


# --- what the scan must refuse or notice beyond the brief (point I) -------------------------


def test_hidden_files_are_included(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"x")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "w").write_bytes(b"y")
    assert [f.path for f in scan(tmp_path).files] == [".cache/w", ".env"]


@pytest.mark.parametrize(
    ("name", "fragment"),
    [
        ("ckpt:2024", "must not contain ':'"),
        ("NUL.txt", "must not use a name Windows reserves"),
        ("trailing.", "must not have a part ending in '.' or a space"),
        ("tab\tname", "must not contain a control"),
    ],
)
def test_names_windows_cannot_hold_are_refused_before_any_request(
    tmp_path: Path, name: str, fragment: str
) -> None:
    (tmp_path / name).write_bytes(b"x")
    with pytest.raises(ManifestError) as caught:
        scan(tmp_path)
    assert fragment in str(caught.value)
    assert str(caught.value).endswith("; rename it.")


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="APFS is normalisation-insensitive: the two names cannot both exist",
)
def test_names_that_differ_only_by_unicode_normalisation_are_refused(tmp_path: Path) -> None:
    (tmp_path / "café.txt").write_bytes(b"1")
    (tmp_path / "café.txt").write_bytes(b"2")
    with pytest.raises(ManifestError, match="differ only by case or Unicode normalisation"):
        scan(tmp_path)


def test_a_directory_with_too_many_files_fails_before_anything_is_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(6):
        (tmp_path / f"f{n}").write_bytes(b"x")

    def never(*_args: object) -> None:
        raise AssertionError("nothing may be hashed once a limit is passed")

    monkeypatch.setattr(artifact_manifest, "_hashed", never)
    with pytest.raises(ManifestError) as caught:
        scan(tmp_path, ScanLimits(max_files=5))
    assert str(caught.value).startswith(
        "The directory has more than the 5 files one artifact may have"
    )
    assert "LAKANA_ARTIFACTS__MAX_FILES" in str(caught.value)


def test_a_tree_of_directories_without_files_is_cut_off_too(tmp_path: Path) -> None:
    for n in range(30):
        (tmp_path / f"d{n}").mkdir()
    with pytest.raises(ManifestError, match="more than 20 entries"):
        scan(tmp_path, ScanLimits(max_files=2))


def test_a_walk_stops_at_the_first_entry_past_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory is read entry by entry, never listed whole first."""
    for n in range(50):
        (tmp_path / f"f{n}").write_bytes(b"x")
    seen = []
    real = os.lstat

    def counting(path: object, *args: object, **kwargs: object) -> os.stat_result:
        seen.append(path)
        return real(path, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(artifact_manifest.os, "lstat", counting)
    with pytest.raises(ManifestError):
        scan(tmp_path, ScanLimits(max_files=3))
    assert len(seen) <= 3 + 2


def test_a_file_or_total_over_the_size_limit_is_refused_with_its_name(tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"123456")
    with pytest.raises(ManifestError) as one:
        scan(tmp_path, ScanLimits(max_file_bytes=5))
    assert str(one.value).startswith("big.bin is 6 bytes, more than the 5 bytes one file may have")
    assert "LAKANA_ARTIFACTS__MAX_FILE_BYTES" in str(one.value)
    (tmp_path / "big.bin").write_bytes(b"123")
    (tmp_path / "more.bin").write_bytes(b"456")
    with pytest.raises(ManifestError, match="add up to more than the 5 bytes"):
        scan(tmp_path, ScanLimits(max_bytes=5))
    with pytest.raises(ManifestError, match="more than the 2 bytes"):
        scan(tmp_path / "big.bin", ScanLimits(max_file_bytes=2))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_directory_is_an_error_not_a_silent_gap(tmp_path: Path) -> None:
    (tmp_path / "ok.bin").write_bytes(b"x")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "secret.bin").write_bytes(b"y")
    locked.chmod(0)
    try:
        with pytest.raises(ManifestError) as caught:
            scan(tmp_path)
    finally:
        locked.chmod(0o700)
    assert str(caught.value).startswith("locked could not be read:")
    assert "permissions" in str(caught.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_file_names_itself_and_the_next_step(tmp_path: Path) -> None:
    target = tmp_path / "locked.bin"
    target.write_bytes(b"y")
    target.chmod(0)
    try:
        with pytest.raises(ManifestError) as caught:
            scan(tmp_path)
    finally:
        target.chmod(0o600)
    assert str(caught.value).startswith("locked.bin could not be read:")
    assert "run the upload again" in str(caught.value)


def test_a_file_that_changed_since_the_walk_is_reported_with_the_next_step(tmp_path: Path) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"one")
    seen = os.lstat(target)
    target.write_bytes(b"three")
    with pytest.raises(ManifestError) as caught:
        _hashed(str(target), "w.bin", seen)
    assert str(caught.value) == (
        "w.bin changed after it was scanned, so nothing was stored from it. Wait until "
        "whatever writes it has finished, then run the upload again."
    )
    assert _hashed(str(target), "w.bin", os.lstat(target)).size == 5


def test_a_file_that_changes_while_it_is_read_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"0123456789")
    monkeypatch.setattr(artifact_manifest, "READ_BYTES", 4)
    real = os.fstat
    calls: list[int] = []

    def growing(descriptor: int) -> os.stat_result:
        calls.append(descriptor)
        info = real(descriptor)
        if len(calls) >= 3:  # the check after the last read sees a file that has grown
            return os.stat_result((*tuple(info)[:6], info.st_size + 5, *tuple(info)[7:]))
        return info

    monkeypatch.setattr(artifact_manifest.os, "fstat", growing)
    with pytest.raises(ManifestError, match="changed after it was scanned"):
        _hashed(str(target), "w.bin")


def test_a_file_that_grows_while_it_is_read_is_cut_off_at_the_size_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"0123456789")
    real = os.fstat
    calls = [0]

    def shrunk(descriptor: int) -> os.stat_result:
        info = real(descriptor)
        calls[0] += 1
        if calls[0] == 2:  # the read sees a file of 4 bytes; 10 are then read
            return os.stat_result((*tuple(info)[:6], 4, *tuple(info)[7:]))
        return info

    monkeypatch.setattr(artifact_manifest.os, "fstat", shrunk)
    with pytest.raises(ManifestError, match="changed after it was scanned"):
        _hashed(str(target), "w.bin")


def test_a_path_given_as_a_pathlike_and_a_relative_directory_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.bin").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    assert [f.path for f in scan(Path(".")).files] == ["a.bin"]


class _FailingRead:
    """A file object whose read fails the way a disk or a network file system can."""

    def __init__(self, real: object) -> None:
        self._real = real

    def __enter__(self) -> _FailingRead:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._real.close()  # ty: ignore[missing-attribute]

    def fileno(self) -> int:
        return self._real.fileno()  # ty: ignore[missing-attribute]

    def read(self, _size: int) -> bytes:
        raise OSError(5, "Input/output error")


def test_a_read_error_names_the_file_and_says_the_upload_can_be_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"x")
    real = os.fdopen
    monkeypatch.setattr(
        artifact_manifest.os, "fdopen", lambda fd, mode: _FailingRead(real(fd, mode))
    )
    with pytest.raises(ManifestError) as caught:
        _hashed(str(target), "w.bin")
    assert (
        str(caught.value) == "w.bin could not be read: Input/output error. "
        "The upload was stopped and can be tried again once the file is readable."
    )


# --- walking by descriptor (I-8) --------------------------------------------------------------


def _swap_directory_for_a_link(
    monkeypatch: pytest.MonkeyPatch, directory: Path, outside: Path, *, when: str
) -> list[str]:
    """Make `os.open` swap a directory for a link to `outside` just before it is opened by name.

    Parameters
    ----------
    monkeypatch
        The fixture.
    directory
        The directory to replace.
    outside
        Where the link points.
    when
        The name whose opening, relative to a parent descriptor, triggers the swap.

    Returns
    -------
    list of str
        Appended to when the swap happened.
    """
    real = os.open
    done: list[str] = []

    def hooked(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == when and kwargs.get("dir_fd") is not None and not done:
            done.append("swapped")
            directory.rename(directory.with_name(directory.name + ".moved"))
            directory.symlink_to(outside, target_is_directory=True)
        return real(path, flags, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(artifact_manifest.os, "open", hooked)
    return done


def test_a_directory_replaced_by_a_link_during_the_walk_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    (out / "sub" / "w.bin").write_bytes(b"inside")
    (out / "model.bin").write_bytes(b"m")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"not yours")
    done = _swap_directory_for_a_link(monkeypatch, out / "sub", outside, when="sub")
    with pytest.raises(ManifestError) as caught:
        scan(out)
    assert done == ["swapped"]
    assert str(caught.value) == (
        "sub was replaced by a link or by a file of another kind while it was being scanned, so "
        "nothing was stored from it. Wait until whatever changes the directory has finished, then "
        "run the upload again."
    )


def test_a_file_is_opened_again_below_its_directory_never_through_a_swapped_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upload reads each file by descriptor from the root, as the scan did."""
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    (out / "sub" / "w.bin").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "w.bin").write_bytes(b"inside")  # even identical bytes are not read through a link
    found = scan(out)
    (file,) = found.files
    assert file.root == str(out)
    done = _swap_directory_for_a_link(monkeypatch, out / "sub", outside, when="sub")
    with pytest.raises(ManifestError, match="was replaced by a link"):
        artifact_manifest.open_regular(file.source, file.path, file.root)
    assert done == ["swapped"]


def test_a_tree_deeper_than_a_path_may_be_says_to_store_a_shallower_directory(
    tmp_path: Path,
) -> None:
    deep = tmp_path
    for n in range(34):
        deep = deep / f"d{n}"
    deep.mkdir(parents=True)
    with pytest.raises(ManifestError, match=r"nested more than 32 levels deep.*shallower"):
        scan(tmp_path)
    shallow = tmp_path.parent / f"{tmp_path.name}-shallow"
    leaf = shallow
    for n in range(32):
        leaf = leaf / f"d{n}"
    leaf.mkdir(parents=True)
    (leaf / "w.bin").write_bytes(b"x")
    with pytest.raises(ManifestError) as caught:
        scan(shallow)
    assert str(caught.value).endswith("has more than 32 segments; store a shallower directory.")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_a_root_that_cannot_be_examined_is_not_reported_as_missing(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "w.bin").write_bytes(b"x")
    locked.chmod(0)
    try:
        with pytest.raises(ManifestError) as caught:
            scan(locked / "w.bin")
    finally:
        locked.chmod(0o700)
    assert "does not exist" not in str(caught.value)
    assert "could not be read: Permission denied" in str(caught.value)


def test_a_long_root_path_is_shown_with_its_end_kept(tmp_path: Path) -> None:
    missing = tmp_path / ("x" * 100) / ("y" * 100) / ("z" * 100) / "last-part"
    with pytest.raises(ManifestError) as caught:
        scan(missing)
    text = str(caught.value)
    assert text.startswith("...")
    assert "last-part does not exist" in text
    assert len(text) < 320


def test_a_path_that_runs_through_a_file_is_reported_as_missing(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"x")
    with pytest.raises(ManifestError, match="does not exist"):
        scan(tmp_path / "f" / "inside")


@pytest.mark.parametrize("by_descriptor", [True, False], ids=["descriptor", "path"])
def test_both_walks_refuse_and_find_the_same_things(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by_descriptor: bool
) -> None:
    monkeypatch.setattr(artifact_manifest, "_BY_DESCRIPTOR", by_descriptor)
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    (out / "sub" / "w.bin").write_bytes(b"w")
    (out / "a.bin").write_bytes(b"a")
    found = scan(out)
    assert [f.path for f in found.files] == ["a.bin", "sub/w.bin"]
    assert all(f.root == str(out) for f in found.files)
    (out / "link").symlink_to(out / "a.bin")
    with pytest.raises(ManifestError, match="link is a symbolic link"):
        scan(out)
    (out / "link").unlink()
    os.mkfifo(out / "sub" / "pipe")
    with pytest.raises(ManifestError, match="sub/pipe is not a regular file"):
        scan(out)
    (out / "sub" / "pipe").unlink()
    with pytest.raises(ManifestError, match="more than the 1 files"):
        scan(out, ScanLimits(max_files=1))


class _Entry:
    """A directory entry whose `stat` fails, as one that vanished mid-walk does."""

    name = "vanished.bin"
    path = "/nowhere/vanished.bin"

    def stat(self, *, follow_symlinks: bool) -> os.stat_result:
        raise FileNotFoundError(2, "No such file or directory")


class _Listing:
    """A directory listing of those entries."""

    def __init__(self, entries: list[_Entry]) -> None:
        self._entries = entries

    def __enter__(self) -> _Listing:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self) -> object:
        return iter(self._entries)


@pytest.mark.parametrize("by_descriptor", [True, False], ids=["descriptor", "path"])
def test_an_entry_that_vanishes_during_the_walk_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by_descriptor: bool
) -> None:
    monkeypatch.setattr(artifact_manifest, "_BY_DESCRIPTOR", by_descriptor)
    monkeypatch.setattr(artifact_manifest.os, "scandir", lambda _path: _Listing([_Entry()]))
    with pytest.raises(ManifestError) as caught:
        scan(tmp_path)
    assert str(caught.value).startswith("vanished.bin could not be read:") or str(
        caught.value
    ).startswith(". could not be read:")
    assert "No such file or directory" in str(caught.value)


@pytest.mark.parametrize("by_descriptor", [True, False], ids=["descriptor", "path"])
def test_a_directory_that_cannot_be_listed_is_reported_with_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by_descriptor: bool
) -> None:
    monkeypatch.setattr(artifact_manifest, "_BY_DESCRIPTOR", by_descriptor)

    def refuse(_path: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(artifact_manifest.os, "scandir", refuse)
    with pytest.raises(ManifestError, match=r"^\. could not be read: Permission denied"):
        scan(tmp_path)


def test_a_root_directory_that_is_swapped_or_locked_when_it_is_opened_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "w.bin").write_bytes(b"x")
    real = os.open

    def swapped(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == str(tmp_path) and "dir_fd" not in kwargs:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")
        return real(path, flags, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(artifact_manifest.os, "open", swapped)
    with pytest.raises(ManifestError, match="was replaced by a link"):
        scan(tmp_path)
    monkeypatch.undo()
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            with pytest.raises(ManifestError, match="could not be read: Permission denied"):
                scan(locked)
        finally:
            locked.chmod(0o700)


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(), reason="open descriptors are counted in /proc"
)
def test_a_scan_leaves_no_descriptor_open_whether_it_succeeds_or_fails(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "c").mkdir()
    (tree / "a" / "b" / "f.bin").write_bytes(b"f")
    (tree / "c" / "g.bin").write_bytes(b"g")
    scan(tree)  # warm-up: anything the first call opens for good is not a leak of the scan
    before = open_descriptors()
    assert len(scan(tree).files) == 2
    assert open_descriptors() == before
    (tree / "c" / "link").symlink_to(tree / "a")
    with pytest.raises(ManifestError):
        scan(tree)
    assert open_descriptors() == before
