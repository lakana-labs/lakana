"""Writing a downloaded artifact to disk (the consumer's copy)."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lakana import _artifact_tree
from lakana._artifact_manifest import ManifestError
from lakana._artifact_tree import TreeWriter, check_manifest
from lakana._models import ArtifactFile
from lakana.errors import ArtifactCorrupt

# A test that finds a path in a message writes under `short_tmp_path`: a message shortens a long
# path, which a long `--basetemp` makes `tmp_path` (#604).


def _file(path: str, data: bytes) -> ArtifactFile:
    """Build a manifest file for some contents.

    Parameters
    ----------
    path
        The path inside the artifact.
    data
        The contents.

    Returns
    -------
    ArtifactFile
        The entry.
    """
    return ArtifactFile(path, len(data), hashlib.sha256(data).hexdigest())


@pytest.fixture(params=["descriptors", "paths"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test once with descriptor-based writes and once with the path-based fallback.

    Parameters
    ----------
    request
        The fixture request.
    monkeypatch
        Used to switch the module's capability flag.

    Returns
    -------
    str
        Which way is in use.
    """
    if request.param == "paths":
        monkeypatch.setattr(_artifact_tree, "_WRITE_BY_DESCRIPTOR", False)
    else:
        assert _artifact_tree._WRITE_BY_DESCRIPTOR, "this platform cannot write by descriptor"
    return str(request.param)


def _leftovers(parent: Path) -> list[str]:
    """List what is in a directory.

    Parameters
    ----------
    parent
        The directory.

    Returns
    -------
    list of str
        The entry names, sorted.
    """
    return sorted(entry.name for entry in parent.iterdir())


def test_a_tree_is_written_verified_and_appears_only_when_complete(
    tmp_path: Path, mode: str
) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 6)
    writer.write_file(_file("model.json", b"abc"), [b"a", b"bc"])
    writer.write_file(_file("deep/er/w.bin", b"xyz"), [b"xyz"])
    assert not (tmp_path / "copy").exists()
    assert [name for name in _leftovers(tmp_path) if name.startswith(".copy.lakana-partial-")]
    root = writer.commit()
    assert root == (tmp_path / "copy").resolve()
    assert (root / "model.json").read_bytes() == b"abc"
    assert (root / "deep" / "er" / "w.bin").read_bytes() == b"xyz"
    assert _leftovers(tmp_path) == ["copy"]
    assert _leftovers(root) == ["deep", "model.json"]


def test_an_empty_file_is_written(tmp_path: Path, mode: str) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 0)
    writer.write_file(_file("empty/file", b""), [])
    root = writer.commit()
    assert (root / "empty" / "file").read_bytes() == b""


@pytest.fixture
def umask() -> Iterator[None]:
    """Run a test under the usual umask, whatever the machine has, and restore it.

    Yields
    ------
    None
        Control for the test.
    """
    old = os.umask(0o022)
    yield
    os.umask(old)


def _mode(path: Path) -> int:
    """Read a path's permission bits without following a link.

    Parameters
    ----------
    path
        Any path.

    Returns
    -------
    int
        The mode's permission bits.
    """
    return stat.S_IMODE(os.lstat(path).st_mode)


def _open_up(root: Path) -> None:
    """Make a sealed tree writable again so that the test directory can be removed.

    Parameters
    ----------
    root
        The tree.
    """
    for directory, _names, _files in os.walk(root):
        os.chmod(directory, 0o700)


def test_a_persons_copy_is_private_and_never_executable(
    tmp_path: Path, mode: str, umask: None
) -> None:
    """The contents are access-controlled on the server: 0600 and 0700, not world-readable."""
    writer = TreeWriter.begin(tmp_path / "copy", 5)
    assert _mode(writer.staging) == 0o700
    writer.write_file(_file("run.sh", b"#!"), [b"#!"])
    writer.write_file(_file("d/e/f.bin", b"xyz"), [b"xyz"])
    assert _mode(writer.staging / "d") == 0o700
    root = writer.commit()
    assert _mode(root) == 0o700
    assert _mode(root / "d") == _mode(root / "d" / "e") == 0o700
    assert _mode(root / "run.sh") == _mode(root / "d" / "e" / "f.bin") == 0o600


def _record_renames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[int, int]]:
    """Record the modes of a staging directory and its subdirectory when it is renamed away.

    Parameters
    ----------
    monkeypatch
        The fixture.
    tmp_path
        The directory the staging directory is in.

    Returns
    -------
    list of tuple
        For each rename of a staging directory: its mode and the mode of its ``d``.
    """
    seen: list[tuple[int, int]] = []
    real = os.rename

    def spy(src: Any, dst: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        name = Path(src).name
        if ".lakana-partial-" in name:
            seen.append((_mode(tmp_path / name), _mode(tmp_path / name / "d")))
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", spy)
    return seen


def test_a_read_only_copy_is_sealed_through_descriptors_before_the_final_rename(
    tmp_path: Path, mode: str, umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_renames(monkeypatch, tmp_path)
    writer = TreeWriter.begin(tmp_path / "copy", 5, read_only=True)
    assert _mode(writer.staging) == 0o700
    writer.write_file(_file("a.bin", b"ab"), [b"ab"])
    writer.write_file(_file("d/e/f.bin", b"xyz"), [b"xyz"])
    assert _mode(writer.staging / "d" / "e" / "f.bin") == 0o400  # sealed as soon as verified
    root = writer.commit()
    try:
        assert seen == [(0o500, 0o500)]  # sealed already when it was moved into place
        assert _mode(root) == _mode(root / "d") == _mode(root / "d" / "e") == 0o500
        assert _mode(root / "a.bin") == _mode(root / "d" / "e" / "f.bin") == 0o400
    finally:
        _open_up(root)


def test_a_read_only_copy_in_an_existing_directory_keeps_the_directorys_own_mode(
    tmp_path: Path, mode: str, umask: None
) -> None:
    target = tmp_path / "here"
    target.mkdir()
    target.chmod(0o750)
    writer = TreeWriter.begin(target, 5, read_only=True)
    writer.write_file(_file("a.bin", b"ab"), [b"ab"])
    writer.write_file(_file("d/e/f.bin", b"xyz"), [b"xyz"])
    root = writer.commit()
    try:
        assert _mode(root) == 0o750
        assert _leftovers(root) == ["a.bin", "d"]
        assert _mode(root / "d") == _mode(root / "d" / "e") == 0o500
        assert _mode(root / "a.bin") == _mode(root / "d" / "e" / "f.bin") == 0o400
    finally:
        _open_up(root)


@pytest.mark.parametrize("existing", [False, True])
def test_sealing_never_follows_a_link_put_into_the_tree(
    tmp_path: Path, mode: str, existing: bool
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    target = tmp_path / "copy"
    if existing:
        target.mkdir()
    writer = TreeWriter.begin(target, 1, read_only=True)
    writer.write_file(_file("sub/a", b"a"), [b"a"])
    (writer.staging / "evil").symlink_to(outside, target_is_directory=True)
    (writer.staging / "sub" / "evil").symlink_to(outside, target_is_directory=True)
    root = writer.commit()
    try:
        assert _mode(outside) == 0o755
        assert (root / "evil").is_symlink()
        assert (root / "sub" / "evil").is_symlink()
    finally:
        _open_up(root)


def test_a_failed_read_only_download_is_removed_although_it_was_sealed(
    tmp_path: Path, mode: str
) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 1, read_only=True)
    writer.write_file(_file("d/e/a", b"a"), [b"a"])
    (tmp_path / "copy").mkdir()
    (tmp_path / "copy" / "squatter").write_bytes(b"x")  # the final rename is refused
    with pytest.raises(ManifestError, match="already exists and is not an empty directory"):
        writer.commit()
    assert _leftovers(tmp_path) == ["copy"]
    assert _leftovers(tmp_path / "copy") == ["squatter"]


def test_an_existing_empty_target_is_never_replaced(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its inode, its mode and a shell standing in it stay valid; a mount point works."""
    target = tmp_path / "here"
    target.mkdir()
    target.chmod(0o750)
    before = os.stat(target)
    monkeypatch.chdir(target)
    writer = TreeWriter.begin(target, 6)
    assert writer.staging.parent == target.resolve()
    assert writer.staging.name.startswith(".here.lakana-partial-")
    assert _mode(writer.staging) == 0o700
    writer.write_file(_file("model.json", b"abc"), [b"abc"])
    writer.write_file(_file("deep/er/w.bin", b"xyz"), [b"xyz"])
    assert os.listdir(".") == [writer.staging.name]
    root = writer.commit()
    after = os.stat(target)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert _mode(target) == 0o750
    assert root == target.resolve()
    assert sorted(os.listdir(".")) == ["deep", "model.json"]
    assert (Path(os.getcwd()) / "deep" / "er" / "w.bin").read_bytes() == b"xyz"


def test_an_empty_target_that_fills_up_before_the_end_is_refused_and_left_alone(
    tmp_path: Path, mode: str
) -> None:
    target = tmp_path / "here"
    target.mkdir()
    writer = TreeWriter.begin(target, 1)
    writer.write_file(_file("a", b"x"), [b"x"])
    (target / "squatter").write_bytes(b"theirs")
    with pytest.raises(ManifestError, match="already exists and is not an empty directory"):
        writer.commit()
    assert _leftovers(target) == ["squatter"]
    assert (target / "squatter").read_bytes() == b"theirs"


def test_a_name_taken_in_the_target_at_the_moment_of_the_move_is_never_overwritten(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "here"
    target.mkdir()
    writer = TreeWriter.begin(target, 1)
    writer.write_file(_file("model.json", b"x"), [b"x"])
    real = os.listdir

    def racing(where: Any) -> list[str]:  # ruff: ignore[any-type]
        found = real(where)
        if found == [writer.staging.name]:
            (target / "model.json").write_bytes(b"theirs")  # after the target was checked
        return found

    monkeypatch.setattr(os, "listdir", racing)
    with pytest.raises(ManifestError, match="an entry of that name appeared in it meanwhile"):
        writer.commit()
    monkeypatch.undo()
    assert _leftovers(target) == ["model.json"]
    assert (target / "model.json").read_bytes() == b"theirs"


def _fail_moves(monkeypatch: pytest.MonkeyPatch, failing: int) -> None:
    """Make the nth move of a top-level entry into the target fail.

    Parameters
    ----------
    monkeypatch
        The fixture.
    failing
        Which move fails, counting from 1.
    """
    real = os.rename
    count = [0]

    def rename(src: Any, dst: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        # A file's own rename keeps to one directory descriptor; a move uses two (or paths).
        if kwargs.get("src_dir_fd") != kwargs.get("dst_dir_fd") or kwargs.get("src_dir_fd") is None:
            count[0] += 1
            if count[0] == failing:
                raise OSError(errno.EIO, "Input/output error")
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", rename)


def test_a_move_that_fails_at_once_leaves_the_target_empty(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "here"
    target.mkdir()
    writer = TreeWriter.begin(target, 2)
    writer.write_file(_file("a", b"a"), [b"a"])
    writer.write_file(_file("b", b"b"), [b"b"])
    _fail_moves(monkeypatch, 1)
    with pytest.raises(ManifestError, match="Input/output error") as caught:
        writer.commit()
    assert "Nothing was kept" in str(caught.value)
    assert _leftovers(target) == []


def test_a_move_that_stops_half_way_says_what_is_where_and_removes_nothing(
    short_tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = short_tmp_path / "here"
    target.mkdir()
    writer = TreeWriter.begin(target, 2)
    writer.write_file(_file("a", b"a"), [b"a"])
    writer.write_file(_file("b", b"b"), [b"b"])
    _fail_moves(monkeypatch, 2)
    with pytest.raises(ManifestError) as caught:
        writer.commit()
    text = str(caught.value)
    assert "stopped after 1 of 2 entries" in text
    assert writer.staging.name in text
    assert "delete both" not in text
    assert f"Empty {target}" in text
    assert "do not remove the directory itself" in text
    assert f"remove the hidden {writer.staging.name} directory" in text
    assert "chmod" not in text
    writer.discard()  # leaves what is there for the person to look at
    inside = [name for name in _leftovers(target) if name.startswith(".here.lakana-partial-")]
    assert len(inside) == 1
    assert len(_leftovers(target)) == 2
    assert len(_leftovers(writer.staging)) == 1


def test_a_read_only_copy_that_stops_half_way_names_the_chmod_that_lets_the_person_empty_it(
    short_tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = short_tmp_path / "here"
    target.mkdir()
    writer = TreeWriter.begin(target, 2, read_only=True)
    writer.write_file(_file("a/x", b"a"), [b"a"])
    writer.write_file(_file("b/y", b"b"), [b"b"])
    _fail_moves(monkeypatch, 2)
    try:
        with pytest.raises(ManifestError) as caught:
            writer.commit()
        assert f"chmod -R u+w {target}" in str(caught.value)
        writer.discard()
    finally:
        monkeypatch.undo()
        _open_up(short_tmp_path)


def test_a_staging_directory_swapped_for_another_one_is_noticed_before_the_rename(
    short_tmp_path: Path,
) -> None:
    """The writer holds its own directory by descriptor; a foreign one never reaches the target."""
    writer = TreeWriter.begin(short_tmp_path / "copy", 1)
    writer.write_file(_file("mine", b"x"), [b"x"])
    writer.write_file(_file("d/mine", b"x"), [b"x"])
    os.rename(writer.staging, short_tmp_path / "ours")
    writer.staging.mkdir()
    (writer.staging / "theirs").write_bytes(b"theirs")
    with pytest.raises(ManifestError, match="was replaced by another") as caught:
        writer.commit()
    assert f"{short_tmp_path / 'copy'} was not created" in str(caught.value)
    assert "left as it is" not in str(caught.value)
    assert not (short_tmp_path / "copy").exists()
    assert (writer.staging / "theirs").read_bytes() == b"theirs"
    assert _leftovers(short_tmp_path / "ours") == []
    writer.discard()
    assert (writer.staging / "theirs").read_bytes() == b"theirs"


def test_a_staging_directory_swapped_during_the_rename_is_noticed_after_it(
    short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swap between the check and the rename cannot be prevented; it is reported truthfully."""
    writer = TreeWriter.begin(short_tmp_path / "copy", 1)
    writer.write_file(_file("mine", b"x"), [b"x"])
    real = os.rename

    def swapping(src: Any, dst: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        if ".lakana-partial-" in str(src):
            real(writer.staging, short_tmp_path / "ours")
            writer.staging.mkdir()
            (writer.staging / "theirs").write_bytes(b"theirs")
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", swapping)
    with pytest.raises(ManifestError, match="was replaced by another") as caught:
        writer.commit()
    monkeypatch.undo()
    text = " ".join(str(caught.value).split())
    copy = short_tmp_path / "copy"
    assert f"The directory now at {copy} is not the one this download wrote" in text
    assert "left as it is" not in text
    assert (short_tmp_path / "copy" / "theirs").read_bytes() == b"theirs"
    assert _leftovers(short_tmp_path / "ours") == []
    writer.discard()


def test_a_staging_directory_swapped_inside_an_existing_target_is_not_touched(
    tmp_path: Path,
) -> None:
    target = tmp_path / "here"
    target.mkdir()
    writer = TreeWriter.begin(target, 1)
    writer.write_file(_file("mine", b"x"), [b"x"])
    writer.write_file(_file("d/mine", b"x"), [b"x"])
    os.rename(writer.staging, tmp_path / "ours")
    writer.staging.mkdir()
    (writer.staging / "theirs").write_bytes(b"theirs")
    with pytest.raises(ManifestError, match="was replaced by another"):
        writer.commit()
    assert (writer.staging / "theirs").read_bytes() == b"theirs"
    assert _leftovers(tmp_path / "ours") == []
    writer.discard()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes everywhere")
def test_an_empty_target_that_cannot_be_written_to_is_refused_with_the_reason(
    short_tmp_path: Path, mode: str
) -> None:
    target = short_tmp_path / "here"
    target.mkdir()
    target.chmod(0o500)
    try:
        with pytest.raises(ManifestError) as caught:
            TreeWriter.begin(target, 1)
        assert f"Cannot write into {target}: Permission denied" in str(caught.value)
        assert _leftovers(target) == []
    finally:
        target.chmod(0o700)


def test_a_refusal_after_the_parents_were_made_removes_them_again(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.mkdir

    def refusing(path: Any, *args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        if ".lakana-partial-" in str(path):
            raise PermissionError(errno.EACCES, "Permission denied")
        real(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", refusing)
    with pytest.raises(ManifestError, match=r"Cannot create .*Permission denied"):
        TreeWriter.begin(tmp_path / "a" / "b" / "copy", 1)
    assert _leftovers(tmp_path) == []


def test_a_staging_directory_whose_opening_fails_is_not_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.open

    def failing(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:  # ruff: ignore[any-type]
        if kwargs.get("dir_fd") is not None and ".lakana-partial-" in str(path):
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing)
    with pytest.raises(ManifestError, match="Too many open files"):
        TreeWriter.begin(tmp_path / "copy", 1)
    monkeypatch.undo()
    assert _leftovers(tmp_path) == []


def test_a_swapped_staging_directory_is_not_followed_where_descriptors_are_not_used(
    tmp_path: Path, mode: str
) -> None:
    """The anchor is resolved once; a staging path turned into a link later leads nowhere."""
    outside = tmp_path / "outside"
    outside.mkdir()
    writer = TreeWriter.begin(tmp_path / "copy", 1)
    os.rename(writer.staging, tmp_path / "moved")
    writer.staging.symlink_to(outside, target_is_directory=True)
    if mode == "paths":
        with pytest.raises(ManifestError, match="was replaced by another"):
            writer.write_file(_file("f", b"x"), [b"x"])
    else:
        writer.write_file(_file("f", b"x"), [b"x"])  # the descriptor keeps it in the real one
        assert (tmp_path / "moved" / "f").read_bytes() == b"x"
    assert _leftovers(outside) == []
    writer.discard()


def test_the_staging_tree_is_removed_through_the_descriptors_that_are_still_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 1)
    writer.write_file(_file("a/b", b"x"), [b"x"])
    seen: list[tuple[str, int | None, bool]] = []
    real = shutil.rmtree

    def spy(path: Any, *args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        seen.append((str(path), kwargs.get("dir_fd"), writer._parent_fd is not None))
        real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", spy)
    parent_fd = writer._parent_fd
    writer.discard()
    assert seen == [(writer.staging.name, parent_fd, True)]
    assert _leftovers(tmp_path) == []


def test_a_name_the_file_system_refuses_is_a_message_naming_the_path(
    short_tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.mkdir

    def refusing(path: Any, *args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        if Path(path).name == "toolong":
            raise OSError(errno.ENAMETOOLONG, "File name too long")
        real(path, *args, **kwargs)

    writer = TreeWriter.begin(short_tmp_path / "copy", 1)
    monkeypatch.setattr(os, "mkdir", refusing)
    with pytest.raises(ManifestError) as caught:
        writer.write_file(_file("toolong/f", b"x"), [b"x"])
    assert f"Cannot write toolong/f for {short_tmp_path / 'copy'}: File name too long." in str(
        caught.value
    )
    assert "Nothing was kept" in str(caught.value)
    monkeypatch.undo()
    writer.discard()
    assert _leftovers(short_tmp_path) == []


def test_a_directory_that_cannot_be_opened_for_another_reason_names_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.open

    def failing(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:  # ruff: ignore[any-type]
        if path == "sub":
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, flags, *args, **kwargs)

    writer = TreeWriter.begin(tmp_path / "copy", 1)
    monkeypatch.setattr(os, "open", failing)
    with pytest.raises(ManifestError, match=r"Cannot write sub/f for .*: Too many open files"):
        writer.write_file(_file("sub/f", b"x"), [b"x"])
    monkeypatch.undo()
    writer.discard()


@pytest.mark.parametrize("failing", ["open", "rename"])
def test_a_permission_problem_on_the_file_is_a_message_and_removes_the_temporary_file(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    real_open, real_rename, real_replace = os.open, os.rename, os.replace

    def deny_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:  # ruff: ignore[any-type]
        if failing == "open" and str(path).endswith(".lakana-part"):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    def deny_rename(src: Any, dst: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        if str(src).endswith(".lakana-part"):
            raise PermissionError(errno.EACCES, "Permission denied")
        real_rename(src, dst, **kwargs)

    def deny_replace(src: Any, dst: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        deny_rename(src, dst, **kwargs)
        real_replace(src, dst, **kwargs)

    writer = TreeWriter.begin(tmp_path / "copy", 1)
    monkeypatch.setattr(os, "open", deny_open)
    monkeypatch.setattr(os, "rename", deny_rename)
    monkeypatch.setattr(os, "replace", deny_replace)
    with pytest.raises(ManifestError, match=r"Cannot write a for .*: Permission denied"):
        writer.write_file(_file("a", b"x"), [b"x"])
    assert _leftovers(writer.staging) == []
    monkeypatch.undo()
    writer.discard()


def test_a_full_disk_has_its_own_sentence(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Full:
        def __init__(self, inner: Any) -> None:  # ruff: ignore[any-type]
            self.inner = inner

        def __enter__(self) -> Full:
            return self

        def __exit__(self, *exc: object) -> None:
            self.inner.close()

        def write(self, data: bytes) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

    real = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda descriptor, how: Full(real(descriptor, how)))
    writer = TreeWriter.begin(tmp_path / "copy", 1)
    with pytest.raises(ManifestError) as caught:
        writer.write_file(_file("d/a", b"x"), [b"x"])
    text = str(caught.value)
    assert text.startswith("There is no space left on the disk while writing d/a")
    assert "free some space" in text
    assert "No space left on device" not in text
    assert _leftovers(writer.staging / "d") == []
    monkeypatch.undo()
    writer.discard()


def test_a_source_that_fails_is_not_taken_for_a_file_system_problem(
    tmp_path: Path, mode: str
) -> None:
    def broken() -> Iterator[bytes]:
        raise ConnectionResetError("cut")
        yield b""  # pragma: no cover

    writer = TreeWriter.begin(tmp_path / "copy", 1)
    with pytest.raises(ConnectionResetError):
        writer.write_file(_file("a", b"x"), broken())
    writer.discard()


def test_a_file_gets_its_real_name_only_after_it_was_verified(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 3)
    at_failure: list[list[str]] = []

    class Recording(ArtifactCorrupt):
        def __init__(self, message: str) -> None:
            at_failure.append(_leftovers(writer.staging))
            super().__init__(message)

    monkeypatch.setattr(_artifact_tree, "ArtifactCorrupt", Recording)
    with pytest.raises(ArtifactCorrupt):
        writer.write_file(ArtifactFile("real", 3, hashlib.sha256(b"abc").hexdigest()), [b"abd"])
    monkeypatch.undo()
    assert len(at_failure) == 1
    assert "real" not in at_failure[0]
    assert _leftovers(writer.staging) == []
    writer.discard()


def test_the_temporary_file_is_created_exclusively_and_never_through_a_link(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_artifact_tree.secrets, "token_hex", lambda n: "ab" * n)
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep me")
    writer = TreeWriter.begin(tmp_path / "copy", 1)
    temporary = writer.staging / f".{'ab' * 8}.lakana-part"
    temporary.symlink_to(victim)
    with pytest.raises(ManifestError, match="File exists"):
        writer.write_file(_file("a", b"x"), [b"x"])
    assert victim.read_bytes() == b"keep me"
    temporary.unlink()
    temporary.write_bytes(b"planted")  # a regular file already at the random name
    with pytest.raises(ManifestError, match="File exists"):
        writer.write_file(_file("a", b"x"), [b"x"])
    assert temporary.read_bytes() == b"planted"
    assert not (writer.staging / "a").exists()
    writer.discard()


def test_a_manifest_is_checked_with_the_clients_own_rules() -> None:
    for path, problem in (
        ("../evil", "must not have an empty, '.' or '..' segment"),
        ("/etc/cron.d/evil", "must be relative, without a leading '/'"),
        ("a\\b", "must not contain a backslash"),
        ("ckpt:2024", "must not contain ':'"),
        ("NUL.txt", "must not use a name Windows reserves"),
        ("a/b.", "must not have a part ending in '.' or a space"),
        ("a\x00b", "must not contain a control, format or surrogate character"),
        ("a‮b", "must not contain a control, format or surrogate character"),
        ("x/" * 40 + "y", "has more than 32 segments"),
        ("é" * 600, "is longer than 1024 bytes"),
    ):
        with pytest.raises(ManifestError) as caught:
            check_manifest([_file(path, b"x")], True)
        assert "The server's manifest has a path this client refuses" in str(caught.value)
        assert problem in str(caught.value)
    with pytest.raises(ManifestError, match="differ only by case"):
        check_manifest([_file("A", b"x"), _file("a", b"y")], True)
    with pytest.raises(ManifestError, match="differ only by case"):
        check_manifest([_file("é", b"x"), _file("é", b"y")], True)
    with pytest.raises(ManifestError, match="both a file and a directory"):
        check_manifest([_file("a", b"x"), _file("a/b", b"y")], True)
    with pytest.raises(ManifestError, match="exactly one file whose path is a file name"):
        check_manifest([_file("a/b", b"x")], False)
    with pytest.raises(ManifestError, match="exactly one file whose path is a file name"):
        check_manifest([_file("a", b"x"), _file("b", b"y")], False)
    with pytest.raises(ManifestError, match="lower-case SHA-256"):
        check_manifest([ArtifactFile("a", 1, "nope")], True)
    with pytest.raises(ManifestError, match="lower-case SHA-256"):
        check_manifest([ArtifactFile("a", 1, "A" * 64)], True)
    with pytest.raises(ManifestError, match="lower-case SHA-256"):
        check_manifest([ArtifactFile("a", -1, "a" * 64)], True)
    with pytest.raises(ManifestError, match="lower-case SHA-256"):
        check_manifest([ArtifactFile("a", True, "a" * 64)], True)
    for size in (10**20, 10**4000, 2.5, "3", None):
        with pytest.raises(ManifestError, match="from 0 to below 10"):
            check_manifest([ArtifactFile("a", size, "a" * 64)], True)  # ty: ignore[invalid-argument-type]
    check_manifest([ArtifactFile("a", 10**20 - 1, "a" * 64)], True)
    for files in (
        [ArtifactFile("a", 1, "1" * 64), ArtifactFile("a", 1, "2" * 64)],
        [_file("d/a", b"x"), _file("b", b"y"), _file("d/a", b"x")],
    ):
        with pytest.raises(ManifestError, match=r"lists (a|d/a) more than once"):
            check_manifest(files, True)
    for path, problem in (("x" * 256, "a part longer than 255 bytes"), ("é" * 128, "255 bytes")):
        with pytest.raises(ManifestError, match=problem):
            check_manifest([_file(path, b"x")], True)
    check_manifest([_file("é" * 127 + "x", b"x")], True)
    with pytest.raises(ManifestError, match="has no files"):
        check_manifest([], True)
    check_manifest([_file("a/b.txt", b"x"), _file("c", b"y")], True)
    check_manifest([_file("m.bin", b"x")], False)


def test_a_hostile_path_in_a_message_is_shown_escaped() -> None:
    with pytest.raises(ManifestError) as caught:
        check_manifest([_file("../\x1b[31mred\n", b"x")], True)
    assert "\x1b" not in str(caught.value)
    assert "\n" not in str(caught.value)


def test_a_link_inside_the_tree_is_never_followed(tmp_path: Path, mode: str) -> None:
    """A directory replaced by a link while the download runs cannot redirect a write."""
    outside = tmp_path / "outside"
    outside.mkdir()
    writer = TreeWriter.begin(tmp_path / "copy", 2)
    writer.write_file(_file("sub/a", b"a"), [b"a"])
    shutil.rmtree(writer.staging / "sub")
    (writer.staging / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManifestError) as escaped:
        writer.write_file(_file("sub/b", b"b"), [b"b"])
    assert "Refusing to write sub/b: its directory resolves outside" in str(escaped.value)
    assert _leftovers(outside) == []
    writer.discard()
    assert _leftovers(tmp_path) == ["outside"]


def test_a_link_where_a_file_goes_is_never_written_through(tmp_path: Path, mode: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"keep me")
    writer = TreeWriter.begin(tmp_path / "copy", 1)
    (writer.staging / "link").symlink_to(victim)
    writer.write_file(_file("link", b"x"), [b"x"])
    assert victim.read_bytes() == b"keep me"
    root = writer.commit()
    # The file replaced the link itself; the victim was never opened.
    assert not (root / "link").is_symlink()
    assert (root / "link").read_bytes() == b"x"


def test_a_directory_swapped_for_a_link_during_a_file_is_not_followed(
    tmp_path: Path,
) -> None:
    """Descriptors keep the file in the directory that was opened, wherever the name points now."""
    outside = tmp_path / "outside"
    outside.mkdir()
    writer = TreeWriter.begin(tmp_path / "copy", 2)

    def swapping() -> Iterator[bytes]:
        yield b"a"
        os.rename(writer.staging / "sub", writer.staging / "moved")
        (writer.staging / "sub").symlink_to(outside, target_is_directory=True)
        yield b"b"

    writer.write_file(_file("sub/f", b"ab"), swapping())
    assert _leftovers(outside) == []
    assert (writer.staging / "moved" / "f").read_bytes() == b"ab"
    writer.discard()


def test_the_directory_must_be_new_or_empty(tmp_path: Path, mode: str) -> None:
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "x").write_bytes(b"x")
    with pytest.raises(ManifestError, match="already exists and is not an empty directory"):
        TreeWriter.begin(taken, 1)
    afile = tmp_path / "afile"
    afile.write_bytes(b"x")
    with pytest.raises(ManifestError, match="already exists and is not an empty directory"):
        TreeWriter.begin(afile, 1)
    outside = tmp_path / "outside"
    outside.mkdir()
    link_inside = tmp_path / "linky"
    link_inside.mkdir()
    (link_inside / "l").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManifestError, match="already exists and is not an empty directory"):
        TreeWriter.begin(link_inside, 1)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "empty", target_is_directory=True)
    with pytest.raises(ManifestError, match="is a symbolic link; give a real directory"):
        TreeWriter.begin(link, 1)
    (tmp_path / "empty").mkdir()
    with pytest.raises(ManifestError, match="is a symbolic link; give a real directory"):
        TreeWriter.begin(link, 1)
    writer = TreeWriter.begin(tmp_path / "empty", 1)
    writer.write_file(_file("a", b"a"), [b"a"])
    assert writer.commit() == (tmp_path / "empty").resolve()
    assert (tmp_path / "empty" / "a").read_bytes() == b"a"
    assert _leftovers(tmp_path / "outside") == []


def test_a_directory_under_a_linked_parent_is_the_users_own_choice(
    tmp_path: Path, mode: str
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    writer = TreeWriter.begin(tmp_path / "alias" / "new" / "copy", 1)
    writer.write_file(_file("a", b"a"), [b"a"])
    assert writer.commit() == (real / "new" / "copy").resolve()
    assert (real / "new" / "copy" / "a").read_bytes() == b"a"


def test_a_file_that_does_not_match_is_corrupt_and_nothing_stays(tmp_path: Path, mode: str) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 3)
    for name, chunks in (
        ("long", [b"abcd"]),
        ("short", [b"ab"]),
        ("other", [b"abd"]),
        ("none", []),
    ):
        with pytest.raises(ArtifactCorrupt) as caught:
            writer.write_file(ArtifactFile(name, 3, hashlib.sha256(b"abc").hexdigest()), chunks)
        assert str(caught.value) == (
            f"The downloaded file {name} does not match its manifest (size or SHA-256); "
            "download it again."
        )
    assert _leftovers(writer.staging) == []
    writer.discard()


def test_a_file_that_is_too_long_is_cut_off_as_it_arrives(tmp_path: Path, mode: str) -> None:
    pulled: list[int] = []

    def endless() -> Iterator[bytes]:
        while True:
            pulled.append(1)
            yield b"x" * 4

    writer = TreeWriter.begin(tmp_path / "copy", 3)
    with pytest.raises(ArtifactCorrupt):
        writer.write_file(_file("f", b"abc"), endless())
    assert len(pulled) == 1
    writer.discard()


def test_a_failure_while_the_bytes_arrive_leaves_no_file_behind(tmp_path: Path, mode: str) -> None:
    def broken() -> Iterator[bytes]:
        yield b"a"
        raise ConnectionResetError("cut")

    writer = TreeWriter.begin(tmp_path / "copy", 2)
    with pytest.raises(ConnectionResetError):
        writer.write_file(_file("d/f", b"ab"), broken())
    assert _leftovers(writer.staging / "d") == []
    writer.discard()


def test_nothing_is_left_at_the_target_after_a_discard(tmp_path: Path, mode: str) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 1)
    writer.write_file(_file("a/b", b"x"), [b"x"])
    writer.discard()
    assert _leftovers(tmp_path) == []
    writer.discard()  # a second discard is harmless
    (tmp_path / "empty").mkdir()
    again = TreeWriter.begin(tmp_path / "empty", 1)
    again.write_file(_file("a", b"x"), [b"x"])
    again.discard()
    assert _leftovers(tmp_path) == ["empty"]
    assert _leftovers(tmp_path / "empty") == []


def test_a_target_that_fills_up_before_the_end_is_refused_and_cleaned(
    tmp_path: Path, mode: str
) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 1)
    writer.write_file(_file("a", b"x"), [b"x"])
    (tmp_path / "copy").mkdir()
    (tmp_path / "copy" / "squatter").write_bytes(b"x")
    with pytest.raises(ManifestError, match="already exists and is not an empty directory"):
        writer.commit()
    assert _leftovers(tmp_path) == ["copy"]
    assert _leftovers(tmp_path / "copy") == ["squatter"]


def test_download_refuses_when_the_disk_is_too_small(
    short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Refused before the first byte, with both numbers."""
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: types.SimpleNamespace(free=10))
    with pytest.raises(ManifestError) as caught:
        TreeWriter.begin(short_tmp_path / "copy", 11)
    assert str(caught.value) == (
        f"The artifact needs 11 bytes but only 10 are free in {short_tmp_path / 'copy'}; free some "
        "space or choose another directory."
    )
    assert _leftovers(short_tmp_path) == []
    with pytest.raises(ManifestError, match="only 10 are free"):
        TreeWriter.begin(short_tmp_path / "a" / "b" / "copy", 11)
    assert _leftovers(short_tmp_path) == []  # no parent was made for a refusal
    TreeWriter.begin(short_tmp_path / "copy", 10).discard()


def test_a_directory_that_cannot_be_made_says_why(tmp_path: Path, mode: str) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    with pytest.raises(ManifestError, match="Cannot create"):
        TreeWriter.begin(blocker / "copy", 1)


def test_the_name_of_an_unfinished_download_says_it_is_unfinished(tmp_path: Path) -> None:
    writer = TreeWriter.begin(tmp_path / "copy", 1)
    assert writer.staging.parent == tmp_path.resolve()
    assert writer.staging.name.startswith(".copy.lakana-partial-")
    long = TreeWriter.begin(tmp_path / ("n" * 200), 1)
    assert len(long.staging.name) < 255
    writer.discard()
    long.discard()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes everywhere")
def test_a_parent_that_cannot_be_written_says_so_and_leaves_nothing(
    tmp_path: Path, mode: str
) -> None:
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with pytest.raises(ManifestError, match="Cannot create"):
            TreeWriter.begin(parent / "copy", 1)
        assert _leftovers(parent) == []
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize(
    ("swap", "message"),
    [
        ("empty", "is a symbolic link; give a real directory"),
        ("full", "is a symbolic link; give a real directory"),
        ("file", "already exists and is not an empty directory"),
    ],
)
def test_a_target_swapped_while_the_writer_sets_up_is_refused_and_nothing_outside_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str, message: str
) -> None:
    """The check and the open used to be two lookups of one path: a link put between them won."""
    assert _artifact_tree._WRITE_BY_DESCRIPTOR, "this platform cannot write by descriptor"
    outside = tmp_path / "outside"
    outside.mkdir()
    if swap == "full":
        (outside / "theirs").write_bytes(b"x")
    target = tmp_path / "here"
    target.mkdir()
    before = (_leftovers(outside), os.stat(outside).st_mtime_ns)

    def swapping(_path: Any) -> Any:  # ruff: ignore[any-type]
        os.rmdir(target)
        if swap == "file":
            target.write_bytes(b"x")
        else:
            target.symlink_to(outside, target_is_directory=True)
        return types.SimpleNamespace(free=10**9)

    monkeypatch.setattr(shutil, "disk_usage", swapping)
    with pytest.raises(ManifestError, match=message):
        TreeWriter.begin(target, 1)
    monkeypatch.undo()
    assert (_leftovers(outside), os.stat(outside).st_mtime_ns) == before


def test_a_parent_made_for_a_refusal_is_removed_when_the_target_appears_as_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    real = Path.mkdir

    def planting(self: Path, *args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        real(self, *args, **kwargs)
        if self == tmp_path / "a":
            (tmp_path / "a" / "copy").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(Path, "mkdir", planting)
    with pytest.raises(ManifestError, match="is a symbolic link"):
        TreeWriter.begin(tmp_path / "a" / "copy", 1)
    monkeypatch.undo()
    assert _leftovers(outside) == []
    assert _leftovers(tmp_path / "a") == ["copy"]  # the link is theirs; the writer made only "a"


def test_the_root_directory_is_never_a_target(tmp_path: Path, mode: str) -> None:
    with pytest.raises(ManifestError, match="already exists and is not an empty directory"):
        TreeWriter.begin(Path("/"), 1)


def test_the_moves_into_an_existing_target_go_through_its_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target's path swapped for a link just before the moves must not redirect them."""
    assert _artifact_tree._WRITE_BY_DESCRIPTOR, "this platform cannot write by descriptor"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "here"
    target.mkdir()
    writer = TreeWriter.begin(target, 2)
    writer.write_file(_file("a", b"a"), [b"a"])
    writer.write_file(_file("d/b", b"b"), [b"b"])
    real = os.rename
    swapped = [False]

    def swapping(src: Any, dst: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        if kwargs.get("src_dir_fd") != kwargs.get("dst_dir_fd") and not swapped[0]:
            swapped[0] = True
            real(target, tmp_path / "aside")
            target.symlink_to(outside, target_is_directory=True)
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", swapping)
    writer.commit()
    monkeypatch.undo()
    assert swapped[0]
    assert _leftovers(tmp_path / "aside") == ["a", "d"]
    assert (tmp_path / "aside" / "d" / "b").read_bytes() == b"b"
    assert _leftovers(outside) == []


@pytest.fixture
def by_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the path-based fallback that platforms without directory descriptors use.

    Parameters
    ----------
    monkeypatch
        Used to switch the module's capability flag.
    """
    monkeypatch.setattr(_artifact_tree, "_WRITE_BY_DESCRIPTOR", False)


def test_a_directory_swapped_for_a_link_gets_nothing_created_outside(
    tmp_path: Path, by_path: None
) -> None:
    """Where descriptors are not used the check comes before the directory is created."""
    outside = tmp_path / "outside"
    outside.mkdir()
    writer = TreeWriter.begin(tmp_path / "copy", 2)
    writer.write_file(_file("sub/a", b"a"), [b"a"])
    shutil.rmtree(writer.staging / "sub")
    (writer.staging / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManifestError, match="resolves outside"):
        writer.write_file(_file("sub/deeper/b", b"b"), [b"b"])
    assert _leftovers(outside) == []
    writer.discard()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("when", ["write", "commit"])
def test_a_staging_directory_replaced_by_another_real_one_is_noticed_without_descriptors(
    tmp_path: Path, by_path: None, existing: bool, when: str
) -> None:
    target = tmp_path / "copy"
    if existing:
        target.mkdir()
    writer = TreeWriter.begin(target, 2)
    writer.write_file(_file("mine", b"x"), [b"x"])
    os.rename(writer.staging, tmp_path / "ours")
    writer.staging.mkdir()
    (writer.staging / "theirs").write_bytes(b"theirs")
    with pytest.raises(ManifestError, match="was replaced by another"):
        if when == "write":
            writer.write_file(_file("more", b"y"), [b"y"])
        else:
            writer.commit()
    writer.discard()
    assert (writer.staging / "theirs").read_bytes() == b"theirs"  # never removed
    assert _leftovers(writer.staging) == ["theirs"]
    assert not (tmp_path / "copy" / "mine").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everywhere")
def test_an_existing_target_that_cannot_be_opened_is_refused_with_the_reason(
    short_tmp_path: Path, mode: str
) -> None:
    target = short_tmp_path / "here"
    target.mkdir()
    target.chmod(0)
    try:
        with pytest.raises(ManifestError) as caught:
            TreeWriter.begin(target, 1)
        assert f"Cannot write into {target}: Permission denied" in str(caught.value)
    finally:
        target.chmod(0o700)


def test_a_refused_target_is_shown_escaped(tmp_path: Path) -> None:
    """A control character in the directory the person gave never reaches a message raw."""
    target = tmp_path / "copy\x1b[31m\nred"
    target.mkdir()
    (target / "x").write_bytes(b"x")
    with pytest.raises(ManifestError) as caught:
        TreeWriter.begin(target, 1)
    text = str(caught.value)
    assert "\x1b" not in text
    assert "\n" not in text
    assert "copy\\x1b[31m\\nred already exists and is not an empty directory" in text


def test_a_long_target_is_shown_by_its_end(tmp_path: Path) -> None:
    """A very long path is cut at the front: its end is what tells it apart."""
    target = tmp_path
    for _ in range(4):
        target = target / ("d" * 120)
    target.mkdir(parents=True)
    (target / "x").write_bytes(b"x")
    with pytest.raises(ManifestError) as caught:
        TreeWriter.begin(target, 1)
    text = str(caught.value)
    assert text.startswith("...")
    assert text.partition(" already exists")[0].endswith("d" * 120)
    assert len(text) < 400


def test_a_failed_write_shows_the_target_escaped(tmp_path: Path) -> None:
    """The write failure names the directory escaped, like the refusal at the start."""
    target = tmp_path / "out\x1b[0m"
    writer = TreeWriter.begin(target, 5)
    try:
        refusal = _artifact_tree._failure(
            OSError(errno.EACCES, "Permission denied"), "a.bin", target
        )
    finally:
        writer.discard()
    assert "\x1b" not in str(refusal)
    assert "out\\x1b[0m" in str(refusal)


_PIN = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_BY_FD = pytest.mark.skipif(
    not _artifact_tree._WRITE_BY_DESCRIPTOR, reason="needs files written relative to a directory"
)


def test_a_target_by_descriptor_needs_a_platform_that_writes_by_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_artifact_tree, "_WRITE_BY_DESCRIPTOR", False)
    with pytest.raises(ValueError, match="Linux or macOS"):
        _artifact_tree.check_target_name("model")


@_BY_FD
def test_a_descriptor_whose_disk_cannot_be_asked_is_refused_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(_fd: int) -> os.statvfs_result:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "statvfs", failing)
    descriptor = os.open(tmp_path, _PIN)
    try:
        with pytest.raises(ManifestError, match="Cannot create model: Input/output error"):
            TreeWriter.begin(Path("model"), 1, dir_fd=descriptor)
    finally:
        os.close(descriptor)


@_BY_FD
def test_a_download_by_descriptor_checks_the_free_space_of_that_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.statvfs

    def small(fd: int) -> Any:  # ruff: ignore[any-type]
        found = real(fd)
        return types.SimpleNamespace(f_bavail=1, f_frsize=found.f_frsize)

    monkeypatch.setattr(os, "statvfs", small)
    descriptor = os.open(tmp_path, _PIN)
    try:
        with pytest.raises(ManifestError, match="free some space"):
            TreeWriter.begin(Path("model"), 10**15, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    assert os.listdir(tmp_path) == []


@_BY_FD
def test_a_descriptor_that_cannot_be_copied_is_refused_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(_fd: int) -> int:
        raise OSError(errno.EMFILE, "Too many open files")

    descriptor = os.open(tmp_path, _PIN)
    monkeypatch.setattr(os, "dup", failing)
    try:
        with pytest.raises(ManifestError, match="Too many open files"):
            TreeWriter.begin(Path("model"), 1, dir_fd=descriptor)
    finally:
        monkeypatch.undo()
        os.close(descriptor)


@_BY_FD
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_directory_by_descriptor_that_cannot_be_written_is_refused_with_the_reason(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "inputs"
    parent.mkdir(mode=0o500)
    descriptor = os.open(parent, _PIN)
    try:
        with pytest.raises(ManifestError, match="Cannot create model: Permission denied"):
            TreeWriter.begin(Path("model"), 1, dir_fd=descriptor)
    finally:
        os.close(descriptor)
        parent.chmod(0o700)
