"""`lakana server reset`: start a local instance over, and every way it refuses."""

from __future__ import annotations

import contextlib
import os
import pwd
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeApi, invoke, make_state, write_profile_file

from lakana.cli import server
from lakana.cli._filelock import try_locked
from lakana.cli._state import CliError
from lakana.cli.local import _reset as reset_module
from lakana.cli.local._datadir import MARKER_NAME, DataDir, prepare_data_dir
from lakana.cli.local._pidfile import write_pidfile


def _instance(tmp_path: Path, *, tables: int = 3) -> DataDir:
    """Build a local instance's data directory with data in it.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    tables
        How many rows the catalog's `tables` table gets.

    Returns
    -------
    DataDir
        The prepared directory, with a database, a lake, a key and stale pidfiles.
    """
    root = prepare_data_dir(tmp_path / "instance")
    with contextlib.closing(sqlite3.connect(root.db_path)) as db, db:
        db.execute("CREATE TABLE tables (id INTEGER PRIMARY KEY, name TEXT)")
        db.executemany("INSERT INTO tables (name) VALUES (?)", [(f"t{i}",) for i in range(tables)])
    (root.lake / "dev" / "bronze" / "readings").mkdir(parents=True)
    (root.lake / "dev" / "bronze" / "readings" / "data.parquet").write_text("x")
    root.master_key_path.write_text("key\n")
    write_pidfile(root.server_pid, 2**22 + 5, 8765)
    write_pidfile(root.worker_pid, 2**22 + 6)
    return root


def _reset(tmp_path: Path, root_path: Path, *extra: str, interactive: bool = False, **kwargs):  # ruff: ignore[missing-type-kwargs, missing-return-type-private-function]
    """Run `lakana server reset --local --data-dir ROOT`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory, for the profile file.
    root_path
        The data directory.
    *extra
        More arguments.
    interactive
        Whether the CLI believes it has a terminal.
    **kwargs
        Passed to `invoke` (`input`, `env`).

    Returns
    -------
    typer.testing.Result
        The result.
    """
    state = make_state(tmp_path, FakeApi(), interactive=interactive)
    return invoke(
        state, "server", "reset", "--local", "--data-dir", str(root_path), *extra, **kwargs
    )


def _untouched(root: DataDir) -> bool:
    """Report whether the instance's data is all still there.

    Parameters
    ----------
    root
        The data directory.

    Returns
    -------
    bool
        `True` if the database, the lake's file and the key exist.
    """
    return (
        root.db_path.exists()
        and (root.lake / "dev" / "bronze" / "readings" / "data.parquet").exists()
        and root.master_key_path.exists()
    )


def test_prepare_data_dir_writes_the_marker(tmp_path: Path) -> None:
    """Creating an instance directory marks it, so a reset can tell it from any other folder."""
    root = prepare_data_dir(tmp_path / "instance")
    assert (root.root / MARKER_NAME).read_text() == "lakana local instance\n"
    prepare_data_dir(tmp_path / "instance")  # idempotent


def test_reset_deletes_the_data_and_keeps_the_marker_and_the_project(tmp_path: Path) -> None:
    """After a reset the directory is in its first-run state; project files are not touched."""
    root = _instance(tmp_path)
    project = tmp_path / "analytics"
    (project / "assets").mkdir(parents=True)
    (project / "assets" / "a.sql").write_text("SELECT 1")

    result = _reset(tmp_path, root.root, "--yes")

    assert result.exit_code == 0, result.output
    assert not root.db_path.exists() and not root.lake.exists()
    assert not root.master_key_path.exists()
    assert not root.server_pid.exists() and not root.worker_pid.exists()
    assert (root.root / MARKER_NAME).exists()
    assert sorted(p.name for p in root.root.iterdir()) == sorted([MARKER_NAME, "lock"])
    assert (project / "assets" / "a.sql").read_text() == "SELECT 1"
    assert "lakana server start --local" in result.stdout


def test_reset_names_what_it_deletes_and_asks(tmp_path: Path) -> None:
    """The prompt names the paths and the number of tables; declining deletes nothing."""
    root = _instance(tmp_path, tables=3)
    declined = _reset(tmp_path, root.root, interactive=True, input="n\n")
    assert declined.exit_code == 2
    assert _untouched(root)
    for path in (root.db_path, root.lake, root.master_key_path):
        assert str(path) in declined.output
    assert "3 tables" in declined.output
    assert "project" in declined.output.lower()

    accepted = _reset(tmp_path, root.root, interactive=True, input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert not root.db_path.exists()


def test_reset_without_a_terminal_and_without_yes_refuses(tmp_path: Path) -> None:
    """A script must pass --yes; without it nothing is deleted."""
    root = _instance(tmp_path)
    result = _reset(tmp_path, root.root)
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert _untouched(root)


def test_one_table_is_singular_and_an_unreadable_database_is_still_named(tmp_path: Path) -> None:
    """The count reads '1 table'; a database that cannot be read does not block the reset."""
    root = _instance(tmp_path, tables=1)
    result = _reset(tmp_path, root.root, interactive=True, input="n\n")
    assert "1 table" in result.output and "1 tables" not in result.output
    root.db_path.write_text("not a database")
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 0, result.output
    assert not root.db_path.exists()


def test_nothing_to_reset_when_there_is_no_instance(tmp_path: Path) -> None:
    """A missing directory is already in its first-run state; nothing is created."""
    result = _reset(tmp_path, tmp_path / "none", "--yes")
    assert result.exit_code == 0
    assert "Nothing to reset" in result.stdout
    assert not (tmp_path / "none").exists()


def test_without_local_it_refuses_and_names_compose(tmp_path: Path) -> None:
    """Without --local the command refuses, and says how to start over elsewhere."""
    root = _instance(tmp_path)
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "server", "reset", "--data-dir", str(root.root), "--yes")
    assert result.exit_code == 2
    assert "Compose" in result.output and "--local" in result.output
    assert _untouched(root)


def test_a_remote_url_is_refused(tmp_path: Path) -> None:
    """--url, LAKANA_URL or a chosen profile that points off this machine refuses the reset."""
    root = _instance(tmp_path)
    state = make_state(tmp_path, FakeApi())
    args = ("server", "reset", "--local", "--data-dir", str(root.root), "--yes")
    by_flag = invoke(state, "--url", "https://lakana.example.org", *args)
    by_env = invoke(state, *args, env={"LAKANA_URL": "https://lakana.example.org"})
    assert by_flag.exit_code == 2 and by_env.exit_code == 2
    assert "remote" in by_flag.output and "remote" in by_env.output
    assert _untouched(root)
    local = invoke(state, "--url", "http://127.0.0.1:8765", *args)
    assert local.exit_code == 0, local.output


def test_a_remote_profile_is_refused(tmp_path: Path) -> None:
    """A profile chosen with --profile that points at a remote server refuses the reset."""
    root = _instance(tmp_path)
    state = make_state(tmp_path, FakeApi())
    write_profile_file(
        tmp_path / "lakana" / "config.toml",
        '[profiles.team]\nurl = "https://lakana.example.org"\ntoken = "t"\n',
    )
    result = invoke(
        state,
        "--profile",
        "team",
        "server",
        "reset",
        "--local",
        "--data-dir",
        str(root.root),
        "--yes",
    )
    assert result.exit_code == 2
    assert "remote" in result.output
    assert _untouched(root)


def test_a_directory_without_the_marker_is_refused(tmp_path: Path) -> None:
    """A folder the local instance did not mark is never deleted from."""
    root = _instance(tmp_path)
    (root.root / MARKER_NAME).unlink()
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 2
    assert "lakana server start --local" in result.output
    assert _untouched(root)


def test_a_marker_with_other_content_or_a_symlink_is_refused(tmp_path: Path) -> None:
    """Only the exact marker file counts."""
    root = _instance(tmp_path)
    marker = root.root / MARKER_NAME
    marker.write_text("something else\n")
    assert _reset(tmp_path, root.root, "--yes").exit_code == 2
    marker.unlink()
    genuine = tmp_path / "genuine"
    genuine.write_text("lakana local instance\n")
    marker.symlink_to(genuine)
    assert _reset(tmp_path, root.root, "--yes").exit_code == 2
    assert _untouched(root)


def test_a_filesystem_root_is_refused(tmp_path: Path) -> None:
    """The root of the filesystem is refused before anything is read from it."""
    result = _reset(tmp_path, Path("/"), "--yes")
    assert result.exit_code == 2
    assert "filesystem root" in result.output


def test_the_home_directory_and_its_parents_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a marked home directory, or a directory that holds it, is refused."""
    home = tmp_path / "people" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for candidate in (home, home.parent):
        (candidate / MARKER_NAME).write_text("lakana local instance\n")
        (candidate / "lakana.db").write_text("x")
        result = _reset(tmp_path, candidate, "--yes")
        assert result.exit_code == 2, candidate
        assert "home directory" in result.output
        assert (candidate / "lakana.db").exists()


def test_a_symlinked_data_directory_is_refused(tmp_path: Path) -> None:
    """A link to an instance is not an instance: nothing is followed."""
    real = _instance(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real.root, target_is_directory=True)
    result = _reset(tmp_path, link, "--yes")
    assert result.exit_code == 2
    assert "symlink" in result.output
    assert _untouched(real)


def test_a_data_directory_that_is_a_file_is_refused(tmp_path: Path) -> None:
    """A path that is not a directory is refused."""
    (tmp_path / "file").write_text("x")
    assert _reset(tmp_path, tmp_path / "file", "--yes").exit_code == 2


def test_a_target_that_leaves_the_root_through_a_symlink_is_refused(tmp_path: Path) -> None:
    """A `lake` or `master.key` that is a link to somewhere else stops the whole reset."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    for name, is_dir in (("lake", True), ("master.key", False), ("lakana.db", False)):
        root = _instance(tmp_path / name)
        victim = root.root / name
        if is_dir:
            shutil.rmtree(victim)
            victim.symlink_to(outside, target_is_directory=True)
        else:
            victim.unlink()
            victim.symlink_to(outside / "precious.txt")
        result = _reset(tmp_path / name, root.root, "--yes")
        assert result.exit_code == 2, name
        assert "symlink" in result.output
        assert (outside / "precious.txt").read_text() == "keep"
        assert (root.root / "server.pid").exists()  # nothing was deleted before the refusal


def test_a_symlink_inside_the_lake_is_removed_not_followed(tmp_path: Path) -> None:
    """A link below `lake/` that points outside is unlinked; its target survives."""
    root = _instance(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    (root.lake / "dev" / "escape").symlink_to(outside, target_is_directory=True)
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 0, result.output
    assert not root.lake.exists()
    assert (outside / "precious.txt").read_text() == "keep"


def test_a_running_instance_is_refused(tmp_path: Path) -> None:
    """A held lock means an instance runs; nothing is deleted."""
    root = _instance(tmp_path)
    with try_locked(root.lock_path) as acquired:
        assert acquired
        result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 2
    assert "running" in result.output and "Ctrl-C" in result.output
    assert _untouched(root)


def test_an_older_instance_without_a_lock_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live, healthy server pidfile counts as running even with a free lock."""
    root = _instance(tmp_path)
    write_pidfile(root.server_pid, os.getpid(), 8765)
    monkeypatch.setattr(server, "_probe_health", lambda url: True)
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 2
    assert "running" in result.output
    assert _untouched(root)


def test_reset_holds_the_lock_while_it_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An instance cannot start in the middle of a reset."""
    root = _instance(tmp_path)
    seen: list[bool] = []
    real = reset_module.delete_targets

    def spy(plan):  # ruff: ignore[missing-type-function-argument, missing-return-type-private-function]
        with try_locked(root.lock_path) as acquired:
            seen.append(acquired)
        return real(plan)

    monkeypatch.setattr(reset_module, "delete_targets", spy)
    assert _reset(tmp_path, root.root, "--yes").exit_code == 0
    assert seen == [False]


def test_a_mount_point_below_the_lake_is_refused_before_anything_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory on another device inside `lake/` stops the reset; the database is kept."""
    root = _instance(tmp_path)
    (root.lake / "dev" / "mnt").mkdir()
    real = reset_module._device_of

    def fake(path: Path) -> int:
        return 4242 if path.name == "mnt" else real(path)

    monkeypatch.setattr(reset_module, "_device_of", fake)
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 2
    assert "mnt" in result.output and "mount" in result.output and "unmount" in result.output
    assert _untouched(root)


def test_a_lake_on_another_device_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lake/` itself on another device is refused the same way."""
    root = _instance(tmp_path)
    real = reset_module._device_of
    monkeypatch.setattr(reset_module, "_device_of", lambda p: 4242 if p.name == "lake" else real(p))
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 2
    assert "mount" in result.output
    assert _untouched(root)


def test_the_database_goes_last_so_a_failed_reset_is_never_a_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the lake can't be deleted, `lakana.db` is still there; the message names what remains."""
    root = _instance(tmp_path)

    def broken(path: object, *args: object, **kwargs: object) -> None:
        raise OSError("device busy")

    monkeypatch.setattr(reset_module.shutil, "rmtree", broken)
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 3
    assert root.db_path.exists()
    text = " ".join(result.output.split())
    assert "device busy" in text and "lakana.db" in text and "again" in text


def test_a_target_swapped_for_a_symlink_after_the_plan_is_refused(tmp_path: Path) -> None:
    """The re-check just before each deletion catches a swap made after the plan."""
    root = _instance(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    plan = reset_module.plan_reset(root.root.resolve())
    shutil.rmtree(root.lake)
    root.lake.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CliError, match="not inside") as caught:
        reset_module.delete_targets(plan)
    assert caught.value.exit_code == 2
    assert (outside / "precious.txt").read_text() == "keep"
    assert root.db_path.exists()


def test_the_home_directory_is_found_by_identity_and_by_the_password_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second path to home (same device and inode) or the passwd home is refused too."""
    home = tmp_path / "people" / "me"
    home.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    (home / MARKER_NAME).write_text("lakana local instance\n")
    result = _reset(tmp_path, home, "--yes")
    assert result.exit_code == 2 and "home directory" in result.output

    bind = tmp_path / "bind"
    bind.mkdir()
    (bind / MARKER_NAME).write_text("lakana local instance\n")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(other)))
    real = os.path.samefile
    monkeypatch.setattr(
        reset_module.os.path,
        "samefile",
        lambda a, b: True if Path(a).name == "bind" and Path(b) == other else real(a, b),
    )
    result = _reset(tmp_path, bind, "--yes")
    assert result.exit_code == 2 and "home directory" in result.output


def test_a_broken_profile_file_does_not_block_a_reset_when_no_profile_is_chosen(
    tmp_path: Path,
) -> None:
    """The profile file is read only when a profile is chosen."""
    root = _instance(tmp_path)
    write_profile_file(tmp_path / "lakana" / "config.toml", "this is not toml [[[")
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 0, result.output


def test_refusals_name_the_next_step(tmp_path: Path) -> None:
    """The remote refusal says what to unset; a file as data dir says what to pass."""
    root = _instance(tmp_path)
    state = make_state(tmp_path, FakeApi())
    remote = invoke(
        state,
        "--url",
        "https://lakana.example.org",
        "server",
        "reset",
        "--local",
        "--data-dir",
        str(root.root),
        "--yes",
    )
    text = " ".join(remote.output.split())
    assert "LAKANA_URL" in text and "--profile" in text
    (tmp_path / "file").write_text("x")
    not_dir = _reset(tmp_path, tmp_path / "file", "--yes")
    assert "--data-dir" in " ".join(not_dir.output.split())


def test_the_question_says_what_stays(tmp_path: Path) -> None:
    """Files of an environment whose storage is outside `lake/` are not deleted, and it says so."""
    root = _instance(tmp_path)
    result = _reset(tmp_path, root.root, interactive=True, input="n\n")
    text = " ".join(result.output.split())
    assert "outside" in text and "lake/" in text


def _mountinfo(tmp_path: Path, *mounts: str) -> Path:
    """Write a fake `/proc/self/mountinfo` and point the reset at it.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    *mounts
        Mount points, already octal-escaped as the kernel writes them.

    Returns
    -------
    Path
        The file.
    """
    lines = ["22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw"]
    lines += [
        f"{40 + i} 22 8:1 /src {m} rw,relatime shared:1 - ext4 /dev/sda1 rw"
        for i, m in enumerate(mounts)
    ]
    path = tmp_path / "mountinfo"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_bind_mount_of_the_same_filesystem_is_refused(
    tmp_path: Path, short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same `st_dev`, but mountinfo lists it: a bind mount in the lake stops the reset."""
    monkeypatch.setattr(reset_module.sys, "platform", "linux")  # the table is Linux's
    root = _instance(short_tmp_path)  # a short path, so the message names it whole (#597)
    (root.lake / "dev" / "mnt").mkdir()
    real = root.root.resolve()
    for mount in (real / "lake", real / "lake" / "dev" / "mnt", real / "master.key"):
        monkeypatch.setattr(reset_module, "_MOUNTINFO", _mountinfo(tmp_path, str(mount)))
        result = _reset(tmp_path, root.root, "--yes")
        assert result.exit_code == 2, mount
        text = " ".join(result.output.split())
        assert str(mount) in text and "mount" in text and "unmount" in text
        assert _untouched(root)


def test_an_escaped_mount_path_is_unescaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""The kernel writes a space as `\040`; a mount at such a path is still found."""
    monkeypatch.setattr(reset_module.sys, "platform", "linux")  # the table is Linux's
    base = tmp_path / "my data"
    root = prepare_data_dir(base / "instance")
    (root.lake / "dev").mkdir()
    (root.lake / "dev" / "x y").mkdir()
    escaped = str(root.root.resolve() / "lake" / "dev" / "x y").replace(" ", "\\040")
    monkeypatch.setattr(reset_module, "_MOUNTINFO", _mountinfo(tmp_path, escaped))
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 2
    assert "x y" in result.output


def test_unreadable_mountinfo_refuses_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the mount table can't be read, the reset fails closed and says why."""
    root = _instance(tmp_path)
    monkeypatch.setattr(reset_module.sys, "platform", "linux")
    monkeypatch.setattr(reset_module, "_MOUNTINFO", tmp_path / "missing")
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 2
    assert "mountinfo" in result.output and _untouched(root)


def test_mounts_elsewhere_do_not_stop_a_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mounts that are not inside the instance's data (`/`, a parent) are fine."""
    root = _instance(tmp_path)
    monkeypatch.setattr(
        reset_module,
        "_MOUNTINFO",
        _mountinfo(tmp_path, str(tmp_path), str(root.root.resolve())),
    )
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 0, result.output
    assert not root.db_path.exists()


def test_a_permission_error_while_scanning_names_the_path_and_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that can't be read is a clean error, not a traceback."""
    root = _instance(tmp_path)
    real = os.scandir

    def denied(path: str):  # ruff: ignore[missing-return-type-private-function]
        if Path(str(path)).name == "bronze":
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(reset_module.os, "scandir", denied)
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 3
    text = " ".join(result.output.split())
    assert "bronze" in text and "chmod" in text
    assert _untouched(root)


def test_a_permission_error_while_deleting_gives_a_chmod_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete that is not allowed says to fix the permissions and run the reset again."""
    root = _instance(tmp_path)

    def denied(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(reset_module.shutil, "rmtree", denied)
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 3
    text = " ".join(result.output.split())
    assert "chmod" in text and "again" in text
    assert root.db_path.exists()


def test_a_marker_that_is_not_text_is_not_a_marker(tmp_path: Path) -> None:
    """Bytes that are not UTF-8 in the marker file mean 'not an instance', not a traceback."""
    root = _instance(tmp_path)
    (root.root / MARKER_NAME).write_bytes(b"\xff\xfe\x00")
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 2
    assert _untouched(root)


def test_a_home_that_cannot_be_resolved_or_compared_is_skipped(tmp_path: Path) -> None:
    """An OSError while resolving a home, or comparing with it, means 'not home'."""

    class Unresolvable:
        """A home whose path can't be resolved."""

        def resolve(self) -> Path:
            """Fail like a path whose parent can't be read.

            Raises
            ------
            OSError
                Always.
            """
            raise OSError("loop")

    assert reset_module._is_or_holds_home(tmp_path, Unresolvable()) is False  # ty: ignore[invalid-argument-type]
    assert reset_module._is_or_holds_home(tmp_path / "missing", tmp_path / "other") is False


def test_without_a_mount_table_only_the_device_walk_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Linux there is no mountinfo: no table is read, and the reset proceeds."""
    root = _instance(tmp_path)
    monkeypatch.setattr(reset_module.sys, "platform", "darwin")
    monkeypatch.setattr(reset_module, "_MOUNTINFO", tmp_path / "missing")
    assert reset_module._mount_points() is None
    result = _reset(tmp_path, root.root, "--yes")
    assert result.exit_code == 0, result.output
    assert not root.db_path.exists()


def test_a_short_mountinfo_line_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A line without a mount-point field is skipped; the others are still read."""
    table = tmp_path / "mountinfo"
    table.write_text("garbage\n22 1 8:1 / /data rw - ext4 /dev/sda1 rw\n")
    monkeypatch.setattr(reset_module.sys, "platform", "linux")
    monkeypatch.setattr(reset_module, "_MOUNTINFO", table)
    assert reset_module._mount_points() == [Path("/data")]
